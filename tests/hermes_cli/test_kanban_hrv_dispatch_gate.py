"""Regression tests for t_88eadaa8 — HRV node gate wired into dispatch_once.

Contract:

When ``dispatch_once`` is called with both a ``node_router`` (that returns
a remote node) and the HRV gate detects a RED probe on that node, the task
MUST:

1. NOT be spawned this tick,
2. have its claim released back to ``status='ready'`` with
   ``claim_lock=NULL`` so a healthy node can pick it up next tick,
3. surface in ``DispatchResult.skipped_node_rejected`` as
   ``(task_id, node_hostname, reason)``,
4. emit a ``node_gate_rejected`` task_event with ``{node, reason, task_id}``,
5. NOT increment ``consecutive_failures`` (a red probe is not a task failure).

When the gate fails-open (missing/stale probe data, gate module unavailable,
exception raised), dispatch MUST proceed normally — the gate is a safety
sieve, not a hard dependency.

When ``node_router`` returns ``None`` (local dispatch), the gate MUST be
skipped entirely — local dispatch has no remote probes to consult.
"""
from __future__ import annotations

import datetime
import json
import sys
import tempfile

import pytest


def _now_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@pytest.fixture()
def isolated_kanban_home(monkeypatch):
    """Fresh HERMES_HOME + kanban DB, freshly-imported kanban_db module."""
    test_home = tempfile.mkdtemp(prefix="hrv_dispatch_gate_test_")
    monkeypatch.setenv("HERMES_HOME", test_home)
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("hermes_cli")
            or mod.startswith("hermes_state")
            or mod == "hermes_constants"
        ):
            del sys.modules[mod]
    from hermes_cli import kanban_db
    yield kanban_db, test_home


def _fake_spawn(*_args, **_kwargs):
    """Stub worker spawn — records the call and returns a fake PID."""
    return 12345


def _build_gate_with_memory_pressure(hostname: str):
    """Return an HRVNodeGate that will reject ``hostname`` for memory_pressure."""
    from hermes_cli.hrv_node_gate import HRVNodeGate, NodeProbeSnapshot
    gate = HRVNodeGate()
    probe = NodeProbeSnapshot(hostname=hostname, swap_pct=95.0, ts=_now_iso())
    gate.set_node_probe_snapshot(hostname, probe)
    return gate


def _build_healthy_gate(hostname: str):
    """Return an HRVNodeGate that will pass ``hostname`` on all checks."""
    from hermes_cli.hrv_node_gate import (
        HRVDigestSnapshot,
        HRVNodeGate,
        NodeProbeSnapshot,
    )
    gate = HRVNodeGate()
    probe = NodeProbeSnapshot(
        hostname=hostname,
        swap_pct=30.0,
        mem_gb_available=8.0,
        bedrock_tpm_remaining=50000,
        ts=_now_iso(),
    )
    gate.set_node_probe_snapshot(hostname, probe)
    gate.set_hrv_digest(HRVDigestSnapshot(interval_class="calm", ts=_now_iso()))
    return gate


# ---------------------------------------------------------------------------
# Core rejection contract
# ---------------------------------------------------------------------------


