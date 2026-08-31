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
    # 2/3 RED on memory_pressure = 0.667 >= 0.5 threshold (per-probe)
    nodes = [
        NodeStress("h1", True, False),   # memory pressure RED
        NodeStress("h2", True, False),   # memory pressure RED
        NodeStress("h3", False, False),
    ]
    snap = compute_backpressure(nodes, current_state=STATE_CLEAR)
    assert snap.state == STATE_ACTIVE
    assert snap.changed is True
    assert snap.red_nodes == ["h1", "h2"]
    assert 0.66 < snap.stress_fraction < 0.67
    assert snap.total_nodes == 3
    assert snap.triggering_probes == ["memory_pressure"]


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

    # Step 2: 2/3 RED on the same probe → per-probe fraction 0.667 → active
    await b.tick(
        [
            NodeStress("h1", True, False),   # memory RED
            NodeStress("h2", True, False),   # memory RED
            NodeStress("h3", False, False),
        ]
    )
    assert bus.published[-1][1]["state"] == "active"
    assert set(bus.published[-1][1]["red_nodes"]) == {"h1", "h2"}
    assert bus.published[-1][1]["triggering_probes"] == ["memory_pressure"]
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


# ── t_5d0beb8f: urgent_state trigger + per-probe threshold semantics ─────────


def test_urgent_state_trigger_alone_fires_active():
    """Third RED condition from t_5d0beb8f: >50% of nodes have interval_class=urgent.

    All three nodes green on memory/dispatcher; 2/3 nodes RED on urgent → fires.
    """
    nodes = [
        NodeStress("h1", False, False, True),   # urgent
        NodeStress("h2", False, False, True),   # urgent
        NodeStress("h3", False, False, False),
    ]
    snap = compute_backpressure(nodes, current_state=STATE_CLEAR)
    assert snap.state == STATE_ACTIVE
    assert snap.changed is True
    assert snap.triggering_probes == ["hrv_urgent_state"]
    assert snap.memory_pressure_fraction == 0.0
    assert snap.dispatcher_health_fraction == 0.0
    assert abs(snap.urgent_state_fraction - 2 / 3) < 1e-9


def test_per_probe_threshold_split_stays_clear():
    """40% mem RED + 40% disp RED (disjoint) → neither probe crosses 50%.

    Old union-fold would fire at 0.8; new per-probe semantics correctly
    stays clear because no single probe crosses threshold. This is the
    key semantic distinction from t_95d86e0c's original module.
    """
    nodes = [
        NodeStress("h1", True, False, False),   # mem only
        NodeStress("h2", True, False, False),   # mem only
        NodeStress("h3", False, True, False),   # disp only
        NodeStress("h4", False, True, False),   # disp only
        NodeStress("h5", False, False, False),
    ]
    snap = compute_backpressure(nodes, current_state=STATE_CLEAR)
    assert snap.state == STATE_CLEAR
    assert snap.changed is False
    assert snap.triggering_probes == []
    # Per-probe visibility still surfaced in the snapshot (for observability)
    assert abs(snap.memory_pressure_fraction - 0.4) < 1e-9
    assert abs(snap.dispatcher_health_fraction - 0.4) < 1e-9
    # But stress_fraction = max = 0.4, still below 0.5
    assert abs(snap.stress_fraction - 0.4) < 1e-9


def test_multiple_probes_over_threshold_lists_all_triggers():
    """Three probes all RED on 2/3 nodes → all three listed in triggering_probes."""
    nodes = [
        NodeStress("h1", True, True, True),
        NodeStress("h2", True, True, True),
        NodeStress("h3", False, False, False),
    ]
    snap = compute_backpressure(nodes, current_state=STATE_CLEAR)
    assert snap.state == STATE_ACTIVE
    # Order is deterministic: memory, dispatcher, urgent
    assert snap.triggering_probes == [
        "memory_pressure",
        "kanban_dispatcher_health",
        "hrv_urgent_state",
    ]


def test_urgent_hysteresis_holds_active_until_all_probes_drop():
    """Active on urgent alone; mem/disp both 0. Urgent drops to 0.4 (in deadband)
    → must stay active (>= threshold - hysteresis)."""
    nodes = [
        NodeStress("h1", False, False, True),   # urgent
        NodeStress("h2", False, False, True),   # urgent
        NodeStress("h3", False, False, False),
        NodeStress("h4", False, False, False),
        NodeStress("h5", False, False, False),
    ]
    # 2/5 = 0.4 == clear_point → in deadband, stay active
    snap = compute_backpressure(nodes, current_state=STATE_ACTIVE)
    assert snap.state == STATE_ACTIVE
    assert snap.changed is False


