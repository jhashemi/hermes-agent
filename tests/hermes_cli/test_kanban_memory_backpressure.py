"""Regression tests for FIX-B (t_ce9a36ca) — memory-aware backpressure gate.

When ``kanban.memory_backpressure_gb`` is set and host available memory
drops below the threshold, ``dispatch_once`` defers ALL spawns this tick.
The reclaim/promote/timed-out sweeps still run so tasks don't stall.

Motivation (parent audit t_c3ba9176 §3): 152 of 208 pid-not-alive crashes
on adr-006b-phase-2 had zero heartbeats and clustered in 5-min bursts
during memory pressure — the fork or child died during context load,
not during work. This gate short-circuits that failure mode by deferring
the spawn loop when host free memory is under threshold, letting
already-running workers finish and free RAM before we pile on more.

Contract:
  - ``DispatchResult.deferred_memory`` is ``(available_gb, threshold_gb)``
    when the gate trips, ``None`` otherwise.
  - Reclaim/promote/timed-out sweeps still run when the gate trips.
  - Gate is disabled when ``memory_backpressure_gb`` is ``None`` or ``<=0``.
  - Gate is fail-open when ``psutil`` is unavailable / raises — never
    fail-closed on a missing dep, or the dispatcher tick silently drops.
  - The gate fires BEFORE ``max_spawn`` is consulted (sweep → gate → spawn).
"""
from __future__ import annotations

import os
import sys
import tempfile
from unittest.mock import patch, MagicMock

import pytest


@pytest.fixture()
def isolated_kanban_home_with_profile(monkeypatch):
    """Fresh HERMES_HOME with a 'jeff_dean' profile so tasks are spawnable."""
    test_home = tempfile.mkdtemp(prefix="kanban_mem_bp_test_")
    for prof in ("jeff_dean", "alpha", "default"):
        os.makedirs(os.path.join(test_home, "profiles", prof), exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db


def _fake_spawn(*args, **kwargs):
    return 12345


def _mock_psutil_avail_gb(avail_gb: float):
    """Build a MagicMock that mimics ``psutil.virtual_memory().available``."""
    mock_mem = MagicMock()
    mock_mem.available = int(avail_gb * 1e9)
    mock_psutil = MagicMock()
    mock_psutil.virtual_memory.return_value = mock_mem
    return mock_psutil


# ─── Core gate behavior ────────────────────────────────────────────────

def test_gate_defers_spawns_when_avail_below_threshold(
    isolated_kanban_home_with_profile,
):
    """500MB available, 1.0GB threshold → deferred_memory set, no spawns."""
    kb = isolated_kanban_home_with_profile
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(3):
            kb.create_task(conn, title=f"t{i}", assignee="jeff_dean")

    with patch.dict(sys.modules, {"psutil": _mock_psutil_avail_gb(0.5)}):
        with kb.connect_closing() as conn:
            res = kb.dispatch_once(
                conn,
                spawn_fn=_fake_spawn,
                dry_run=False,
                memory_backpressure_gb=1.0,
            )

    assert res.deferred_memory is not None, "gate should trip"
    avail, threshold = res.deferred_memory
    assert threshold == 1.0
    assert avail == pytest.approx(0.5, abs=0.01)
    assert res.spawned == [], "no spawns when memory-gated"


def test_gate_does_not_defer_when_avail_above_threshold(
    isolated_kanban_home_with_profile,
):
    """4.0GB available, 1.0GB threshold → gate passes, spawns proceed."""
    kb = isolated_kanban_home_with_profile
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(3):
            kb.create_task(conn, title=f"t{i}", assignee="jeff_dean")

    with patch.dict(sys.modules, {"psutil": _mock_psutil_avail_gb(4.0)}):
        with kb.connect_closing() as conn:
            res = kb.dispatch_once(
                conn,
                spawn_fn=_fake_spawn,
                dry_run=False,
                memory_backpressure_gb=1.0,
            )

    assert res.deferred_memory is None
    assert len(res.spawned) == 3


def test_gate_disabled_when_threshold_is_none(
    isolated_kanban_home_with_profile,
):
    """threshold=None (default) → gate never runs, even under low memory."""
    kb = isolated_kanban_home_with_profile
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(2):
            kb.create_task(conn, title=f"t{i}", assignee="jeff_dean")

    with patch.dict(sys.modules, {"psutil": _mock_psutil_avail_gb(0.1)}):
        with kb.connect_closing() as conn:
            res = kb.dispatch_once(
                conn,
                spawn_fn=_fake_spawn,
                dry_run=False,
                memory_backpressure_gb=None,
            )

    assert res.deferred_memory is None
    assert len(res.spawned) == 2


def test_gate_disabled_when_threshold_is_zero_or_negative(
    isolated_kanban_home_with_profile,
):
    """threshold<=0 → gate disabled (defensive: same as None). Prevents
    a "gate on host with 0GB free" nonsense config from spinning."""
    kb = isolated_kanban_home_with_profile
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="t1", assignee="jeff_dean")

    with patch.dict(sys.modules, {"psutil": _mock_psutil_avail_gb(0.1)}):
        for bad_threshold in (0.0, -1.0):
            with kb.connect_closing() as conn:
                res = kb.dispatch_once(
                    conn,
                    spawn_fn=_fake_spawn,
                    dry_run=True,  # dry-run so we don't consume the ready row
                    memory_backpressure_gb=bad_threshold,
                )
            assert res.deferred_memory is None, (
                f"threshold={bad_threshold} must disable gate"
            )


