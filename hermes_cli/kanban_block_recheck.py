"""Block-recheck watchdog for kanban tickets stuck in ``blocked`` (FIX-7).

Background
----------
FIX-6 (``kanban_stall_watchdog.py``) auto-escalates tickets stuck in
``todo`` / ``ready``. It **explicitly leaves ``blocked`` alone.** But the
majority of tickets that never self-heal live in ``blocked``:

* Crashed workers where the dispatcher retried, tripped
  ``consecutive_failures >= failure_limit``, emitted ``gave_up`` and
  flipped the task to ``blocked``. Ticket sits forever unless a human
  manually unblocks — no retry attempt is ever made even after the
  transient cause (OOM, network flap, kernel panic) has passed.
* Resource-preconditioned tickets ("waiting on swap free MB", "RAM
  below threshold") that a worker blocked with a reason string. The
  precondition eventually clears but nobody re-checks.
* Time-gated tickets ("Do not re-promote before 14:00") — a cron OR a
  clock is supposed to bring them back, but the cron slot might be
  broken.
* Review-required tickets — legitimately blocked on a human, but if
  they sit for hours nobody's escalating.

This module implements a periodic sweep — called by the gateway every
15 minutes (or via ``hermes kanban block-recheck-sweep`` from cron /
a systemd timer) — that inspects each ``blocked`` ticket and applies
one of five policies (A..E) to decide whether to auto-unblock, escalate,
or leave it alone.

Design constraints
------------------
* Only touches tickets in ``blocked``. Never ``running`` (a worker is
  on it), never ``ready`` / ``todo`` / ``triage`` / ``done``.
* Each policy is idempotent: once we've fired an auto-action for a
  given trigger event we MUST NOT fire again on the same trigger. The
  guard mirrors :func:`kanban_stall_watchdog._already_escalated` — an
  audit event newer than the trigger event id is the fence.
* All writes go through :func:`kanban_db.write_txn`. A crash or
  concurrent writer can never leave a task half-updated.
* Every action emits an audit event so downstream (dashboards, NATS
  bridges, alerters) can observe what the watchdog did:

    Policy A -> ``blocked_auto_retry_after_cooldown``
    Policy B -> ``precondition_cleared``
    Policy C -> ``time_gate_released``
    Policy D -> ``review_pending_operator_needed``

* When a policy A/B/C fires it unblocks via :func:`kanban_db.unblock_task`
  which already handles the parent-gating dance (blocked -> ready OR
  blocked -> todo depending on parent status). Policy D never unblocks —
  it only appends an escalation event + a comment.

Feature flag
------------
``kanban.enable_block_recheck`` (default ``True`` — the self-heal is
what we're trying to enable). Set to ``False`` to disable both the
gateway loop and the CLI when the operator wants to freeze the current
blocked lane.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterator, Optional

from hermes_cli import kanban_db as _kb
from hermes_cli.kanban_db import (
    _GOVERNANCE_BLOCK_KINDS,
    _append_event,
    unblock_task,
    write_txn,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prometheus metrics (optional — silently no-op if prometheus_client missing)
# ---------------------------------------------------------------------------
#
# DoD item 5: ``hermes_kanban_block_recheck_actions_total{policy=A|B|C|D}``
# is incremented for every action taken (unblocked or escalated). We also
# expose a per-outcome dimension so dashboards can split unblocks from
# escalations without cross-referencing policy semantics.
#
# Import guarded because prometheus_client isn't a hard dep of hermes-agent
# core — it's only pulled in by observability-enabled deployments. A missing
# import must never brick the watchdog.
try:
    from prometheus_client import Counter as _PromCounter  # type: ignore

    _BLOCK_RECHECK_ACTIONS = _PromCounter(
        "hermes_kanban_block_recheck_actions_total",
        "Actions taken by the kanban block-recheck watchdog",
        ["policy", "action"],  # policy in {A,B,C,D,E}, action in {unblocked,escalated,skipped}
    )
    _BLOCK_RECHECK_SWEEPS = _PromCounter(
        "hermes_kanban_block_recheck_sweeps_total",
        "Kanban block-recheck sweep ticks completed",
        ["outcome"],  # ok | error
    )
    _BLOCK_RECHECK_BOARD_ERRORS = _PromCounter(
        "hermes_kanban_block_recheck_board_errors_total",
        "Per-board errors during a block-recheck sweep",
    )
except Exception:  # pragma: no cover - metrics are optional
    _BLOCK_RECHECK_ACTIONS = None
    _BLOCK_RECHECK_SWEEPS = None
    _BLOCK_RECHECK_BOARD_ERRORS = None


def _observe_action(action: "RecheckAction") -> None:
    """Bump the prometheus counter for a recorded action. No-op when missing."""
    if _BLOCK_RECHECK_ACTIONS is None:
        return
    try:
        _BLOCK_RECHECK_ACTIONS.labels(
            policy=action.policy or "?",
            action=action.action or "?",
        ).inc()
    except Exception:  # pragma: no cover
        logger.debug("block-recheck: prometheus inc failed", exc_info=True)


# ---------------------------------------------------------------------------
# Constants / defaults
# ---------------------------------------------------------------------------

# Statuses we'll consider. blocked only — anything else is out of scope
# for this watchdog (FIX-6 handles todo/ready; running has a worker;
# triage/done are terminal for our purposes).
_TARGET_STATUS = "blocked"

# Trigger event kinds we inspect. ``gave_up`` is the dispatcher's
# breaker-tripped ended-here event. ``blocked`` is what ``block_task``
# emits when a worker or human deliberately blocks a task.
_TRIGGER_KINDS = ("gave_up", "blocked")

# Policy A: cooldown before retrying a gave_up ticket. 15 min matches
# the sweep cadence so on a 15-min tick a gave_up event triggers a
# retry on the *next* tick, not this one.
DEFAULT_GAVE_UP_COOLDOWN_S = 900

# Policy A: after this many auto-retry cycles for the same task we stop
# and leave it for a human. Each cycle = one gave_up -> retry ->
# gave_up loop. Counted by looking at how many
# ``blocked_auto_retry_after_cooldown`` events already exist on the task.
DEFAULT_GAVE_UP_MAX_CYCLES = 5

# Policy D: how old (in seconds) a review-required block has to be
# before we bump an escalation event. 2h matches the DoD.
DEFAULT_REVIEW_STALE_S = 7200

# Sweep cadence — the gateway loop / systemd timer runs this often.
DEFAULT_SWEEP_INTERVAL_S = 900


# Regexes for parsing block reasons. Compiled once, reused per sweep.

# Policy B — precondition/resource block. Matches:
#   "swap free below 512 MB"
#   "RAM insufficient"
#   "memory backpressure exceeded"
#   "free MB dropped"
#   "disk usage below threshold"
# All case-insensitive; anchored with .* so word order in the reason
# doesn't matter.
_RE_PRECONDITION = re.compile(
    r"(swap|ram|memory|free\s*mb|disk).*(fail|exceed|below|insufficient|"
    r"backpressure|low|too\s+low)",
    re.IGNORECASE,
)

# Policy C — time-gated. Matches:
#   "Do not re-promote before 2026-08-22T18:00:00Z"
#   "before 2026-08-22 18:00"
#   "until 2026-08-22T18:00"
#   "release at 2026-08-22T18:00Z"
#   "T+2h" / "T+30m" / "T+1d"  (relative to trigger event time)
_RE_TIME_ISO = re.compile(
    r"(?:do\s+not\s+re-?promote\s+before|before|until|release\s+at)\s*"
    r"(?P<iso>\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE,
)
_RE_TIME_REL = re.compile(
    r"(?:do\s+not\s+re-?promote\s+before|before|until|release\s+at)\s*"
    r"T\+(?P<count>\d+)(?P<unit>[hmd])",
    re.IGNORECASE,
)

# Policy D — review-required.
_RE_REVIEW = re.compile(
    r"(review-required|review\s+required|sign-off|peer-review|"
    r"awaiting\s+review|needs\s+review)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class RecheckAction:
    """One action taken (or skipped) against one blocked ticket."""

    task_id: str
    policy: str            # 'A' | 'B' | 'C' | 'D' | 'E'
    action: str            # 'unblocked' | 'escalated' | 'skipped'
    reason: str            # short human-readable summary
    trigger_kind: str      # 'gave_up' | 'blocked'
    trigger_event_id: int
    trigger_created_at: int
    age_s: int
    board: Optional[str] = None


@dataclass
class RecheckResult:
    """Aggregate outcome for one sweep tick over one board."""

    considered: int = 0
    actions: list[RecheckAction] = field(default_factory=list)
    skipped_no_policy: int = 0
    skipped_already_actioned: int = 0
    errors: int = 0

    @property
    def unblocked_count(self) -> int:
        return sum(1 for a in self.actions if a.action == "unblocked")

    @property
    def escalated_count(self) -> int:
        return sum(1 for a in self.actions if a.action == "escalated")

    @property
    def acted_count(self) -> int:
        return sum(
            1 for a in self.actions if a.action in ("unblocked", "escalated")
        )


# ---------------------------------------------------------------------------
# Precondition telemetry hook
# ---------------------------------------------------------------------------


def _current_host_resources() -> dict:
    """Return a best-effort snapshot of host resources for Policy B.

    Returns a dict of numeric fields so the caller can compare against
    thresholds parsed out of a block reason. Never raises — resource
    telemetry is best-effort and a broken probe must not stop the
    sweep.

    Keys (all optional):
      mem_available_mb    -- /proc/meminfo MemAvailable, MB
      swap_free_mb        -- /proc/meminfo SwapFree, MB
      disk_free_mb        -- root fs free MB via os.statvfs
      load_1              -- 1-min load average
    """
    out: dict[str, float] = {}
    try:
        with open("/proc/meminfo", "r") as f:
            info = {}
            for line in f:
                parts = line.split(":", 1)
                if len(parts) == 2:
                    v = parts[1].strip().split()
                    if v and v[0].isdigit():
                        info[parts[0]] = int(v[0])  # kB
        if "MemAvailable" in info:
            out["mem_available_mb"] = info["MemAvailable"] / 1024.0
        if "SwapFree" in info:
            out["swap_free_mb"] = info["SwapFree"] / 1024.0
    except (OSError, ValueError):
        pass
    try:
        import os
        st = os.statvfs("/")
        out["disk_free_mb"] = (st.f_bavail * st.f_frsize) / (1024 * 1024)
    except OSError:
        pass
    try:
        import os
        la = os.getloadavg()
        out["load_1"] = float(la[0])
    except (OSError, AttributeError):
        pass
    return out


def _extract_threshold_mb(reason: str) -> Optional[float]:
    """Parse a numeric MB threshold out of a Policy B reason string.

    ``reason='swap free below 512 MB'`` -> 512.0
    ``reason='RAM insufficient (min 2048 MiB)'`` -> 2048.0
    Returns None when no threshold is found — the caller then falls
    back to checking whether resources have MOVED IN THE RIGHT
    DIRECTION since the block was written (a heuristic).
    """
    m = re.search(r"(\d+(?:\.\d+)?)\s*(mib|mb|gib|gb)\b", reason, re.IGNORECASE)
    if not m:
        return None
    n = float(m.group(1))
    unit = m.group(2).lower()
    if unit in ("gib", "gb"):
        n *= 1024.0
    return n


# ---------------------------------------------------------------------------
# Candidate discovery
# ---------------------------------------------------------------------------


def _iter_blocked_candidates(
    conn: sqlite3.Connection,
) -> Iterator[dict]:
    """Yield one row per ``blocked`` ticket with its most recent trigger event.

    The query joins each ``blocked`` task to the newest ``task_events``
    row whose ``kind`` is in :data:`_TRIGGER_KINDS`. Tasks with no such
    event (e.g. legacy rows blocked manually with no audit trail) are
    silently omitted — we can't reason about them.

    The yielded dict is the minimum the policy layer needs; downstream
    reads (comments, block_kind, etc.) can go back to the DB by task
    id on the rare case a policy needs them.
    """
    sql = """
        SELECT
            t.id                AS task_id,
            t.status            AS status,
            t.block_kind        AS block_kind,
            t.created_at        AS task_created_at,
            e.id                AS event_id,
            e.kind              AS event_kind,
            e.created_at        AS event_created_at,
            e.payload           AS event_payload
        FROM tasks t
        JOIN task_events e ON e.task_id = t.id
        JOIN (
            SELECT task_id, MAX(id) AS max_event_id
              FROM task_events
             WHERE kind IN ({placeholders})
             GROUP BY task_id
        ) latest ON latest.task_id = e.task_id AND latest.max_event_id = e.id
        WHERE t.status = ?
        ORDER BY t.created_at ASC
    """.format(placeholders=", ".join("?" for _ in _TRIGGER_KINDS))
    params: list = list(_TRIGGER_KINDS) + [_TARGET_STATUS]
    for row in conn.execute(sql, params).fetchall():
        yield {
            "task_id": row["task_id"],
            "status": row["status"],
            "block_kind": (
                row["block_kind"] if "block_kind" in row.keys() else None
            ),
            "task_created_at": int(row["task_created_at"] or 0),
            "event_id": int(row["event_id"] or 0),
            "event_kind": str(row["event_kind"]),
            "event_created_at": int(row["event_created_at"] or 0),
            "event_payload": row["event_payload"],
        }


def _already_actioned(
    conn: sqlite3.Connection,
    task_id: str,
    since_event_id: int,
    action_kinds: tuple[str, ...],
) -> bool:
    """True if ``task_id`` already carries any of ``action_kinds`` newer
    than ``since_event_id`` — the idempotency fence.

    ``action_kinds`` = the audit event(s) this policy emits. Comparing
    event ids (monotonic per DB) is cheaper and race-free vs timestamps.
    """
    placeholders = ", ".join("?" for _ in action_kinds)
    row = conn.execute(
        f"SELECT 1 FROM task_events "
        f"WHERE task_id = ? AND kind IN ({placeholders}) AND id > ? "
        f"LIMIT 1",
        (task_id, *action_kinds, int(since_event_id)),
    ).fetchone()
    return row is not None


def _count_auto_retries(conn: sqlite3.Connection, task_id: str) -> int:
    """How many times has Policy A already retried this task?

    Counts ``blocked_auto_retry_after_cooldown`` audit events. Used as
    the ``max_cycles`` cap so a task that keeps crashing after auto
    retries stops being auto-retried.
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM task_events "
        "WHERE task_id = ? AND kind = 'blocked_auto_retry_after_cooldown'",
        (task_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


# ---------------------------------------------------------------------------
# Payload / reason extraction
# ---------------------------------------------------------------------------


def _payload(candidate: dict) -> dict:
    raw = candidate.get("event_payload")
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _reason_of(candidate: dict) -> str:
    """Pull the human reason string out of the trigger event.

    ``blocked`` payloads carry ``{"reason": "..."}``.
    ``gave_up`` payloads carry ``{"error": "...", ...}``. Either way we
    return the human-readable stringy field, or the empty string.
    """
    pl = _payload(candidate)
    for key in ("reason", "error"):
        v = pl.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


# ---------------------------------------------------------------------------
# Policy classification
# ---------------------------------------------------------------------------


def _classify(candidate: dict, *, now: int) -> str:
    """Return the policy letter ('A'..'E') for this blocked ticket.

    A: gave_up cooldown retry
    B: precondition recheck
    C: time-gated release
    D: review-required escalation
    E: no matching policy (skip)

    The order matters — a ``gave_up`` event ALWAYS goes to A even if
    its error string mentions "review" or "memory" (the dispatcher's
    breaker semantics dominate). ``blocked`` events fall through the
    B/C/D matchers on their ``reason`` payload.
    """
    kind = candidate["event_kind"]
    if kind == "gave_up":
        return "A"

    # kind == 'blocked' — inspect the payload's ``kind`` field FIRST.
    #
    # Governance-block kinds (``needs_input``, ``capability``) are by
    # definition human-gated: only an explicit operator ``kanban_unblock``
    # (or a direct SQL override) may clear them. Auto-unblocking these
    # via Policy B/C — even when the reason text incidentally mentions
    # "memory" / "swap" / "before <date>" as CONTEXT (host details, not
    # the block precondition itself) — silently violates the phantom-
    # override invariant. RCA: t_53fbabd5 on adr-006b-phase-2 was blocked
    # ``needs_input`` with a reason describing host memory constraints;
    # Policy B's ``_RE_PRECONDITION`` matched "memory ... insufficient"
    # against that context and auto-unblocked the ticket 41 minutes
    # later, whereupon the worker ran on a model that was not one of
    # the gated GO/NO-GO options. See task t_604eec8f.
    #
    # This short-circuit must come BEFORE the reason-text regex path so
    # the safe default (skip → operator intervention required) always
    # wins for governance kinds. ``_GOVERNANCE_BLOCK_KINDS`` is the same
    # frozenset ``_has_outstanding_governance_gate`` uses, so the two
    # gates stay in sync.
    payload_kind = _payload(candidate).get("kind")
    if isinstance(payload_kind, str) and payload_kind in _GOVERNANCE_BLOCK_KINDS:
        return "E"

    # kind == 'blocked' with a non-governance payload kind — pick a
    # policy from the reason text.
    reason = _reason_of(candidate)
    if not reason:
        return "E"
    # D takes precedence over B/C because review-required blocks
    # sometimes also mention "before <date>" in the reason (e.g.
    # "review required before merge on 2026-08-22"). We MUST NOT
    # auto-unblock a review; escalation-only is the safe default.
    if _RE_REVIEW.search(reason):
        return "D"
    if _RE_TIME_ISO.search(reason) or _RE_TIME_REL.search(reason):
        return "C"
    if _RE_PRECONDITION.search(reason):
        return "B"
    return "E"


# ---------------------------------------------------------------------------
# Policy A — gave_up cooldown retry
# ---------------------------------------------------------------------------


def _apply_policy_a(
    conn: sqlite3.Connection,
    candidate: dict,
    *,
    now: int,
    cooldown_s: int,
    max_cycles: int,
    dry_run: bool,
) -> Optional[RecheckAction]:
    """Retry a ``gave_up`` ticket that's cooled off.

    Fires only when:
      - trigger event age >= cooldown_s
      - Policy A hasn't already fired for this trigger (idempotency)
      - the task hasn't already been auto-retried more than ``max_cycles``
        times overall (bounded retry — an infinite crash loop stops
        eventually)
    """
    task_id = candidate["task_id"]
    trigger_event_id = candidate["event_id"]
    trigger_created_at = candidate["event_created_at"]
    age = now - trigger_created_at
    reason = _reason_of(candidate) or "gave_up"

    if age < cooldown_s:
        return None

    # Idempotency: was Policy A already fired for this exact trigger?
    if _already_actioned(
        conn, task_id,
        since_event_id=trigger_event_id - 1,
        action_kinds=("blocked_auto_retry_after_cooldown",),
    ):
        return None

    cycles = _count_auto_retries(conn, task_id)
    if cycles >= max_cycles:
        return None

    task_age = max(0, now - candidate["task_created_at"])
    if dry_run:
        return RecheckAction(
            task_id=task_id,
            policy="A",
            action="unblocked",
            reason=f"gave_up cooldown ({age}s >= {cooldown_s}s), cycle {cycles + 1}/{max_cycles}",
            trigger_kind=candidate["event_kind"],
            trigger_event_id=trigger_event_id,
            trigger_created_at=trigger_created_at,
            age_s=task_age,
        )

    with write_txn(conn):
        # Re-check status inside the txn — a human might have already
        # unblocked/completed/archived this ticket since the scan.
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None or row["status"] != "blocked":
            return None
        # Re-check idempotency inside the txn.
        if _already_actioned(
            conn, task_id,
            since_event_id=trigger_event_id - 1,
            action_kinds=("blocked_auto_retry_after_cooldown",),
        ):
            return None

    # Reset consecutive_failures (the dispatcher's spawn-retry
    # counter) so the ticket gets a fresh retry budget. This is
    # exactly what unblock_task already does, so we DELEGATE to
    # unblock_task — it also handles the parent-gating dance
    # (blocked -> ready OR blocked -> todo). NOTE: unblock_task
    # opens its own write_txn, so we cannot call it inside one — do
    # the pre-check and the audit-event write as two separate
    # transactions with unblock_task in between.
    ok = unblock_task(conn, task_id)
    if not ok:
        # Race: task moved out from under us. That's fine.
        return None

    payload = {
        "policy": "A",
        "trigger_kind": candidate["event_kind"],
        "trigger_event_id": trigger_event_id,
        "trigger_created_at": trigger_created_at,
        "cooldown_s": cooldown_s,
        "age_s_since_trigger": age,
        "cycle": cycles + 1,
        "max_cycles": max_cycles,
        "prev_error": reason[:200],
    }
    with write_txn(conn):
        _append_event(
            conn, task_id, "blocked_auto_retry_after_cooldown", payload,
        )

    return RecheckAction(
        task_id=task_id,
        policy="A",
        action="unblocked",
        reason=f"gave_up cooldown ({age}s), retry {cycles + 1}/{max_cycles}",
        trigger_kind=candidate["event_kind"],
        trigger_event_id=trigger_event_id,
        trigger_created_at=trigger_created_at,
        age_s=task_age,
    )


# ---------------------------------------------------------------------------
# Policy B — precondition recheck
# ---------------------------------------------------------------------------


def _apply_policy_b(
    conn: sqlite3.Connection,
    candidate: dict,
    *,
    now: int,
    host_resources: dict,
    dry_run: bool,
) -> Optional[RecheckAction]:
    """Unblock a resource-preconditioned ticket if the host now clears.

    Threshold discovery order:
      1. Parse a numeric threshold from the reason string (e.g. "512 MB").
      2. If no threshold, apply a "moved in the right direction" heuristic
         with defaults (memory: >= 512 MB, disk: >= 1024 MB).
    """
    task_id = candidate["task_id"]
    trigger_event_id = candidate["event_id"]
    trigger_created_at = candidate["event_created_at"]
    reason = _reason_of(candidate)

    if _already_actioned(
        conn, task_id,
        since_event_id=trigger_event_id - 1,
        action_kinds=("precondition_cleared",),
    ):
        return None

    threshold_mb = _extract_threshold_mb(reason)

    # Pick the right resource to compare against based on the reason
    # keywords.
    reason_l = reason.lower()
    if "swap" in reason_l:
        current = host_resources.get("swap_free_mb")
        resource = "swap_free_mb"
        default_min = 256.0
    elif "disk" in reason_l:
        current = host_resources.get("disk_free_mb")
        resource = "disk_free_mb"
        default_min = 1024.0
    else:  # ram / memory / free MB
        current = host_resources.get("mem_available_mb")
        resource = "mem_available_mb"
        default_min = 512.0

    if current is None:
        # Telemetry unavailable — can't decide. Skip this tick.
        return None

    min_required = threshold_mb if threshold_mb is not None else default_min
    if current < min_required:
        # Still failing — no-op, wait for next tick.
        return None

    task_age = max(0, now - candidate["task_created_at"])
    if dry_run:
        return RecheckAction(
            task_id=task_id,
            policy="B",
            action="unblocked",
            reason=f"precondition cleared ({resource}={current:.0f}MB >= {min_required:.0f}MB)",
            trigger_kind=candidate["event_kind"],
            trigger_event_id=trigger_event_id,
            trigger_created_at=trigger_created_at,
            age_s=task_age,
        )

    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None or row["status"] != "blocked":
            return None
        if _already_actioned(
            conn, task_id,
            since_event_id=trigger_event_id - 1,
            action_kinds=("precondition_cleared",),
        ):
            return None

    ok = unblock_task(conn, task_id)
    if not ok:
        return None

    payload = {
        "policy": "B",
        "resource": resource,
        "current_mb": round(current, 1),
        "required_mb": round(min_required, 1),
        "threshold_source": "reason" if threshold_mb is not None else "default",
        "trigger_event_id": trigger_event_id,
        "trigger_created_at": trigger_created_at,
        "reason": reason[:200],
    }
    with write_txn(conn):
        _append_event(conn, task_id, "precondition_cleared", payload)

    return RecheckAction(
        task_id=task_id,
        policy="B",
        action="unblocked",
        reason=f"precondition cleared ({resource}={current:.0f}MB >= {min_required:.0f}MB)",
        trigger_kind=candidate["event_kind"],
        trigger_event_id=trigger_event_id,
        trigger_created_at=trigger_created_at,
        age_s=task_age,
    )


# ---------------------------------------------------------------------------
# Policy C — time-gated release
# ---------------------------------------------------------------------------


def _parse_release_time(reason: str, *, trigger_created_at: int) -> Optional[int]:
    """Extract a release epoch-second from a Policy C reason string.

    Handles absolute ISO ("2026-08-22T18:00Z") and relative
    ("T+2h" / "T+30m" / "T+1d") forms. Returns ``None`` on parse
    failure — the caller falls through to Policy E.
    """
    m = _RE_TIME_ISO.search(reason)
    if m:
        raw = m.group("iso").strip()
        # sqlite / python both accept 'YYYY-MM-DD HH:MM' with a space
        # but datetime.fromisoformat wants 'T'. Normalize.
        raw = raw.replace(" ", "T")
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())

    m = _RE_TIME_REL.search(reason)
    if m:
        count = int(m.group("count"))
        unit = m.group("unit").lower()
        seconds = {"m": 60, "h": 3600, "d": 86400}.get(unit)
        if seconds is None:
            return None
        return int(trigger_created_at + count * seconds)

    return None


