"""Unit tests for the register_remote_spawner kernel hook (t_5d3725c3).

Verifies four contract behaviours:
  (a) None registered → remote branch skipped, local spawn path used.
  (b) Registered callable is invoked and its pid is returned.
  (c) None return from callable → local fallback.
  (d) Exception in callable → local fallback + log warning.
"""
import logging
import types

import pytest

from hermes_cli import kanban_db
from hermes_cli.kanban_db import register_remote_spawner


# ── shared helpers ───────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_hook():
    """Guarantee a clean hook slot before and after each test."""
    register_remote_spawner(None)
    yield
    register_remote_spawner(None)


def _fake_task(task_id="t_test", assignee="jeff_dean"):
    return types.SimpleNamespace(
        id=task_id,
        assignee=assignee,
        skills=None,
        provider_override=None,
        model_override=None,
        tenant=None,
        branch_name=None,
        claim_lock=None,
        current_run_id=None,
        goal_max_turns=None,
        goal_mode=None,
        max_runtime_seconds=None,
        # body/title/status/workspace_kind/workspace_path/result are not
        # accessed in the _default_spawn path exercised by these tests.
    )


def _fake_popen(pid=12345):
    """Return a minimal Popen-like object accepted by _default_spawn."""
    return types.SimpleNamespace(pid=pid)


# ── (a) None registered → local fallback ────────────────────────────────────


def test_none_registered_falls_back_to_local(monkeypatch):
    """No hook → remote branch is skipped; local spawn path is exercised."""
    spawned = {}

    def _popen_stub(argv, **kw):
        spawned["argv"] = argv
        return _fake_popen(pid=11111)

    monkeypatch.setattr("subprocess.Popen", _popen_stub)
    register_remote_spawner(None)

    from hermes_cli.kanban_db import _default_spawn
    pid = _default_spawn(_fake_task(), "/tmp/ws_none", target_node="hermes1")

    assert pid == 11111
    # Local placement leaves _last_actual_node as None.
    assert getattr(_default_spawn, "_last_actual_node", "unset") is None


# ── (b) Registered callable is invoked ──────────────────────────────────────


def test_registered_hook_is_invoked(monkeypatch):
    """Hook returns a pid → kernel returns that pid, no local spawn."""
    calls = []

    def _hook(task, workspace, *, board, target_node, log_path, env):
        calls.append((task.id, target_node))
        from hermes_cli.kanban_db import _default_spawn
        _default_spawn._last_actual_node = target_node  # type: ignore[attr-defined]
        return 55555

    register_remote_spawner(_hook)

    # local Popen must NOT be called — use a sentinel that fails loudly.
    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("local Popen called")),
    )

    from hermes_cli.kanban_db import _default_spawn
    pid = _default_spawn(_fake_task(), "/tmp/ws_hook", target_node="hermes1")

    assert pid == 55555
    assert calls == [("t_test", "hermes1")]
    assert _default_spawn._last_actual_node == "hermes1"


# ── (c) Hook returns None → local fallback ──────────────────────────────────


def test_hook_returns_none_falls_back_to_local(monkeypatch):
    """Hook returns None → kernel proceeds to the local spawn path."""
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: _fake_popen(pid=77777))

    def _hook(task, workspace, *, board, target_node, log_path, env):
        return None

    register_remote_spawner(_hook)

    from hermes_cli.kanban_db import _default_spawn
    pid = _default_spawn(_fake_task(), "/tmp/ws_none_ret", target_node="hermes1")

    assert pid == 77777
    assert _default_spawn._last_actual_node is None


# ── (d) Hook raises → local fallback + log warning ──────────────────────────


def test_hook_raises_falls_back_to_local(monkeypatch, caplog):
    """Hook raises → kernel logs a warning and proceeds to local spawn."""
    monkeypatch.setattr("subprocess.Popen", lambda *a, **kw: _fake_popen(pid=88888))

    def _hook(task, workspace, *, board, target_node, log_path, env):
        raise RuntimeError("simulated hook failure")

    register_remote_spawner(_hook)

    from hermes_cli.kanban_db import _default_spawn
    with caplog.at_level(logging.WARNING):
        pid = _default_spawn(_fake_task(), "/tmp/ws_raise", target_node="hermes1")

    assert pid == 88888
    assert _default_spawn._last_actual_node is None
    assert any(
        "remote-spawner hook raised" in r.message for r in caplog.records
    ), f"Expected warning not found. Records: {[r.message for r in caplog.records]}"
