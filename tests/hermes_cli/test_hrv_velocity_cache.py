"""Tests for HRVVelocityCache — the hrv.pulse.tick consumer in the kanban dispatcher.

Tests the pure-logic parts (cache update, stale check, velocity_factor
lookup, autonomic_state lookup) without a real NATS connection.
"""
import json
import time
import threading

from hermes_cli.hrv_velocity_cache import HRVVelocityCache, MAX_CACHE_AGE_SEC


class FakeMsg:
    """Mimics nats.aio.Msg for the _on_tick callback."""
    def __init__(self, data: bytes):
        self.data = data


def _make_tick(agents: dict, seq: int = 1) -> FakeMsg:
    return FakeMsg(json.dumps({
        "seq": seq,
        "ts": "2026-08-17T06:00:00+00:00",
        "node": "hermes1",
        "interval_sec": 60,
        "agents": agents,
    }).encode())


def test_velocity_factor_default_no_data():
    """No tick received → velocity_factor=1.0 (safe default)."""
    cache = HRVVelocityCache()
    assert cache.velocity_factor("any_agent") == 1.0


def test_velocity_factor_after_tick():
    """After a tick, velocity_factor matches the payload."""
    cache = HRVVelocityCache()
    cache._on_tick(_make_tick({
        "margaret_hamilton": {
            "autonomic_state": "STRESS",
            "vfe_kl_bits": 2.1,
            "sdnn": 4.5,
            "velocity_factor": 0.75,
        }
    }))
    assert cache.velocity_factor("margaret_hamilton") == 0.75


def test_velocity_factor_unknown_agent():
    """Agent not in tick → 1.0."""
    cache = HRVVelocityCache()
    cache._on_tick(_make_tick({"agent_a": {"velocity_factor": 0.5}}))
    assert cache.velocity_factor("unknown_agent") == 1.0


def test_velocity_factor_stale_cache():
    """Cache older than MAX_CACHE_AGE_SEC → all agents return 1.0."""
    cache = HRVVelocityCache()
    cache._on_tick(_make_tick({
        "agent_a": {"velocity_factor": 0.5, "autonomic_state": "CRISIS"}
    }))
    # Fast-forward the last tick timestamp
    cache._last_tick_ts = time.time() - MAX_CACHE_AGE_SEC - 1
    assert cache.velocity_factor("agent_a") == 1.0
    assert cache.is_stale() is True


def test_autonomic_state_default():
    """No data → HOMEOSTATIC."""
    cache = HRVVelocityCache()
    assert cache.autonomic_state("any_agent") == "HOMEOSTATIC"


def test_autonomic_state_after_tick():
    cache = HRVVelocityCache()
    cache._on_tick(_make_tick({
        "agent_a": {"autonomic_state": "CRISIS", "velocity_factor": 0.5}
    }))
    assert cache.autonomic_state("agent_a") == "CRISIS"


def test_autonomic_state_stale():
    """Stale cache → HOMEOSTATIC."""
    cache = HRVVelocityCache()
    cache._on_tick(_make_tick({
        "agent_a": {"autonomic_state": "CRISIS", "velocity_factor": 0.5}
    }))
    cache._last_tick_ts = time.time() - MAX_CACHE_AGE_SEC - 1
    assert cache.autonomic_state("agent_a") == "HOMEOSTATIC"


def test_all_agents_empty():
    cache = HRVVelocityCache()
    assert cache.all_agents() == {}


def test_all_agents_after_tick():
    cache = HRVVelocityCache()
    agents = {"a": {"velocity_factor": 1.0}, "b": {"velocity_factor": 0.5}}
    cache._on_tick(_make_tick(agents))
    result = cache.all_agents()
    assert set(result.keys()) == {"a", "b"}


def test_all_agents_stale():
    """Stale cache → empty dict."""
    cache = HRVVelocityCache()
    cache._on_tick(_make_tick({"a": {"velocity_factor": 0.5}}))
    cache._last_tick_ts = time.time() - MAX_CACHE_AGE_SEC - 1
    assert cache.all_agents() == {}


def test_malformed_tick_ignored():
    """Malformed JSON → cache unchanged, no crash."""
    cache = HRVVelocityCache()
    cache._on_tick(FakeMsg(b"not json"))
    assert cache.velocity_factor("any") == 1.0


def test_tick_no_agents_key_ignored():
    """Tick without 'agents' key → cache unchanged."""
    cache = HRVVelocityCache()
    cache._on_tick(FakeMsg(json.dumps({"seq": 1, "ts": "x"}).encode()))
    assert cache.all_agents() == {}


def test_tick_empty_agents_ignored():
    """Tick with empty agents dict → cache unchanged."""
    cache = HRVVelocityCache()
    cache._on_tick(_make_tick({}))
    assert cache.all_agents() == {}


def test_tick_replaces_previous():
    """New tick replaces old agents entirely."""
    cache = HRVVelocityCache()
    cache._on_tick(_make_tick({"a": {"velocity_factor": 0.5}}))
    cache._on_tick(_make_tick({"b": {"velocity_factor": 0.75}}))
    assert "a" not in cache.all_agents()
    assert "b" in cache.all_agents()


def test_is_stale_no_data():
    """No ticks → stale."""
    cache = HRVVelocityCache()
    assert cache.is_stale() is True


def test_is_stale_fresh():
    """After a tick → not stale."""
    cache = HRVVelocityCache()
    cache._on_tick(_make_tick({"a": {"velocity_factor": 1.0}}))
    assert cache.is_stale() is False


def test_thread_safety():
    """Concurrent reads + writes don't crash."""
    cache = HRVVelocityCache()
    errors = []

    def writer():
        for i in range(100):
            try:
                cache._on_tick(_make_tick({
                    f"agent_{i}": {"velocity_factor": 0.5, "autonomic_state": "STRESS"}
                }, seq=i))
            except Exception as e:
                errors.append(e)

    def reader():
        for i in range(100):
            try:
                cache.velocity_factor(f"agent_{i}")
                cache.all_agents()
            except Exception as e:
                errors.append(e)

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reader)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert errors == []