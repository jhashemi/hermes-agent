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
    ``_TRIGGER_KINDS`` list against future drift.

    Note (t_d5c662fb): the escalation path now filters typed
    ``dependency_wait`` events (those with ``waiting_for`` /
    ``waiting_for_commit`` / ``waiting_for_event`` /
    ``waiting_for_condition``). This test uses a BARE
    ``dependency_wait`` (no typed field) so it still escalates —
    matching the plain dispatcher-skip stall pattern this trigger was
    added to catch.
    """
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
# t_d5c662fb: Layer 1 — typed dependency_wait is NEVER escalated.
# ---------------------------------------------------------------------------


def _append_typed_dependency_wait(
    conn,
    *,
    task_id: str,
    created_at: int,
    waiting_for: str | None = None,
    waiting_for_commit: str | None = None,
    waiting_for_event: str | None = None,
    waiting_for_condition: str | None = None,
    reason: str = "waiting_on_parent",
) -> int:
    """Insert a ``dependency_wait`` event whose payload carries at
    least one typed handoff field.

    Mirrors the payload shape ``block_task(kind='dependency', ...)``
    emits (see kanban_db.py around line 8145). We build the payload
    here instead of calling ``block_task`` so we can control the
    ``created_at`` — the sweep filter is age-window sensitive.
    """
    import json as _json

    payload: dict = {"reason": reason, "kind": "dependency"}
    if waiting_for is not None:
        payload["waiting_for"] = waiting_for
    if waiting_for_commit is not None:
        payload["waiting_for_commit"] = waiting_for_commit
    if waiting_for_event is not None:
        payload["waiting_for_event"] = waiting_for_event
    if waiting_for_condition is not None:
        payload["waiting_for_condition"] = waiting_for_condition
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, ?, ?, ?)",
            (task_id, "dependency_wait", _json.dumps(payload), created_at),
        )
        row = conn.execute(
            "SELECT id FROM task_events WHERE task_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return int(row["id"])


@pytest.mark.parametrize(
    "typed_field",
    ["waiting_for", "waiting_for_commit", "waiting_for_event", "waiting_for_condition"],
)
def test_typed_dependency_wait_is_never_escalated(kanban_home, typed_field):
    """Regression for t_d5c662fb: a ``dependency_wait`` event carrying
    any one of the typed handoff fields represents a DESIGNED wait
    with an auto-resume path in ``recompute_ready`` /
    ``_dependency_waiting_for_satisfied``. The stall-watchdog must
    NOT escalate it — doing so clobbers ``block_kind='dependency'``,
    bypasses the VFE-DISPATCH-01 promotion guard, and creates an
    infinite respawn loop that burns one full LLM worker run per
    15-min sweep tick.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = f"t_typed_{typed_field}"

    _make_ticket(conn, task_id=task_id, status="todo", created_at=now - 7200)
    _append_typed_dependency_wait(
        conn,
        task_id=task_id,
        created_at=now - 300,
        **{typed_field: "t_target_that_never_completes"},
    )

    result = sw.sweep_once(conn, now=now)

    # Escalation candidate was scanned then filtered out — NOT counted
    # as considered. The invariant we care about is: no escalation
    # writes, no status flip, and NO extra ``stall_escalated`` /
    # ``blocked`` events emitted.
    assert result.considered == 0
    assert result.escalated_count == 0
    assert _status(conn, task_id) == "todo"
    stall_evts = [e for e in _events(conn, task_id) if e["kind"] == "stall_escalated"]
    assert stall_evts == []
    blocked_evts = [e for e in _events(conn, task_id) if e["kind"] == "blocked"]
    assert blocked_evts == []


