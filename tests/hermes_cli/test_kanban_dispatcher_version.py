"""Unit tests for VFE-DEPLOY-03: dispatcher version check on claim.

Tests the protocol where:
1. Dispatcher advertises hermes_agent_version in claimed events.
2. kanban_db refuses claims from strictly-older versions.
3. claim_refused_stale_version event emitted with incoming/highest payload.
"""

from __future__ import annotations

import sqlite3
import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_claimed_event_includes_version_when_provided(kanban_home):
    """Claimed events should include hermes_agent_version when provided."""
    conn = kb.connect()
    conn.row_factory = sqlite3.Row

    # Create and promote a task
    task_id = kb.create_task(conn, title="Test", assignee="test-agent")
    kb.promote_task(conn, task_id, actor="test-actor")

    version = "0123456789abcdef0123456789abcdef01234567"
    
    # Claim with version
    claimed = kb.claim_task(conn, task_id, claimer="dispatcher-1", hermes_agent_version=version)
    assert claimed is not None

    # Check event has version
    events = kb.list_events(conn, task_id)
    claimed_event = next((e for e in events if e.kind == "claimed"), None)
    assert claimed_event is not None
    assert claimed_event.payload is not None
    assert claimed_event.payload["hermes_agent_version"] == version
    assert claimed_event.payload["lock"] == "dispatcher-1"

    conn.close()


def test_same_version_both_can_claim(kanban_home):
    """Two dispatchers with same version should both be able to claim tasks."""
    conn = kb.connect()
    conn.row_factory = sqlite3.Row

    # Create two tasks
    task_id_1 = kb.create_task(conn, title="Task 1", assignee="test-agent")
    task_id_2 = kb.create_task(conn, title="Task 2", assignee="test-agent")

    # Set both to ready
    kb.promote_task(conn, task_id_1, actor="test-actor")
    kb.promote_task(conn, task_id_2, actor="test-actor")

    version = "0123456789abcdef0123456789abcdef01234567"

    # First dispatcher claims task_1
    claimed_1 = kb.claim_task(
        conn, task_id_1, claimer="dispatcher-1", hermes_agent_version=version
    )
    assert claimed_1 is not None, "First claim should succeed"

    # Second dispatcher claims task_2 (same version should work)
    claimed_2 = kb.claim_task(
        conn, task_id_2, claimer="dispatcher-2", hermes_agent_version=version
    )
    assert claimed_2 is not None, "Second claim with same version should succeed"

    conn.close()


def test_newer_version_rejects_older_claim(kanban_home):
    """When a newer version has claimed recently, older version claims are refused."""
    conn = kb.connect()
    conn.row_factory = sqlite3.Row

    # Create two tasks
    task_id_1 = kb.create_task(conn, title="Task claimed by newer", assignee="test-agent")
    task_id_2 = kb.create_task(conn, title="Task denied to older", assignee="test-agent")

    # Promote both to ready
    kb.promote_task(conn, task_id_1, actor="test-actor")
    kb.promote_task(conn, task_id_2, actor="test-actor")

    # Use real-ish SHAs where old < new lexicographically
    version_old = "0000000000000000000000000000000000000000"
    version_new = "ffffffffffffffffffffffffffffffffffffffff"

    # Newer dispatcher claims task_1
    claimed_new = kb.claim_task(
        conn, task_id_1, claimer="dispatcher-new", hermes_agent_version=version_new
    )
    assert claimed_new is not None, "Newer version claim should succeed"

    # Verify the event has the newer version recorded
    events_1 = kb.list_events(conn, task_id_1)
    claimed_1 = next((e for e in events_1 if e.kind == "claimed"), None)
    assert claimed_1 and claimed_1.payload and claimed_1.payload.get("hermes_agent_version") == version_new

    # Older dispatcher tries to claim task_2
    # With version check: should be refused because version_new was seen recently
    claimed_old = kb.claim_task(
        conn, task_id_2, claimer="dispatcher-old", hermes_agent_version=version_old
    )

    # Old version should be refused
    assert claimed_old is None, "Older version claim should be refused"

    # Verify claim_refused_stale_version event exists
    events = kb.list_events(conn, task_id_2)
    refused_event = next(
        (e for e in events if e.kind == "claim_refused_stale_version"),
        None,
    )
    assert refused_event is not None, "Should emit claim_refused_stale_version"
    assert refused_event.payload is not None
    assert refused_event.payload["incoming"] == version_old
    assert refused_event.payload["highest"] == version_new

    conn.close()


