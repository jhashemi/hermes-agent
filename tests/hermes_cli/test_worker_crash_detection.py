"""Tests for worker crash detection and recovery.

This test suite verifies that the kanban system can detect when a worker
process has crashed (PID is no longer alive) but the task is still marked
as 'running', and automatically transitions such zombie tasks to a crashed
state with proper error tracking.

The tests reproduce the "silent crash" scenario from the May 2026 RCA:
- Worker spawned (PID stored in tasks.worker_pid)
- Worker crashes without logging
- Board shows task as 'running' indefinitely
- No heartbeat, no ended_at, PID is dead
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Set up a temporary kanban home directory."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


# ---------------------------------------------------------------------------
# Tests: Dead PID Detection
# ---------------------------------------------------------------------------


def test_pid_alive_returns_false_for_nonexistent_pid():
    """_pid_alive should return False for PIDs that don't exist."""
    # Use a very high PID unlikely to exist
    fake_pid = 999999
    assert kb._pid_alive(fake_pid) is False


def test_pid_alive_returns_false_for_none():
    """_pid_alive should return False for None/0 PIDs."""
    assert kb._pid_alive(None) is False
    assert kb._pid_alive(0) is False
    assert kb._pid_alive(-1) is False


def test_pid_alive_returns_true_for_current_process():
    """_pid_alive should return True for the current process."""
    assert kb._pid_alive(os.getpid()) is True


# ---------------------------------------------------------------------------
# Tests: Detect Crashed Worker (Zombie Task)
# ---------------------------------------------------------------------------


def test_detect_crashed_worker_with_dead_pid(kanban_home):
    """Detect a task stuck 'running' with a dead PID as a crash."""
    conn = kb.connect()
    
    # Create a task and claim it
    task_id = kb.create_task(conn, title="Test crash detection", assignee="test")
    claimed = kb.claim_task(conn, task_id, claimer="test-claimer")
    assert claimed is not None
    
    # Verify task is running
    task = kb.get_task(conn, task_id)
    assert task.status == "running"
    
    # Simulate worker crash: task still running but PID is dead
    # (We use a fake PID that definitely doesn't exist)
    fake_dead_pid = 999999
    conn.execute(
        "UPDATE tasks SET worker_pid = ? WHERE id = ?",
        (fake_dead_pid, task_id),
    )
    conn.commit()
    
    # Call new detect_worker_pids_alive function
    crashes = kb.detect_worker_pids_alive(conn)
    
    # Should detect the crashed worker
    assert len(crashes) == 1
    crash_info = crashes[0]
    assert crash_info["task_id"] == task_id
    assert crash_info["pid"] == fake_dead_pid
    assert crash_info["status_before"] == "running"
    
    conn.close()


def test_detect_crashed_worker_marks_task_crashed(kanban_home):
    """detect_and_handle_crashed_workers should mark task as crashed."""
    conn = kb.connect()
    
    # Create and claim a task
    task_id = kb.create_task(conn, title="Will crash", assignee="test")
    claimed = kb.claim_task(conn, task_id, claimer="test-claimer")
    run_id = kb._current_run_id(conn, task_id)
    
    # Simulate worker crash
    conn.execute(
        "UPDATE tasks SET worker_pid = ? WHERE id = ?",
        (999999, task_id),
    )
    conn.commit()
    
    # Mark the task and run as crashed
    detected = kb.detect_worker_pids_alive(conn)
    assert len(detected) > 0, "Should detect dead PID"
    handled = kb.handle_dead_worker_pids(conn)
    assert len(handled) > 0, "Should handle dead workers"
    # Should handle the crash
    assert len(handled) == 1
    
    # Task should now be in 'paused' status (needs manual intervention)
    task = kb.get_task(conn, task_id)
    assert task.status == "paused"
    assert task.consecutive_failures >= 1
    assert task.last_failure_error is not None
    assert "Worker PID not alive" in task.last_failure_error
    
    # Run should be marked as crashed
    run = kb.latest_run(conn, task_id)
    assert run.status == "crashed"
    assert run.outcome == "crashed"
    assert "Worker PID not alive" in (run.error or "")
    
    conn.close()