def _apply_policy_c(
    conn: sqlite3.Connection,
    candidate: dict,
    *,
    now: int,
    dry_run: bool,
) -> Optional[RecheckAction]:
    """Release a time-gated ticket once wall-clock >= its release time."""
    task_id = candidate["task_id"]
    trigger_event_id = candidate["event_id"]
    trigger_created_at = candidate["event_created_at"]
    reason = _reason_of(candidate)

    if _already_actioned(
        conn, task_id,
        since_event_id=trigger_event_id - 1,
        action_kinds=("time_gate_released",),
    ):
        return None

    release_at = _parse_release_time(reason, trigger_created_at=trigger_created_at)
    if release_at is None:
        return None
    if now < release_at:
        return None  # Still before the gate.

    task_age = max(0, now - candidate["task_created_at"])
    if dry_run:
        return RecheckAction(
            task_id=task_id,
            policy="C",
            action="unblocked",
            reason=f"time gate released (release_at={release_at}, now={now})",
            trigger_kind=candidate["event_kind"],
            trigger_event_id=trigger_event_id,
            trigger_created_at=trigger_created_at,
            age_s=task_age,
        )

    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None or row["status"] != "blocked":
            return None
        if _already_actioned(
            conn, task_id,
            since_event_id=trigger_event_id - 1,
            action_kinds=("time_gate_released",),
        ):
            return None

    ok = unblock_task(conn, task_id)
    if not ok:
        return None

    payload = {
        "policy": "C",
        "release_at": release_at,
        "released_at": now,
        "trigger_event_id": trigger_event_id,
        "trigger_created_at": trigger_created_at,
        "reason": reason[:200],
    }
    with write_txn(conn):
        _append_event(conn, task_id, "time_gate_released", payload)

    return RecheckAction(
        task_id=task_id,
        policy="C",
        action="unblocked",
        reason=f"time gate released (release_at={release_at})",
        trigger_kind=candidate["event_kind"],
        trigger_event_id=trigger_event_id,
        trigger_created_at=trigger_created_at,
        age_s=task_age,
    )


