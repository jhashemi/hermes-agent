"""Tests for the stall-watchdog auto-escalation sweep (FIX-6).

Covers the DoD from t_5c8fce1b:

1. Synthetic stalled ticket is auto-escalated.
2. Tickets already ``blocked`` / ``running`` / ``done`` are NEVER touched.
3. A ``stall_escalated`` event is emitted with the expected payload so
   observability dashboards can subscribe.
4. The sweep is idempotent — a second tick doesn't re-escalate.
5. Tickets younger than ``min_age_s`` are skipped even with a fresh
   ``claim_rejected`` event.
6. Tickets older than ``min_age_s`` are skipped when their most recent
   ``claim_rejected`` / ``dependency_wait`` event is OUTSIDE the
   ``recent_window_s`` (i.e. dispatcher stopped retrying — probably
   fine, definitely not a stall).
7. ``dry_run=True`` produces the same report list without any DB mutation.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_stall_watchdog as sw


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty default-board kanban DB.

    Mirrors the fixture used across tests/hermes_cli/test_kanban_db.py
    so the schema init path is identical to production.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _open(kanban_home):
    return kb.connect()


def _make_ticket(
    conn,
    *,
    task_id: str,
    status: str,
    created_at: int,
    assignee: str = "backend-eng",
    title: str = "test",
) -> None:
    """Insert a raw row into ``tasks`` with a specific status + age.

    Bypasses ``create_task`` because that helper normalises status to
    ``ready`` (or via the parent-gate machinery). We need direct
    control of both ``status`` and ``created_at`` to fabricate a stall.
    """
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (task_id, title, "test body", assignee, status, created_at),
        )


def _append_trigger(
    conn,
    *,
    task_id: str,
    kind: str,
    created_at: int,
    reason: str = "parents_not_done",
) -> int:
    """Insert a ``claim_rejected`` or ``dependency_wait`` event into
    ``task_events`` with a specific ``created_at``.

    Returns the new event id so tests can pin idempotency assertions.
    Reaches through the private ``_append_event`` — the same primitive
    the dispatcher uses to emit ``claim_rejected`` — then rewrites the
    ``created_at`` to the desired age.
    """
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, ?, ?, ?)",
            (task_id, kind, json.dumps({"reason": reason}), created_at),
        )
        row = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return int(row["id"])


def _events(conn, task_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, kind, payload, created_at FROM task_events "
        "WHERE task_id = ? ORDER BY id ASC",
        (task_id,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "kind": r["kind"],
            "payload": json.loads(r["payload"]) if r["payload"] else None,
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def _status(conn, task_id: str) -> str:
    row = conn.execute(
        "SELECT status FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    return row["status"] if row else ""


# ---------------------------------------------------------------------------
# DoD 2: happy path — synthetic stalled ticket gets escalated.
# ---------------------------------------------------------------------------


def test_stalled_ready_ticket_is_escalated(kanban_home):
    """A ``ready`` ticket >1h old with a recent ``claim_rejected``
    event is transitioned to ``blocked`` with kind ``needs_input``,
    a comment is filed, and a ``stall_escalated`` event is emitted."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_stall_ready"

    # Ticket created 2h ago — comfortably past the 1h min_age_s.
    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    # Trigger event 10 min ago — well inside the 1h recent window.
    trigger_id = _append_trigger(
        conn, task_id=task_id, kind="claim_rejected",
        created_at=now - 600, reason="parents_not_done",
    )

    result = sw.sweep_once(conn, now=now)

    assert result.considered == 1
    assert result.escalated_count == 1
    esc = result.escalated[0]
    assert esc.task_id == task_id
    assert esc.prev_status == "ready"
    assert esc.trigger_kind == "claim_rejected"
    assert esc.trigger_reason == "parents_not_done"
    assert esc.trigger_event_id == trigger_id
    assert esc.age_s == 7200

    # Row is now blocked with the right kind.
    row = conn.execute(
        "SELECT status, block_kind FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["status"] == "blocked"
    assert row["block_kind"] == "needs_input"

    # Comment filed with an ``auto_escalated:`` header.
    cmts = conn.execute(
        "SELECT author, body FROM task_comments WHERE task_id = ?", (task_id,)
    ).fetchall()
    assert len(cmts) == 1
    assert cmts[0]["author"] == "stall-watchdog"
    assert cmts[0]["body"].startswith("auto_escalated: parents_not_done")

    # stall_escalated event carries the observability payload
    # dashboards will subscribe to (DoD item 4).
    stall_evts = [e for e in _events(conn, task_id) if e["kind"] == "stall_escalated"]
    assert len(stall_evts) == 1
    pl = stall_evts[0]["payload"]
    assert pl["reason"] == "parents_not_done"
    assert pl["trigger_kind"] == "claim_rejected"
    assert pl["trigger_event_id"] == trigger_id
    assert pl["prev_status"] == "ready"
    assert pl["age_s"] == 7200


def test_stalled_todo_ticket_is_escalated(kanban_home):
    """DoD explicitly requires ``todo`` handling — ``block_task`` alone
    can't do this because it only accepts ``running``/``ready``. The
    watchdog does its own UPDATE and must succeed."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_stall_todo"

    _make_ticket(conn, task_id=task_id, status="todo", created_at=now - 5400)
    _append_trigger(
        conn, task_id=task_id, kind="dependency_wait",
        created_at=now - 900, reason="waiting_on_parent",
    )

    result = sw.sweep_once(conn, now=now)

    assert result.escalated_count == 1
    assert result.escalated[0].prev_status == "todo"
    assert _status(conn, task_id) == "blocked"


# ---------------------------------------------------------------------------
# DoD 3: running / blocked / done tickets are NEVER touched.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["running", "blocked", "done", "archived", "triage"])
def test_non_todo_ready_tickets_are_untouched(kanban_home, status):
    """The sweep MUST NOT flip running / blocked / done / archived /
    triage tickets, even when they have matching trigger events. This
    is DoD item 3 — the property that makes the sweep safe to enable
    board-wide."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = f"t_untouched_{status}"

    _make_ticket(conn, task_id=task_id, status=status, created_at=now - 7200)
    _append_trigger(
        conn, task_id=task_id, kind="claim_rejected",
        created_at=now - 600,
    )

    result = sw.sweep_once(conn, now=now)

    assert result.considered == 0
    assert result.escalated_count == 0
    # Status unchanged.
    assert _status(conn, task_id) == status
    # No stall_escalated event was emitted.
    stall_evts = [e for e in _events(conn, task_id) if e["kind"] == "stall_escalated"]
    assert stall_evts == []


# ---------------------------------------------------------------------------
# DoD idempotency — second tick doesn't re-escalate.
# ---------------------------------------------------------------------------


def test_second_sweep_is_idempotent(kanban_home):
    """Once a task has a ``stall_escalated`` event newer than its
    triggering event, subsequent sweeps must skip it silently. This
    prevents a 15-min cron from filing 96 comments/day on a stuck
    ticket."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_idempotent"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    _append_trigger(
        conn, task_id=task_id, kind="claim_rejected",
        created_at=now - 600,
    )

    r1 = sw.sweep_once(conn, now=now)
    assert r1.escalated_count == 1

    # Now the task is blocked. Its status changed so no candidate will
    # match on the second sweep — but even if we simulate it going
    # back to ready without a new trigger event, the idempotency guard
    # (stall_escalated > trigger_event_id) will skip it. Simulate that.
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,)
        )

    r2 = sw.sweep_once(conn, now=now + 60)
    assert r2.considered == 1  # We DID re-see it in the candidate scan…
    assert r2.escalated_count == 0  # …but the idempotency guard blocked re-escalation.
    assert r2.skipped_already_escalated == 1
    # Exactly one stall_escalated event survives.
    stall_evts = [e for e in _events(conn, task_id) if e["kind"] == "stall_escalated"]
    assert len(stall_evts) == 1


def test_new_trigger_after_previous_escalation_re_escalates(kanban_home):
    """The idempotency guard is trigger-scoped, not task-scoped: if a
    task was unblocked, retried, and stalled *again* with a new
    ``claim_rejected``, that fresh trigger justifies a fresh
    escalation."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_reesc"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 10800)
    _append_trigger(conn, task_id=task_id, kind="claim_rejected", created_at=now - 3000)

    r1 = sw.sweep_once(conn, now=now)
    assert r1.escalated_count == 1

    # Simulate the operator unblocking the task and the dispatcher
    # emitting a fresh claim_rejected 5 min later.
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?", (task_id,)
        )
    _append_trigger(conn, task_id=task_id, kind="claim_rejected", created_at=now + 300)

    r2 = sw.sweep_once(conn, now=now + 600)
    assert r2.escalated_count == 1
    stall_evts = [e for e in _events(conn, task_id) if e["kind"] == "stall_escalated"]
    assert len(stall_evts) == 2