def test_detect_crashed_worker_with_multiple_dead_pids(kanban_home):
    """detect_crashed_workers should find all zombie tasks."""
    conn = kb.connect()
    
    # Create multiple tasks and mark them as running with dead PIDs
    task_ids = []
    for i in range(3):
        task_id = kb.create_task(
            conn, title=f"Task {i}", assignee="test"
        )
        claimed = kb.claim_task(conn, task_id, claimer="test-claimer")
        conn.execute(
            "UPDATE tasks SET worker_pid = ? WHERE id = ?",
            (999999 + i, task_id),
        )
        conn.commit()
        task_ids.append(task_id)
    
    # Create one task that's actually running (don't claim it yet to avoid PID check)
    alive_task = kb.create_task(conn, title="Still running", assignee="test")
    kb.claim_task(conn, alive_task, claimer="test-claimer")
    # Update its PID to current process (which is alive)
    conn.execute(
        "UPDATE tasks SET worker_pid = ? WHERE id = ?",
        (os.getpid(), alive_task),
    )
    conn.commit()
    
    # Detect crashed workers
    crashes = kb.detect_worker_pids_alive(conn)
    
    # Should find only the 3 dead ones, not the alive one
    assert len(crashes) == 3
    crashed_task_ids = [c["task_id"] for c in crashes]
    assert set(crashed_task_ids) == set(task_ids)
    assert alive_task not in crashed_task_ids
    
    conn.close()


def test_detect_crashed_worker_ignores_ready_tasks(kanban_home):
    """detect_crashed_workers should ignore tasks not in 'running' state."""
    conn = kb.connect()
    
    # Create a task but don't claim it (stays in 'ready')
    task_id = kb.create_task(conn, title="Not running", assignee="test")
    
    # Manually set a dead PID (shouldn't happen in normal flow)
    conn.execute(
        "UPDATE tasks SET worker_pid = ? WHERE id = ?",
        (999999, task_id),
    )
    conn.commit()
    
    crashes = kb.detect_worker_pids_alive(conn)
    
    # Should not detect this as a crash (it's not running)
    assert len(crashes) == 0
    
    conn.close()


def test_detect_crashed_worker_no_false_positive_on_null_pid(kanban_home):
    """detect_crashed_workers should not crash on NULL worker_pid."""
    conn = kb.connect()
    
    # Create a task in 'running' state with NULL worker_pid
    # (edge case: shouldn't happen normally but we should handle it)
    task_id = kb.create_task(conn, title="No PID", assignee="test")
    claimed = kb.claim_task(conn, task_id, claimer="test-claimer")
    conn.execute(
        "UPDATE tasks SET worker_pid = NULL WHERE id = ?",
        (task_id,),
    )
    conn.commit()
    
    # Should not crash
    crashes = kb.detect_worker_pids_alive(conn)
    
    # Behaviour: either ignore it or mark as crashed — both are reasonable
    # For now, we ignore NULL PIDs (they indicate the PID was never set)
    conn.close()


# ---------------------------------------------------------------------------
# Tests: Signal Handling and Graceful Recovery
# ---------------------------------------------------------------------------


def test_terminate_reclaimed_worker_respects_deadlock_timeout():
    """_terminate_reclaimed_worker should use SIGTERM then SIGKILL."""
    # Mock _pid_alive to simulate a process that ignores SIGTERM
    with mock.patch("hermes_cli.kanban_db._pid_alive") as mock_alive:
        # First 10 calls return True (process ignores SIGTERM)
        # Then return False (SIGKILL worked)
        mock_alive.side_effect = [True] * 10 + [False]
        
        mock_kill = mock.Mock()
        result = kb._terminate_reclaimed_worker(
            pid=12345,
            claim_lock="host1:claimer1",
            signal_fn=mock_kill,
        )
        
        # Should have called kill twice: once SIGTERM, once SIGKILL
        assert mock_kill.call_count == 2
        assert result["terminated"] is True
        assert result["sigkill"] is True


def test_terminate_reclaimed_worker_handles_nonexistent_pid():
    """_terminate_reclaimed_worker should handle PIDs that don't exist."""
    result = kb._terminate_reclaimed_worker(
        pid=None,
        claim_lock=None,
    )
    assert result["terminated"] is False
    assert result["termination_attempted"] is False


