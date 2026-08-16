"""Tests for typed handoff fields on kanban tasks (waiting_for, waiting_for_commit, etc).

Covers NERVE-03: soft-refusal on waiting_for when the upstream task is already done.
"""

import pytest
from datetime import datetime, timedelta
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_db(_isolate_hermes_home):
    """Fixture: fresh kanban board in isolated temp home."""
    kb.init_db()
    conn = kb.connect()
    yield conn
    conn.close()


def test_block_task_waiting_for_not_found(kanban_db):
    """Test: block_task soft-refusal when waiting_for task doesn't exist."""
    conn = kanban_db
    
    # Create a task to block
    tid = kb.create_task(
        conn,
        title="Task A",
        body="Test task",
        assignee="worker",
    )
    
    # Try to block with non-existent waiting_for
    result = kb.block_task(
        conn, tid,
        reason="waiting for upstream",
        kind="dependency",
        waiting_for="t_does_not_exist",
    )
    
    # Should return refusal dict
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["code"] == "waiting_for_not_found"
    assert "doesn't exist" in result["message"]
    assert "t_does_not_exist" in result["message"]
    
    # Task should NOT be blocked
    task = kb.get_task(conn, tid)
    assert task.status == "ready"


def test_block_task_waiting_for_already_done(kanban_db):
    """Test: block_task soft-refusal when waiting_for task is already done."""
    conn = kanban_db
    
    # Create two tasks: upstream (will complete) and current
    upstream_id = kb.create_task(
        conn,
        title="Upstream Task",
        body="The dependency",
        assignee="worker",
    )
    
    current_id = kb.create_task(
        conn,
        title="Current Task",
        body="Blocked on upstream",
        assignee="worker",
    )
    
    # Complete the upstream task
    kb.complete_task(
        conn, upstream_id,
        result="Upstream work done",
    )
    
    # Verify upstream is done
    upstream = kb.get_task(conn, upstream_id)
    assert upstream.status == "done"
    
    # Try to block current task waiting on completed upstream
    result = kb.block_task(
        conn, current_id,
        reason="waiting for upstream to finish",
        kind="dependency",
        waiting_for=upstream_id,
    )
    
    # Should return refusal dict
    assert isinstance(result, dict)
    assert result["ok"] is False
    assert result["code"] == "waiting_for_already_done"
    assert "completed at" in result["message"]
    assert upstream_id in result["message"]
    assert "Upstream Task" in result["message"]
    assert "upstream_summary" in result
    # upstream_summary is the result/summary field value
    
    # Current task should NOT be blocked
    current = kb.get_task(conn, current_id)
    assert current.status == "ready"


def test_block_task_waiting_for_running_succeeds(kanban_db):
    """Test: block_task succeeds when waiting_for task is in a non-done status."""
    conn = kanban_db
    
    # Create two tasks
    upstream_id = kb.create_task(
        conn,
        title="Upstream Task",
        body="The dependency",
        assignee="worker",
    )
    
    current_id = kb.create_task(
        conn,
        title="Current Task",
        body="Blocked on upstream",
        assignee="worker",
    )
    
    # upstream_id starts in 'todo' status. Block current task waiting on it — should succeed
    result = kb.block_task(
        conn, current_id,
        reason="waiting for upstream to finish",
        kind="dependency",
        waiting_for=upstream_id,
    )
    
    # Should succeed (return True)
    assert result is True
    
    # Current task should be blocked (dependency kind → todo, actually)
    current = kb.get_task(conn, current_id)
    assert current.status == "todo"  # dependency kind routes to todo, not blocked


def test_block_task_waiting_for_force_bypass(kanban_db):
    """Test: block_task with force=True bypasses the soft-refusal check."""
    conn = kanban_db
    
    # Create two tasks
    upstream_id = kb.create_task(
        conn,
        title="Upstream Task",
        body="The dependency",
        assignee="worker",
    )
    
    current_id = kb.create_task(
        conn,
        title="Current Task",
        body="Blocked on upstream",
        assignee="worker",
    )
    
    # Complete the upstream task
    kb.complete_task(
        conn, upstream_id,
        summary="Upstream work done",
    )
    
    # Block current task with force=True — should succeed despite upstream being done
    result = kb.block_task(
        conn, current_id,
        reason="force blocking even though upstream is done",
        kind="needs_input",
        waiting_for=upstream_id,
        force=True,
    )
    
    # Should succeed
    assert result is True
    
    # Current task should be blocked
    current = kb.get_task(conn, current_id)
    assert current.status == "blocked"


