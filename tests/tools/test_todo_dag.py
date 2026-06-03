"""Tests for the task DAG: blocks/blocked_by edges + active_form + blocker resolution."""

import json

from tools.todo_tool import TodoStore, todo_tool


class TestTaskDAGFields:
    """Tests for blocks, blocked_by edges and active_form field (Phase 2)."""

    def test_write_preserves_blocks_field(self):
        """items can carry a 'blocks' list of task IDs they block."""
        store = TodoStore()
        result = store.write([
            {"id": "1", "content": "Blocked task", "status": "pending",
             "blocks": ["2", "3"]},
        ])
        assert result[0]["blocks"] == ["2", "3"]

    def test_write_preserves_blocked_by_field(self):
        """items can carry a 'blocked_by' list of task IDs blocking them."""
        store = TodoStore()
        result = store.write([
            {"id": "2", "content": "Waiting for #1", "status": "pending",
             "blocked_by": ["1"]},
        ])
        assert result[0]["blocked_by"] == ["1"]

    def test_write_preserves_active_form(self):
        """items can carry an 'active_form' present-continuous label."""
        store = TodoStore()
        result = store.write([
            {"id": "1", "content": "Running diagnostics", "status": "in_progress",
             "active_form": "Diagnosing NATS cluster"},
        ])
        assert result[0]["active_form"] == "Diagnosing NATS cluster"

    def test_blocks_and_blocked_by_are_optional(self):
        """Existing code without these fields continues to work."""
        store = TodoStore()
        result = store.write([
            {"id": "1", "content": "Plain task", "status": "pending"},
        ])
        assert "blocks" in result[0]  # defaults to empty
        assert "blocked_by" in result[0]  # defaults to empty
        assert "active_form" in result[0]  # defaults to empty

    def test_merge_preserves_blocks_and_blocked_by(self):
        """Merge mode keeps blocks/blocked_by from existing items."""
        store = TodoStore()
        store.write([
            {"id": "1", "content": "Root", "status": "in_progress",
             "blocks": ["2"]},
            {"id": "2", "content": "Dependent", "status": "pending",
             "blocked_by": ["1"]},
        ])
        store.write(
            [{"id": "2", "status": "in_progress",
              "active_form": "Running dependent task"}],
            merge=True,
        )
        items = store.read()
        item2 = [i for i in items if i["id"] == "2"][0]
        assert item2["status"] == "in_progress"
        assert item2["blocked_by"] == ["1"]  # preserved from original
        assert item2["active_form"] == "Running dependent task"


class TestBlockedStatusResolution:
    """The tool response shows blocking relationships in the task list."""

    def test_todo_tool_response_includes_blocked_info(self):
        store = TodoStore()
        store.write([
            {"id": "1", "content": "Foundation", "status": "completed"},
            {"id": "2", "content": "Middle", "status": "in_progress",
             "blocks": ["3"]},
            {"id": "3", "content": "Final", "status": "pending",
             "blocked_by": ["2"]},
        ])
        result = json.loads(todo_tool(store=store))
        # Completed blockers should not block
        final = [t for t in result["todos"] if t["id"] == "3"][0]
        assert final["blocked_by"] == ["2"]  # field present
        # #2 is in_progress (NOT completed) so #3 is effectively blocked
        assert result["blocked_tasks"] == ["3"]

    def test_completed_blocker_unblocks_dependent(self):
        store = TodoStore()
        store.write([
            {"id": "1", "content": "Base", "status": "completed"},
            {"id": "2", "content": "Target", "status": "pending",
             "blocked_by": ["1"]},
        ])
        result = json.loads(todo_tool(store=store))
        # #1 is completed so #2 is NOT blocked
        assert "2" not in result.get("blocked_tasks", [])


class TestFormatForInjectionDAG:
    def test_blocked_items_shown_with_blocker_info(self):
        store = TodoStore()
        store.write([
            {"id": "1", "content": "Prereq", "status": "in_progress",
             "blocks": ["2"]},
            {"id": "2", "content": "After prereq", "status": "pending",
             "blocked_by": ["1"]},
        ])
        text = store.format_for_injection()
        assert "blocked by" in text.lower()
        # #1 blocks #2 so the blocked_by info surfaces
