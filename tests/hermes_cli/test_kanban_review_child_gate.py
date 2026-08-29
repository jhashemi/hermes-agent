"""Review-child promotion & atomic ``block_task(unblocks=...)`` tests.

Covers three fixes cut against task ``t_40375cc9``:

* Fix (a) — ``recompute_ready`` / ``claim_task`` / ``unblock_task``
  treat a governance-blocked parent that names THIS child in
  ``waiting_for`` as non-gating. Prevents the review-child deadlock
  where a review handoff child sits invisibly in ``todo`` because its
  parent flipped to ``blocked``.

* Fix (b) — ``block_task(unblocks=[...])`` atomically promotes listed
  child ids to ``ready`` in the same transaction. Records applied /
  skipped ids in a follow-up event.

* Fix (c) — ``dispatch_once(dry_run=True)`` populates
  ``DispatchResult.skipped_parent_deadlock`` with ``(child, parent)``
  tuples for todo children of blocked parents that do NOT match the
  review-child exception. Live ticks skip the extra scan.

Also asserts three regressions that must NOT be broken:

* A dependency-block-linked parent (``kind='dependency'``) still gates
  the child even with a ``waiting_for``.
* A parent with a governance block but NO ``waiting_for`` (or one that
  names a different task) still gates the child.
* A parent that is simply ``running`` / ``ready`` still gates normally.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME/HERMES_KANBAN_HOME with an empty kanban DB.

    Mirrors the fixture in ``tests/hermes_cli/test_kanban_db.py`` but
    also pins ``HERMES_KANBAN_HOME`` to the tempdir so writes never
    leak into the real board root (RCA 2026-08-22 completion-theater).
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Fix (a): recompute_ready / claim_task / unblock_task honour the
# review-child exception.
# ---------------------------------------------------------------------------


def test_recompute_ready_promotes_child_when_parent_blocked_waiting_for_it(
    kanban_home,
):
    """The core deadlock: parent blocks with ``waiting_for=<child_id>``.

    Without the exception the child would stay in ``todo`` because the
    ``all(parents in done/archived)`` predicate rejects the blocked
    parent. With ``_effective_parent_status`` treating the parent as
    ``done`` for this specific child, the child promotes to ``ready``.
    """
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="review-child", assignee="reviewer",
            parents=[parent],
        )
        # Parent blocks itself with a handoff to the child.
        assert kb.block_task(
            conn, parent,
            reason="review-required: needs approval",
            kind="needs_input",
            waiting_for=child,
        )
        assert kb.get_task(conn, parent).status == "blocked"
        # Before the fix, the child is stuck in todo forever.
        # After the fix, recompute_ready flips it to ready.
        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        assert kb.get_task(conn, child).status == "ready"


def test_recompute_ready_does_not_promote_when_waiting_for_names_other_task(
    kanban_home,
):
    """The exception is scoped to the SPECIFIC child named in
    ``waiting_for``. A different governance-blocked parent — one that
    names an unrelated task — must still gate this child."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        other = kb.create_task(conn, title="other", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="reviewer",
            parents=[parent],
        )
        # Parent blocks with waiting_for pointing at a THIRD task, not
        # at the child. The child must remain gated.
        assert kb.block_task(
            conn, parent,
            reason="need info X",
            kind="needs_input",
            waiting_for=other,
        )
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"


def test_recompute_ready_does_not_promote_when_parent_block_kind_is_dependency(
    kanban_home,
):
    """A dependency-block never routes to ``blocked`` (it goes to
    ``todo``), but even so, if a dependency-block ever landed a parent
    in ``blocked`` via legacy code paths, the exception must NOT fire
    — dependency blocks strictly wait on OTHER tasks, never on a
    child of the same graph edge."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="reviewer",
            parents=[parent],
        )
        # Force parent into blocked+dependency-shaped block event.
        # (Directly SQL-write to sidestep the ``kind='dependency'`` route
        # to ``todo`` — this is exactly the legacy-drift case the
        # exception scoping must survive.)
        conn.execute(
            "UPDATE tasks SET status = 'blocked', block_kind = 'dependency' "
            "WHERE id = ?",
            (parent,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', ?, strftime('%s','now'))",
            (parent, json.dumps({"kind": "dependency", "waiting_for": child})),
        )
        conn.commit()
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"


def test_recompute_ready_does_not_promote_when_unblock_supersedes_block(
    kanban_home,
):
    """An ``unblocked`` event newer than the ``blocked`` event closes
    the exception — the parent has moved on and any residual review
    child must once again wait for the parent to finish."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="reviewer",
            parents=[parent],
        )
        assert kb.block_task(
            conn, parent,
            reason="review", kind="needs_input", waiting_for=child,
        )
        # Operator unblocks the parent. Both go back into the queue on
        # their own schedules; the parent no longer names the child.
        assert kb.unblock_task(conn, parent)
        # Recompute_ready must NOT auto-promote the child now: the
        # exception is closed and the parent is not done.
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"


