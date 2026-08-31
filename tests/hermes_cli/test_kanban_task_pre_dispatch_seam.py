"""Behavior tests for the ``kanban_task_pre_dispatch`` veto seam.

ADR-006b P3 row 2 (t_172dc2b3). This is the seam that lets a policy
plugin (``vfe-hrv-node-gate`` at land time) reject a remote-node spawn
between claim and worker fork WITHOUT the kernel importing plugin code
or reaching back into plugin internals.

Contract we verify here (matches the ``VALID_HOOKS`` docstring):

  * Registered in ``VALID_HOOKS`` so plugin loaders accept callbacks.
  * Fires from ``_dispatch_once_locked`` AFTER the claim commits and
    BEFORE the worker spawn, only when the cluster router selected a
    remote node (``target_node is not None``). Local dispatch skips
    the fire entirely.
  * Carries stable, additive-only kwargs — ``task_id, node_hostname,
    model_name, priority, min_resources, board, assignee,
    profile_name`` — sufficient for a plugin to evaluate a rejection
    without re-opening a SQLite handle mid-dispatch.
  * A callback returning ``{"veto": True, "reason": "..."}`` causes
    the kernel to:
       - atomically release the claim back to ``status='ready'`` with
         ``claim_lock=NULL, claim_expires=NULL, worker_pid=NULL``,
       - emit a ``node_gate_rejected`` task_event with
         ``{node, reason, task_id, [source]}``,
       - append ``(task_id, node, reason)`` to
         ``DispatchResult.skipped_node_rejected``,
       - NOT increment ``consecutive_failures``.
    Multi-callback veto reasons and sources are '; '/','-joined.
  * Abstentions (``None``, non-dict, dict without ``veto: True``)
    let dispatch proceed to spawn.
  * A raising callback is swallowed (fail-open) — dispatch proceeds.

Named consumer at land time: ``vfe-hrv-node-gate`` plugin. Its own
gate-5 red-probe suite covers the 5 HRV rejection conditions
(memory_pressure, kanban_dispatcher_health,
bedrock_rate_limit_saturation[model], hrv_urgent_state,
min_resources_overflow) and the ``dispatch.node_rejected`` telemetry
side effect; those tests live in the plugin, not here — they are HRV
policy, not kernel-seam contract.
"""
from __future__ import annotations

import importlib
import json

import pytest


@pytest.fixture()
def isolated_kanban_home(monkeypatch, tmp_path):
    """Fresh HERMES_HOME + kanban DB for this test.

    Uses ``monkeypatch`` on env only (never purges ``sys.modules``) so
    the shared ``PluginManager`` singleton the kernel sees is the same
    one this test registers callbacks on. Purging ``sys.modules`` here
    would give the kernel a *new* plugin_manager and any callback
    registered before dispatch would be silently dropped — the reason
    the ``test_kanban_write_op_seam`` fixture had to work around a
    similar sibling test.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_ALLOW_UNKNOWN_ASSIGNEE", "1")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    from hermes_cli import kanban_db
    kanban_db.init_db()
    yield kanban_db, home


@pytest.fixture()
def seam_recorder():
    """Register a capturing/scripted callback on ``kanban_task_pre_dispatch``.

    Yields a dict with:
      * ``calls`` — list of kwarg dicts the callback observed
      * ``set_response(fn)`` — install a callable that receives the
        kwargs and returns the callback's return value (default: None)

    Restores the plugin manager's hook registry on teardown so
    callbacks never leak into a sibling test.

    Re-resolves the plugin manager singleton via a fresh import (same
    pattern as ``test_kanban_write_op_seam.py``) — the fixture above
    purges ``sys.modules`` for the whole ``hermes_cli`` package, so
    the plugins module and its ``_plugin_manager`` singleton are
    freshly imported each test.
    """
    plugins_mod = importlib.import_module("hermes_cli.plugins")
    mgr = plugins_mod.get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    calls: list[dict] = []
    state = {"response": lambda **_kw: None}

    def _callback(**kw):
        calls.append(kw)
        return state["response"](**kw)

    mgr._hooks.setdefault("kanban_task_pre_dispatch", []).append(_callback)
    try:
        yield {
            "calls": calls,
            "set_response": lambda fn: state.__setitem__("response", fn),
        }
    finally:
        mgr._hooks = saved


def _spawn_ok(claimed, workspace, **_kwargs):
    """Stub spawn — records the call, returns a fake PID."""
    return 12345


# --------------------------------------------------------------------------- #
# 1. VALID_HOOKS registration
# --------------------------------------------------------------------------- #


def test_pre_dispatch_hook_is_registered_in_valid_hooks(isolated_kanban_home):
    """The freeze-list must carry the seam; without it, plugin loaders
    reject callbacks and the veto path never fires."""
    from hermes_cli.plugins import VALID_HOOKS
    assert "kanban_task_pre_dispatch" in VALID_HOOKS


# --------------------------------------------------------------------------- #
# 2. Seam kwargs contract (Demis amendment A1 binding)
# --------------------------------------------------------------------------- #


def test_seam_fires_with_stable_kwargs(isolated_kanban_home, seam_recorder):
    """Every documented kwarg lands on the callback with the right type,
    so the plugin never needs a SQLite handle mid-dispatch."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(
            conn, title="t1", assignee="default",
            priority=3,
            model_override="claude-haiku",
        )
        # min_resources isn't a create_task param — set it directly.
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET min_resources = ? WHERE id = ?",
                (json.dumps({"mem_gb": 4.0, "cpu_cores": 2}), task_id),
            )

    def router(_task_id, _assignee):
        return "hermes2"

    with kb.connect_closing() as conn:
        kb.dispatch_once(
            conn, spawn_fn=_spawn_ok, dry_run=False, node_router=router,
        )

    assert len(seam_recorder["calls"]) == 1, (
        f"seam expected to fire exactly once; got {len(seam_recorder['calls'])}"
    )
    kw = seam_recorder["calls"][0]
    # Framing kwargs
    assert kw["task_id"] == task_id
    assert kw["node_hostname"] == "hermes2"
    assert kw["model_name"] == "claude-haiku"
    assert kw["priority"] == 3
    assert kw["min_resources"] == {"mem_gb": 4.0, "cpu_cores": 2}
    assert kw["assignee"] == "default"
    assert "board" in kw  # slug present, exact value from get_current_board
    assert "profile_name" in kw
    assert isinstance(kw["profile_name"], str)


