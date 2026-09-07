"""Focused tests for ``GatewayKanbanWatchersMixin._kanban_block_recheck``.

The mixin loop is the gateway wiring for FIX-7B / t_d9aec252 — it wraps
:func:`hermes_cli.kanban_block_recheck.sweep_all_boards` and applies the
usual guard-gate → schedule → run → sleep-in-slices pattern shared with
``_kanban_stall_watchdog``. These tests exercise the guard paths, the
config plumbing, and the single-tick happy path with a monkeypatched
sweep so the test doesn't touch a real kanban DB.
"""

from __future__ import annotations

import asyncio
import types
from unittest.mock import patch

import pytest

from gateway.kanban_watchers import GatewayKanbanWatchersMixin


class _FakeGateway(GatewayKanbanWatchersMixin):
    """Minimal gateway shim exposing only the attributes the loop touches."""

    def __init__(self, running: bool = True) -> None:
        self._running = running


def _run(coro):
    """Run *coro* in a fresh event loop and return its result."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Guard-gate tests — every early-return must exit cleanly.
# ---------------------------------------------------------------------------


def test_disabled_via_env_var(monkeypatch):
    """Setting HERMES_KANBAN_DISPATCH_IN_GATEWAY=false disables the loop."""
    monkeypatch.setenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", "false")
    gw = _FakeGateway()
    # If it didn't short-circuit it would call asyncio.sleep(45) and hang.
    _run(gw._kanban_block_recheck())


def test_disabled_via_dispatch_in_gateway_false(monkeypatch):
    """kanban.dispatch_in_gateway=false disables the loop (shared gate)."""
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)

    def _fake_load():
        return {"kanban": {"dispatch_in_gateway": False}}

    monkeypatch.setattr(
        "hermes_cli.config.load_config", _fake_load, raising=True
    )
    gw = _FakeGateway()
    _run(gw._kanban_block_recheck())


def test_disabled_via_block_recheck_enabled_false(monkeypatch):
    """kanban.block_recheck_enabled=false disables the loop (own gate)."""
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)

    def _fake_load():
        return {"kanban": {
            "dispatch_in_gateway": True,
            "block_recheck_enabled": False,
        }}

    monkeypatch.setattr(
        "hermes_cli.config.load_config", _fake_load, raising=True
    )
    gw = _FakeGateway()
    _run(gw._kanban_block_recheck())


def test_config_loader_missing_disables_loop(monkeypatch):
    """A broken config loader must be tolerated and not crash the gateway."""
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)

    def _raise():
        raise RuntimeError("broken config")

    monkeypatch.setattr(
        "hermes_cli.config.load_config", _raise, raising=True
    )
    gw = _FakeGateway()
    # Must return without propagating the exception.
    _run(gw._kanban_block_recheck())


# ---------------------------------------------------------------------------
# Happy-path — one tick, mocked sweep.
# ---------------------------------------------------------------------------


def test_single_tick_calls_sweep_all_boards(monkeypatch):
    """Loop should invoke sweep_all_boards with config-plumbed knobs."""
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)

    called: dict = {}

    def _fake_sweep(**kwargs):
        called["kwargs"] = kwargs
        # Return an empty dict so the loop's aggregation code exercises
        # the "no actions" debug branch (still valid execution).
        return {}

    def _fake_load():
        return {
            "kanban": {
                "dispatch_in_gateway": True,
                "block_recheck_enabled": True,
                "block_recheck_interval_seconds": 900,
                "block_recheck_gave_up_cooldown_s": 111,
                "block_recheck_gave_up_max_cycles": 3,
                "block_recheck_review_stale_s": 222,
            }
        }

    monkeypatch.setattr("hermes_cli.config.load_config", _fake_load)

    # Short-circuit the sleep(45) stagger and the interval loop so the
    # test finishes fast. We patch asyncio.sleep to always return
    # immediately AND we flip `_running` False after the first tick
    # so the while-loop exits.
    from hermes_cli import kanban_block_recheck as _br
    monkeypatch.setattr(_br, "sweep_all_boards", _fake_sweep)

    gw = _FakeGateway(running=True)
    tick_count = {"n": 0}

    async def _stub_sleep(_secs):
        # First sleep is the 45s stagger. Everything after that is
        # inside the tick loop. After the sweep runs once we tick the
        # loop's kill-switch so `_running` goes False.
        if tick_count["n"] > 1:
            gw._running = False
        tick_count["n"] += 1

    with patch("gateway.kanban_watchers.asyncio.sleep", new=_stub_sleep):
        _run(gw._kanban_block_recheck())

    assert "kwargs" in called, "sweep_all_boards was never called"
    kw = called["kwargs"]
    assert kw["gave_up_cooldown_s"] == 111
    assert kw["gave_up_max_cycles"] == 3
    assert kw["review_stale_s"] == 222


def test_tick_survives_sweep_exception(monkeypatch, caplog):
    """A sweep_all_boards exception must be logged and the loop continue."""
    import logging
    caplog.set_level(logging.INFO, logger="gateway.kanban_watchers")
    monkeypatch.delenv("HERMES_KANBAN_DISPATCH_IN_GATEWAY", raising=False)

    def _fake_load():
        return {"kanban": {
            "dispatch_in_gateway": True,
            "block_recheck_enabled": True,
        }}
    monkeypatch.setattr("hermes_cli.config.load_config", _fake_load)

    from hermes_cli import kanban_block_recheck as _br

    call_count = {"n": 0}

    def _boom(**_kwargs):
        call_count["n"] += 1
        raise RuntimeError("boom")

    monkeypatch.setattr(_br, "sweep_all_boards", _boom)

    gw = _FakeGateway(running=True)

    async def _stub_sleep(_secs):
        # Kill after the first tick's error is handled.
        if call_count["n"] >= 1:
            gw._running = False

    with patch("gateway.kanban_watchers.asyncio.sleep", new=_stub_sleep):
        _run(gw._kanban_block_recheck())

    assert call_count["n"] >= 1
    # The exception message must be captured (via logger.exception).
    assert any("tick failed" in r.getMessage() for r in caplog.records), \
        f"expected 'tick failed' log; got: {[r.getMessage() for r in caplog.records]}"