def test_recompute_ready_still_gates_on_normal_parent(kanban_home):
    """Regression: a plain parent in ``running`` still gates the child.
    The fix must not weaken the normal parent-completion invariant."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="reviewer",
            parents=[parent],
        )
        assert kb.recompute_ready(conn) == 0
        assert kb.get_task(conn, child).status == "todo"


def test_claim_task_review_child_promotion_survives_ready_flip(kanban_home):
    """``claim_task`` re-checks the parent invariant defensively. The
    exception must apply there too, or a promoted review child would
    be immediately demoted back to ``todo`` on claim."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="reviewer",
            parents=[parent],
        )
        assert kb.block_task(
            conn, parent,
            reason="review", kind="needs_input", waiting_for=child,
        )
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child).status == "ready"
        # Claim the ready child. The defensive parent-check must not
        # demote it back to todo.
        claimed = kb.claim_task(conn, child)
        assert claimed is not None
        assert kb.get_task(conn, child).status == "running"


# ---------------------------------------------------------------------------
# Fix (b): block_task(unblocks=[...])
# ---------------------------------------------------------------------------


def _last_event(conn, task_id, kind):
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, kind),
    ).fetchone()
    return json.loads(row["payload"]) if row and row["payload"] else None


def test_block_task_unblocks_promotes_named_child_atomically(kanban_home):
    """The intended primary use: parent blocks with review-child
    handoff and names the child in ``unblocks`` to guarantee it lands
    in ``ready`` in the same txn."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="review-child", assignee="reviewer",
            parents=[parent],
        )
        assert kb.block_task(
            conn, parent,
            reason="review", kind="needs_input",
            waiting_for=child, unblocks=[child],
        )
        assert kb.get_task(conn, parent).status == "blocked"
        assert kb.get_task(conn, child).status == "ready"
        applied = _last_event(conn, parent, "unblocks_applied")
        assert applied is not None
        assert applied["applied"] == [child]
        assert applied["skipped"] == []


def test_block_task_unblocks_rejects_non_child(kanban_home):
    """An id in ``unblocks`` that is not a child of ``task_id`` is
    recorded in ``skipped`` with reason ``not_a_child`` and the block
    still succeeds."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        stranger = kb.create_task(conn, title="stranger", assignee="worker")
        assert kb.block_task(
            conn, parent,
            reason="review", kind="needs_input", unblocks=[stranger],
        )
        applied = _last_event(conn, parent, "unblocks_applied")
        assert applied["applied"] == []
        assert applied["skipped"] == [
            {"id": stranger, "reason": "not_a_child"},
        ]
        # Stranger is untouched (still in whatever status it was created in;
        # we just care that block_task did not touch it).
        stranger_status = kb.get_task(conn, stranger).status
        assert stranger_status in ("todo", "ready", "running")
        # But specifically: no ``promoted`` event was fired for it AS A
        # RESULT of this block — the "not_a_child" reject short-circuits
        # before any UPDATE. Its status is only whatever create_task gave.