# ─── Reclaim/promote/sweeps still run when gate trips ─────────────────

def test_gate_still_runs_reclaim_and_promote_sweeps(
    isolated_kanban_home_with_profile,
):
    """Critical invariant: the gate fires AFTER reclaim/promote/timed_out
    sweeps. Prove it structurally by scanning ``_dispatch_once_locked``
    source and asserting the sweep call sites appear BEFORE the memory
    backpressure gate.

    A behavioral test would need to set up a stale claim + dead PID +
    host-prefix match + heartbeat gap — every detail brittle to unrelated
    dispatcher refactors. The source-order test locks the invariant that
    matters (sweep → gate → spawn) without depending on those specifics.
    Failing this test means someone reordered dispatch tick phases and
    memory-starved hosts will silently stop cleaning up stale tasks.
    """
    kb = isolated_kanban_home_with_profile
    import inspect
    src = inspect.getsource(kb._dispatch_once_locked)
    # Strip the docstring so tokens inside prose don't skew our source-order
    # scan (the docstring names ``spawn_fn`` in its Parameters section).
    doc = inspect.getdoc(kb._dispatch_once_locked) or ""
    if doc:
        # inspect.getsource preserves the raw triple-quoted docstring —
        # keep the code *outside* that block.
        try:
            first_line_end = src.index("\n") + 1
            after_docstring = src.index('"""', src.index('"""', first_line_end) + 3) + 3
            src = src[:first_line_end] + src[after_docstring:]
        except ValueError:
            pass  # no docstring found — keep src as-is

    # Sweep phase call sites (any refactoring of dispatch phases must
    # keep at least one of these markers ABOVE the memory gate).
    sweep_markers = [
        "enforce_max_runtime(",
        "recompute_ready(",
        "detect_stale_running(",
        "detect_crashed_workers(",
    ]
    gate_marker = "_check_memory_backpressure("

    gate_idx = src.find(gate_marker)
    assert gate_idx > 0, (
        f"expected {gate_marker!r} in _dispatch_once_locked source"
    )

    sweeps_before_gate = [
        m for m in sweep_markers
        if 0 <= src.find(m) < gate_idx
    ]
    assert len(sweeps_before_gate) >= 2, (
        f"expected at least 2 sweep call sites BEFORE the memory gate; "
        f"found {sweeps_before_gate!r}. Sweep phases MUST run first so "
        f"reclaim/promote still happen when the gate defers spawns."
    )

    # And separately: assert the spawn call site is after the gate. The
    # gate returns early from the tick, so ``_spawn(`` (the resolved
    # spawn function invocation) must appear in source AFTER the gate
    # marker. We look for ``_spawn`` specifically (not ``spawn_fn``) to
    # skip the closure alias and any docstring references.
    spawn_call_idx = src.find("_spawn(")
    if spawn_call_idx >= 0:
        assert spawn_call_idx > gate_idx, (
            "_spawn() call site must appear AFTER the memory gate — "
            "otherwise the gate cannot preempt spawns"
        )


# ─── Fail-open on psutil issues ────────────────────────────────────────

