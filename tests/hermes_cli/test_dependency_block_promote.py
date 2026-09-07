"""Tests for VFE-DISPATCH-01: dependency-block auto-promote guard.

``block_task(kind='dependency', waiting_for=X)`` routes the blocked task
to ``status='todo'`` so that the same ``recompute_ready`` machinery that
clears parent-gated tasks also clears it. But the ``waiting_for`` id is
stored on the emitted ``dependency_wait`` event's payload, NOT as a
``task_links`` edge. Before this guard, ``recompute_ready`` looked up
``task_links`` parents (empty), evaluated the vacuous
``all([]) == True``, and re-promoted the task on the next dispatcher
tick — worker respawned, re-blocked for the same reason, looped.

The guard in :func:`hermes_cli.kanban_db._dependency_waiting_for_satisfied`
is a POSITIVE assertion (``waiting_for.status in ('done', 'archived')``)
so future intermediate task states (e.g. a new ``paused`` / ``review``)
never silently start satisfying the predicate.
"""

from __future__ import annotations

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


def _dep_block(conn, waiting_for: str, *, title: str = "waiter") -> str:
    """Create a task and dependency-block it on ``waiting_for``."""
    t = kb.create_task(conn, title=title)
    kb.claim_task(conn, t)  # ready -> running (block_task requires that)
    assert kb.block_task(
        conn, t, reason="waiting", kind="dependency", waiting_for=waiting_for,
    )
    return t


# ---------------------------------------------------------------------------
# Core bug: dependency block on a still-running parent must NOT auto-promote
# ---------------------------------------------------------------------------


def test_dependency_block_does_not_auto_promote_while_parent_running(conn):
    parent = kb.create_task(conn, title="parent")
    kb.claim_task(conn, parent)  # parent is running
    child = _dep_block(conn, waiting_for=parent, title="child")

    assert _status(conn, child) == "todo"
    assert _status(conn, parent) == "running"

    promoted = kb.recompute_ready(conn)
    assert promoted == 0
    assert _status(conn, child) == "todo", (
        "child must stay in todo while parent is still running — this was the "
        "vacuous-True bug that burned inference in a re-block loop"
    )


def test_dependency_block_does_not_auto_promote_while_parent_ready(conn):
    parent = kb.create_task(conn, title="parent")
    # parent is 'ready' (no assignee -> stays in ready once promoted)
    child = _dep_block(conn, waiting_for=parent, title="child")
    kb.recompute_ready(conn)
    assert _status(conn, child) == "todo"


def test_dependency_block_does_not_auto_promote_while_parent_todo(conn):
    grandparent = kb.create_task(conn, title="grandparent")
    kb.claim_task(conn, grandparent)  # keeps grandparent running
    parent = kb.create_task(conn, title="parent", parents=[grandparent])
    assert _status(conn, parent) == "todo"
    child = _dep_block(conn, waiting_for=parent, title="child")
    kb.recompute_ready(conn)
    assert _status(conn, child) == "todo"


def test_dependency_block_does_not_auto_promote_while_parent_blocked(conn):
    parent = kb.create_task(conn, title="parent")
    kb.claim_task(conn, parent)
    # Human-visible block (capability), NOT dependency
    kb.block_task(conn, parent, reason="need input", kind="capability")
    assert _status(conn, parent) == "blocked"

    child = _dep_block(conn, waiting_for=parent, title="child")
    kb.recompute_ready(conn)
    assert _status(conn, child) == "todo"


# ---------------------------------------------------------------------------
# Happy path: promotes exactly when waiting_for reaches done / archived
# ---------------------------------------------------------------------------


def test_dependency_block_promotes_when_parent_done(conn):
    parent = kb.create_task(conn, title="parent")
    kb.claim_task(conn, parent)
    child = _dep_block(conn, waiting_for=parent, title="child")
    assert _status(conn, child) == "todo"

    # Set parent to done via direct SQL to isolate ``recompute_ready``
    # from NERVE-02 cascade-unblock in ``complete_task``.
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
    conn.commit()

    promoted = kb.recompute_ready(conn)
    assert promoted == 1
    assert _status(conn, child) == "ready"