# ---------------------------------------------------------------------------
# Policy D — review escalation
# ---------------------------------------------------------------------------


def _apply_policy_d(
    conn: sqlite3.Connection,
    candidate: dict,
    *,
    now: int,
    stale_s: int,
    dry_run: bool,
) -> Optional[RecheckAction]:
    """Escalate a review-required ticket that has aged past ``stale_s``.

    Never unblocks — review is a genuine gate. Emits an escalation
    event plus a single comment; subsequent sweeps are idempotent (the
    escalation event is newer than the trigger, so the guard fires).
    """
    task_id = candidate["task_id"]
    trigger_event_id = candidate["event_id"]
    trigger_created_at = candidate["event_created_at"]
    reason = _reason_of(candidate)
    age = now - trigger_created_at

    if age < stale_s:
        return None
    if _already_actioned(
        conn, task_id,
        since_event_id=trigger_event_id - 1,
        action_kinds=("review_pending_operator_needed",),
    ):
        return None

    task_age = max(0, now - candidate["task_created_at"])
    if dry_run:
        return RecheckAction(
            task_id=task_id,
            policy="D",
            action="escalated",
            reason=f"review pending {age // 3600}h — operator escalation",
            trigger_kind=candidate["event_kind"],
            trigger_event_id=trigger_event_id,
            trigger_created_at=trigger_created_at,
            age_s=task_age,
        )

    comment_body = (
        f"review_pending_operator_needed: {reason[:200]}\n\n"
        f"Block-recheck watchdog: this review-required ticket has been "
        f"blocked for {age // 3600}h ({age // 60} min). Escalated for "
        f"operator attention. (Ticket is NOT auto-unblocked — review is "
        f"a genuine gate; a human still needs to make the call.)"
    )

    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        if row is None or row["status"] != "blocked":
            return None
        if _already_actioned(
            conn, task_id,
            since_event_id=trigger_event_id - 1,
            action_kinds=("review_pending_operator_needed",),
        ):
            return None

        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, ?, ?, ?)",
            (task_id, "block-recheck", comment_body, now),
        )
        _append_event(
            conn, task_id, "commented",
            {"author": "block-recheck", "len": len(comment_body)},
        )
        _append_event(
            conn, task_id, "review_pending_operator_needed",
            {
                "policy": "D",
                "age_s_since_trigger": age,
                "trigger_event_id": trigger_event_id,
                "trigger_created_at": trigger_created_at,
                "reason": reason[:200],
            },
        )

    return RecheckAction(
        task_id=task_id,
        policy="D",
        action="escalated",
        reason=f"review pending {age // 3600}h — operator escalation",
        trigger_kind=candidate["event_kind"],
        trigger_event_id=trigger_event_id,
        trigger_created_at=trigger_created_at,
        age_s=task_age,
    )


