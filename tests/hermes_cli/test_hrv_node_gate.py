"""Tests for HRV node evaluation gate."""
import pytest
import time
import datetime
from hermes_cli.hrv_node_gate import (
    HRVNodeGate,
    NodeProbeSnapshot,
    HRVDigestSnapshot,
)


def get_now_ts():
    """Return current time as ISO8601 timestamp."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def get_old_ts(seconds_ago=90):
    """Return a timestamp from N seconds ago."""
    then = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return then.isoformat()


class TestMemoryPressure:
    """Test memory_pressure rejection condition (swap_pct >= 90%)."""
    
    def test_healthy_memory_passes(self):
        """Node with swap_pct < 90% should pass."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=45.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result is None
    
    def test_high_swap_rejected(self):
        """Node with swap_pct >= 90% should be rejected."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=95.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result == "memory_pressure"
    
    def test_stale_probe_fails_open(self):
        """Stale memory probe should NOT reject (fail-open)."""
        gate = HRVNodeGate(max_probe_age_seconds=60)
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=95.0,  # Would be rejected if fresh
            ts=get_old_ts(90),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        # Should NOT reject due to staleness
        assert result is None  # fail-open on stale probe
    
    def test_missing_probe_fails_open(self):
        """Missing probe should NOT reject (fail-open)."""
        gate = HRVNodeGate()
        # Don't set any probe
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result is None  # fail-open on missing probe


class TestBedrockRateLimit:
    """Test bedrock_rate_limit_saturation rejection condition."""
    
    def test_healthy_bedrock_tpm_passes(self):
        """Node with sufficient Bedrock TPM should pass."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            bedrock_tpm_remaining=50000,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result is None
    
    def test_low_bedrock_tpm_rejected(self):
        """Node with bedrock_tpm_remaining < 1000 should be rejected."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            bedrock_tpm_remaining=500,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result == "bedrock_rate_limit_saturation[claude-haiku]"
    
    def test_stale_bedrock_fails_open(self):
        """Stale Bedrock TPM probe should NOT reject."""
        gate = HRVNodeGate(max_probe_age_seconds=60)
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            bedrock_tpm_remaining=500,  # Would be rejected if fresh
            ts=get_old_ts(90),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result is None  # fail-open


class TestHRVUrgency:
    """Test hrv urgency gate (urgent + priority < P0)."""
    
    def test_urgent_p0_task_passes(self):
        """P0 task during urgent state should pass."""
        gate = HRVNodeGate()
        digest = HRVDigestSnapshot(
            interval_class="urgent",
            ts=get_now_ts(),
        )
        gate.set_hrv_digest(digest)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku", task_priority=0)
        assert result is None
    
    def test_urgent_p1_task_rejected(self):
        """P1 task during urgent state should be rejected."""
        gate = HRVNodeGate()
        digest = HRVDigestSnapshot(
            interval_class="urgent",
            ts=get_now_ts(),
        )
        gate.set_hrv_digest(digest)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku", task_priority=1)
        assert result == "hrv_urgent_state"
    
    def test_calm_state_allows_all_priorities(self):
        """Calm state should allow any priority."""
        gate = HRVNodeGate()
        digest = HRVDigestSnapshot(
            interval_class="calm",
            ts=get_now_ts(),
        )
        gate.set_hrv_digest(digest)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku", task_priority=5)
        assert result is None
    
    def test_stale_urgency_fails_open(self):
        """Stale urgency digest should NOT reject."""
        gate = HRVNodeGate(max_probe_age_seconds=60)
        digest = HRVDigestSnapshot(
            interval_class="urgent",
            ts=get_old_ts(90),
        )
        gate.set_hrv_digest(digest)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku", task_priority=1)
        assert result is None  # fail-open


class TestMinResources:
    """Test min_resources overflow rejection condition."""
    
    def test_sufficient_memory_passes(self):
        """Node with sufficient memory should pass."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            mem_gb_available=4.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        task_min_resources = {"mem_gb": 1.0, "cpu_cores": 1}
        result = gate.evaluate_node(
            "task1", "hermes2", "claude-haiku",
            task_min_resources=task_min_resources
        )
        assert result is None
    
    def test_insufficient_memory_rejected(self):
        """Node with insufficient memory should be rejected."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            mem_gb_available=0.5,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        task_min_resources = {"mem_gb": 1.0}
        result = gate.evaluate_node(
            "task1", "hermes2", "claude-haiku",
            task_min_resources=task_min_resources
        )
        assert result == "min_resources_overflow"
    
    def test_insufficient_bedrock_tpm_rejected(self):
        """Node with insufficient Bedrock TPM reservation should be rejected."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            bedrock_tpm_remaining=8000,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        task_min_resources = {"bedrock_tpm_reservation": 10000}
        result = gate.evaluate_node(
            "task1", "hermes2", "claude-haiku",
            task_min_resources=task_min_resources
        )
        assert result == "min_resources_overflow"
    
    def test_no_min_resources_passes(self):
        """Node with no min_resources requirement should pass."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            mem_gb_available=0.1,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node(
            "task1", "hermes2", "claude-haiku",
            task_min_resources=None
        )
        assert result is None
    
    def test_stale_resources_fails_open(self):
        """Stale resource probe should NOT reject."""
        gate = HRVNodeGate(max_probe_age_seconds=60)
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            mem_gb_available=0.1,
            ts=get_old_ts(90),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        task_min_resources = {"mem_gb": 1.0}
        result = gate.evaluate_node(
            "task1", "hermes2", "claude-haiku",
            task_min_resources=task_min_resources
        )
        assert result is None  # fail-open


class TestIntegration:
    """Integration tests: healthy node with all signals green."""
    
    def test_healthy_node_passes_all_gates(self):
        """Node with all signals green should pass."""
        gate = HRVNodeGate()
        
        # Healthy probe
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=30.0,
            mem_gb_available=8.0,
            bedrock_tpm_remaining=50000,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        # Calm HRV state
        digest = HRVDigestSnapshot(
            interval_class="calm",
            ts=get_now_ts(),
        )
        gate.set_hrv_digest(digest)
        
        # Task with moderate requirements
        task_min_resources = {"mem_gb": 1.0, "cpu_cores": 1, "bedrock_tpm_reservation": 10000}
        
        result = gate.evaluate_node(
            "task1", "hermes2", "claude-haiku",
            task_priority=5,
            task_min_resources=task_min_resources
        )
        assert result is None
    
    def test_rejection_priority_order(self):
        """When multiple conditions are true, the first one wins."""
        gate = HRVNodeGate()
        
        # Probe with multiple issues
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=95.0,  # Memory pressure
            bedrock_tpm_remaining=500,  # Rate limit
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        # Urgent state
        digest = HRVDigestSnapshot(
            interval_class="urgent",
            ts=get_now_ts(),
        )
        gate.set_hrv_digest(digest)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku", task_priority=1)
        # Should reject on the first condition (memory_pressure)
        assert result == "memory_pressure"


class TestProbeSnapshotDeserialization:
    """Test KV payload deserialization."""
    
    def test_deserialize_from_kv_payload(self):
        """Load probe snapshot from NATS KV payload."""
        gate = HRVNodeGate()
        
        kv_payload = {
            "hostname": "hermes2",
            "swap_pct": 45.0,
            "mem_gb_available": 8.5,
            "load_1m": 1.2,
            "load_5m": 0.9,
            "disk_free_gb": 100.0,
            "active_workers": 3,
            "max_workers": 8,
            "bedrock_tpm_remaining": 45000,
            "ts": get_now_ts(),
        }
        
        probe = gate.load_node_probe_from_kv(kv_payload)
        
        assert probe.hostname == "hermes2"
        assert probe.swap_pct == 45.0
        assert probe.mem_gb_available == 8.5
        assert probe.bedrock_tpm_remaining == 45000


class TestMetricEmission:
    """Test metric emission on rejection."""
    
    def test_emit_rejection_metric(self):
        """Metric callback should be called with reason."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=95.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        # Call evaluate to trigger rejection
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result == "memory_pressure"
        
        # Capture metric emissions
        metrics_called = []
        def capture_metric(reason):
            metrics_called.append(reason)
        
        gate.emit_rejection_metric("task1", "hermes2", result, metrics_fn=capture_metric)
        assert metrics_called == ["memory_pressure"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
