"""Lifecycle tests for the voice-agents JetStream bridge.

Production incident (hermes1, 2026-08): hermes processes logged

    Task was destroyed but it is pending!
    task: <Task pending coro=<Client._ping_interval() ...>>
    task: <Task pending coro=<Client._flusher() ...>>
    task: <Task pending coro=<Subscription._wait_for_msgs() ...>>

during interpreter teardown. Root cause: ``JetStreamBridge`` had a
start-only lifecycle —

* ``get_or_start_bridge()`` would *block-start* the NATS connection on
  any loop it could find, including a borrowed, never-run-again loop in
  non-gateway processes (CLI, kanban workers, cron). The client's
  ping/flusher tasks were then stranded on a loop nothing owned.
* ``close()`` existed but had zero call sites — dead code.
* Nothing drained the connection at process exit.

Fix invariants under test:

1. No running loop → the bridge DEFERS (never connects on a borrowed
   loop, never blocks the caller).
2. A running loop → start is scheduled onto it.
3. start() is guarded against concurrent double-connects.
4. close() is idempotent, unsubscribes, drains, and clears all state.
5. A successful start registers an atexit finalizer that drains the
   connection when the process exits with its loop still open.
"""

import asyncio
import importlib.util
import subprocess
import sys
import textwrap
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BRIDGE_PATH = REPO_ROOT / "plugins" / "voice-agents" / "jetstream_bridge.py"