# ---------------------------------------------------------------------------
# Public sweep entrypoint
# ---------------------------------------------------------------------------


def sweep_once(
    conn: sqlite3.Connection,
    *,
    now: Optional[int] = None,
    gave_up_cooldown_s: int = DEFAULT_GAVE_UP_COOLDOWN_S,
    gave_up_max_cycles: int = DEFAULT_GAVE_UP_MAX_CYCLES,
    review_stale_s: int = DEFAULT_REVIEW_STALE_S,
    host_resources: Optional[dict] = None,
    dry_run: bool = False,
    board: Optional[str] = None,
) -> RecheckResult:
    """Run one block-recheck sweep against ``conn``.

    Parameters
    ----------
    conn:
        Open connection to a kanban DB.
    now:
        Injected epoch seconds — tests pin this deterministically;
        production leaves it ``None`` for ``time.time()``.
    gave_up_cooldown_s:
        Policy A cooldown before an auto-retry (default 900).
    gave_up_max_cycles:
        Policy A max auto-retries per task (default 5).
    review_stale_s:
        Policy D age threshold for operator escalation (default 7200).
    host_resources:
        Policy B — inject a resources dict for tests; production leaves
        this ``None`` and the sweep captures live host state via
        :func:`_current_host_resources`.
    dry_run:
        When True, no DB writes; :class:`RecheckAction`s are still
        returned so the caller can preview.
    board:
        Optional slug for logging / event attribution.

    Returns
    -------
    :class:`RecheckResult`.
    """
    _now = int(now if now is not None else time.time())
    result = RecheckResult()
    # Force sqlite3.Row row factory for column-name indexing. Production
    # connections don't set this by default; tests do. Setting here means
    # helpers can rely on row["col"] regardless of caller convention.
    # We do NOT restore the previous factory because sweep_once takes
    # ownership of `conn` for the duration of the sweep — callers pass
    # a scratch connection.
    conn.row_factory = sqlite3.Row
    if host_resources is None:
        host_resources = _current_host_resources()

    try:
        candidates = list(_iter_blocked_candidates(conn))
    except sqlite3.Error as exc:
        logger.exception(
            "block-recheck: candidate scan failed on board %s: %s",
            board, exc,
        )
        result.errors += 1
        return result

    result.considered = len(candidates)

    for cand in candidates:
        try:
            policy = _classify(cand, now=_now)
            action: Optional[RecheckAction]
            if policy == "A":
                action = _apply_policy_a(
                    conn, cand, now=_now,
                    cooldown_s=gave_up_cooldown_s,
                    max_cycles=gave_up_max_cycles,
                    dry_run=dry_run,
                )
            elif policy == "B":
                action = _apply_policy_b(
                    conn, cand, now=_now,
                    host_resources=host_resources,
                    dry_run=dry_run,
                )
            elif policy == "C":
                action = _apply_policy_c(
                    conn, cand, now=_now, dry_run=dry_run,
                )
            elif policy == "D":
                action = _apply_policy_d(
                    conn, cand, now=_now,
                    stale_s=review_stale_s,
                    dry_run=dry_run,
                )
            else:  # 'E' — no policy matched
                result.skipped_no_policy += 1
                logger.debug(
                    "block-recheck: no matching policy for %s (%s) reason=%r",
                    cand["task_id"], board or "-", _reason_of(cand)[:120],
                )
                continue
        except sqlite3.Error as exc:
            logger.exception(
                "block-recheck: DB error on %s (%s): %s",
                cand["task_id"], board, exc,
            )
            result.errors += 1
            continue

        if action is None:
            # Policy matched but didn't fire (cooldown not elapsed,
            # precondition still failing, already-actioned, etc.).
            # Count as considered but not acted.
            result.skipped_already_actioned += 1
            continue

        action.board = board
        result.actions.append(action)
        _observe_action(action)
        logger.info(
            "block-recheck: %s %s (%s) policy=%s reason=%s",
            action.action, action.task_id, board or "-",
            action.policy, action.reason,
        )

    return result