def test_seam_skipped_when_router_returns_none(isolated_kanban_home, seam_recorder):
    """Local dispatch (router → None) MUST NOT fire the seam.

    Local dispatch has no remote probe to consult; the pre-extraction
    inline gate short-circuited on the same condition, so preserving
    it here keeps the extraction behavior-neutral.
    """
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="t-local", assignee="default")

    def router(_task_id, _assignee):
        return None

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_ok, dry_run=False, node_router=router,
        )

    assert seam_recorder["calls"] == [], (
        "seam must not fire for local dispatch (target_node is None)"
    )
    assert len(res.spawned) == 1
    assert res.skipped_node_rejected == []


def test_seam_skipped_when_no_router(isolated_kanban_home, seam_recorder):
    """Dispatch without any router: same as local — the seam does not fire.

    Guards against a regression where callers that never wired up a
    router accidentally start firing the seam on every local spawn.
    """
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="t-norouter", assignee="default")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(conn, spawn_fn=_spawn_ok, dry_run=False)

    assert seam_recorder["calls"] == []
    assert len(res.spawned) == 1
    # Attribute must still exist (regression guard on the local path).
    assert hasattr(res, "skipped_node_rejected")
    assert res.skipped_node_rejected == []


# --------------------------------------------------------------------------- #
# 3. Veto semantics — the primitive the plugin drives
# --------------------------------------------------------------------------- #


def test_veto_releases_claim_and_emits_event(isolated_kanban_home, seam_recorder):
    """A single veto: claim released, event emitted, result surfaced,
    no failure counted, no spawn.

    This is the P2/P3 primitive — the kernel MUST do exactly and only
    this in response to a veto, and nothing more.
    """
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t-veto", assignee="default")

    def router(_task_id, _assignee):
        return "hermes2"

    seam_recorder["set_response"](lambda **kw: {
        "veto": True,
        "reason": "memory_pressure",
        "source": "vfe-hrv-node-gate",
    })

    spawn_calls: list = []

    def _spawn_recording(claimed, workspace, **_kwargs):
        spawn_calls.append(claimed.id)
        return 12345

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_recording, dry_run=False, node_router=router,
        )

    assert spawn_calls == [], "veto must prevent spawn"
    assert res.spawned == []
    assert len(res.skipped_node_rejected) == 1
    entry = res.skipped_node_rejected[0]
    assert entry == (task_id, "hermes2", "memory_pressure")

    # Task released back to ready with no claim; no failure counted.
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
    assert (row["consecutive_failures"] or 0) == 0

    # Audit event with the documented payload shape.
    with kb.connect_closing() as conn:
        evs = list(conn.execute(
            "SELECT kind, payload FROM task_events "
            "WHERE task_id = ? AND kind = 'node_gate_rejected'",
            (task_id,),
        ))
    assert len(evs) == 1, "expected exactly one node_gate_rejected event"
    payload = json.loads(evs[0][1])
    assert payload["task_id"] == task_id
    assert payload["node"] == "hermes2"
    assert payload["reason"] == "memory_pressure"
    assert payload["source"] == "vfe-hrv-node-gate"