def test_block_task_unblocks_respects_child_governance_gate(kanban_home):
    """A child that carries its OWN governance gate must NOT be
    promoted by this parent's ``unblocks``. The parent block succeeds;
    the child stays gated."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="reviewer",
            parents=[parent],
        )
        # Manually stamp a governance gate on the child. Symmetric with
        # the pattern used by ``_has_outstanding_governance_gate``.
        conn.execute(
            "UPDATE tasks SET status = 'blocked', block_kind = 'needs_input' "
            "WHERE id = ?",
            (child,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', ?, strftime('%s','now'))",
            (child, json.dumps({"kind": "needs_input"})),
        )
        conn.commit()
        assert kb.block_task(
            conn, parent,
            reason="review", kind="needs_input", unblocks=[child],
        )
        applied = _last_event(conn, parent, "unblocks_applied")
        assert applied["skipped"] == [
            {"id": child, "reason": "governance_gate"},
        ]
        # Child is unmoved.
        assert kb.get_task(conn, child).status == "blocked"


def test_block_task_unblocks_ignored_when_task_not_blockable(kanban_home):
    """When the block itself fails (task not in a blockable state),
    ``unblocks`` must not fire — the whole transaction rolls back."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="reviewer",
            parents=[parent],
        )
        # Force parent into 'done' — not blockable.
        conn.execute(
            "UPDATE tasks SET status = 'done' WHERE id = ?", (parent,),
        )
        conn.commit()
        assert not kb.block_task(
            conn, parent,
            reason="try", kind="needs_input", unblocks=[child],
        )
        # Child untouched.
        assert kb.get_task(conn, child).status == "todo"


# ---------------------------------------------------------------------------
# Fix (c): DispatchResult.skipped_parent_deadlock reported in dry-run
# ---------------------------------------------------------------------------


def test_dispatch_dry_run_reports_parent_deadlock(kanban_home):
    """A blocked-parent-without-review-child-exception + todo-child
    pair shows up under ``skipped_parent_deadlock`` on a dry-run tick.
    The identical scenario with the ``waiting_for``-names-child
    exception does NOT (that child will promote on this same tick)."""
    with kb.connect() as conn:
        # Case 1: genuine deadlock — blocked parent, todo child, no
        # waiting_for pointing at the child.
        p1 = kb.create_task(conn, title="p1", assignee="worker")
        c1 = kb.create_task(conn, title="c1", assignee="reviewer", parents=[p1])
        assert kb.block_task(
            conn, p1, reason="need info", kind="needs_input",
        )
        # Case 2: review-child pattern — blocked parent waiting_for child.
        # Must NOT appear in the deadlock bucket.
        p2 = kb.create_task(conn, title="p2", assignee="worker")
        c2 = kb.create_task(conn, title="c2", assignee="reviewer", parents=[p2])
        assert kb.block_task(
            conn, p2, reason="review", kind="needs_input", waiting_for=c2,
        )

    # Fresh connection for dispatch (independent of the setup txn).
    with kb.connect() as conn:
        result = kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: None, dry_run=True,
        )
    ids = {child for child, _parent in result.skipped_parent_deadlock}
    assert c1 in ids
    assert c2 not in ids
    # And the pair is (child, parent) shape.
    for child, parent in result.skipped_parent_deadlock:
        if child == c1:
            assert parent == p1


def test_dispatch_live_tick_populates_skipped_parent_deadlock(kanban_home):
    """A live (non-dry-run) tick also populates ``skipped_parent_deadlock``
    now — the field is authoritative on every tick so telemetry /
    watchdogs can consume it without polling with ``dry_run=True``. The
    scan is a single indexed join and only walks pairs where the child
    is already ``todo`` and the parent is already ``blocked``, so the
    hot-path cost is bounded even on large boards."""
    with kb.connect() as conn:
        p = kb.create_task(conn, title="p", assignee="worker")
        c = kb.create_task(conn, title="c", assignee="reviewer", parents=[p])
        assert kb.block_task(
            conn, p, reason="need info", kind="needs_input",
        )
    with kb.connect() as conn:
        result = kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: None, dry_run=False,
        )
    assert (c, p) in result.skipped_parent_deadlock


# ---------------------------------------------------------------------------
# Fix (d): live-tick ``parent_deadlock_detected`` event emission.
#
# Recurrence-proof for task ``t_115096f9``: a review child created as a
# CHILD of a governance-blocked parent (without the ``waiting_for``
# handshake) was silently un-dispatchable for 4+ hours. The dry-run
# bucket existed but never landed in the event log, so
# ``hermes kanban tail`` and stall-watchdogs were blind.
# ---------------------------------------------------------------------------


def _count_events(conn, task_id, kind):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM task_events "
        "WHERE task_id = ? AND kind = ?",
        (task_id, kind),
    ).fetchone()
    return int(row["n"])