def test_dependency_block_promotes_when_parent_archived(conn):
    parent = kb.create_task(conn, title="parent")
    kb.claim_task(conn, parent)
    child = _dep_block(conn, waiting_for=parent, title="child")

    conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (parent,))
    conn.commit()

    kb.recompute_ready(conn)
    assert _status(conn, child) == "ready"


def test_dependency_block_promotes_exactly_once(conn):
    """DoD #7: KR2 was blocked 4× before the fix. After the fix, once the
    parent reaches ``done`` the child must promote exactly ONCE (not repeatedly
    on every dispatcher tick).
    """
    parent = kb.create_task(conn, title="parent")
    kb.claim_task(conn, parent)
    child = _dep_block(conn, waiting_for=parent, title="child")
    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
    conn.commit()

    p1 = kb.recompute_ready(conn)
    assert p1 == 1 and _status(conn, child) == "ready"

    # Repeat ticks are no-ops
    for _ in range(3):
        assert kb.recompute_ready(conn) == 0
    assert _status(conn, child) == "ready"


def test_multiple_dependency_children_all_promote_when_parent_done(conn):
    """DoD #4: three paused KR tickets should all promote exactly once
    when their shared blocker reaches ``done``.
    """
    parent = kb.create_task(conn, title="parent")
    kb.claim_task(conn, parent)
    kr2 = _dep_block(conn, waiting_for=parent, title="KR2")
    kr3 = _dep_block(conn, waiting_for=parent, title="KR3")
    kr4 = _dep_block(conn, waiting_for=parent, title="KR4")

    kb.recompute_ready(conn)
    for kr in (kr2, kr3, kr4):
        assert _status(conn, kr) == "todo"

    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
    conn.commit()

    promoted = kb.recompute_ready(conn)
    assert promoted == 3
    for kr in (kr2, kr3, kr4):
        assert _status(conn, kr) == "ready"


# ---------------------------------------------------------------------------
# Backward-compat / edge cases: guard must not regress unrelated paths
# ---------------------------------------------------------------------------


def test_non_dependency_todo_task_still_promotes_when_parents_done(conn):
    """Regression: a task in 'todo' with ``block_kind`` != 'dependency'
    (or None) must still be gated only by task_links parents. The guard is
    scoped to ``block_kind == 'dependency'`` and must not touch anything
    else.
    """
    parent = kb.create_task(conn, title="parent")
    child = kb.create_task(conn, title="child", parents=[parent])
    assert _status(conn, child) == "todo"

    conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
    conn.commit()

    kb.recompute_ready(conn)
    assert _status(conn, child) == "ready"


def test_dependency_block_with_missing_waiting_for_falls_through(conn):
    """A task marked ``block_kind='dependency'`` but whose emitted
    ``dependency_wait`` event lacks a ``waiting_for`` (legacy / racy data)
    must fall through to the normal ``task_links`` gate — otherwise a
    legacy dependency task with no gate at all could sit in 'todo' forever.
    """
    import json as _json
    t = kb.create_task(conn, title="legacy")
    conn.execute(
        "UPDATE tasks SET status = 'todo', block_kind = 'dependency' WHERE id = ?",
        (t,),
    )
    conn.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) "
        "VALUES (?, ?, ?, strftime('%s','now'))",
        (t, "dependency_wait", _json.dumps({"reason": "legacy"})),
    )
    conn.commit()

    # No parents, no waiting_for → falls through to parents=[] gate → promotes
    kb.recompute_ready(conn)
    assert _status(conn, t) == "ready"


def test_dependency_block_waiting_for_task_deleted_falls_through(conn):
    """If the waited-on task existed at block time but was later deleted,
    the guard cannot check its status. Fall through so the operator can
    resolve via ``kanban_unblock`` (returning ``False`` would strand the
    task in ``todo`` forever with no signal).
    """
    parent = kb.create_task(conn, title="parent")
    kb.claim_task(conn, parent)
    child = _dep_block(conn, waiting_for=parent, title="child")

    conn.execute("DELETE FROM tasks WHERE id = ?", (parent,))
    conn.commit()

    kb.recompute_ready(conn)
    # waiting_for gone → helper returns True → parents=[] → promote
    assert _status(conn, child) == "ready"


