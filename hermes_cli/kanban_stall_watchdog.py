"""Stall-watchdog sweep for silently-unclaimable kanban tickets (FIX-6).

Background
----------
When the dispatcher can't claim a ticket it emits ``claim_rejected``
(``parents_not_done``, ``resource_low``, ``skill_missing`` etc.) or
``dependency_wait``. It then retries silently on the next tick. On a
sick board these tickets can sit in ``todo`` or ``ready`` for hours
before an operator notices.

This module implements a periodic sweep — called by the gateway every
15 minutes (or via ``hermes kanban stall-sweep`` from cron / a systemd
timer) — that finds tickets stuck in this pattern and auto-escalates
them into ``blocked`` with kind ``needs_input`` so they surface on the
board's blocked lane instead of hiding in the ready pool.

Design constraints
------------------
* Only touches tickets in ``todo`` or ``ready``. Never ``running`` (a
  worker is on it), never ``blocked`` / ``triage`` / ``done``.
* Only escalates tickets whose *creation age* exceeds ``min_age_s``
  AND that have a ``claim_rejected`` or ``dependency_wait`` event in
  the trailing ``recent_window_s`` window. (The recent-event
  requirement is what proves the dispatcher is actively retrying and
  failing — plain age would also fire on cards that are simply parked
  behind slow parents, which is not a stall.)
* Idempotent: a ``stall_escalated`` event is written next to the
  status transition; subsequent sweeps skip tickets that already have
  a ``stall_escalated`` event in the same window as their latest
  ``claim_rejected`` / ``dependency_wait``.
* The status flip, the escalation event, and the audit comment all
  happen inside a single write transaction — no half-escalated rows.

Observability
-------------
Every escalation appends a ``stall_escalated`` event with payload::

    {
        "reason": "<short reason from the triggering event>",
        "trigger_kind": "claim_rejected" | "dependency_wait",
        "trigger_event_id": <int>,
        "trigger_created_at": <unix seconds>,
        "prev_status": "todo" | "ready",
        "age_s": <int>,
        "sweep_run": "<iso timestamp>",
    }

Downstream observers (dashboards, NATS bridges, alerters) subscribe to
``task_events`` where ``kind = 'stall_escalated'`` via the existing
kanban event-tail infra — same pattern as ``block_loop_detected``.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional

from hermes_cli import kanban_db as _kb
from hermes_cli.kanban_db import (
    _append_event,
    _dependency_waiting_for_satisfied,
    write_txn,
)


logger = logging.getLogger(__name__)


# Event kinds that mean "the dispatcher tried to move this and could
# not." ``claim_rejected`` is emitted by ``dispatch_once`` when a
# candidate row fails the pre-flight checks (parents_not_done,
# max_in_progress, etc.). ``dependency_wait`` is emitted by
# ``block_task`` when a dependency parent isn't done yet.
# ``dispatch_skipped`` (FIX-9 / t_7fa94b1b) is emitted by
# ``dispatch_once`` when a row can't spawn for a "silent" reason
# (unassigned, unknown_profile, per_profile_capped, assign_failed).
# All three are trigger events for the stall-watchdog: a ticket that
# keeps generating one of these without ever transitioning is stuck.
_TRIGGER_KINDS: tuple[str, ...] = (
    "claim_rejected",
    "dependency_wait",
    "dispatch_skipped",
)

# Statuses we're willing to auto-escalate. running/blocked/triage/done
# are all off-limits — a running task has a live worker (blocking it
# would kill in-flight work), and the other three are already surfaced
# to humans in one form or another.
_ESCALATABLE_STATUSES = ("todo", "ready")

# Defaults chosen to match the DoD in FIX-6. Overrideable via
# config.yaml or the sweep_once() kwargs.
DEFAULT_MIN_AGE_S = 3600          # ticket must be >1h old to be a stall
DEFAULT_RECENT_WINDOW_S = 3600    # trigger event must be within the last hour
DEFAULT_SWEEP_INTERVAL_S = 900    # sweep runs every 15 minutes


@dataclass
class EscalationReport:
    """Structured record of one auto-escalated ticket.

    Emitted by :func:`sweep_once` so callers (gateway watcher, CLI,
    tests) can log, aggregate, or forward to NATS without re-reading
    the DB.
    """

    task_id: str
    prev_status: str
    trigger_kind: str
    trigger_reason: str
    trigger_event_id: int
    trigger_created_at: int
    age_s: int
    board: Optional[str] = None


@dataclass
class SweepResult:
    """Aggregate outcome for one sweep tick over one board."""

    considered: int = 0
    escalated: list[EscalationReport] = field(default_factory=list)
    skipped_already_escalated: int = 0
    errors: int = 0

    @property
    def escalated_count(self) -> int:
        return len(self.escalated)


# ---------------------------------------------------------------------------
# Candidate detection
# ---------------------------------------------------------------------------


def _iter_candidate_tasks(
    conn: sqlite3.Connection,
    *,
    now: int,
    min_age_s: int,
    recent_window_s: int,
) -> Iterator[dict]:
    """Yield rows describing each ticket that meets the escalation predicate.

    A candidate row has:

    * ``t.status`` in ``('todo', 'ready')``
    * ``t.created_at <= now - min_age_s`` (ticket is at least ``min_age_s`` old)
    * At least one ``task_events`` row of kind ``claim_rejected`` or
      ``dependency_wait`` created within the trailing ``recent_window_s``
    * No ``stall_escalated`` event newer than the latest trigger event
      (idempotency guard — see :func:`_already_escalated`).

    The yielded dict carries just what :func:`sweep_once` needs to
    build an :class:`EscalationReport`: task id, current status,
    creation time, and the winning trigger event id / kind / created_at
    / payload-reason. We rely on the ``task_events(task_id, created_at)``
    index (see ``kanban_db.py`` schema) so this stays a cheap sweep even
    on boards with millions of events.
    """
    age_cutoff = now - int(min_age_s)
    window_cutoff = now - int(recent_window_s)

    # Pick the newest triggering event per task (in the window). Using
    # a windowed inner join lets us return the reason payload for the
    # exact event that fired without a second per-row lookup.
    sql = """
        SELECT
            t.id           AS task_id,
            t.status       AS status,
            t.block_kind   AS block_kind,
            t.created_at   AS task_created_at,
            e.id           AS event_id,
            e.kind         AS event_kind,
            e.created_at   AS event_created_at,
            e.payload      AS event_payload
        FROM tasks t
        JOIN task_events e ON e.task_id = t.id
        JOIN (
            SELECT task_id, MAX(id) AS max_event_id
              FROM task_events
             WHERE kind IN ({trigger_placeholders})
               AND created_at >= ?
             GROUP BY task_id
        ) latest ON latest.task_id = e.task_id AND latest.max_event_id = e.id
        WHERE t.status IN ({status_placeholders})
          AND t.created_at <= ?
        ORDER BY t.created_at ASC
    """.format(
        trigger_placeholders=", ".join("?" for _ in _TRIGGER_KINDS),
        status_placeholders=", ".join("?" for _ in _ESCALATABLE_STATUSES),
    )
    params: list = list(_TRIGGER_KINDS) + [window_cutoff] + list(_ESCALATABLE_STATUSES) + [age_cutoff]
    for row in conn.execute(sql, params).fetchall():
        task_id = row["task_id"]
        event_kind = str(row["event_kind"])
        block_kind = row["block_kind"] if "block_kind" in row.keys() else None

        # t_6e2342f2 — dep-block guard. ``block_task(kind='dependency')``
        # LEGITIMATELY parks tasks in ``status='todo'`` with
        # ``block_kind='dependency'`` and emits a ``dependency_wait``
        # event on every retry — that's not a stall, that's the
        # DISPATCH-01 wait working as designed. Skip candidates whose
        # latest trigger is ``dependency_wait`` when the same predicate
        # ``recompute_ready`` uses to gate promotion
        # (:func:`_dependency_waiting_for_satisfied`) reports the
        # ``waiting_for`` peer is NOT yet satisfied. Only escalate when
        # the guard says the wait is satisfiable — i.e. the peer HAS
        # reached ``done``/``archived`` (or is unresolvable), which
        # means the task truly is stuck.
        if event_kind == "dependency_wait" and (
            block_kind == "dependency" or row["status"] == "todo"
        ):
            try:
                satisfied = _dependency_waiting_for_satisfied(conn, task_id)
            except sqlite3.Error:
                # If the guard itself errors, defer to the old behavior
                # (escalate) rather than silently ignore a genuine stall.
                satisfied = True
            if not satisfied:
                # Legitimate dependency wait — skip. The dispatcher's
                # DISPATCH-01 guard will re-promote this task the tick
                # after its peer reaches ``done``.
                continue

        yield {
            "task_id": task_id,
            "status": row["status"],
            "task_created_at": int(row["task_created_at"] or 0),
            "event_id": int(row["event_id"] or 0),
            "event_kind": event_kind,
            "event_created_at": int(row["event_created_at"] or 0),
            "event_payload": row["event_payload"],
        }


def _already_escalated(
    conn: sqlite3.Connection, task_id: str, since_event_id: int
) -> bool:
    """True when ``task_id`` already carries a ``stall_escalated`` event
    with ``id > since_event_id``.

    This is the idempotency guard: once we've escalated for a given
    trigger, another sweep tick MUST NOT re-escalate for the same
    trigger. Comparing event ids (monotonically increasing per DB) is
    cheaper and race-free vs comparing timestamps.
    """
    row = conn.execute(
        "SELECT 1 FROM task_events "
        "WHERE task_id = ? AND kind = 'stall_escalated' AND id > ? "
        "LIMIT 1",
        (task_id, int(since_event_id)),
    ).fetchone()
    return row is not None


def _extract_reason(event_payload_json: Optional[str]) -> str:
    """Pull a short reason string out of a ``claim_rejected`` /
    ``dependency_wait`` / ``dispatch_skipped`` payload for the
    human-facing comment.

    All three payload shapes carry ``{"reason": "...", ...}`` (see
    ``dispatch_once`` and ``_emit_dispatch_skipped`` in kanban_db.py,
    and ``block_task`` for ``dependency_wait``). Fall back to
    ``"unknown"`` if payload is missing or unparseable — the sweep is
    best-effort; a missing reason string must not block escalation.
    """
    if not event_payload_json:
        return "unknown"
    try:
        import json as _json

        obj = _json.loads(event_payload_json)
    except (ValueError, TypeError):
        return "unknown"
    if not isinstance(obj, dict):
        return "unknown"
    reason = obj.get("reason")
    if isinstance(reason, str) and reason.strip():
        return reason.strip()[:200]  # keep payloads bounded
    return "unknown"


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------


def _escalate_one(
    conn: sqlite3.Connection,
    candidate: dict,
    *,
    now: int,
    sweep_run_iso: str,
    dry_run: bool,
) -> Optional[EscalationReport]:
    """Perform one escalation transaction: comment → status → event.

    All three writes go through a single ``write_txn`` so a crash or
    concurrent writer can never leave a task half-escalated.

    Returns the :class:`EscalationReport` on success, or ``None`` when
    the row lost the race (status changed between the candidate scan
    and the update — a worker claimed it, a human moved it, etc.).
    """
    task_id = candidate["task_id"]
    prev_status = candidate["status"]
    trigger_kind = candidate["event_kind"]
    trigger_event_id = candidate["event_id"]
    trigger_created_at = candidate["event_created_at"]
    task_created_at = candidate["task_created_at"]
    reason = _extract_reason(candidate.get("event_payload"))
    # t_6e2342f2 — for dependency_wait triggers on parked-in-``todo``
    # tasks, report time-since-latest-block instead of time-since-row-
    # creation. A dep-block re-fires each dispatcher tick, so
    # ``tasks.created_at`` is misleading (the audit comment on the
    # original bug read "738 min in todo" for a block that was 6 min
    # old). For all other trigger kinds, keep the old semantics so
    # ``claim_rejected`` on an old-and-stale ticket still surfaces the
    # true row age operators expect to see.
    if trigger_kind == "dependency_wait" and trigger_created_at > 0:
        age_s = max(0, now - trigger_created_at)
    else:
        age_s = max(0, now - task_created_at)

    if dry_run:
        return EscalationReport(
            task_id=task_id,
            prev_status=prev_status,
            trigger_kind=trigger_kind,
            trigger_reason=reason,
            trigger_event_id=trigger_event_id,
            trigger_created_at=trigger_created_at,
            age_s=age_s,
        )

    comment_body = (
        f"auto_escalated: {reason}\n\n"
        f"Stall-watchdog escalated this ticket to `blocked` (kind "
        f"`needs_input`) after {age_s // 60} min in `{prev_status}` "
        f"with a `{trigger_kind}` event at "
        f"{datetime.fromtimestamp(trigger_created_at, tz=timezone.utc).isoformat()}."
    )

    with write_txn(conn):
        # Re-check status inside the txn — the candidate row was read
        # outside a locked read, so a concurrent writer could have
        # moved this task to running/blocked/etc. between the scan and
        # here. We refuse to escalate anything that isn't still in the
        # exact status we captured. Belt-and-braces.
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None or row["status"] != prev_status:
            return None

        # Idempotency re-check inside the txn: another sweep worker
        # (unlikely — gateway holds the singleton dispatcher lock —
        # but a manual `hermes kanban stall-sweep` run in parallel is
        # legal) may have won the race.
        if _already_escalated(conn, task_id, trigger_event_id - 1):
            return None

        # Insert the audit comment first so its ``commented`` event is
        # ordered before the ``stall_escalated`` event. This keeps the
        # timeline readable in the UI: "commented … status … stall".
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, "stall-watchdog", comment_body, now),
        )
        _append_event(
            conn, task_id, "commented",
            {"author": "stall-watchdog", "len": len(comment_body)},
        )

        # Flip status → blocked. We DO NOT use ``block_task()`` here
        # because that primitive requires the task to be in
        # ``running``/``ready`` (see kanban_db.block_task) and rejects
        # ``todo``. Escalating from ``todo`` is a first-class part of
        # this feature, so we do the UPDATE directly. block_recurrences
        # / block_kind columns are populated exactly like a manual
        # ``kanban_block(kind='needs_input')`` call so the loop-breaker
        # in ``block_task`` continues to see this state next cycle.
        cur = conn.execute(
            """
            UPDATE tasks
               SET status            = 'blocked',
                   block_kind        = 'needs_input',
                   block_recurrences = COALESCE(block_recurrences, 0),
                   claim_lock        = NULL,
                   claim_expires     = NULL,
                   worker_pid        = NULL
             WHERE id = ?
               AND status = ?
            """,
            (task_id, prev_status),
        )
        if cur.rowcount != 1:
            # Lost the race between the pre-check and the UPDATE. Roll
            # back by raising — write_txn's context manager will abort
            # the transaction cleanly and no comment/event will leak.
            raise _RaceLostError(task_id)

        payload = {
            "reason": reason,
            "trigger_kind": trigger_kind,
            "trigger_event_id": trigger_event_id,
            "trigger_created_at": trigger_created_at,
            "prev_status": prev_status,
            "age_s": age_s,
            "sweep_run": sweep_run_iso,
        }
        _append_event(conn, task_id, "stall_escalated", payload)

    return EscalationReport(
        task_id=task_id,
        prev_status=prev_status,
        trigger_kind=trigger_kind,
        trigger_reason=reason,
        trigger_event_id=trigger_event_id,
        trigger_created_at=trigger_created_at,
        age_s=age_s,
    )


class _RaceLostError(Exception):
    """Internal-only. Raised inside a write_txn to roll it back when
    the status changed under our feet between pre-check and UPDATE."""


# ---------------------------------------------------------------------------
# Public sweep entrypoint
# ---------------------------------------------------------------------------


def sweep_once(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
    min_age_s: int = DEFAULT_MIN_AGE_S,
    recent_window_s: int = DEFAULT_RECENT_WINDOW_S,
    dry_run: bool = False,
    board: Optional[str] = None,
) -> SweepResult:
    """Run one stall-detection sweep against ``conn``.

    Parameters
    ----------
    conn:
        Open connection to a kanban DB.
    now:
        Injected epoch seconds — tests pin this to a deterministic
        value; production leaves it ``None`` to use ``time.time()``.
    min_age_s:
        Minimum ticket age (seconds since ``tasks.created_at``) for
        escalation eligibility. Defaults to 1 hour.
    recent_window_s:
        Trigger event must have occurred within this trailing window.
        Defaults to 1 hour.
    dry_run:
        When True, ``EscalationReport``s are still returned but no
        DB writes happen — useful for a ``hermes kanban stall-sweep
        --dry-run`` preview.
    board:
        Optional slug for logging / event attribution. Does not affect
        the DB query (that's already scoped to the ``conn`` you passed).

    Returns
    -------
    :class:`SweepResult` describing the tick.
    """
    _now = int(now if now is not None else time.time())
    result = SweepResult()
    sweep_run_iso = datetime.fromtimestamp(_now, tz=timezone.utc).isoformat()

    try:
        candidates = list(
            _iter_candidate_tasks(
                conn,
                now=_now,
                min_age_s=min_age_s,
                recent_window_s=recent_window_s,
            )
        )
    except sqlite3.Error as exc:
        logger.exception(
            "stall-watchdog: candidate scan failed on board %s: %s",
            board, exc,
        )
        result.errors += 1
        return result

    result.considered = len(candidates)
    for cand in candidates:
        try:
            if _already_escalated(
                conn, cand["task_id"], since_event_id=cand["event_id"] - 1
            ):
                # A prior sweep already handled this exact trigger.
                # Skip silently — this is expected on every subsequent
                # tick until the trigger event ages out of the window.
                result.skipped_already_escalated += 1
                continue

            report = _escalate_one(
                conn,
                cand,
                now=_now,
                sweep_run_iso=sweep_run_iso,
                dry_run=dry_run,
            )
        except _RaceLostError:
            # Concurrent writer moved the task; that's fine, log and
            # let the next tick handle it if it stalls again.
            logger.info(
                "stall-watchdog: lost race on task %s (%s); skipping",
                cand["task_id"], board,
            )
            continue
        except sqlite3.Error as exc:
            logger.exception(
                "stall-watchdog: DB error while escalating %s on %s: %s",
                cand["task_id"], board, exc,
            )
            result.errors += 1
            continue

        if report is not None:
            report.board = board
            result.escalated.append(report)
            logger.info(
                "stall-watchdog: escalated %s (%s) → blocked/needs_input "
                "(trigger=%s, reason=%r, age=%ds)",
                report.task_id, board or "-",
                report.trigger_kind, report.trigger_reason, report.age_s,
            )

    return result


def sweep_all_boards(
    *,
    now: Optional[int] = None,
    min_age_s: int = DEFAULT_MIN_AGE_S,
    recent_window_s: int = DEFAULT_RECENT_WINDOW_S,
    dry_run: bool = False,
) -> dict[str, SweepResult]:
    """Run :func:`sweep_once` against every board on this host.

    Uses :func:`kanban_db.list_boards` for discovery and
    :func:`kanban_db.connect_closing` for per-board connections so no
    handles leak if one board errors. Returns a ``{slug: SweepResult}``
    map that the gateway watcher logs / forwards.

    Errors on one board never stop the sweep on other boards.
    """
    results: dict[str, SweepResult] = {}
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception as exc:  # pragma: no cover - list_boards is very stable
        logger.warning("stall-watchdog: list_boards failed (%s); skipping tick", exc)
        return results

    for board in boards:
        slug = board.get("slug") if isinstance(board, dict) else getattr(board, "slug", None)
        if not slug:
            continue
        try:
            with _kb.connect_closing(board=slug) as conn:
                result = sweep_once(
                    conn,
                    now=now,
                    min_age_s=min_age_s,
                    recent_window_s=recent_window_s,
                    dry_run=dry_run,
                    board=slug,
                )
        except Exception as exc:  # noqa: BLE001 - one bad board must not tank the tick
            logger.exception(
                "stall-watchdog: sweep failed on board %s: %s", slug, exc,
            )
            r = SweepResult()
            r.errors += 1
            results[slug] = r
            continue
        results[slug] = result

    return results