def test_all_probes_clear_transitions_from_active():
    """Active on any trigger; all probes drop to 0 → clears."""
    nodes = [NodeStress(f"h{i}", False, False, False) for i in range(5)]
    snap = compute_backpressure(nodes, current_state=STATE_ACTIVE)
    assert snap.state == STATE_CLEAR
    assert snap.changed is True
    assert snap.triggering_probes == []


def test_payload_includes_probe_fractions_when_active():
    """Payload contract (t_5d0beb8f AC: 'enough context for subscribers')."""
    nodes = [
        NodeStress("h1", False, False, True),
        NodeStress("h2", False, False, True),
        NodeStress("h3", False, False, False),
    ]
    snap = compute_backpressure(nodes, current_state=STATE_CLEAR)
    payload = snap.to_payload()
    assert payload["state"] == "active"
    assert payload["triggering_probes"] == ["hrv_urgent_state"]
    assert payload["probe_fractions"] == {
        "memory_pressure": 0.0,
        "kanban_dispatcher_health": 0.0,
        "hrv_urgent_state": 0.6667,
    }
    assert payload["total_nodes"] == 3
    assert "ts" in payload


def test_payload_clear_state_is_minimal():
    """Clear payload stays minimal (state + ts) for cheap subscribers."""
    nodes = [NodeStress("h1", False, False, False)]
    snap = compute_backpressure(nodes, current_state=STATE_CLEAR)
    payload = snap.to_payload()
    assert payload == {"state": "clear", "ts": payload["ts"]}


def test_nodes_from_probe_cache_derives_urgent_from_interval_class():
    """The bridge picks up interval_class from the probe snapshot when present."""
    from types import SimpleNamespace

    probe_cache = {
        "hermes2": SimpleNamespace(swap_pct=10.0, ts="2026-08-31T19:00:00Z",
                                    interval_class="urgent"),
        "hermes1": SimpleNamespace(swap_pct=10.0, ts="2026-08-31T19:00:00Z",
                                    interval_class="calm"),
    }
    stresses = nodes_from_probe_cache(probe_cache)
    by_host = {s.hostname: s for s in stresses}
    assert by_host["hermes2"].urgent_state_red is True
    assert by_host["hermes1"].urgent_state_red is False
    # Case insensitivity + trimming
    probe_cache["h3"] = SimpleNamespace(swap_pct=10.0, ts="x",
                                         interval_class=" Urgent  ")
    stresses = nodes_from_probe_cache(probe_cache)
    assert {s.hostname: s.urgent_state_red for s in stresses}["h3"] is True


def test_nodes_from_probe_cache_explicit_urgent_override_wins():
    """Explicit urgent_state_red map overrides any interval_class inference."""
    from types import SimpleNamespace

    probe_cache = {
        "h1": SimpleNamespace(swap_pct=None, ts=None, interval_class="urgent"),
        "h2": SimpleNamespace(swap_pct=None, ts=None, interval_class="calm"),
    }
    override = {"h1": False, "h2": True}
    stresses = nodes_from_probe_cache(probe_cache, urgent_state_red=override)
    by = {s.hostname: s.urgent_state_red for s in stresses}
    assert by == {"h1": False, "h2": True}


def test_nodes_from_probe_cache_global_urgent_broadcast_pattern():
    """When only a cluster-wide HRV digest exists, callers broadcast to all
    hostnames. Verifies the >50% trigger fires cleanly under this pattern."""
    from types import SimpleNamespace

    probe_cache = {
        f"h{i}": SimpleNamespace(swap_pct=10.0, ts="x", interval_class=None)
        for i in range(3)
    }
    # Global urgent signal broadcast to every hostname
    global_urgent = True
    override = {h: global_urgent for h in probe_cache}
    stresses = nodes_from_probe_cache(probe_cache, urgent_state_red=override)
    snap = compute_backpressure(stresses, current_state=STATE_CLEAR)
    assert snap.state == STATE_ACTIVE
    assert snap.triggering_probes == ["hrv_urgent_state"]
    assert snap.urgent_state_fraction == 1.0


def test_is_red_or_folds_all_three_conditions():
    """Any of three probes RED → is_red True (used for red_nodes list)."""
    assert NodeStress("h", memory_pressure_red=True).is_red is True
    assert NodeStress("h", dispatcher_health_red=True).is_red is True
    assert NodeStress("h", urgent_state_red=True).is_red is True
    assert NodeStress("h").is_red is False
