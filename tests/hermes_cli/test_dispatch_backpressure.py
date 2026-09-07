"""Tests for hermes_cli.dispatch_backpressure — cluster stress signalling."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import List, Tuple

import pytest

from hermes_cli.dispatch_backpressure import (
    DEFAULT_HYSTERESIS,
    DEFAULT_THRESHOLD,
    KV_BUCKET_BACKPRESSURE,
    KV_KEY_CURRENT,
    STATE_ACTIVE,
    STATE_CLEAR,
    SUBJECT_BACKPRESSURE,
    BackpressureBroadcaster,
    NodeStress,
    compute_backpressure,
    nodes_from_probe_cache,
)


# ── compute_backpressure — pure ──────────────────────────────────────────────


def test_compute_all_green_stays_clear():
    nodes = [
        NodeStress("h1", False, False),
        NodeStress("h2", False, False),
        NodeStress("h3", False, False),
    ]
    snap = compute_backpressure(nodes, current_state=STATE_CLEAR)
    assert snap.state == STATE_CLEAR
    assert snap.changed is False
    assert snap.red_nodes == []
    assert snap.stress_fraction == 0.0
    assert snap.total_nodes == 3


def test_compute_half_red_at_default_threshold_goes_active():
    # 2/3 red = 0.667 >= 0.5 threshold
    nodes = [
        NodeStress("h1", True, False),  # memory pressure RED
        NodeStress("h2", False, True),  # dispatcher_health RED
        NodeStress("h3", False, False),
    ]
    snap = compute_backpressure(nodes, current_state=STATE_CLEAR)
    assert snap.state == STATE_ACTIVE
    assert snap.changed is True
    assert snap.red_nodes == ["h1", "h2"]
    assert 0.66 < snap.stress_fraction < 0.67
    assert snap.total_nodes == 3


def test_compute_exact_threshold_goes_active():
    # 1/2 = 0.5 exactly — must trip (>= not >)
    nodes = [NodeStress("h1", True, False), NodeStress("h2", False, False)]
    snap = compute_backpressure(nodes, current_state=STATE_CLEAR)
    assert snap.state == STATE_ACTIVE
    assert snap.changed is True


def test_compute_below_threshold_stays_clear():
    # 1/3 = 0.33 < 0.5
    nodes = [
        NodeStress("h1", True, False),
        NodeStress("h2", False, False),
        NodeStress("h3", False, False),
    ]
    snap = compute_backpressure(nodes, current_state=STATE_CLEAR)
    assert snap.state == STATE_CLEAR
    assert snap.changed is False


def test_compute_hysteresis_holds_active_in_deadband():
    # In active, hover between 0.4 and 0.5 — should stay active
    nodes = [
        NodeStress("h1", True, False),
        NodeStress("h2", True, False),
        NodeStress("h3", False, False),
        NodeStress("h4", False, False),
        NodeStress("h5", False, False),
    ]
    # 2/5 = 0.4, which is above (threshold - hysteresis) = 0.4 (equal counts as still in band)
    snap = compute_backpressure(nodes, current_state=STATE_ACTIVE)
    assert snap.state == STATE_ACTIVE
    assert snap.changed is False


def test_compute_hysteresis_clears_below_band():
    # In active, drop through the clear point (< 0.4)
    nodes = [
        NodeStress("h1", True, False),
        NodeStress("h2", False, False),
        NodeStress("h3", False, False),
        NodeStress("h4", False, False),
        NodeStress("h5", False, False),
    ]
    # 1/5 = 0.2 < 0.4 → clear
    snap = compute_backpressure(nodes, current_state=STATE_ACTIVE)
    assert snap.state == STATE_CLEAR
    assert snap.changed is True


def test_compute_zero_nodes_fails_open():
    """No probes yet → never go active. Cold-cluster protection."""
    snap = compute_backpressure([], current_state=STATE_CLEAR)
    assert snap.state == STATE_CLEAR
    assert snap.stress_fraction == 0.0


def test_compute_custom_threshold_and_hysteresis():
    nodes = [
        NodeStress("h1", True, False),
        NodeStress("h2", True, False),
        NodeStress("h3", True, False),
        NodeStress("h4", False, False),
    ]
    # 3/4 = 0.75. With threshold=0.8 → stay clear
    snap = compute_backpressure(
        nodes, current_state=STATE_CLEAR, threshold=0.8, hysteresis=0.2
    )
    assert snap.state == STATE_CLEAR


def test_compute_dispatcher_health_alone_triggers_red():
    nodes = [
        NodeStress("h1", False, True),  # only dispatcher_health RED
        NodeStress("h2", False, False),
    ]
    snap = compute_backpressure(nodes, current_state=STATE_CLEAR)
    assert snap.state == STATE_ACTIVE
    assert snap.red_nodes == ["h1"]


def test_snapshot_payload_active_shape():
    nodes = [NodeStress("h1", True, False), NodeStress("h2", False, False)]
    snap = compute_backpressure(nodes, current_state=STATE_CLEAR, now_iso="2026-08-18T00:00:00Z")
    p = snap.to_payload()
    assert p["state"] == "active"
    assert p["red_nodes"] == ["h1"]
    assert p["ts"] == "2026-08-18T00:00:00Z"
    assert p["total_nodes"] == 2
    assert "stress_fraction" in p


def test_snapshot_payload_clear_shape():
    snap = compute_backpressure([], current_state=STATE_CLEAR, now_iso="2026-08-18T00:00:00Z")
    p = snap.to_payload()
    assert p == {"state": "clear", "ts": "2026-08-18T00:00:00Z"}


# ── BackpressureBroadcaster — async, transition-only publish ────────────────


class _CaptureBus:
    """In-memory NATS + KV double for tests."""

    def __init__(self) -> None:
        self.published: List[Tuple[str, dict]] = []
        self.kv: dict[Tuple[str, str], dict] = {}

    async def publish(self, subject: str, data: bytes) -> None:
        self.published.append((subject, json.loads(data.decode())))

    async def kv_put(self, bucket: str, key: str, data: bytes) -> None:
        self.kv[(bucket, key)] = json.loads(data.decode())


@pytest.mark.asyncio
async def test_broadcaster_publishes_on_clear_to_active_transition():
    bus = _CaptureBus()
    b = BackpressureBroadcaster(publish=bus.publish, kv_put=bus.kv_put)

    # Start clear — all green — no publish
    await b.tick([NodeStress("h1", False, False)])
    assert len(bus.published) == 0
    assert b.state == STATE_CLEAR

    # 2/3 RED → active, publish once
    await b.tick(
        [
            NodeStress("h1", True, False),
            NodeStress("h2", True, False),
            NodeStress("h3", False, False),
        ]
    )
    assert len(bus.published) == 1
    subject, payload = bus.published[0]
    assert subject == SUBJECT_BACKPRESSURE
    assert payload["state"] == "active"
    assert set(payload["red_nodes"]) == {"h1", "h2"}
    assert b.state == STATE_ACTIVE
    assert b.transitions == 1


@pytest.mark.asyncio
async def test_broadcaster_idempotent_when_state_unchanged():
    bus = _CaptureBus()
    b = BackpressureBroadcaster(publish=bus.publish, kv_put=bus.kv_put)

    # Multiple ticks in active state → single publish
    nodes_red = [
        NodeStress("h1", True, False),
        NodeStress("h2", True, False),
        NodeStress("h3", False, False),
    ]
    for _ in range(5):
        await b.tick(nodes_red)

    assert len(bus.published) == 1
    assert b.transitions == 1


@pytest.mark.asyncio
async def test_broadcaster_hysteresis_prevents_flap():
    bus = _CaptureBus()
    b = BackpressureBroadcaster(publish=bus.publish, kv_put=bus.kv_put)

    # Go active
    await b.tick(
        [NodeStress("h1", True, False), NodeStress("h2", False, False)]
    )
    assert b.state == STATE_ACTIVE
    # Bounce back to 0/2 red = 0.0 < clear point (0.4) → clears
    await b.tick(
        [NodeStress("h1", False, False), NodeStress("h2", False, False)]
    )
    assert b.state == STATE_CLEAR
    # 2 publish events: active, clear
    assert [p[1]["state"] for p in bus.published] == ["active", "clear"]


@pytest.mark.asyncio
async def test_broadcaster_hysteresis_holds_in_deadband():
    """Between clear-point (0.4) and threshold (0.5): stay active."""
    bus = _CaptureBus()
    b = BackpressureBroadcaster(publish=bus.publish, kv_put=bus.kv_put)

    # 3/5 red = 0.6 → active
    await b.tick(
        [
            NodeStress("h1", True, False),
            NodeStress("h2", True, False),
            NodeStress("h3", True, False),
            NodeStress("h4", False, False),
            NodeStress("h5", False, False),
        ]
    )
    assert b.state == STATE_ACTIVE

    # 2/5 = 0.4 → in deadband, stay active
    await b.tick(
        [
            NodeStress("h1", True, False),
            NodeStress("h2", True, False),
            NodeStress("h3", False, False),
            NodeStress("h4", False, False),
            NodeStress("h5", False, False),
        ]
    )
    assert b.state == STATE_ACTIVE
    assert len(bus.published) == 1  # still just the one active publish


@pytest.mark.asyncio
async def test_broadcaster_mirrors_state_to_kv_every_tick():
    """KV mirror must be up-to-date even when nothing has changed —
    late subscribers need to read current state without waiting for
    a transition."""
    bus = _CaptureBus()
    b = BackpressureBroadcaster(publish=bus.publish, kv_put=bus.kv_put)

    await b.tick([NodeStress("h1", False, False)])
    assert (KV_BUCKET_BACKPRESSURE, KV_KEY_CURRENT) in bus.kv
    assert bus.kv[(KV_BUCKET_BACKPRESSURE, KV_KEY_CURRENT)]["state"] == "clear"

    # Tick again, still clear — KV overwritten (same content)
    await b.tick([NodeStress("h1", False, False)])
    assert bus.kv[(KV_BUCKET_BACKPRESSURE, KV_KEY_CURRENT)]["state"] == "clear"

    # Go active
    await b.tick(
        [NodeStress("h1", True, False), NodeStress("h2", True, False)]
    )
    assert bus.kv[(KV_BUCKET_BACKPRESSURE, KV_KEY_CURRENT)]["state"] == "active"


@pytest.mark.asyncio
async def test_broadcaster_no_publish_if_transport_omitted():
    """Publish/kv_put are optional — broadcaster must still evaluate."""
    b = BackpressureBroadcaster()
    snap = await b.tick(
        [NodeStress("h1", True, False), NodeStress("h2", True, False)]
    )
    assert snap.state == STATE_ACTIVE
    assert b.state == STATE_ACTIVE
    # No transport → no crash


@pytest.mark.asyncio
async def test_broadcaster_survives_publish_exception():
    async def bad_publish(subject: str, data: bytes) -> None:
        raise RuntimeError("nats down")

    async def bad_kv(bucket: str, key: str, data: bytes) -> None:
        raise RuntimeError("kv down")

    b = BackpressureBroadcaster(publish=bad_publish, kv_put=bad_kv)
    # Must not raise despite transport failures
    snap = await b.tick(
        [NodeStress("h1", True, False), NodeStress("h2", True, False)]
    )
    assert snap.state == STATE_ACTIVE
    # State machine still tracks the transition
    assert b.state == STATE_ACTIVE


# ── nodes_from_probe_cache — bridge from HRV gate ────────────────────────────


def test_nodes_from_probe_cache_maps_memory_red():
    probes = {
        "h1": SimpleNamespace(swap_pct=95.0, ts="2026-08-18T00:00:00Z"),
        "h2": SimpleNamespace(swap_pct=50.0, ts="2026-08-18T00:00:00Z"),
        "h3": SimpleNamespace(swap_pct=None, ts="2026-08-18T00:00:00Z"),
    }
    ns = nodes_from_probe_cache(probes)
    by_host = {n.hostname: n for n in ns}
    assert by_host["h1"].memory_pressure_red is True
    assert by_host["h2"].memory_pressure_red is False
    assert by_host["h3"].memory_pressure_red is False


def test_nodes_from_probe_cache_maps_dispatcher_health():
    probes = {"h1": SimpleNamespace(swap_pct=10.0, ts="x")}
    ns = nodes_from_probe_cache(probes, dispatcher_health_red={"h1": True})
    assert ns[0].dispatcher_health_red is True
    assert ns[0].is_red is True


def test_nodes_from_probe_cache_defaults_dispatcher_health_green():
    probes = {"h1": SimpleNamespace(swap_pct=10.0, ts="x")}
    ns = nodes_from_probe_cache(probes)  # dispatcher_health_red omitted
    assert ns[0].dispatcher_health_red is False


# ── Full integration walk — the acceptance-criteria scenario ────────────────


@pytest.mark.asyncio
async def test_acceptance_scenario_2_of_3_red_then_recover():
    """Acceptance from t_95d86e0c:

      1. 2/3 nodes RED → backpressure `active` published
      2. Nodes recover (0/3 RED) → backpressure `clear` published
      3. Only two NATS publishes total, both on the KV mirror
    """
    bus = _CaptureBus()
    b = BackpressureBroadcaster(publish=bus.publish, kv_put=bus.kv_put)

    # Step 1: all-green start (warm the KV mirror, no publish)
    await b.tick(
        [
            NodeStress("h1", False, False),
            NodeStress("h2", False, False),
            NodeStress("h3", False, False),
        ]
    )
    assert bus.kv[(KV_BUCKET_BACKPRESSURE, KV_KEY_CURRENT)]["state"] == "clear"
    assert len(bus.published) == 0

    # Step 2: 2/3 RED → active
    await b.tick(
        [
            NodeStress("h1", True, False),  # memory
            NodeStress("h2", False, True),  # dispatcher
            NodeStress("h3", False, False),
        ]
    )
    assert bus.published[-1][1]["state"] == "active"
    assert set(bus.published[-1][1]["red_nodes"]) == {"h1", "h2"}
    assert b.state == STATE_ACTIVE

    # Step 3: recovery — nodes back to green
    await b.tick(
        [
            NodeStress("h1", False, False),
            NodeStress("h2", False, False),
            NodeStress("h3", False, False),
        ]
    )
    assert bus.published[-1][1]["state"] == "clear"
    assert b.state == STATE_CLEAR
    assert [p[1]["state"] for p in bus.published] == ["active", "clear"]