def test_typed_dependency_wait_with_empty_string_still_escalates(kanban_home):
    """Empty-string typed fields ("waiting_for": "") don't count as
    typed — an accidental empty value should not confer exemption.
    A bare ``dependency_wait`` payload still escalates.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_empty_typed"

    _make_ticket(conn, task_id=task_id, status="todo", created_at=now - 7200)
    _append_typed_dependency_wait(
        conn,
        task_id=task_id,
        created_at=now - 300,
        waiting_for="",  # empty string — treated as absent
    )

    result = sw.sweep_once(conn, now=now)
    assert result.escalated_count == 1
    assert _status(conn, task_id) == "blocked"


def test_mixed_typed_and_bare_dependency_wait(kanban_home):
    """When two tickets are candidates in the same sweep — one with a
    typed ``dependency_wait``, one with a bare ``dependency_wait`` —
    only the bare one is escalated. Guards against overreach of the
    Layer 1 filter (e.g. accidentally exempting the whole kind).
    """
    conn = _open(kanban_home)
    now = 1_800_000_000

    _make_ticket(conn, task_id="t_typed", status="todo", created_at=now - 7200)
    _append_typed_dependency_wait(
        conn, task_id="t_typed", created_at=now - 300,
        waiting_for="t_dependency_target",
    )

    _make_ticket(conn, task_id="t_bare", status="todo", created_at=now - 7200)
    _append_trigger(
        conn, task_id="t_bare", kind="dependency_wait",
        created_at=now - 300, reason="dispatcher_skip_no_typed_field",
    )

    result = sw.sweep_once(conn, now=now)

    assert result.escalated_count == 1
    assert result.escalated[0].task_id == "t_bare"
    assert _status(conn, "t_typed") == "todo"
    assert _status(conn, "t_bare") == "blocked"


# ---------------------------------------------------------------------------
# t_d5c662fb: Layer 2 — escalation emits a ``blocked`` event that makes
# ``recompute_ready`` respect the stall block instead of silently
# undoing it on the next tick.
# ---------------------------------------------------------------------------


def test_stall_escalation_emits_blocked_event(kanban_home):
    """Regression for t_d5c662fb Layer 2: the escalation must emit a
    ``blocked`` event alongside ``stall_escalated`` so downstream
    predicates (``_has_sticky_block``, ``_has_outstanding_governance_gate``)
    can see the block. Without this event, ``recompute_ready`` on the
    next tick silently promotes the row back to ``ready`` because
    parents-done + no-governance-gate + failure-count-ok evaluates
    True, restarting the respawn loop.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_layer2_blocked_event"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    _append_trigger(
        conn, task_id=task_id, kind="claim_rejected",
        created_at=now - 600, reason="parents_not_done",
    )

    result = sw.sweep_once(conn, now=now)
    assert result.escalated_count == 1

    evts = _events(conn, task_id)
    blocked_evts = [e for e in evts if e["kind"] == "blocked"]
    stall_evts = [e for e in evts if e["kind"] == "stall_escalated"]

    # Exactly one blocked event, emitted in the same escalation txn.
    assert len(blocked_evts) == 1
    assert len(stall_evts) == 1

    payload = blocked_evts[0]["payload"]
    # ``kind`` must be a governance kind so
    # ``_has_outstanding_governance_gate`` fires. ``source`` marks it
    # as watchdog-emitted for downstream tools.
    assert payload["kind"] == "needs_input"
    assert payload["source"] == "stall_watchdog"
    assert payload["prev_status"] == "ready"
    assert payload["trigger_kind"] == "claim_rejected"

    # Ordering matters for readability: stall_escalated first, then
    # blocked. Both after the commented event.
    kinds_in_order = [e["kind"] for e in evts]
    assert kinds_in_order.index("stall_escalated") < kinds_in_order.index("blocked")