# ---------------------------------------------------------------------------
# Age / window filters
# ---------------------------------------------------------------------------


def test_young_ticket_is_skipped(kanban_home):
    """Ticket <1h old is not a stall — the dispatcher may just be
    ramping up. Fresh claim_rejected on a 5-min-old ticket must not
    trigger escalation."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_young"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 300)
    _append_trigger(conn, task_id=task_id, kind="claim_rejected", created_at=now - 30)

    result = sw.sweep_once(conn, now=now)
    assert result.considered == 0
    assert _status(conn, task_id) == "ready"


def test_old_trigger_outside_window_is_skipped(kanban_home):
    """Old ticket whose last claim_rejected was 3h ago (dispatcher
    stopped retrying) is not currently stalled. Human may already have
    noticed. Skip."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_old_trigger"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 14400)
    # Trigger 3h ago — outside the default 1h window.
    _append_trigger(conn, task_id=task_id, kind="claim_rejected", created_at=now - 10800)

    result = sw.sweep_once(conn, now=now)
    assert result.considered == 0
    assert _status(conn, task_id) == "ready"


# ---------------------------------------------------------------------------
# dry_run mode — reports, no mutations.
# ---------------------------------------------------------------------------


def test_dry_run_reports_without_mutating(kanban_home):
    """`hermes kanban stall-sweep --dry-run` and the ops preview flow
    both use ``dry_run=True``; they should still get a list of what
    WOULD escalate, but the DB must be unchanged."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_dry"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    _append_trigger(conn, task_id=task_id, kind="claim_rejected", created_at=now - 600)

    result = sw.sweep_once(conn, now=now, dry_run=True)
    assert result.escalated_count == 1

    # Zero writes: status unchanged, no comment, no stall event.
    assert _status(conn, task_id) == "ready"
    assert conn.execute(
        "SELECT COUNT(*) AS n FROM task_comments WHERE task_id = ?", (task_id,)
    ).fetchone()["n"] == 0
    stall_evts = [e for e in _events(conn, task_id) if e["kind"] == "stall_escalated"]
    assert stall_evts == []


# ---------------------------------------------------------------------------
# Payload extraction resilience
# ---------------------------------------------------------------------------


def test_missing_reason_payload_falls_back_to_unknown(kanban_home):
    """A trigger event with no payload / malformed JSON must not block
    escalation — the sweep is best-effort. Reason defaults to
    ``"unknown"``."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_no_reason"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    # Insert a claim_rejected with NULL payload.
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, ?, NULL, ?)",
            (task_id, "claim_rejected", now - 600),
        )

    result = sw.sweep_once(conn, now=now)
    assert result.escalated_count == 1
    assert result.escalated[0].trigger_reason == "unknown"