def test_sticky_blocked_task_untouched_by_guard(conn):
    """Regression: worker-initiated ``blocked`` (non-dependency) tasks
    must still be gated by ``_has_sticky_block`` and never auto-promoted.
    """
    t = kb.create_task(conn, title="human-blocked")
    kb.claim_task(conn, t)
    kb.block_task(conn, t, reason="need decision", kind="needs_input")
    assert _status(conn, t) == "blocked"

    kb.recompute_ready(conn)
    assert _status(conn, t) == "blocked"


# ---------------------------------------------------------------------------
# VFE-DISPATCH-02: running → dependency_wait → exit must not respawn-loop
# ---------------------------------------------------------------------------


class TestDispatch02DependencyWaitCrashReclaim:
    """VFE-DISPATCH-02: when a worker on a ``running`` ticket emits a
    ``dependency_wait`` event (via ``block_task(kind='dependency')``) but
    the task stays ``running`` (block transition failed — e.g.
    ``expected_run_id`` mismatch on a re-claimed run), and the worker then
    exits, ``detect_crashed_workers`` must route the task to ``todo`` (where
    the DISPATCH-01 guard holds) instead of ``ready`` (where it would be
    immediately re-claimed, respawning the worker in a ~30s inference-burning
    loop).
    """

    @pytest.fixture
    def crash_env(self, kanban_home, monkeypatch):
        """Disable the crash grace period and force ``_pid_alive`` to False."""
        monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
        import hermes_cli.kanban_db as _kb
        monkeypatch.setattr(_kb, "_pid_alive", lambda pid: False)
        # ``_classify_worker_exit`` reads from ``_recent_worker_exits``;
        # leave it empty so the exit is classified as ``"unknown"`` (a
        # genuine crash), not ``"clean_exit"`` (which would be a protocol
        # violation). The fix must fire regardless of exit classification.
        _kb._recent_worker_exits.clear()
        return monkeypatch

    def _emit_dep_wait(self, conn, task_id, waiting_for):
        """Simulate what ``block_task(kind='dependency')`` does: emit the
        ``dependency_wait`` event with a ``waiting_for`` handle but leave
        the task in ``running`` (simulating a failed transition).
        """
        import json as _json
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'dependency_wait', ?, strftime('%s','now'))",
            (task_id, _json.dumps({"waiting_for": waiting_for, "kind": "dependency"})),
        )
        conn.commit()

    def test_crash_with_dep_wait_routes_to_todo_not_ready(self, conn, crash_env):
        """DoD #2: worker emits ``dependency_wait`` + exits → task moves
        to ``todo``, NOT ``ready``.
        """
        parent = kb.create_task(conn, title="parent")
        kb.claim_task(conn, parent)  # parent is running

        child = kb.create_task(conn, title="child")
        kb.claim_task(conn, child)  # child is running
        kb._set_worker_pid(conn, child, 999999)

        # Simulate: block_task emitted the event but the UPDATE failed
        # (expected_run_id mismatch on a re-claimed run). Task is still
        # ``running`` with a ``dependency_wait`` event on record.
        self._emit_dep_wait(conn, child, waiting_for=parent)

        crashed = kb.detect_crashed_workers(conn)
        assert child not in crashed, (
            "dependency-wait reclaim must NOT count as a crash — the task "
            "is waiting on a parent, not failing"
        )
        assert _status(conn, child) == "todo", (
            "crash-reclaim of a dependency-wait task must route to ``todo`` "
            "(where the DISPATCH-01 guard holds), not ``ready`` (where "
            "claim_task would immediately re-claim and respawn the worker)"
        )

    def test_crash_with_dep_wait_blocks_recompute_ready(self, conn, crash_env):
        """DoD #3: after crash-reclaim routes to ``todo``,
        ``recompute_ready`` must NOT promote the task while the parent
        is still running.
        """
        parent = kb.create_task(conn, title="parent")
        kb.claim_task(conn, parent)

        child = kb.create_task(conn, title="child")
        kb.claim_task(conn, child)
        kb._set_worker_pid(conn, child, 999999)
        self._emit_dep_wait(conn, child, waiting_for=parent)

        kb.detect_crashed_workers(conn)
        assert _status(conn, child) == "todo"

        promoted = kb.recompute_ready(conn)
        assert promoted == 0
        assert _status(conn, child) == "todo", (
            "DISPATCH-01 guard must hold for the crash-reclaimed task — "
            "no promotion while the parent is still running"
        )

    def test_crash_with_dep_wait_promotes_when_parent_done(self, conn, crash_env):
        """DoD #4: once the parent completes, the crash-reclaimed task
        promotes exactly once (same as a normal dependency block).
        """
        parent = kb.create_task(conn, title="parent")
        kb.claim_task(conn, parent)

        child = kb.create_task(conn, title="child")
        kb.claim_task(conn, child)
        kb._set_worker_pid(conn, child, 999999)
        self._emit_dep_wait(conn, child, waiting_for=parent)

        kb.detect_crashed_workers(conn)
        assert _status(conn, child) == "todo"

        # Parent completes
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        conn.commit()

        promoted = kb.recompute_ready(conn)
        assert promoted == 1
        assert _status(conn, child) == "ready"

        # Repeat ticks are no-ops
        for _ in range(3):
            assert kb.recompute_ready(conn) == 0
        assert _status(conn, child) == "ready"

    def test_crash_without_dep_wait_still_resets_to_ready(self, conn, crash_env):
        """Regression: a normal crash (no ``dependency_wait`` event) must
        still reset to ``ready``. The DISPATCH-02 fix only diverts tasks
        that have a pending dependency wait.
        """
        t = kb.create_task(conn, title="plain crash")
        kb.claim_task(conn, t)
        kb._set_worker_pid(conn, t, 999999)

        crashed = kb.detect_crashed_workers(conn)
        assert t in crashed
        assert _status(conn, t) == "ready"

    def test_crash_with_dep_wait_parent_done_resets_to_ready(self, conn, crash_env):
        """Regression: if the waited-on task is already ``done`` when the
        worker crashes, the dependency is satisfied — the crash-reclaim
        should reset to ``ready`` (not ``todo``) so ``recompute_ready`` can
        promote it immediately.
        """
        parent = kb.create_task(conn, title="parent")
        kb.claim_task(conn, parent)
        conn.execute("UPDATE tasks SET status = 'done' WHERE id = ?", (parent,))
        conn.commit()

        child = kb.create_task(conn, title="child")
        kb.claim_task(conn, child)
        kb._set_worker_pid(conn, child, 999999)
        self._emit_dep_wait(conn, child, waiting_for=parent)

        kb.detect_crashed_workers(conn)
        assert _status(conn, child) == "ready", (
            "when the waited-on task is already done, the crash-reclaim "
            "must reset to ``ready`` so the task can be re-claimed — "
            "routing to ``todo`` would strand it unnecessarily"
        )

    def test_dep_wait_reclaim_emits_audit_event(self, conn, crash_env):
        """The crash-reclaim must emit a ``dependency_wait_reclaim`` event
        so an operator inspecting the board understands why the task
        landed in ``todo`` instead of ``ready``.
        """
        parent = kb.create_task(conn, title="parent")
        kb.claim_task(conn, parent)

        child = kb.create_task(conn, title="child")
        kb.claim_task(conn, child)
        kb._set_worker_pid(conn, child, 999999)
        self._emit_dep_wait(conn, child, waiting_for=parent)

        kb.detect_crashed_workers(conn)

        events = conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id DESC",
            (child,),
        ).fetchall()
        kinds = [e["kind"] for e in events]
        assert "dependency_wait_reclaim" in kinds, (
            "a ``dependency_wait_reclaim`` event must be emitted so the "
            "audit trail shows the crash-reclaim respected the pending dep"
        )