def test_recompute_ready_respects_stall_escalation(kanban_home):
    """End-to-end regression for the t_d5c662fb infinite-respawn loop:
    after the stall-watchdog escalates a ticket, ``recompute_ready``
    on the next tick MUST NOT silently promote it back to ``ready``.
    Before the Layer 2 fix, the direct UPDATE flipped status to
    ``blocked`` but emitted no ``blocked`` event —
    ``_has_outstanding_governance_gate`` saw nothing, parents were
    (trivially) done, and the row got re-promoted. Now the
    watchdog-emitted ``blocked`` event carries a governance kind, so
    the governance-gate predicate fires and blocks re-promotion until
    an explicit ``unblock_task`` (which emits ``unblocked``) clears
    it.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_loop_regression"

    # Stall setup: old ticket, recent trigger, no parents.
    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    _append_trigger(
        conn, task_id=task_id, kind="claim_rejected",
        created_at=now - 600, reason="parents_not_done",
    )

    result = sw.sweep_once(conn, now=now)
    assert result.escalated_count == 1
    assert _status(conn, task_id) == "blocked"

    # Simulate the recompute_ready call that runs after every complete /
    # unblock / delete / archive path. Before Layer 2 this silently
    # promoted the row back to ``ready`` and burned a fresh LLM run.
    promoted = kb.recompute_ready(conn)  # returns count of promotions
    # We don't assert exact count (other test rows may or may not
    # exist), but the specific row must remain blocked.
    assert _status(conn, task_id) == "blocked", (
        f"recompute_ready promoted a watchdog-escalated task back to ready "
        f"(promoted={promoted}); Layer 2 governance-gate emission failed"
    )

    # And the governance-gate predicate agrees.
    assert sw._kb._has_outstanding_governance_gate(conn, task_id) is True


def test_unblock_task_clears_stall_escalation(kanban_home):
    """The exit path for a watchdog-escalated ticket is ``unblock_task``,
    identical to any other governance-blocked ticket. Emitting
    ``unblocked`` clears the governance-gate predicate and lets
    ``recompute_ready`` promote again.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_unblock_after_stall"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    _append_trigger(
        conn, task_id=task_id, kind="claim_rejected",
        created_at=now - 600, reason="parents_not_done",
    )

    sw.sweep_once(conn, now=now)
    assert _status(conn, task_id) == "blocked"
    assert sw._kb._has_outstanding_governance_gate(conn, task_id) is True

    # Operator unblocks — kanban_db emits the ``unblocked`` event and
    # flips the status to ready.
    kb.unblock_task(conn, task_id)
    assert sw._kb._has_outstanding_governance_gate(conn, task_id) is False
    assert _status(conn, task_id) == "ready"


# ---------------------------------------------------------------------------
# t_847892e6 — block_kind preservation across escalation.
#
# The Layer 2 fix (t_d5c662fb) emitted the ``blocked`` event but still
# unconditionally wrote ``block_kind='needs_input'`` into the row. That
# clobbered any pre-existing typed ``block_kind`` — most importantly
# ``'dependency'`` (the exact predicate the VFE-DISPATCH-01 guard in
# ``recompute_ready`` reads to skip a designed-wait task). This block
# of tests pins the preservation contract:
#
#   * If the row has NO typed ``block_kind`` (NULL / empty / unknown),
#     the watchdog defaults to ``'needs_input'`` (legacy behavior).
#   * If the row already has a typed ``block_kind`` (any of the four
#     :data:`sw._TYPED_BLOCK_KINDS`), the watchdog PRESERVES it and
#     records the escalation in the event log only. The emitted
#     ``blocked`` event carries ``kind`` = the preserved value and
#     ``preserved_block_kind: True`` for audit.
# ---------------------------------------------------------------------------


def _set_block_kind(conn, task_id: str, block_kind) -> None:
    """Set ``tasks.block_kind`` directly, bypassing block_task().

    block_task() has status-transition side-effects we don't want; we
    just need to seed the column so we can test the watchdog's
    preservation logic in isolation.
    """
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE tasks SET block_kind = ? WHERE id = ?",
            (block_kind, task_id),
        )


