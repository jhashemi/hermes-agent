"""Tests for VFE-NERVE-02: atomic cascade auto-unblock in ``complete_task``.

Covers the guardrails documented in ``_cascade_unblock_one`` and the
``unblocks_verified`` wiring in ``complete_task``:

* happy path: ``blocked`` downstream with matching ``waiting_for`` flips
  to ``ready`` in the same txn as the completion;
* ``block_kind`` gating: ``capability`` / ``needs_input`` blocks stay
  blocked, ``dependency`` / ``transient`` / legacy-None cascade;
* ``waiting_for`` gating: missing or mismatched envelope skips the
  cascade with an audit event, no state flip;
* idempotency: cascading an ``unblocks=[X]`` on a task already ``ready``
  is a no-op audit event, not an error;
* atomicity: a failing completion (bad ``expected_run_id``) does NOT
  cascade downstream state — the whole txn rolls back;
* parent gate: a downstream with other undone parents lands in ``todo``
  (not ``ready``) after cascade, matching manual ``unblock_task``;
* multi-cascade: one completion can unblock several downstream tickets
  in the same txn;
* audit surface: every cascade emits exactly one
  ``cascade_unblocked`` or ``cascade_skipped`` event carrying
  ``source_task``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixture — mirrors tests/hermes_cli/test_kanban_db.py
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_blocked_task(
    conn,
    *,
    title: str,
    waiting_for: str,
    kind: Optional[str] = None,
) -> str:
    """Create a task and block it with a typed ``waiting_for`` envelope.

    Uses ``block_task`` (rather than raw SQL) so the ``waiting_for``
    lands in the event payload the way the production code emits it.
    ``block_task`` only transitions from ``running``/``ready`` — so we
    claim the ready task first.
    """
    t = kb.create_task(conn, title=title)
    kb.claim_task(conn, t)  # ready -> running
    assert kb.block_task(
        conn, t,
        reason="waiting",
        kind=kind,
        waiting_for=waiting_for,
    )
    return t


def _events(conn, task_id: str, kind: str) -> list[dict]:
    """Return payloads of every event with the given kind on task_id."""
    return [
        e.payload or {}
        for e in kb.list_events(conn, task_id)
        if e.kind == kind
    ]


def _status(conn, task_id: str) -> str:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None, f"task {task_id} not found"
    return row["status"]


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_cascade_unblocks_blocked_downstream_when_waiting_for_matches(kanban_home):
    """Completion of task A with ``unblocks=[B]`` flips B blocked -> ready."""
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        b = _make_blocked_task(conn, title="downstream", waiting_for=a, kind="transient")
        assert _status(conn, b) == "blocked"

        assert kb.complete_task(conn, a, result="done", unblocks=[b])

        assert _status(conn, b) == "ready"
        cascades = _events(conn, b, "cascade_unblocked")
        assert len(cascades) == 1
        assert cascades[0]["source_task"] == a
        assert cascades[0]["new_status"] == "ready"


def test_cascade_recorded_on_completion_event_payload(kanban_home):
    """``unblocks_verified`` list is still recorded on the ``completed`` event."""
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        b = _make_blocked_task(conn, title="downstream", waiting_for=a, kind="transient")
        assert kb.complete_task(conn, a, result="done", unblocks=[b])
        completed = _events(conn, a, "completed")
        assert completed and completed[-1].get("unblocks") == [b]


# ---------------------------------------------------------------------------
# Guardrail: waiting_for match
# ---------------------------------------------------------------------------


def test_cascade_skipped_when_waiting_for_mismatch(kanban_home):
    """Downstream blocked on task C is NOT cascaded when task A completes."""
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream A")
        c = kb.create_task(conn, title="unrelated C")
        b = _make_blocked_task(conn, title="downstream", waiting_for=c, kind="transient")

        assert kb.complete_task(conn, a, result="done", unblocks=[b])

        # b MUST still be blocked — its waiting_for was c, not a.
        assert _status(conn, b) == "blocked"
        skipped = _events(conn, b, "cascade_skipped")
        assert any(
            s.get("reason") == "waiting_for_mismatch"
            and s.get("source_task") == a
            and s.get("waiting_for") == c
            for s in skipped
        )
        assert not _events(conn, b, "cascade_unblocked")


def test_cascade_skipped_when_waiting_for_missing(kanban_home):
    """Legacy prose-only blocks (no ``waiting_for`` in envelope) are skipped."""
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        b = kb.create_task(conn, title="downstream")
        kb.claim_task(conn, b)
        # block_task with no waiting_for — legacy shape.
        assert kb.block_task(conn, b, reason="just because", kind="transient")

        assert kb.complete_task(conn, a, result="done", unblocks=[b])

        assert _status(conn, b) == "blocked"
        skipped = _events(conn, b, "cascade_skipped")
        assert any(s.get("reason") == "waiting_for_missing" for s in skipped)


# ---------------------------------------------------------------------------
# Guardrail: human-gated block kinds
# ---------------------------------------------------------------------------


def test_cascade_skipped_for_capability_block_kind(kanban_home):
    """``capability`` blocks require human input — never cascade past."""
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        b = _make_blocked_task(
            conn, title="downstream", waiting_for=a, kind="capability",
        )
        assert kb.complete_task(conn, a, result="done", unblocks=[b])
        assert _status(conn, b) == "blocked"
        skipped = _events(conn, b, "cascade_skipped")
        assert any(
            s.get("reason") == "human_gated_block_kind"
            and s.get("block_kind") == "capability"
            for s in skipped
        )


def test_cascade_skipped_for_needs_input_block_kind(kanban_home):
    """``needs_input`` blocks require human input — never cascade past."""
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        b = _make_blocked_task(
            conn, title="downstream", waiting_for=a, kind="needs_input",
        )
        assert kb.complete_task(conn, a, result="done", unblocks=[b])
        assert _status(conn, b) == "blocked"
        skipped = _events(conn, b, "cascade_skipped")
        assert any(
            s.get("reason") == "human_gated_block_kind"
            and s.get("block_kind") == "needs_input"
            for s in skipped
        )


def test_cascade_proceeds_for_transient_block_kind(kanban_home):
    """``transient`` blocks cascade normally."""
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        b = _make_blocked_task(conn, title="downstream", waiting_for=a, kind="transient")
        assert kb.complete_task(conn, a, result="done", unblocks=[b])
        assert _status(conn, b) == "ready"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_cascade_idempotent_when_already_ready(kanban_home):
    """``unblocks=[B]`` when B is already ready is a no-op audit, not error.

    ``unblocks_verified`` filters to blocked/todo, so a ready B will not
    even reach the cascade helper; either way the completion succeeds
    and no state change occurs.
    """
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        b = kb.create_task(conn, title="downstream")  # b is ready
        assert _status(conn, b) == "ready"

        assert kb.complete_task(conn, a, result="done", unblocks=[b])

        # B stays ready.
        assert _status(conn, b) == "ready"
        # completed event carries B under unblocks_not_blocked, NOT
        # unblocks (validation happens before the cascade).
        completed = _events(conn, a, "completed")
        assert completed and b in completed[-1].get("unblocks_not_blocked", [])
        assert b not in completed[-1].get("unblocks", [])


# ---------------------------------------------------------------------------
# Parent gate on the new status
# ---------------------------------------------------------------------------


def test_cascade_lands_in_todo_when_other_parents_still_open(kanban_home):
    """A cascaded downstream with other undone parents goes to 'todo', not 'ready'."""
    with kb.connect() as conn:
        # Structure: b has two parents (a and p2). b is 'blocked' with
        # waiting_for=a. When a completes and cascades b, p2 is still
        # 'ready' (not 'done') — so b lands in 'todo', not 'ready'.
        a = kb.create_task(conn, title="upstream A")
        p2 = kb.create_task(conn, title="other parent")
        b = kb.create_task(conn, title="downstream", parents=[a, p2])
        # b is 'todo' pending parents. Get it into 'blocked' with a
        # waiting_for envelope pointing at a. Directly set status +
        # emit the block envelope so we exercise the cascade path
        # rather than block_task's status-gating.
        conn.execute(
            "UPDATE tasks SET status = 'blocked', block_kind = 'transient' WHERE id = ?",
            (b,),
        )
        # Emit the block event with typed waiting_for so the cascade
        # helper's payload lookup finds it (this is exactly what
        # block_task would have written).
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, 'blocked', ?, strftime('%s','now'))",
            (b, json.dumps({"reason": "waiting", "kind": "transient", "waiting_for": a})),
        )

        assert kb.complete_task(conn, a, result="done", unblocks=[b])

        # b cascade-unblocked, but lands in 'todo' because p2 is still open.
        assert _status(conn, b) == "todo"
        cascades = _events(conn, b, "cascade_unblocked")
        assert cascades and cascades[-1]["new_status"] == "todo"


# ---------------------------------------------------------------------------
# Multiple downstream cascades in one completion
# ---------------------------------------------------------------------------


def test_multi_cascade_atomic(kanban_home):
    """One completion can unblock several downstream tickets in the same txn."""
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        b1 = _make_blocked_task(conn, title="d1", waiting_for=a, kind="transient")
        b2 = _make_blocked_task(conn, title="d2", waiting_for=a, kind="transient")
        b3 = _make_blocked_task(conn, title="d3", waiting_for=a, kind="transient")

        assert kb.complete_task(conn, a, result="done", unblocks=[b1, b2, b3])

        for b in (b1, b2, b3):
            assert _status(conn, b) == "ready", f"{b} still blocked"
            assert _events(conn, b, "cascade_unblocked")


def test_multi_cascade_mixed_guardrails(kanban_home):
    """A batch cascade where some pass and some are skipped, all atomically."""
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        other = kb.create_task(conn, title="other")
        # b_ok cascades cleanly.
        b_ok = _make_blocked_task(conn, title="ok", waiting_for=a, kind="transient")
        # b_needs_human is capability-blocked — stays put.
        b_gated = _make_blocked_task(conn, title="gated", waiting_for=a, kind="capability")
        # b_wrong points at a different upstream — stays put.
        b_wrong = _make_blocked_task(conn, title="wrong", waiting_for=other, kind="transient")

        assert kb.complete_task(
            conn, a, result="done", unblocks=[b_ok, b_gated, b_wrong],
        )

        assert _status(conn, b_ok) == "ready"
        assert _status(conn, b_gated) == "blocked"
        assert _status(conn, b_wrong) == "blocked"

        assert _events(conn, b_ok, "cascade_unblocked")
        assert any(
            s.get("reason") == "human_gated_block_kind"
            for s in _events(conn, b_gated, "cascade_skipped")
        )
        assert any(
            s.get("reason") == "waiting_for_mismatch"
            for s in _events(conn, b_wrong, "cascade_skipped")
        )


# ---------------------------------------------------------------------------
# Atomicity: failed completion must NOT cascade
# ---------------------------------------------------------------------------


def test_failed_completion_does_not_cascade(kanban_home):
    """If complete_task returns False (bad expected_run_id), downstream is untouched."""
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        b = _make_blocked_task(conn, title="downstream", waiting_for=a, kind="transient")

        # Force a False return by passing a bogus expected_run_id.  The
        # completion txn rolls back; the cascade must not have run.
        assert not kb.complete_task(
            conn, a, result="done",
            unblocks=[b],
            expected_run_id=999_999_999,
        )

        assert _status(conn, b) == "blocked"
        assert not _events(conn, b, "cascade_unblocked")
        # And no cascade_skipped either — we never reached the helper.
        assert not _events(conn, b, "cascade_skipped")


# ---------------------------------------------------------------------------
# Run-row invariant on cascade
# ---------------------------------------------------------------------------


def test_cascade_reclaims_dangling_run_row(kanban_home):
    """If the downstream had a lingering current_run_id, cascade reclaims it."""
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        b = _make_blocked_task(conn, title="downstream", waiting_for=a, kind="transient")

        # Inject a dangling run_id — simulates a crashed worker whose
        # block_task path didn't close the run (production RCA scenario).
        row = conn.execute(
            "SELECT id FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
            (b,),
        ).fetchone()
        assert row is not None
        run_id = int(row["id"])
        conn.execute(
            "UPDATE task_runs SET ended_at = NULL, outcome = NULL, status = 'running' WHERE id = ?",
            (run_id,),
        )
        conn.execute(
            "UPDATE tasks SET current_run_id = ? WHERE id = ?",
            (run_id, b),
        )

        assert kb.complete_task(conn, a, result="done", unblocks=[b])

        assert _status(conn, b) == "ready"
        # current_run_id is cleared.
        row2 = conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (b,)).fetchone()
        assert row2["current_run_id"] is None
        # The dangling run is closed as reclaimed.
        run_row = conn.execute(
            "SELECT status, outcome, ended_at FROM task_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
        assert run_row["status"] == "reclaimed"
        assert run_row["outcome"] == "reclaimed"
        assert run_row["ended_at"] is not None


# ---------------------------------------------------------------------------
# Block-loop counter preservation
# ---------------------------------------------------------------------------


def test_cascade_preserves_block_kind_and_recurrences(kanban_home):
    """Cascade must not reset ``block_kind`` / ``block_recurrences`` (matches unblock_task)."""
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        b = _make_blocked_task(conn, title="downstream", waiting_for=a, kind="transient")
        # Simulate a re-block history — bump recurrences directly.
        conn.execute(
            "UPDATE tasks SET block_recurrences = 2 WHERE id = ?", (b,),
        )

        assert kb.complete_task(conn, a, result="done", unblocks=[b])

        row = conn.execute(
            "SELECT status, block_kind, block_recurrences FROM tasks WHERE id = ?",
            (b,),
        ).fetchone()
        assert row is not None
        assert row["status"] == "ready"
        assert row["block_kind"] == "transient"  # PRESERVED
        assert row["block_recurrences"] == 2      # PRESERVED (loop-breaker memory)


# ---------------------------------------------------------------------------
# Audit surface — every cascade path emits exactly one downstream event
# ---------------------------------------------------------------------------


def test_every_cascade_emits_exactly_one_downstream_event(kanban_home):
    """Every downstream in ``unblocks_verified`` receives exactly one
    cascade_unblocked OR cascade_skipped event per completion.
    """
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        b_ok = _make_blocked_task(conn, title="ok", waiting_for=a, kind="transient")
        b_skip = _make_blocked_task(conn, title="skip", waiting_for=a, kind="capability")

        assert kb.complete_task(conn, a, result="done", unblocks=[b_ok, b_skip])

        ok_events = [
            e for e in kb.list_events(conn, b_ok)
            if e.kind in ("cascade_unblocked", "cascade_skipped")
        ]
        skip_events = [
            e for e in kb.list_events(conn, b_skip)
            if e.kind in ("cascade_unblocked", "cascade_skipped")
        ]
        assert len(ok_events) == 1 and ok_events[0].kind == "cascade_unblocked"
        assert len(skip_events) == 1 and skip_events[0].kind == "cascade_skipped"
        # Every event carries source_task in the payload.
        assert ok_events[0].payload["source_task"] == a
        assert skip_events[0].payload["source_task"] == a


def test_cascade_json_payload_shape_is_stable(kanban_home):
    """Downstream tooling (dashboard / gateway) can rely on stable payload keys."""
    with kb.connect() as conn:
        a = kb.create_task(conn, title="upstream")
        b = _make_blocked_task(conn, title="downstream", waiting_for=a, kind="transient")

        assert kb.complete_task(conn, a, result="done", unblocks=[b])

        row = conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id = ? AND kind = 'cascade_unblocked' "
            "ORDER BY id DESC LIMIT 1",
            (b,),
        ).fetchone()
        payload = json.loads(row["payload"])
        assert set(payload.keys()) == {"source_task", "new_status"}
        assert payload["source_task"] == a
        assert payload["new_status"] in ("ready", "todo")