def sweep_all_boards(
    *,
    now: Optional[int] = None,
    gave_up_cooldown_s: int = DEFAULT_GAVE_UP_COOLDOWN_S,
    gave_up_max_cycles: int = DEFAULT_GAVE_UP_MAX_CYCLES,
    review_stale_s: int = DEFAULT_REVIEW_STALE_S,
    dry_run: bool = False,
) -> dict[str, RecheckResult]:
    """Run :func:`sweep_once` against every board on this host.

    Mirrors :func:`kanban_stall_watchdog.sweep_all_boards` — uses
    :func:`kanban_db.list_boards` for discovery,
    :func:`kanban_db.connect_closing` for per-board connections, and
    isolates errors per board so one bad DB doesn't stop the tick.

    Host resources are captured once per tick (not per board) — the
    resource snapshot is host-wide and re-reading /proc/meminfo per
    board is wasteful.
    """
    results: dict[str, RecheckResult] = {}
    host_resources = _current_host_resources()
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "block-recheck: list_boards failed (%s); skipping tick", exc,
        )
        return results

    for board in boards:
        slug = (
            board.get("slug")
            if isinstance(board, dict)
            else getattr(board, "slug", None)
        )
        if not slug:
            continue
        try:
            with _kb.connect_closing(board=slug) as conn:
                result = sweep_once(
                    conn,
                    now=now,
                    gave_up_cooldown_s=gave_up_cooldown_s,
                    gave_up_max_cycles=gave_up_max_cycles,
                    review_stale_s=review_stale_s,
                    host_resources=host_resources,
                    dry_run=dry_run,
                    board=slug,
                )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "block-recheck: sweep failed on board %s: %s", slug, exc,
            )
            r = RecheckResult()
            r.errors += 1
            results[slug] = r
            if _BLOCK_RECHECK_BOARD_ERRORS is not None:
                try:
                    _BLOCK_RECHECK_BOARD_ERRORS.inc()
                except Exception:  # pragma: no cover
                    pass
            continue
        results[slug] = result

    if _BLOCK_RECHECK_SWEEPS is not None:
        try:
            any_errors = any(r.errors for r in results.values())
            _BLOCK_RECHECK_SWEEPS.labels(
                outcome="error" if any_errors else "ok"
            ).inc()
        except Exception:  # pragma: no cover
            pass

    return results