def test_gate_survives_psutil_missing(isolated_kanban_home_with_profile):
    """When psutil is unimportable, the gate falls through silently and
    the spawn loop proceeds normally. Guarantees the fix never fail-closes
    on missing deps — a dispatcher tick silently dropping is worse than
    an ungated tick."""
    kb = isolated_kanban_home_with_profile
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="t1", assignee="jeff_dean")

    class _BoomModule:
        def __getattr__(self, name):
            raise ImportError("psutil disabled for test")

    with patch.dict(sys.modules, {"psutil": _BoomModule()}):
        with kb.connect_closing() as conn:
            res = kb.dispatch_once(
                conn,
                spawn_fn=_fake_spawn,
                dry_run=False,
                memory_backpressure_gb=1.0,
            )

    assert res.deferred_memory is None
    assert len(res.spawned) == 1, "psutil failure must not block dispatch"


def test_gate_survives_virtual_memory_raising(
    isolated_kanban_home_with_profile,
):
    """When ``psutil.virtual_memory()`` itself raises (exotic containers,
    /proc not mounted), the gate falls through — same fail-open contract."""
    kb = isolated_kanban_home_with_profile
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="t1", assignee="jeff_dean")

    mock_psutil = MagicMock()
    mock_psutil.virtual_memory.side_effect = OSError("/proc not available")

    with patch.dict(sys.modules, {"psutil": mock_psutil}):
        with kb.connect_closing() as conn:
            res = kb.dispatch_once(
                conn,
                spawn_fn=_fake_spawn,
                dry_run=False,
                memory_backpressure_gb=1.0,
            )

    assert res.deferred_memory is None
    assert len(res.spawned) == 1


# ─── Interaction with existing caps ────────────────────────────────────

def test_max_spawn_is_live_concurrency_cap(
    isolated_kanban_home_with_profile,
):
    """Ticket verification #1 (‘max_spawn semantics’): pin cap to 2, spawn
    5 ready tasks, verify only 2 spawn per tick and the other 3 wait as
    still-ready. This is the invariant the FIX-A/FIX-B pair depends on."""
    kb = isolated_kanban_home_with_profile
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(5):
            kb.create_task(conn, title=f"t{i}", assignee="jeff_dean")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=False,
            max_spawn=2,
        )

    assert len(res.spawned) == 2, "only 2 of 5 should spawn under max_spawn=2"
    with kb.connect_closing() as conn:
        counts = conn.execute(
            "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
        ).fetchall()
    status_map = {r["status"]: r["n"] for r in counts}
    assert status_map.get("running", 0) == 2
    assert status_map.get("ready", 0) == 3


def test_max_spawn_counts_already_running_toward_cap(
    isolated_kanban_home_with_profile,
):
    """max_spawn is a LIVE cap: with 2 already-running tasks and cap=3,
    only 1 more spawns this tick — not 3. This is the FIX-B ordering
    guarantee the ticket asked us to verify."""
    kb = isolated_kanban_home_with_profile
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        already_running = [
            kb.create_task(conn, title=f"r{i}", assignee="jeff_dean")
            for i in range(2)
        ]
        with kb.write_txn(conn):
            for tid in already_running:
                conn.execute(
                    "UPDATE tasks SET status = 'running', claim_lock = 'test:1' "
                    "WHERE id = ?",
                    (tid,),
                )
        for i in range(4):
            kb.create_task(conn, title=f"n{i}", assignee="jeff_dean")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn,
            spawn_fn=_fake_spawn,
            dry_run=False,
            max_spawn=3,
        )

    assert len(res.spawned) == 1, (
        "only 1 new spawn: 2 already running + 1 = 3 == cap"
    )


def test_gate_preempts_max_spawn(isolated_kanban_home_with_profile):
    """Memory backpressure trips BEFORE max_spawn is consulted, so no spawns
    happen even if the cap would allow them. Locks the sweep→gate→spawn
    order."""
    kb = isolated_kanban_home_with_profile
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        for i in range(3):
            kb.create_task(conn, title=f"t{i}", assignee="jeff_dean")

    with patch.dict(sys.modules, {"psutil": _mock_psutil_avail_gb(0.5)}):
        with kb.connect_closing() as conn:
            res = kb.dispatch_once(
                conn,
                spawn_fn=_fake_spawn,
                dry_run=False,
                max_spawn=10,
                memory_backpressure_gb=1.0,
            )

    assert res.deferred_memory is not None
    assert res.spawned == []