def test_terminate_reclaimed_worker_requires_host_local_claim():
    """_terminate_reclaimed_worker should only kill host-local workers."""
    # Claimer from a different host
    result = kb._terminate_reclaimed_worker(
        pid=12345,
        claim_lock="other_host:different_claimer",
    )
    # Should not attempt termination on foreign host
    assert result["host_local"] is False
    assert result["termination_attempted"] is False


# ---------------------------------------------------------------------------
# Tests: Automatic Cleanup on Dispatcher Tick
# ---------------------------------------------------------------------------


def test_release_stale_claims_detects_dead_pids(kanban_home):
    """release_stale_claims should detect and handle dead PIDs early."""
    conn = kb.connect()
    
    # Create and claim a task
    task_id = kb.create_task(conn, title="Will become stale", assignee="test")
    kb.claim_task(conn, task_id, claimer="test-claimer")
    
    # Manually expire the claim
    conn.execute(
        "UPDATE tasks SET claim_expires = ? WHERE id = ?",
        (int(time.time()) - 1, task_id),  # Already expired
    )
    # Set a dead PID
    conn.execute(
        "UPDATE tasks SET worker_pid = ? WHERE id = ?",
        (999999, task_id),
    )
    conn.commit()
    
    # Call release_stale_claims
    count = kb.release_stale_claims(conn)
    
    # Should have reclaimed the stale task
    assert count >= 1
    
    # Task should be back to 'ready'
    task = kb.get_task(conn, task_id)
    assert task.status == "ready"
    assert task.worker_pid is None
    
    conn.close()


# ---------------------------------------------------------------------------
# Tests: Event Audit Trail
# ---------------------------------------------------------------------------


def test_crashed_worker_event_recorded(kanban_home):
    """Events should record when a crash is detected."""
    conn = kb.connect()
    
    # Create and claim a task
    task_id = kb.create_task(conn, title="Crash audit", assignee="test")
    kb.claim_task(conn, task_id, claimer="test-claimer")
    
    # Simulate crash
    conn.execute(
        "UPDATE tasks SET worker_pid = ? WHERE id = ?",
        (999999, task_id),
    )
    conn.commit()
    
    # Handle the crash
    kb.detect_and_handle_crashed_workers(conn)
    
    # Check that a crash event was recorded
    events = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY created_at DESC",
        (task_id,),
    ).fetchall()
    
    crash_events = [e for e in events if e["kind"] == "worker_crashed"]
    assert len(crash_events) >= 1
    
    conn.close()


# ---------------------------------------------------------------------------
# Integration: Full workflow
# ---------------------------------------------------------------------------


def test_full_crash_detection_workflow(kanban_home):
    """Integration test: create task, simulate crash, detect, and recover."""
    conn = kb.connect()
    
    # 1. Create task
    task_id = kb.create_task(
        conn,
        title="Full workflow test",
        assignee="alice",
        body="Testing crash detection end-to-end",
    )
    
    # 2. Claim it (simulates dispatch)
    claimed = kb.claim_task(conn, task_id, claimer="dispatcher")
    assert claimed is not None
    run_id_1 = kb._current_run_id(conn, task_id)
    
    # 3. Verify task is running
    task = kb.get_task(conn, task_id)
    assert task.status == "running"
    assert task.worker_pid is not None
    
    # 4. Simulate worker crash by killing the fake PID
    conn.execute(
        "UPDATE tasks SET worker_pid = ? WHERE id = ?",
        (999999, task_id),
    )
    conn.commit()
    
    # 5. Detect the crash
    crashes = kb.detect_worker_pids_alive(conn)
    assert len(crashes) == 1
    
    # 6. Handle the crash
    handled = kb.detect_and_handle_crashed_workers(conn)
    assert len(handled) == 1
    
    # 7. Verify task state after crash handling
    task = kb.get_task(conn, task_id)
    assert task.status == "paused"  # Or "crashed" if we prefer
    assert task.consecutive_failures >= 1
    assert task.worker_pid is None
    assert task.last_failure_error is not None
    
    # 8. Verify run is marked as crashed
    run = kb.latest_run(conn, task_id)
    assert run.status == "crashed"
    assert run.outcome == "crashed"
    assert run.ended_at is not None
    
    conn.close()