def _load_bridge():
    """Load jetstream_bridge.py standalone (the plugin dir name has a
    hyphen, so it is not importable as a normal package)."""
    spec = importlib.util.spec_from_file_location(
        "voice_agents_jetstream_bridge_under_test", BRIDGE_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def bridge_mod():
    return _load_bridge()


@pytest.fixture()
def fresh_bridge(bridge_mod):
    """Swap in a fresh singleton so tests don't leak state into each other."""
    original = bridge_mod._jet
    bridge_mod._jet = bridge_mod.JetStreamBridge()
    try:
        yield bridge_mod._jet
    finally:
        bridge_mod._jet = original


def _fake_nats_module(monkeypatch):
    """Install a fake `nats` module whose connect() returns a mock client."""
    nc = types.SimpleNamespace()
    nc.drain = AsyncMock()
    js = types.SimpleNamespace()
    js.stream_info = AsyncMock(return_value=object())
    js.subscribe = AsyncMock()
    nc.jetstream = lambda: js
    fake = types.SimpleNamespace(connect=AsyncMock(return_value=nc))
    monkeypatch.setitem(sys.modules, "nats", fake)
    return nc, js


# ---------------------------------------------------------------------------
# 1. Deferral without a running loop (the CLI/worker/cron leak path)
# ---------------------------------------------------------------------------

def test_get_or_start_bridge_defers_without_running_loop(bridge_mod, fresh_bridge):
    """No running loop (sync context): must NOT block-start the bridge.

    Current behaviour run_until_complete()s start() on a borrowed loop,
    stranding NATS ping/flusher tasks on a loop nobody owns. After the
    fix, start is never invoked from a sync context.
    """
    fresh_bridge.start = AsyncMock()
    result = bridge_mod.get_or_start_bridge()
    assert result is fresh_bridge
    fresh_bridge.start.assert_not_called()
    assert not fresh_bridge.connected


def test_get_or_start_bridge_never_blocks_caller(bridge_mod, fresh_bridge):
    """Even if a non-running loop exists, no run_until_complete on it."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        fresh_bridge.start = AsyncMock()
        bridge_mod.get_or_start_bridge()
        fresh_bridge.start.assert_not_called()
        assert not loop.is_running()
    finally:
        asyncio.set_event_loop(None)
        loop.close()


# ---------------------------------------------------------------------------
# 2. Scheduling onto a running loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_or_start_bridge_schedules_on_running_loop(bridge_mod, fresh_bridge):
    fresh_bridge.start = AsyncMock()
    bridge_mod.get_or_start_bridge()
    await asyncio.sleep(0.05)  # let the scheduled task run
    fresh_bridge.start.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. start() connect + double-start guard
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_connects_and_captures_running_loop(bridge_mod, fresh_bridge, monkeypatch):
    nc, _js = _fake_nats_module(monkeypatch)
    await fresh_bridge.start()
    assert fresh_bridge.connected
    assert fresh_bridge._loop is asyncio.get_running_loop()


@pytest.mark.asyncio
async def test_start_concurrent_calls_connect_once(bridge_mod, fresh_bridge, monkeypatch):
    nc, _js = _fake_nats_module(monkeypatch)
    connect = sys.modules["nats"].connect
    await asyncio.gather(fresh_bridge.start(), fresh_bridge.start(), fresh_bridge.start())
    assert connect.await_count == 1
    assert fresh_bridge.connected


# ---------------------------------------------------------------------------
# 4. close() semantics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_close_unsubscribes_drains_and_clears_state(bridge_mod, fresh_bridge):
    sub = types.SimpleNamespace(unsubscribe=AsyncMock())
    nc = types.SimpleNamespace(drain=AsyncMock())
    fresh_bridge._subs = [sub]
    fresh_bridge._nc = nc
    fresh_bridge._js = object()
    fresh_bridge._loop = asyncio.get_running_loop()
    fresh_bridge._connected = True

    await fresh_bridge.close()

    sub.unsubscribe.assert_awaited_once()
    nc.drain.assert_awaited_once()
    assert not fresh_bridge.connected
    assert fresh_bridge._nc is None
    assert fresh_bridge._js is None
    assert fresh_bridge._subs == []
    assert fresh_bridge._loop is None


@pytest.mark.asyncio
async def test_close_is_idempotent(bridge_mod, fresh_bridge):
    nc = types.SimpleNamespace(drain=AsyncMock())
    fresh_bridge._nc = nc
    fresh_bridge._connected = True

    await fresh_bridge.close()
    await fresh_bridge.close()  # second call must be a no-op

    nc.drain.assert_awaited_once()


# ---------------------------------------------------------------------------
# 5. atexit finalizer drains a still-open connection
# ---------------------------------------------------------------------------

def test_atexit_finalizer_drains_connected_bridge(bridge_mod, fresh_bridge, monkeypatch):
    nc, _js = _fake_nats_module(monkeypatch)
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(fresh_bridge.start())
        assert fresh_bridge.connected

        # Fire the finalizer the way atexit would: loop open, not running.
        bridge_mod._drain_bridge_at_exit()

        nc.drain.assert_awaited_once()
        assert not fresh_bridge.connected
    finally:
        loop.close()


def test_atexit_finalizer_is_safe_when_never_connected(bridge_mod, fresh_bridge):
    # Must be a silent no-op — this runs for EVERY hermes process exit.
    bridge_mod._drain_bridge_at_exit()


def test_atexit_finalizer_tolerates_closed_loop(bridge_mod, fresh_bridge):
    loop = asyncio.new_event_loop()
    fresh_bridge._connected = True
    fresh_bridge._nc = types.SimpleNamespace(drain=AsyncMock())
    fresh_bridge._loop = loop
    loop.close()
    bridge_mod._drain_bridge_at_exit()  # must not raise


# ---------------------------------------------------------------------------
# 6. End-to-end against a live NATS server (skipped when none is running)
# ---------------------------------------------------------------------------

def _nats_up() -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", 4222), timeout=1):
            return True
    except OSError:
        return False


@pytest.mark.skipif(not _nats_up(), reason="no NATS server on 127.0.0.1:4222")
def test_e2e_process_exit_drains_real_nats_connection():
    """Real nats client, real server, real process exit.

    The bridge connects + subscribes on a loop that is left open; the
    process exits through normal finalization. The atexit drain must run
    (bridge disconnected, state cleared) and stderr must stay free of
    "Task was destroyed" / "Logging error" noise.
    """
    script = textwrap.dedent(
        f"""
        import asyncio, atexit, importlib.util, sys

        spec = importlib.util.spec_from_file_location(
            "jb", {str(BRIDGE_PATH)!r})
        jb = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(jb)

        # Registered before the drain hook -> runs after it (atexit is LIFO).
        atexit.register(
            lambda: sys.stderr.write(
                f"ATEXIT-OBSERVED connected={{jb._jet.connected}}\\n"))

        async def amain():
            await jb._jet.start()
            assert jb._jet.connected, "bridge failed to connect"
            await jb._jet.subscribe("voice_bridge.e2e_test.>", lambda p: None)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(amain())
        # Loop deliberately left open, close() never called — the production
        # shape that leaked tasks at teardown.
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    assert "ATEXIT-OBSERVED connected=False" in proc.stderr
    assert "Task was destroyed" not in proc.stderr
    assert "Logging error" not in proc.stderr