def test_live_tick_emits_parent_deadlock_detected_event(kanban_home):
    """Exact t_4f53c009 shape: parent blocks without naming child in
    ``waiting_for``; child sits in ``todo`` behind the blocked parent.
    One live tick must emit exactly one ``parent_deadlock_detected``
    event on the child, with the parent id and the parent's block
    event id captured in the payload so ``hermes kanban tail`` can
    surface it and stall-watchdogs can act on it."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="review-child", assignee="reviewer",
            parents=[parent],
        )
        # Parent goes blocked WITHOUT waiting_for=<child_id> — the
        # invisible-deadlock shape.
        assert kb.block_task(
            conn, parent,
            reason="review-required: needs approval",
            kind="needs_input",
        )
        assert kb.get_task(conn, parent).status == "blocked"
        assert kb.get_task(conn, child).status == "todo"

    with kb.connect() as conn:
        result = kb.dispatch_once(
            conn, spawn_fn=lambda *a, **k: None, dry_run=False,
        )
    # Result field populated on live tick too (was dry-run-only).
    assert (child, parent) in result.skipped_parent_deadlock

    with kb.connect() as conn:
        assert _count_events(conn, child, "parent_deadlock_detected") == 1
        payload = _last_event(conn, child, "parent_deadlock_detected")
        assert payload is not None
        assert payload["parent_id"] == parent
        # The block event id should be a positive integer (points at the
        # parent's most recent ``blocked`` event).
        assert isinstance(payload["parent_block_event_id"], int)
        assert payload["parent_block_event_id"] > 0
        assert "waiting_for" in payload["hint"]


def test_live_tick_rate_limits_repeated_deadlock_emission(kanban_home):
    """Repeated dispatch ticks on the same unresolved deadlock must
    emit the event exactly once — not once per tick. The rate limit
    key is the parent's most recent ``blocked`` event id."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="reviewer", parents=[parent],
        )
        assert kb.block_task(
            conn, parent, reason="need info", kind="needs_input",
        )

    for _tick in range(5):
        with kb.connect() as conn:
            kb.dispatch_once(
                conn, spawn_fn=lambda *a, **k: None, dry_run=False,
            )

    with kb.connect() as conn:
        assert _count_events(conn, child, "parent_deadlock_detected") == 1


def test_live_tick_reemits_after_new_block_event(kanban_home):
    """A fresh ``blocked`` task_event on the parent resets the
    rate-limit key so the NEXT deadlock cycle re-emits exactly once.
    This is the intended behaviour: a re-block after an unblock is a
    new deadlock cycle, and operators need to see it once.

    We SQL-inject the second ``blocked`` event to sidestep the
    BLOCK_RECURRENCE_LIMIT loop-breaker (``block_task`` routes a
    repeat-block on the same parent to ``triage`` on the second hit).
    That loop-breaker is intentional; here we're stress-testing the
    dedup key, not the loop-breaker."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="child", assignee="reviewer", parents=[parent],
        )
        assert kb.block_task(
            conn, parent, reason="cycle 1", kind="needs_input",
        )

    with kb.connect() as conn:
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, dry_run=False)

    # Emulate a fresh block cycle: insert a NEW 'blocked' event on the
    # parent. Parent status stays 'blocked'; only the most-recent
    # blocked event id (which is our dedup key) changes.
    with kb.connect() as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'blocked', ?, strftime('%s','now'))",
            (parent, json.dumps({"reason": "cycle 2", "kind": "needs_input"})),
        )
        conn.commit()

    with kb.connect() as conn:
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, dry_run=False)

    with kb.connect() as conn:
        assert _count_events(conn, child, "parent_deadlock_detected") == 2


def test_live_tick_does_not_emit_for_review_child_exception(kanban_home):
    """The review-child exception (parent block names child in
    ``waiting_for``) auto-promotes the child on the same tick. No
    ``parent_deadlock_detected`` event should fire for that shape —
    it is not a deadlock. Regression against over-eager emission."""
    with kb.connect() as conn:
        parent = kb.create_task(conn, title="parent", assignee="worker")
        child = kb.create_task(
            conn, title="review-child", assignee="reviewer",
            parents=[parent],
        )
        assert kb.block_task(
            conn, parent, reason="review",
            kind="needs_input", waiting_for=child,
        )

    with kb.connect() as conn:
        kb.dispatch_once(conn, spawn_fn=lambda *a, **k: None, dry_run=False)

    with kb.connect() as conn:
        assert _count_events(conn, child, "parent_deadlock_detected") == 0
        assert kb.get_task(conn, child).status in ("ready", "running")
