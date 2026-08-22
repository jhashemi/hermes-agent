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
