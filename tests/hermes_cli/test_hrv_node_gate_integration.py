"""Integration tests for HRV node gate with kanban dispatcher."""
import pytest
import json
import sqlite3
import tempfile
from pathlib import Path

from hermes_cli.kanban_db import init_db, create_task, Task
from hermes_cli.hrv_node_gate import HRVNodeGate, NodeProbeSnapshot, HRVDigestSnapshot
from hermes_cli.hrv_node_gate_integration import check_node_gate


def get_now_ts():
    """Return current time as ISO8601 timestamp."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@pytest.fixture
def temp_db():
    """Create a temporary kanban DB for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "kanban.db"
        # init_db expects a Path object
        init_db(db_path)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        yield conn
        conn.close()


class TestNodeGateIntegration:
    """Integration tests with kanban DB."""
    
    def test_node_gate_rejects_high_memory_pressure(self, temp_db):
        """Gate should reject node with high memory pressure."""
        # Create task
        task_id = create_task(
            temp_db,
            title="test-task",
            assignee="backend-eng",
            body="Test task",
        )
        
        # Set up gate with memory pressure
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=95.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        # Check gate
        result = check_node_gate(
            temp_db,
            task_id,
            "hermes2",
            "claude-haiku",
            gate=gate,
        )
        assert result == "memory_pressure"
    
    def test_node_gate_respects_task_priority(self, temp_db):
        """Gate should reject lower-priority tasks during urgent state."""
        # Create P1 task
        task_id = create_task(
            temp_db,
            title="Low priority task",
            body="",
            assignee="backend-eng",
            priority=1,  # P1
        )
        
        # Set up gate with urgent state
        gate = HRVNodeGate()
        digest = HRVDigestSnapshot(
            interval_class="urgent",
            ts=get_now_ts(),
        )
        gate.set_hrv_digest(digest)
        
        # Check gate — should reject
        result = check_node_gate(
            temp_db,
            task_id,
            "hermes2",
            "claude-haiku",
            gate=gate,
        )
        assert result == "hrv_urgent_state"
    
    def test_node_gate_reads_min_resources_from_db(self, temp_db):
        """Gate should fetch min_resources from task and evaluate."""
        # Create task with min_resources in body
        body = """---
min_resources:
  mem_gb: 2.0
  cpu_cores: 2
  bedrock_tpm_reservation: 20000
---
Task body here.
"""
        task_id = create_task(
            temp_db,
            title="test-task-resources",
            assignee="backend-eng",
            body=body,
        )
        
        # Set up gate with insufficient resources
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            mem_gb_available=1.0,  # Less than required 2.0
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        # Check gate — should reject
        result = check_node_gate(
            temp_db,
            task_id,
            "hermes2",
            "claude-haiku",
            gate=gate,
        )
        assert result == "min_resources_overflow"
    
    def test_node_gate_passes_healthy_node(self, temp_db):
        """Gate should pass healthy node."""
        task_id = create_task(
            temp_db,
            title="test-task-healthy",
            assignee="backend-eng",
            body="Task",
        )
        
        # Set up gate with healthy node
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=30.0,
            mem_gb_available=8.0,
            bedrock_tpm_remaining=50000,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        digest = HRVDigestSnapshot(
            interval_class="calm",
            ts=get_now_ts(),
        )
        gate.set_hrv_digest(digest)
        
        # Check gate — should pass
        result = check_node_gate(
            temp_db,
            task_id,
            "hermes2",
            "claude-haiku",
            gate=gate,
        )
        assert result is None
    
    def test_node_gate_local_dispatch_always_passes(self, temp_db):
        """Local dispatch (node=None) should always pass."""
        task_id = create_task(
            temp_db,
            title="test-task-local",
            assignee="backend-eng",
            body="Task",
        )
        
        # Gate would reject remote node
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=95.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        # But local dispatch should pass
        result = check_node_gate(
            temp_db,
            task_id,
            None,  # Local
            "claude-haiku",
            gate=gate,
        )
        assert result is None
    
    def test_node_gate_fails_open_on_missing_gate(self, temp_db):
        """Gate check should fail-open when gate is unavailable."""
        task_id = create_task(
            temp_db,
            title="test-task-no-gate",
            assignee="backend-eng",
            body="Task",
        )
        
        # Call with gate=None (will try to load default, which doesn't exist in test)
        # Should fail-open (return None)
        result = check_node_gate(
            temp_db,
            task_id,
            "hermes2",
            "claude-haiku",
            gate=None,  # Will use default (not set up in test)
        )
        # Fail-open: None means no rejection
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