def test_block_task_waiting_for_none_skips_check(kanban_db):
    """Test: block_task skips waiting_for check when waiting_for is None."""
    conn = kanban_db
    
    # Create a task
    tid = kb.create_task(
        conn,
        title="Task",
        body="Test task",
        assignee="worker",
    )
    
    # Block without waiting_for — should succeed
    result = kb.block_task(
        conn, tid,
        reason="user input needed",
        kind="needs_input",
        waiting_for=None,  # explicitly None
    )
    
    # Should succeed
    assert result is True
    
    # Task should be blocked
    task = kb.get_task(conn, tid)
    assert task.status == "blocked"


def test_block_task_waiting_for_other_statuses_succeed(kanban_db):
    """Test: block_task succeeds when waiting_for is in todo/ready/blocked (not done)."""
    conn = kanban_db
    
    for status in ["todo", "blocked"]:
        # Create two tasks
        upstream_id = kb.create_task(
            conn,
            title=f"Upstream Task ({status})",
            body="The dependency",
            assignee="worker",
        )
        
        current_id = kb.create_task(
            conn,
            title=f"Current Task (waiting on {status})",
            body="Blocked on upstream",
            assignee="worker",
        )
        
        # Move upstream to the target status (it starts as 'todo')
        if status == "blocked":
            # Block the upstream task
            kb.block_task(
                conn, upstream_id,
                reason="independently blocked",
                kind="needs_input",
            )
        
        # Block current task waiting on upstream in target status
        result = kb.block_task(
            conn, current_id,
            reason=f"waiting for upstream ({status})",
            kind="dependency",
            waiting_for=upstream_id,
        )
        
        # Should succeed
        assert result is True, f"Failed for upstream status={status}"
        
        # Current task should be in todo (dependency routing)
        current = kb.get_task(conn, current_id)
        assert current.status == "todo"


def test_block_task_stores_waiting_for_in_event(kanban_db):
    """Test: block_task stores waiting_for in the dependency_wait event."""
    conn = kanban_db
    
    # Create two tasks
    upstream_id = kb.create_task(
        conn,
        title="Upstream Task",
        body="The dependency",
        assignee="worker",
    )
    
    current_id = kb.create_task(
        conn,
        title="Current Task",
        body="Blocked on upstream",
        assignee="worker",
    )
    
    # Block current task with waiting_for (upstream is in 'todo' status)
    result = kb.block_task(
        conn, current_id,
        reason="waiting for upstream",
        kind="dependency",
        waiting_for=upstream_id,
        waiting_for_condition="upstream reaches 95% progress",
    )
    
    # Should succeed
    assert result is True
    
    # Verify the event was recorded with typed fields
    events = kb.list_events(conn, current_id)
    dependency_wait_events = [e for e in events if e.kind == "dependency_wait"]
    assert len(dependency_wait_events) > 0
    
    event_payload = dependency_wait_events[0].payload
    assert event_payload.get("waiting_for") == upstream_id
    assert event_payload.get("waiting_for_condition") == "upstream reaches 95% progress"


def test_block_task_all_typed_fields_preserved(kanban_db):
    """Test: all typed handoff fields are preserved in events."""
    conn = kanban_db
    
    # Create two tasks
    upstream_id = kb.create_task(
        conn,
        title="Upstream Task",
        body="The dependency",
        assignee="worker",
    )
    
    current_id = kb.create_task(
        conn,
        title="Current Task",
        body="Blocked on upstream",
        assignee="worker",
    )
    
    # Block with all typed fields
    result = kb.block_task(
        conn, current_id,
        reason="waiting for upstream",
        kind="dependency",
        waiting_for=upstream_id,
        waiting_for_commit="abc123def456",
        waiting_for_event="merge_completed",
        waiting_for_condition="all CI checks pass",
    )
    
    # Should succeed
    assert result is True
    
    # Verify all fields are in the event
    events = kb.list_events(conn, current_id)
    dependency_wait_events = [e for e in events if e.kind == "dependency_wait"]
    assert len(dependency_wait_events) > 0
    
    event_payload = dependency_wait_events[0].payload
    assert event_payload["waiting_for"] == upstream_id
    assert event_payload["waiting_for_commit"] == "abc123def456"
    assert event_payload["waiting_for_event"] == "merge_completed"
    assert event_payload["waiting_for_condition"] == "all CI checks pass"