# ---------------------------------------------------------------------------
# `python -m hermes_cli.kanban_block_recheck` entrypoint
# ---------------------------------------------------------------------------
#
# Systemd-timer users invoke the sweep as ``python -m hermes_cli.kanban_block_recheck``.
# Kept intentionally simple — the full-featured surface with per-board
# targeting, JSON output, etc., lives at ``hermes kanban block-recheck-sweep``.


def main(argv: Optional[list[str]] = None) -> int:
    """Run one block-recheck sweep across all boards. Returns 0 on success.

    Config is read via :func:`hermes_cli.config.load_config` when available;
    absent / bad config falls back to module defaults. This mirrors the
    stall-watchdog CLI (`hermes_cli/kanban_stall_watchdog.py`) so the two
    watchdogs have symmetric operator ergonomics.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m hermes_cli.kanban_block_recheck",
        description=(
            "Run one block-recheck sweep across every kanban board on this "
            "host. See `hermes kanban block-recheck-sweep --help` for the "
            "full-featured CLI wrapper with per-board targeting and JSON."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report actions without applying them.",
    )
    parser.add_argument(
        "--gave-up-cooldown", type=int, default=None,
        help="Policy A cooldown seconds (default 900).",
    )
    parser.add_argument(
        "--gave-up-max-cycles", type=int, default=None,
        help="Policy A max auto-retries per task (default 5).",
    )
    parser.add_argument(
        "--review-stale", type=int, default=None,
        help="Policy D age threshold seconds (default 7200).",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable DEBUG-level logging for this run.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Load config so operator YAML tuning applies to `python -m` runs too.
    kcfg: dict = {}
    try:
        from hermes_cli.config import load_config as _load_config
        _cfg = _load_config()
        if isinstance(_cfg, dict):
            kcfg = _cfg.get("kanban", {}) or {}
    except Exception:
        pass

    def _pick(cli_val: Optional[int], key: str, fallback: int) -> int:
        if cli_val is not None:
            return int(cli_val)
        try:
            return int(kcfg.get(key, fallback) or fallback)
        except (TypeError, ValueError):
            return fallback

    cooldown = _pick(
        args.gave_up_cooldown, "block_recheck_gave_up_cooldown_s",
        DEFAULT_GAVE_UP_COOLDOWN_S,
    )
    max_cycles = _pick(
        args.gave_up_max_cycles, "block_recheck_gave_up_max_cycles",
        DEFAULT_GAVE_UP_MAX_CYCLES,
    )
    review_stale = _pick(
        args.review_stale, "block_recheck_review_stale_s",
        DEFAULT_REVIEW_STALE_S,
    )

    results = sweep_all_boards(
        gave_up_cooldown_s=cooldown,
        gave_up_max_cycles=max_cycles,
        review_stale_s=review_stale,
        dry_run=args.dry_run,
    )

    total_acted = sum(r.acted_count for r in results.values())
    total_unblock = sum(r.unblocked_count for r in results.values())
    total_esc = sum(r.escalated_count for r in results.values())
    total_err = sum(r.errors for r in results.values())
    prefix = "[DRY RUN] " if args.dry_run else ""
    print(
        f"{prefix}block-recheck sweep: applied {total_acted} actions "
        f"({total_unblock} unblocked, {total_esc} escalated) "
        f"across {len(results)} boards (errors={total_err})"
    )
    return 0 if total_err == 0 else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