@pytest.mark.parametrize("typed_kind", ["dependency", "needs_input", "capability", "transient"])
def test_typed_block_kind_is_preserved_on_escalation(kanban_home, typed_kind):
    """Every value in :data:`sw._TYPED_BLOCK_KINDS` must be preserved
    across a watchdog escalation. Regression for t_847892e6: the
    historical UPDATE always wrote ``'needs_input'``, wiping out
    ``block_kind='dependency'`` and defeating the VFE-DISPATCH-01
    guard predicate that ``recompute_ready`` uses.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = f"t_preserve_{typed_kind}"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    _set_block_kind(conn, task_id, typed_kind)
    _append_trigger(
        conn, task_id=task_id, kind="claim_rejected",
        created_at=now - 600, reason="parents_not_done",
    )

    result = sw.sweep_once(conn, now=now)
    assert result.escalated_count == 1

    # Row moved to blocked but block_kind is preserved.
    row = conn.execute(
        "SELECT status, block_kind FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["status"] == "blocked"
    assert row["block_kind"] == typed_kind, (
        f"watchdog clobbered pre-existing block_kind={typed_kind!r} "
        f"(got {row['block_kind']!r}); preservation regression from t_847892e6"
    )

    # Blocked event carries the preserved value and the audit flag.
    blocked_evts = [e for e in _events(conn, task_id) if e["kind"] == "blocked"]
    assert len(blocked_evts) == 1
    payload = blocked_evts[0]["payload"]
    assert payload["kind"] == typed_kind
    assert payload["preserved_block_kind"] is True
    assert payload["prev_block_kind"] == typed_kind
    assert payload["source"] == "stall_watchdog"


def test_null_block_kind_defaults_to_needs_input(kanban_home):
    """Legacy / un-typed row (``block_kind IS NULL``) still defaults to
    ``'needs_input'`` so ``_has_outstanding_governance_gate`` fires.
    This is the pre-t_847892e6 behavior, preserved for backward
    compatibility.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_null_bk_defaults"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    # No _set_block_kind call — column stays NULL.
    _append_trigger(
        conn, task_id=task_id, kind="claim_rejected",
        created_at=now - 600, reason="parents_not_done",
    )

    result = sw.sweep_once(conn, now=now)
    assert result.escalated_count == 1

    row = conn.execute(
        "SELECT block_kind FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["block_kind"] == "needs_input"

    blocked_evts = [e for e in _events(conn, task_id) if e["kind"] == "blocked"]
    payload = blocked_evts[0]["payload"]
    assert payload["kind"] == "needs_input"
    assert payload["preserved_block_kind"] is False
    assert payload["prev_block_kind"] is None


def test_unknown_block_kind_string_defaults_to_needs_input(kanban_home):
    """A garbage ``block_kind`` value (not in :data:`_TYPED_BLOCK_KINDS`)
    is treated as un-typed and overwritten with ``'needs_input'``.
    Guards against a stale / mistyped column value being preserved by
    accident.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_bogus_bk_defaults"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    _set_block_kind(conn, task_id, "not_a_real_kind")
    _append_trigger(
        conn, task_id=task_id, kind="claim_rejected",
        created_at=now - 600, reason="parents_not_done",
    )

    result = sw.sweep_once(conn, now=now)
    assert result.escalated_count == 1

    row = conn.execute(
        "SELECT block_kind FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["block_kind"] == "needs_input"

    blocked_evts = [e for e in _events(conn, task_id) if e["kind"] == "blocked"]
    payload = blocked_evts[0]["payload"]
    assert payload["preserved_block_kind"] is False
    # prev_block_kind still captured for audit even when not preserved.
    assert payload["prev_block_kind"] == "not_a_real_kind"


def test_empty_block_kind_defaults_to_needs_input(kanban_home):
    """Empty-string ``block_kind`` (should never happen but let's be
    defensive) is treated as absent. Same defense-in-depth reasoning
    as the empty-string check in _is_typed_dependency_wait — a bad row
    doesn't confer preservation.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_empty_bk_defaults"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    _set_block_kind(conn, task_id, "")
    _append_trigger(
        conn, task_id=task_id, kind="claim_rejected",
        created_at=now - 600, reason="parents_not_done",
    )

    result = sw.sweep_once(conn, now=now)
    assert result.escalated_count == 1

    row = conn.execute(
        "SELECT block_kind FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["block_kind"] == "needs_input"


def test_preserved_dependency_still_makes_sticky_block(kanban_home):
    """When ``block_kind='dependency'`` is preserved, ``recompute_ready``
    still MUST NOT re-promote the row — the emitted ``blocked`` event
    is what ``_has_sticky_block`` reads, and it does NOT depend on the
    ``kind`` being in ``_GOVERNANCE_BLOCK_KINDS``. This is the full
    end-to-end regression: preserving ``'dependency'`` gives us the
    VFE-DISPATCH-01 guard AND the sticky-block guard together.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_dep_sticky_e2e"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    _set_block_kind(conn, task_id, "dependency")
    _append_trigger(
        conn, task_id=task_id, kind="claim_rejected",
        created_at=now - 600, reason="parents_not_done",
    )

    sw.sweep_once(conn, now=now)
    assert _status(conn, task_id) == "blocked"

    # block_kind kept dependency — DISPATCH-01 guard predicate intact.
    row = conn.execute(
        "SELECT block_kind FROM tasks WHERE id = ?", (task_id,)
    ).fetchone()
    assert row["block_kind"] == "dependency"

    # recompute_ready must NOT re-promote. The sticky-block predicate
    # scans blocked/unblocked events regardless of kind, so it fires
    # even though 'dependency' is not in _GOVERNANCE_BLOCK_KINDS.
    kb.recompute_ready(conn)
    assert _status(conn, task_id) == "blocked", (
        "recompute_ready re-promoted a row whose block_kind='dependency' "
        "was preserved by the watchdog; sticky-block predicate did not fire"
    )
    assert sw._kb._has_sticky_block(conn, task_id) is True


def test_is_typed_block_kind_helper():
    """Unit test for the :func:`_is_typed_block_kind` helper's edge
    cases. Belongs here rather than a separate unit test file — the
    helper's contract is tightly coupled to escalation preservation.
    """
    # All four typed kinds are recognized.
    for kind in sw._TYPED_BLOCK_KINDS:
        assert sw._is_typed_block_kind(kind) is True

    # Non-typed inputs all reject.
    for value in [None, "", "  ", "unknown", "DEPENDENCY", "dependency ", 42, True]:
        assert sw._is_typed_block_kind(value) is False, (
            f"value={value!r} unexpectedly counted as typed"
        )


def test_stall_escalated_payload_records_block_kind(kanban_home):
    """The ``stall_escalated`` audit event carries both the effective
    and prior ``block_kind`` so an operator can reconstruct what
    happened without re-reading the row. Sanity check on the audit
    payload we added in t_847892e6.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_stall_evt_payload_dep"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    _set_block_kind(conn, task_id, "dependency")
    _append_trigger(
        conn, task_id=task_id, kind="claim_rejected",
        created_at=now - 600, reason="parents_not_done",
    )

    sw.sweep_once(conn, now=now)
    stall_evts = [e for e in _events(conn, task_id) if e["kind"] == "stall_escalated"]
    assert len(stall_evts) == 1
    payload = stall_evts[0]["payload"]
    assert payload["block_kind"] == "dependency"
    assert payload["prev_block_kind"] == "dependency"


def test_stall_escalated_payload_records_prev_block_kind_when_none(kanban_home):
    """Complement of the previous test: when there was no prior typed
    block_kind, both fields still exist on the payload for uniform
    downstream parsing — ``block_kind`` is ``'needs_input'``,
    ``prev_block_kind`` is ``None``.
    """
    conn = _open(kanban_home)
    now = 1_800_000_000
    task_id = "t_stall_evt_payload_none"

    _make_ticket(conn, task_id=task_id, status="ready", created_at=now - 7200)
    # No block_kind set — column is NULL.
    _append_trigger(
        conn, task_id=task_id, kind="claim_rejected",
        created_at=now - 600, reason="parents_not_done",
    )

    sw.sweep_once(conn, now=now)
    stall_evts = [e for e in _events(conn, task_id) if e["kind"] == "stall_escalated"]
    assert len(stall_evts) == 1
    payload = stall_evts[0]["payload"]
    assert payload["block_kind"] == "needs_input"
    assert payload["prev_block_kind"] is None