def test_multi_callback_veto_reasons_joined(isolated_kanban_home):
    """Two callbacks both vetoing: reasons '; '-joined, sources ','-joined."""
    kb, _home = isolated_kanban_home
    plugins_mod = importlib.import_module("hermes_cli.plugins")
    mgr = plugins_mod.get_plugin_manager()
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    try:
        mgr._hooks.setdefault("kanban_task_pre_dispatch", []).extend([
            lambda **kw: {"veto": True, "reason": "reason-a", "source": "cb-a"},
            lambda **kw: {"veto": True, "reason": "reason-b", "source": "cb-b"},
        ])
        with kb.connect_closing() as conn:
            kb.create_board(slug="default", name="Test")
            task_id = kb.create_task(conn, title="t-multi", assignee="default")

        def router(_task_id, _assignee):
            return "hermes2"

        with kb.connect_closing() as conn:
            res = kb.dispatch_once(
                conn, spawn_fn=_spawn_ok, dry_run=False, node_router=router,
            )
        assert len(res.skipped_node_rejected) == 1
        assert res.skipped_node_rejected[0][2] == "reason-a; reason-b"

        with kb.connect_closing() as conn:
            evs = list(conn.execute(
                "SELECT payload FROM task_events WHERE task_id = ? "
                "AND kind = 'node_gate_rejected'", (task_id,),
            ))
        payload = json.loads(evs[0][0])
        assert payload["reason"] == "reason-a; reason-b"
        assert payload["source"] == "cb-a,cb-b"
    finally:
        mgr._hooks = saved


def test_abstain_none_lets_dispatch_proceed(isolated_kanban_home, seam_recorder):
    """Callback returning None is an abstention: spawn proceeds."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t-abstain", assignee="default")

    def router(_task_id, _assignee):
        return "hermes2"

    seam_recorder["set_response"](lambda **kw: None)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_ok, dry_run=False, node_router=router,
        )
    assert len(res.spawned) == 1 and res.spawned[0][0] == task_id
    assert res.skipped_node_rejected == []


def test_abstain_non_dict_lets_dispatch_proceed(isolated_kanban_home, seam_recorder):
    """Non-dict return values are abstentions — no veto."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t-nondict", assignee="default")

    def router(_task_id, _assignee):
        return "hermes2"

    seam_recorder["set_response"](lambda **kw: "not a dict")

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_ok, dry_run=False, node_router=router,
        )
    assert len(res.spawned) == 1 and res.spawned[0][0] == task_id
    assert res.skipped_node_rejected == []


def test_dict_without_veto_key_is_abstention(isolated_kanban_home, seam_recorder):
    """A dict that lacks ``veto: True`` is an abstention — spawn proceeds."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        kb.create_task(conn, title="t-dict-no-veto", assignee="default")

    def router(_task_id, _assignee):
        return "hermes2"

    seam_recorder["set_response"](lambda **kw: {"reason": "not vetoing"})

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_ok, dry_run=False, node_router=router,
        )
    assert len(res.spawned) == 1
    assert res.skipped_node_rejected == []


def test_veto_without_reason_gets_default_reason(isolated_kanban_home, seam_recorder):
    """A veto with no reason still rejects; kernel supplies a placeholder
    string so audit consumers never see an empty ``reason`` field."""
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t-noreason", assignee="default")

    def router(_task_id, _assignee):
        return "hermes2"

    seam_recorder["set_response"](lambda **kw: {"veto": True})

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_ok, dry_run=False, node_router=router,
        )
    assert len(res.skipped_node_rejected) == 1
    reason = res.skipped_node_rejected[0][2]
    assert reason  # non-empty
    assert "no reason" in reason.lower()

    with kb.connect_closing() as conn:
        evs = list(conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? "
            "AND kind = 'node_gate_rejected'", (task_id,),
        ))
    assert len(evs) == 1


# --------------------------------------------------------------------------- #
# 4. Fail-open contract — a busted plugin must never wedge dispatch
# --------------------------------------------------------------------------- #


def test_raising_callback_is_swallowed(isolated_kanban_home, seam_recorder):
    """A callback that raises: kernel MUST spawn anyway (fail-open).

    This preserves the pre-extraction ``check_node_gate`` contract:
    a red probe pipeline is worse than a missing one.
    """
    kb, _home = isolated_kanban_home
    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t-raise", assignee="default")

    def router(_task_id, _assignee):
        return "hermes2"

    def _boom(**kw):
        raise RuntimeError("simulated plugin explosion")
    seam_recorder["set_response"](_boom)

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_ok, dry_run=False, node_router=router,
        )
    assert len(res.spawned) == 1 and res.spawned[0][0] == task_id
    assert res.skipped_node_rejected == []


def test_no_callbacks_registered_dispatches_normally(isolated_kanban_home):
    """No plugin callbacks on the hook: dispatch proceeds normally.

    This is the ``vfe-hrv-node-gate`` uninstalled state — remote
    dispatch must still work without the policy plugin loaded (the
    seam is purely optional).
    """
    kb, _home = isolated_kanban_home
    plugins_mod = importlib.import_module("hermes_cli.plugins")
    mgr = plugins_mod.get_plugin_manager()
    # Ensure no callbacks are registered for the hook.
    mgr._hooks.pop("kanban_task_pre_dispatch", None)

    with kb.connect_closing() as conn:
        kb.create_board(slug="default", name="Test")
        task_id = kb.create_task(conn, title="t-nocb", assignee="default")

    def router(_task_id, _assignee):
        return "hermes2"

    with kb.connect_closing() as conn:
        res = kb.dispatch_once(
            conn, spawn_fn=_spawn_ok, dry_run=False, node_router=router,
        )
    assert len(res.spawned) == 1 and res.spawned[0][0] == task_id
    assert res.skipped_node_rejected == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
