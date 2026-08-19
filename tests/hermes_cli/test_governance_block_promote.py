"""Tests for the governance-block auto-promote guard.

**Hole (observed 2026-08-19 01:27 UTC on t_93231838, B2b RED-ZONE):**
a card that had been ``block_task(kind='needs_input')`` twice was later
decomposed to ``status='todo'``. When its final child completed,
``recompute_ready``'s ``todo`` branch checked only the parent-completion
predicate — it never consulted ``_has_sticky_block`` (that check lived
only inside the ``blocked`` branch). The root was promoted to ``ready``,
the dispatcher spawned a worker, and the operator had to manually
re-block mid-run.

**Invariant (this test module enforces):** a card that has ever been
blocked with ``kind='needs_input'`` / ``'capability'`` — or that carries
a ``block_loop_detected`` event with ``waiting_for_condition`` — must
NOT be auto-promoted to ``ready`` by child completion alone. Auto-
promotion resumes only after the governance block is cleared by
:func:`hermes_cli.kanban_db.unblock_task` (which emits an
``"unblocked"`` event that supersedes the earlier ``"blocked"`` in the
``_has_sticky_block`` predicate).

Dependency- and transient-typed blocks retain their existing
auto-recovery semantics (they clear themselves once parents finish).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


def _status(conn, task_id: str) -> str:
    row = conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
    assert row is not None, f"task {task_id} not found"
    return row["status"]


def _block_needs_input(conn, task_id: str, *, reason: str = "gate") -> None:
    """Claim + block with kind='needs_input'. Mirrors the governance path."""
    kb.claim_task(conn, task_id)  # ready -> running
    assert kb.block_task(conn, task_id, reason=reason, kind="needs_input")
    assert _status(conn, task_id) == "blocked"


def _wire_child_gates_parent(conn, parent: str, child: str) -> None:
    """Wire the decomposer-style edge: ``child`` is a task_links.parent of
    ``parent`` — i.e. ``parent``'s promotion waits until ``child`` is done.
    """
    conn.execute(
        "INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)",
        (child, parent),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Core bug: needs_input block bypassed via decomposition path
# ---------------------------------------------------------------------------


def test_needs_input_then_todo_via_child_completion_stays_gated(conn):
    """The exact B2b bypass observed on 2026-08-19: parent is blocked
    ``needs_input``, then external state routes it back to ``todo`` (in
    production this was ``decompose_triage_task``; here we simulate that
    row-level flip directly so the test doesn't couple to the decomposer
    surface), a child completes, and ``recompute_ready`` must NOT promote
    the parent to ``ready``.
    """
    parent = kb.create_task(conn, title="human-gated parent")
    # Block the parent with needs_input BEFORE any linking so we don't
    # trip the ``claim_task`` parent-completion guard.
    _block_needs_input(conn, parent, reason="human Reviewed-by: required")

    # Create the leaf child and wire the decomposer-style edge (child
    # gates parent). Do this AFTER the block so ``claim_task`` inside
    # ``_block_needs_input`` sees no undone parents.
    child = kb.create_task(conn, title="child")
    _wire_child_gates_parent(conn, parent, child)

    # External path (decomposition, operator SQL, hook) flips the row back
    # to ``todo``. The governance history remains in ``task_events``.
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent,))
    conn.commit()

    # The leaf child completes.
    kb.claim_task(conn, child)
    assert kb.complete_task(conn, child, result="done")
    assert _status(conn, child) == "done"

    # The invariant: the parent must stay non-ready because the
    # ``needs_input`` block was never explicitly cleared.
    assert _status(conn, parent) != "ready", (
        "governance-gated parent must not be auto-promoted by child "
        "completion — the operator has to unblock it deliberately"
    )


def test_needs_input_still_blocked_recompute_no_promote(conn):
    """Guard is symmetric across the ``blocked`` branch (already covered by
    ``_has_sticky_block``) and this new ``todo`` branch. Sanity: a task in
    ``blocked`` with a ``needs_input`` history still stays blocked when
    ``recompute_ready`` runs — no regression on the pre-existing behaviour.
    """
    t = kb.create_task(conn, title="human-blocked")
    _block_needs_input(conn, t, reason="need decision")
    assert _status(conn, t) == "blocked"

    kb.recompute_ready(conn)
    assert _status(conn, t) == "blocked"


def test_unblock_clears_the_gate_and_next_recompute_promotes(conn):
    """After an explicit ``unblock_task``, the governance gate is
    cleared and the same completion-driven promotion path DOES fire.
    This confirms the gate is not overly sticky — it lifts on the
    single legitimate exit (operator unblock).
    """
    parent = kb.create_task(conn, title="parent")
    _block_needs_input(conn, parent, reason="gate")

    child = kb.create_task(conn, title="child")
    _wire_child_gates_parent(conn, parent, child)

    # Decomposition-style flip: parent back to ``todo`` while the
    # governance history stands.
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent,))
    conn.commit()

    kb.claim_task(conn, child)
    assert kb.complete_task(conn, child, result="done")
    # Still gated at this point.
    assert _status(conn, parent) == "todo"

    # Operator lifts the gate. Note: unblock_task requires status in
    # ('blocked', 'scheduled'), so flip to blocked first (matches the
    # real remediation path: operator sees the sticky-block, re-blocks
    # or triages, then unblocks).
    conn.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (parent,))
    conn.commit()
    assert kb.unblock_task(conn, parent)

    # A subsequent recompute now promotes: the child is done, the gate
    # is lifted.
    kb.recompute_ready(conn)
    assert _status(conn, parent) == "ready", (
        "after unblock the parent must promote on the next recompute — "
        "otherwise the gate would be permanent and no work could ever "
        "resume"
    )


def test_capability_block_is_also_sticky_across_todo(conn):
    """``needs_input`` is the reported case; ``capability`` is the other
    human-must-decide kind (per :data:`VALID_BLOCK_KINDS`). Same rule
    applies: a ``capability`` block that is later routed to ``todo`` by
    an external path must not be silently auto-promoted.
    """
    parent = kb.create_task(conn, title="parent")
    kb.claim_task(conn, parent)
    assert kb.block_task(
        conn, parent, reason="no creds available", kind="capability",
    )
    assert _status(conn, parent) == "blocked"

    child = kb.create_task(conn, title="child")
    _wire_child_gates_parent(conn, parent, child)

    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent,))
    conn.commit()

    kb.claim_task(conn, child)
    assert kb.complete_task(conn, child, result="done")
    assert _status(conn, parent) != "ready", (
        "capability-blocked parent must also stay gated across the "
        "decomposition/todo path"
    )


# ---------------------------------------------------------------------------
# Loop-breaker path: block_loop_detected + waiting_for_condition
# ---------------------------------------------------------------------------


def test_block_loop_detected_with_waiting_for_condition_gates_promotion(conn):
    """When :data:`BLOCK_RECURRENCE_LIMIT` is hit, ``block_task`` routes
    the task to ``triage`` and emits ``block_loop_detected`` (NOT
    ``blocked``). ``_has_sticky_block`` alone doesn't cover that (it
    only looks at ``blocked``/``unblocked`` events). The task body
    invariant is explicit: "or carries a ``block_loop_detected`` event
    with ``waiting_for_condition``".

    Here we directly emit a ``block_loop_detected`` event, flip the
    row-level ``status`` to ``todo`` (as would happen if a decomposer or
    hook mutated the task later on), and verify ``recompute_ready``
    respects the loop-detected gate.
    """
    parent = kb.create_task(conn, title="parent")
    # Emit ``block_loop_detected`` with ``waiting_for_condition`` — the
    # loop-breaker's marker of "this task escalated past self-recovery;
    # human must clear the condition explicitly".
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, 'block_loop_detected', ?, strftime('%s','now'))",
        (
            parent,
            json.dumps(
                {
                    "reason": "recurrent",
                    "kind": "needs_input",
                    "recurrences": kb.BLOCK_RECURRENCE_LIMIT,
                    "limit": kb.BLOCK_RECURRENCE_LIMIT,
                    "waiting_for_condition": "reviewer signs docs/proofs/xxx.md",
                }
            ),
        ),
    )
    conn.commit()

    child = kb.create_task(conn, title="child")
    _wire_child_gates_parent(conn, parent, child)
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent,))
    conn.commit()

    kb.claim_task(conn, child)
    assert kb.complete_task(conn, child, result="done")
    assert _status(conn, parent) != "ready", (
        "a task carrying a ``block_loop_detected`` event with an "
        "unresolved ``waiting_for_condition`` must not be silently "
        "auto-promoted on child completion"
    )


# ---------------------------------------------------------------------------
# Regression guards on unrelated paths
# ---------------------------------------------------------------------------


def test_transient_block_still_auto_recovers_after_child_completion(conn):
    """``transient`` blocks are meant to clear on their own — a worker
    uses them to signal "flaky, might clear on its own". They must NOT
    be treated as governance gates.
    """
    parent = kb.create_task(conn, title="parent")
    kb.claim_task(conn, parent)
    assert kb.block_task(conn, parent, reason="flaky", kind="transient")

    child = kb.create_task(conn, title="child")
    _wire_child_gates_parent(conn, parent, child)

    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent,))
    conn.commit()

    kb.claim_task(conn, child)
    assert kb.complete_task(conn, child, result="done")
    # ``transient`` isn't a human-governance kind → recovery is allowed.
    assert _status(conn, parent) == "ready", (
        "transient blocks must retain the historical auto-recover "
        "behaviour on child completion — they aren't governance gates"
    )


def test_task_with_no_prior_block_history_promotes_normally(conn):
    """The check must not regress the base case: a task that has never
    been blocked promotes to ``ready`` on child completion as before.
    """
    parent = kb.create_task(conn, title="parent")
    child = kb.create_task(conn, title="child")
    _wire_child_gates_parent(conn, parent, child)
    # Parent starts in ``todo`` (decomposer state) with no block history.
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (parent,))
    conn.commit()

    kb.claim_task(conn, child)
    assert kb.complete_task(conn, child, result="done")
    assert _status(conn, parent) == "ready"