def test_dependency_wait_trigger_is_recognised(kanban_home):
    """Both trigger kinds (``claim_rejected`` and ``dependency_wait``)
    must fire the escalation path. Guards the parity of the
    ``_TRIGGER_KINDS`` list against future drift."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_depwait"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    _append_trigger(
        conn, task_id=task_id, kind="dependency_wait",
        created_at=now - 300, reason="waiting_on: t_parent",
    )

    result = sw.sweep_once(conn, now=now)
    assert result.escalated_count == 1
    assert result.escalated[0].trigger_kind == "dependency_wait"


# ---------------------------------------------------------------------------
# t_6e2342f2 — dependency-block-aware escalation.
#
# The stall-watchdog previously escalated any ``todo`` task with a recent
# ``dependency_wait`` event, using ``tasks.created_at`` for the age check.
# But ``block_task(kind='dependency')`` LEGITIMATELY parks tasks in ``todo``
# (with ``block_kind='dependency'``) until the waited-on peer transitions
# to ``done``/``archived``. Those tasks are not stalled — they're correctly
# gated — and the sweep must leave them alone. The observed failure was a
# ROOM-NS-GOV parent claimed → re-blocked → auto-promoted → re-claimed 4×
# in ~2h while its unsatisfied dependency's assignee had not responded.
# ---------------------------------------------------------------------------


def _make_dep_blocked_task(
    conn,
    *,
    task_id: str,
    waiting_for: str,
    task_created_at: int,
    wait_event_at: int,
) -> int:
    """Fabricate a dependency-blocked task in ``todo`` with a
    ``dependency_wait`` event payload pointing at ``waiting_for``.

    Mirrors what ``block_task(kind='dependency', waiting_for=...)``
    produces: status=todo, block_kind='dependency', task_events row of
    kind=dependency_wait with ``{"waiting_for": <id>, "kind": "dependency"}``.
    """
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, "
            "block_kind, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, "dep-blocked", "b", "backend-eng", "todo",
             "dependency", task_created_at),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, ?, ?, ?)",
            (
                task_id,
                "dependency_wait",
                json.dumps({
                    "reason": "waiting on peer",
                    "kind": "dependency",
                    "waiting_for": waiting_for,
                }),
                wait_event_at,
            ),
        )
        row = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return int(row["id"])


def test_dep_blocked_task_with_unsatisfied_peer_is_not_escalated(kanban_home):
    """Regression for t_6e2342f2: an old ``todo`` task carrying a
    ``dependency_wait`` payload whose ``waiting_for`` peer is still
    running (i.e. NOT ``done``/``archived``) must be left in ``todo``.

    Before the fix, the sweep saw the fresh ``dependency_wait`` event
    inside the 1h window plus the 12h+ ``created_at`` and auto-flipped
    the task to ``blocked``/``needs_input`` — the exact loop the bug
    report describes on t_c239044c.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    peer_id = "t_peer"
    task_id = "t_dep_blocked"

    # The peer task the dependency is waiting on. It's ``running`` —
    # the dependency is genuinely unsatisfied.
    _make_ticket(conn, task_id=peer_id, status="running", created_at=now - 3600)

    # The dependency-blocked task itself. Created 12h ago (well past
    # ``min_age_s``); block landed 6 min ago (well inside the recent
    # window). This is the exact shape of t_c239044c at 18:38 UTC.
    _make_dep_blocked_task(
        conn,
        task_id=task_id,
        waiting_for=peer_id,
        task_created_at=now - 43200,   # 12h ago
        wait_event_at=now - 360,       # 6 min ago
    )

    result = sw.sweep_once(conn, now=now)

    # Zero escalations — the sweep must recognise this as a legitimate
    # dependency wait, not a stall.
    assert result.escalated_count == 0, (
        "dep-blocked task with unsatisfied peer must not be escalated"
    )
    # Task stays in todo with its dependency block intact.
    row = conn.execute(
        "SELECT status, block_kind FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()
    assert row["status"] == "todo"
    assert row["block_kind"] == "dependency"
    # No stall_escalated event emitted.
    stall_evts = [e for e in _events(conn, task_id) if e["kind"] == "stall_escalated"]
    assert stall_evts == []


def test_dep_blocked_task_with_done_peer_is_still_escalatable(kanban_home):
    """Complement to the above: if the ``waiting_for`` peer HAS reached
    ``done`` but the task is still stuck in ``todo`` (dispatcher hasn't
    re-promoted for some reason), that IS a real stall and the sweep
    should escalate as before.

    This guarantees the fix doesn't over-broadly silence the sweep on
    dep-blocked tasks — it only silences the ones with a still-open
    ``waiting_for``.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    peer_id = "t_peer_done"
    task_id = "t_dep_stuck"

    _make_ticket(conn, task_id=peer_id, status="done", created_at=now - 3600)
    _make_dep_blocked_task(
        conn,
        task_id=task_id,
        waiting_for=peer_id,
        task_created_at=now - 43200,
        wait_event_at=now - 360,
    )

    result = sw.sweep_once(conn, now=now)

    # Peer is done — the block is stale — this task really is stuck.
    assert result.escalated_count == 1
    assert result.escalated[0].task_id == task_id
    assert _status(conn, task_id) == "blocked"


def test_dep_blocked_task_with_missing_peer_falls_through(kanban_home):
    """When ``waiting_for`` points at a task that no longer exists,
    ``_dependency_waiting_for_satisfied`` returns True (fall through
    so operator can unblock manually). The sweep is allowed to
    escalate — this task IS abandoned and needs a human to look."""
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_dep_orphaned"

    _make_dep_blocked_task(
        conn,
        task_id=task_id,
        waiting_for="t_ghost_never_created",
        task_created_at=now - 43200,
        wait_event_at=now - 360,
    )

    result = sw.sweep_once(conn, now=now)

    # Waiting_for points at nothing — legit escalation candidate.
    assert result.escalated_count == 1
    assert _status(conn, task_id) == "blocked"


def test_dep_blocked_task_with_no_waiting_for_falls_through(kanban_home):
    """Legacy / racy shape: a ``dependency_wait`` payload with no
    ``waiting_for`` field. ``_dependency_waiting_for_satisfied`` returns
    True in this case (see its docstring: "legacy dependency blocks
    with no ``waiting_for`` on the event payload return True"). The
    sweep must still be allowed to escalate — this is a data-integrity
    edge case that operators need to see.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_dep_legacy"

    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO tasks (id, title, body, assignee, status, "
            "block_kind, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (task_id, "dep-legacy", "b", "backend-eng", "todo",
             "dependency", now - 43200),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, ?, ?, ?)",
            (task_id, "dependency_wait",
             json.dumps({"reason": "legacy"}),
             now - 360),
        )

    result = sw.sweep_once(conn, now=now)
    assert result.escalated_count == 1


def test_dep_blocked_age_uses_wait_event_time_not_row_creation(kanban_home):
    """When a dependency-blocked task IS legitimately escalated (its
    peer is done but the task is still parked), the reported ``age_s``
    should reflect time-since-latest-dependency_wait rather than
    time-since-task-creation.

    In the bug report the audit comment read "738 min in todo" even
    though the block was 6 min old — misleading operators. Reporting
    the ACTUAL block age keeps the audit trail truthful.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    peer_id = "t_peer_done_2"
    task_id = "t_dep_stuck_age"

    _make_ticket(conn, task_id=peer_id, status="done", created_at=now - 3600)
    _make_dep_blocked_task(
        conn,
        task_id=task_id,
        waiting_for=peer_id,
        task_created_at=now - 43200,   # 12h old row
        wait_event_at=now - 600,       # dep block re-fired 10 min ago
    )

    result = sw.sweep_once(conn, now=now)
    assert result.escalated_count == 1
    # The reported age should reflect the dependency_wait event age
    # (10 min = 600s), not the row age (12h = 43200s).
    assert result.escalated[0].age_s == 600, (
        f"dependency-block escalation must use wait-event age, "
        f"got {result.escalated[0].age_s}s (expected 600s)"
    )