def test_refusal_window_expires(kanban_home, monkeypatch):
    """After 15 minutes (900 sec), older version can claim even if newer was seen."""
    conn = kb.connect()
    conn.row_factory = sqlite3.Row

    task_id_1 = kb.create_task(conn, title="Task claimed by newer", assignee="test-agent")
    task_id_2 = kb.create_task(conn, title="Task for old after window", assignee="test-agent")

    kb.promote_task(conn, task_id_1, actor="test-actor")
    kb.promote_task(conn, task_id_2, actor="test-actor")

    version_old = "0000000000000000000000000000000000000000"
    version_new = "ffffffffffffffffffffffffffffffffffffffff"

    # Newer version claims at T0
    claimed_new = kb.claim_task(
        conn, task_id_1, claimer="dispatcher-new", hermes_agent_version=version_new
    )
    assert claimed_new is not None

    # Simulate advancing time by 16 minutes
    original_time = time.time
    current_time_ref = [original_time()]
    
    def fake_time():
        return current_time_ref[0] + 960  # 16 minutes later
    
    monkeypatch.setattr("time.time", fake_time)
    monkeypatch.setattr("hermes_cli.kanban_db.time.time", fake_time)

    # Now older version should be able to claim again (refusal window expired)
    claimed_old = kb.claim_task(
        conn, task_id_2, claimer="dispatcher-old", hermes_agent_version=version_old
    )
    assert claimed_old is not None, "After refusal window, older version should claim"

    conn.close()


def test_no_refusal_if_no_newer_version_observed(kanban_home):
    """If no newer version has been seen, older version can claim freely."""
    conn = kb.connect()
    conn.row_factory = sqlite3.Row

    task_id_1 = kb.create_task(conn, title="Task 1", assignee="test-agent")
    task_id_2 = kb.create_task(conn, title="Task 2", assignee="test-agent")
    kb.promote_task(conn, task_id_1, actor="test-actor")
    kb.promote_task(conn, task_id_2, actor="test-actor")

    version_old = "0000000000000000000000000000000000000000"

    # Only old version is active; it should always be able to claim
    claimed_1 = kb.claim_task(
        conn, task_id_1, claimer="dispatcher-old-a", hermes_agent_version=version_old
    )
    assert claimed_1 is not None

    # Release the first task and claim another with same old version
    # (in a real scenario, new task would be ready)
    task_id_3 = kb.create_task(conn, title="Task 3", assignee="test-agent")
    kb.promote_task(conn, task_id_3, actor="test-actor")
    
    claimed_3 = kb.claim_task(
        conn, task_id_3, claimer="dispatcher-old-b", hermes_agent_version=version_old
    )
    assert claimed_3 is not None, "No newer version exists, so old should always work"

    conn.close()


def test_claim_without_version_still_works(kanban_home):
    """Legacy claims without version should still work (no version check)."""
    conn = kb.connect()
    conn.row_factory = sqlite3.Row

    task_id = kb.create_task(conn, title="Legacy task", assignee="test-agent")
    kb.promote_task(conn, task_id, actor="test-actor")

    # Claim without version (should work, no check performed)
    claimed = kb.claim_task(conn, task_id, claimer="dispatcher-legacy")
    assert claimed is not None

    # Event should not have version field
    events = kb.list_events(conn, task_id)
    claimed_event = next((e for e in events if e.kind == "claimed"), None)
    assert claimed_event is not None
    assert "hermes_agent_version" not in (claimed_event.payload or {})

    conn.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
