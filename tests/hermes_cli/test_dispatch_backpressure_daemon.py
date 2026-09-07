"""Tests for :mod:`hermes_cli.dispatch_backpressure_daemon`.

Proves the daemon:

  * Samples the probe source, converts probes to :class:`NodeStress`,
    and drives :class:`BackpressureBroadcaster.tick`.
  * Correctly forwards a RED memory-pressure probe → ACTIVE state on
    the broadcaster.
  * Correctly forwards a clean probe cache → CLEAR state.
  * Publishes to the injected NATS transport only on state transitions
    (idempotent KV every tick).
  * Handles an empty probe cache without exception.
  * Swallows exceptions raised by a broken probe source so the run
    loop keeps going.
  * Optional dispatcher_health_source feeds into the RED signal.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import pytest

from hermes_cli.dispatch_backpressure import (
    STATE_ACTIVE,
    STATE_CLEAR,
    BackpressureBroadcaster,
)
from hermes_cli.dispatch_backpressure_daemon import (
    BackpressureBroadcastDaemon,
)


# ── Test doubles ────────────────────────────────────────────────────────────


@dataclass
class _FakeProbe:
    """Duck-typed NodeProbeSnapshot for tests."""
    hostname: str
    swap_pct: Optional[float]
    ts: float = 1000.0


class _FakeGate:
    """Duck-typed HRVNodeGate with just ``_node_cache``."""

    def __init__(self, cache: Dict[str, _FakeProbe]):
        self._node_cache = cache


class _PublishRecorder:
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def __call__(self, subject: str, payload: bytes) -> None:
        self.calls.append((subject, payload))


class _KVRecorder:
    def __init__(self) -> None:
        self.calls: List[tuple] = []

    async def __call__(self, bucket: str, key: str, value: bytes) -> None:
        self.calls.append((bucket, key, value))


def _make_broadcaster() -> tuple[BackpressureBroadcaster, _PublishRecorder, _KVRecorder]:
    pub = _PublishRecorder()
    kv = _KVRecorder()
    b = BackpressureBroadcaster(publish=pub, kv_put=kv)
    return b, pub, kv


# ── tick_once() ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tick_once_activates_broadcaster_on_red_probe():
    """A probe with swap_pct >= 90 flips the broadcaster to ACTIVE."""
    b, pub, kv = _make_broadcaster()

    cache = {"h1": _FakeProbe("h1", swap_pct=95.0)}
    daemon = BackpressureBroadcastDaemon(
        broadcaster=b, probe_source=lambda: _FakeGate(cache),
    )

    snap = await daemon.tick_once()
    assert snap is not None
    assert snap.state == STATE_ACTIVE
    assert snap.changed is True
    assert "h1" in snap.red_nodes
    assert daemon.ticks == 1
    assert b.state == STATE_ACTIVE

    # NATS publish fires on transition; KV mirror always fires
    assert len(pub.calls) == 1
    assert pub.calls[0][0] == "dispatch.backpressure"
    assert len(kv.calls) == 1


@pytest.mark.asyncio
async def test_tick_once_stays_clear_on_green_probes():
    """All-green probes leave broadcaster in CLEAR with no NATS publish."""
    b, pub, kv = _make_broadcaster()

    cache = {"h1": _FakeProbe("h1", swap_pct=50.0)}
    daemon = BackpressureBroadcastDaemon(
        broadcaster=b, probe_source=lambda: _FakeGate(cache),
    )

    snap = await daemon.tick_once()
    assert snap is not None
    assert snap.state == STATE_CLEAR
    assert snap.changed is False  # CLEAR → CLEAR is not a transition
    assert daemon.ticks == 1

    # No NATS publish (no transition); KV mirror still fires
    assert pub.calls == []
    assert len(kv.calls) == 1


@pytest.mark.asyncio
async def test_tick_once_returns_none_on_empty_probe_cache():
    """Empty probe cache → daemon skips broadcast, no exception."""
    b, _pub, _kv = _make_broadcaster()

    daemon = BackpressureBroadcastDaemon(
        broadcaster=b, probe_source=lambda: _FakeGate({}),
    )

    snap = await daemon.tick_once()
    assert snap is None
    assert daemon.ticks == 0
    assert b.state == STATE_CLEAR  # unchanged


@pytest.mark.asyncio
async def test_tick_once_swallows_probe_source_exceptions():
    """A broken probe source is logged and swallowed — daemon keeps running."""
    b, _pub, _kv = _make_broadcaster()

    def _boom() -> Any:
        raise RuntimeError("probe source broken")

    daemon = BackpressureBroadcastDaemon(broadcaster=b, probe_source=_boom)

    snap = await daemon.tick_once()
    assert snap is None


@pytest.mark.asyncio
async def test_tick_once_uses_dispatcher_health_source_when_provided():
    """Dispatcher-health RED alone (no memory pressure) still turns ACTIVE."""
    b, pub, _kv = _make_broadcaster()

    cache = {"h1": _FakeProbe("h1", swap_pct=50.0)}  # green memory
    daemon = BackpressureBroadcastDaemon(
        broadcaster=b,
        probe_source=lambda: _FakeGate(cache),
        dispatcher_health_source=lambda: {"h1": True},  # RED
    )

    snap = await daemon.tick_once()
    assert snap is not None
    assert snap.state == STATE_ACTIVE
    assert "h1" in snap.red_nodes
    assert pub.calls, "expected a NATS publish on ACTIVE transition"


# ── State transitions across multiple ticks ────────────────────────────────


@pytest.mark.asyncio
async def test_broadcaster_transitions_clear_active_clear_across_ticks():
    """Simulate CLEAR → ACTIVE → CLEAR by mutating the cache between ticks."""
    b, pub, kv = _make_broadcaster()

    cache: Dict[str, _FakeProbe] = {"h1": _FakeProbe("h1", swap_pct=50.0)}
    daemon = BackpressureBroadcastDaemon(
        broadcaster=b, probe_source=lambda: _FakeGate(cache),
    )

    # Tick 1: green
    s1 = await daemon.tick_once()
    assert s1 is not None and s1.state == STATE_CLEAR and s1.changed is False

    # Tick 2: h1 goes red → ACTIVE transition (publish fires)
    cache["h1"] = _FakeProbe("h1", swap_pct=95.0)
    s2 = await daemon.tick_once()
    assert s2 is not None and s2.state == STATE_ACTIVE and s2.changed is True

    # Tick 3: h1 recovers → CLEAR transition (below hysteresis threshold)
    cache["h1"] = _FakeProbe("h1", swap_pct=50.0)
    s3 = await daemon.tick_once()
    assert s3 is not None and s3.state == STATE_CLEAR and s3.changed is True

    # Two transitions → two NATS publishes; every tick → one KV put
    assert len(pub.calls) == 2, f"expected 2 publishes, got {pub.calls}"
    assert len(kv.calls) == 3
    assert daemon.ticks == 3


# ── run_forever() cadence + stop() ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_forever_stops_promptly_when_stop_is_set():
    """stop() wakes the sleep and exits the loop within one interval."""
    b, _pub, _kv = _make_broadcaster()

    cache = {"h1": _FakeProbe("h1", swap_pct=50.0)}
    daemon = BackpressureBroadcastDaemon(
        broadcaster=b,
        probe_source=lambda: _FakeGate(cache),
        interval_seconds=10.0,  # long — stop() must wake early
    )

    task = asyncio.create_task(daemon.run_forever())
    await asyncio.sleep(0.05)  # let first tick land
    daemon.stop()

    # Should exit well under interval_seconds — cancel guards the test.
    await asyncio.wait_for(task, timeout=1.0)
    assert daemon.ticks >= 1


@pytest.mark.asyncio
async def test_run_forever_ticks_multiple_times_at_short_interval():
    """A short interval drives multiple ticks; each tick calls the source."""
    b, _pub, _kv = _make_broadcaster()

    cache = {"h1": _FakeProbe("h1", swap_pct=50.0)}
    calls: List[int] = []

    def _source() -> _FakeGate:
        calls.append(1)
        return _FakeGate(cache)

    daemon = BackpressureBroadcastDaemon(
        broadcaster=b, probe_source=_source, interval_seconds=0.02,
    )
    task = asyncio.create_task(daemon.run_forever())
    await asyncio.sleep(0.15)  # ~6-7 ticks at 20ms interval
    daemon.stop()
    await asyncio.wait_for(task, timeout=1.0)
    assert daemon.ticks >= 3, f"expected ≥3 ticks, got {daemon.ticks}"
    assert len(calls) == daemon.ticks


# ── Interval validation ────────────────────────────────────────────────────


def test_interval_must_be_positive():
    b, _pub, _kv = _make_broadcaster()
    with pytest.raises(ValueError, match="interval_seconds must be > 0"):
        BackpressureBroadcastDaemon(
            broadcaster=b,
            probe_source=lambda: _FakeGate({}),
            interval_seconds=0.0,
        )