def test_dispatch_rejects_red_memory_node(isolated_kanban_home, monkeypatch):
    """A ready task routed to a node with RED memory_pressure must be
    released back to ready without being spawned, and surface in
    ``result.skipped_node_rejected``."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t1", assignee="default")

    gate = _build_gate_with_memory_pressure("hermes2")

    # Wire the gate through the default_gate helper the integration uses.
    monkeypatch.setattr(
        "hermes_cli.hrv_node_gate.get_default_gate", lambda: gate
    )

    def router(_task_id, _assignee):
        return "hermes2"

    spawn_calls = []

    def spawn_fn(claimed, workspace, **_kwargs):
        spawn_calls.append((claimed.id, str(workspace)))
        return 12345

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=spawn_fn, dry_run=False, node_router=router,
        )

    # No spawn happened.
    assert spawn_calls == []
    assert res.spawned == []
    # Rejection surfaced.
    assert hasattr(res, "skipped_node_rejected"), (
        "DispatchResult must expose skipped_node_rejected list"
    )
    assert len(res.skipped_node_rejected) == 1
    entry = res.skipped_node_rejected[0]
    assert entry[0] == task_id
    assert entry[1] == "hermes2"
    assert entry[2] == "memory_pressure"

    # Task released back to 'ready' with no claim.
    with kb.connect_closing() as conn:
        row = conn.execute(
            "SELECT status, claim_lock, claim_expires, worker_pid, "
            "consecutive_failures FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
    assert row["status"] == "ready"
    assert row["claim_lock"] is None
    assert row["claim_expires"] is None
    assert row["worker_pid"] is None
    # Gate rejection is not a task failure.
    assert (row["consecutive_failures"] or 0) == 0

    # Audit event emitted.
    with kb.connect_closing() as conn:
        evs = list(conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'node_gate_rejected'",
            (task_id,),
        ))
    assert len(evs) == 1, "expected exactly one node_gate_rejected event"
    payload = json.loads(evs[0][1])
    assert payload["node"] == "hermes2"
    assert payload["reason"] == "memory_pressure"


def test_dispatch_spawns_when_gate_passes(isolated_kanban_home, monkeypatch):
    """Healthy remote node: dispatch proceeds normally, spawn happens,
    skipped_node_rejected is empty."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t2", assignee="default")

    gate = _build_healthy_gate("hermes2")
    monkeypatch.setattr(
        "hermes_cli.hrv_node_gate.get_default_gate", lambda: gate
    )

    def router(_task_id, _assignee):
        return "hermes2"

    spawn_calls = []

    def spawn_fn(claimed, workspace, **_kwargs):
        spawn_calls.append((claimed.id, str(workspace)))
        return 12345

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=spawn_fn, dry_run=False, node_router=router,
        )

    assert len(spawn_calls) == 1
    assert spawn_calls[0][0] == task_id
    assert len(res.spawned) == 1
    assert res.spawned[0][0] == task_id
    assert res.skipped_node_rejected == []


def test_dispatch_skips_gate_when_router_returns_none(isolated_kanban_home, monkeypatch):
    """Local dispatch (node_router returns None) must NOT invoke the gate —
    even if the gate would reject something, local has no remote probe."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t3", assignee="default")

    # If the gate is consulted at all this test fails.
    poisoned_gate = _build_gate_with_memory_pressure("hermes2")

    def _no_gate():
        raise AssertionError(
            "get_default_gate must NOT be called when target_node is None"
        )

    monkeypatch.setattr(
        "hermes_cli.hrv_node_gate.get_default_gate", _no_gate
    )

    def router(_task_id, _assignee):
        return None  # local dispatch

    spawn_calls = []

    def spawn_fn(claimed, workspace, **_kwargs):
        spawn_calls.append((claimed.id, str(workspace)))
        return 12345

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=spawn_fn, dry_run=False, node_router=router,
        )

    assert len(spawn_calls) == 1
    assert spawn_calls[0][0] == task_id
    assert res.skipped_node_rejected == []
    # Unused reference so linter doesn't flag the fixture.
    del poisoned_gate


def test_dispatch_fails_open_when_gate_module_missing(isolated_kanban_home, monkeypatch):
    """If the gate module raises on load, dispatch must proceed (fail-open) —
    a busted probe pipeline must not halt all remote dispatch."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t4", assignee="default")

    def _explode():
        raise RuntimeError("simulated gate module failure")

    monkeypatch.setattr(
        "hermes_cli.hrv_node_gate.get_default_gate", _explode
    )

    def router(_task_id, _assignee):
        return "hermes2"

    spawn_calls = []

    def spawn_fn(claimed, workspace, **_kwargs):
        spawn_calls.append((claimed.id, str(workspace)))
        return 12345

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=spawn_fn, dry_run=False, node_router=router,
        )

    # Fail-open: task spawned normally.
    assert len(spawn_calls) == 1
    assert spawn_calls[0][0] == task_id
    assert res.skipped_node_rejected == []


def test_dispatch_gate_no_regression_when_router_none_and_no_gate(isolated_kanban_home):
    """Baseline: without node_router at all, dispatch works unchanged and
    the new gate code path is a strict no-op. Guards against regressions
    in the local-only default dispatch path."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t5", assignee="default")

    spawn_calls = []

    def spawn_fn(claimed, workspace, **_kwargs):
        spawn_calls.append((claimed.id, str(workspace)))
        return 12345

    with kb.connect_closing() as conn:
        # node_router omitted entirely — no cluster in play.
        res = kb.dispatch_once(conn, spawn_fn=spawn_fn, dry_run=False)

    assert len(spawn_calls) == 1
    assert spawn_calls[0][0] == task_id
    # skipped_node_rejected must exist as an attribute even in the local path.
    assert hasattr(res, "skipped_node_rejected")
    assert res.skipped_node_rejected == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
