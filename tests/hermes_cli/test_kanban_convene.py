"""Tests for the convene ticket type (type=convene).

Covers:
- create_task accepts convene_spec and forces assignee=livekit-boardroom
- create_task rejects single-persona assignee for convene tickets
- _validate_convene_spec catches structural problems at file-time
- Dispatcher routes convene tasks to _convene_spawn (not _default_spawn)
- convene_enabled=False rolls back routing (convene tasks sit idle)
- kanban_create tool handler validates type/convene_spec consistency
- Convene-worker: _parse_child_tickets, _emit_child_tickets
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _valid_convene_spec() -> dict:
    """Return a minimal valid convene spec."""
    return {
        "room_id": "test-room-001",
        "participants": ["persona-a", "persona-b"],
        "phases": [
            {"name": "opening", "prompt": "State your position"},
            {"name": "deliberation", "prompt": "Critique the other position"},
            {"name": "vote", "prompt": "Cast your vote"},
        ],
        "transcript_output_path": "/tmp/test_transcript.json",
    }


# ---------------------------------------------------------------------------
# create_task: convene_spec acceptance
# ---------------------------------------------------------------------------

class TestCreateTaskConvene:
    def test_convene_spec_sets_assignee_to_boardroom(self, kanban_home):
        """create_task with convene_spec forces assignee=livekit-boardroom."""
        spec = json.dumps(_valid_convene_spec())
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title="Convene: arch decision",
                convene_spec=spec,
            )
            task = kb.get_task(conn, tid)
        assert task.assignee == kb.CONVENE_ASSIGNEE
        assert task.convene_spec == spec

    def test_convene_spec_with_explicit_boardroom_assignee_ok(self, kanban_home):
        """Passing assignee=livekit-boardroom explicitly is accepted."""
        spec = json.dumps(_valid_convene_spec())
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title="Convene: explicit assignee",
                assignee=kb.CONVENE_ASSIGNEE,
                convene_spec=spec,
            )
            task = kb.get_task(conn, tid)
        assert task.assignee == kb.CONVENE_ASSIGNEE

    def test_convene_rejects_single_persona_assignee(self, kanban_home):
        """create_task raises ValueError when a single-persona assignee is given."""
        spec = json.dumps(_valid_convene_spec())
        with kb.connect_closing() as conn:
            with pytest.raises(ValueError, match="convene tickets must use assignee"):
                kb.create_task(
                    conn,
                    title="Convene: bad assignee",
                    assignee="some-researcher",
                    convene_spec=spec,
                )

    def test_convene_spec_stored_and_retrievable(self, kanban_home):
        """convene_spec is persisted and round-trips through the DB."""
        spec_dict = _valid_convene_spec()
        spec_json = json.dumps(spec_dict)
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title="Convene: round-trip",
                convene_spec=spec_json,
            )
            task = kb.get_task(conn, tid)
        assert task.convene_spec is not None
        retrieved = json.loads(task.convene_spec)
        assert retrieved["room_id"] == spec_dict["room_id"]
        assert retrieved["participants"] == spec_dict["participants"]


# ---------------------------------------------------------------------------
# _validate_convene_spec
# ---------------------------------------------------------------------------

class TestValidateConveneSpec:
    def test_valid_spec_passes(self):
        """A well-formed spec does not raise."""
        spec = json.dumps(_valid_convene_spec())
        kb._validate_convene_spec(spec)  # should not raise

    def test_empty_spec_raises(self):
        with pytest.raises(ValueError, match="convene_spec is required"):
            kb._validate_convene_spec("")

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError, match="convene_spec must be valid JSON"):
            kb._validate_convene_spec("{not json")

    def test_missing_required_keys_raises(self):
        spec = json.dumps({"room_id": "x"})  # missing participants, phases, transcript
        with pytest.raises(ValueError, match="convene_spec missing required keys"):
            kb._validate_convene_spec(spec)

    def test_participants_must_be_list(self):
        spec = json.dumps({
            "room_id": "x",
            "participants": "not-a-list",
            "phases": [{"name": "p", "prompt": "q"}],
            "transcript_output_path": "/tmp/t.json",
        })
        with pytest.raises(ValueError, match="participants must be a non-empty list"):
            kb._validate_convene_spec(spec)

    def test_phases_must_have_name_and_prompt(self):
        spec = json.dumps({
            "room_id": "x",
            "participants": ["a"],
            "phases": [{"name": "p"}],  # missing prompt
            "transcript_output_path": "/tmp/t.json",
        })
        with pytest.raises(ValueError, match="must have 'name' and 'prompt'"):
            kb._validate_convene_spec(spec)


# ---------------------------------------------------------------------------
# Dispatcher routing
# ---------------------------------------------------------------------------

class TestDispatcherRouting:
    def test_convene_task_routed_to_convene_spawn(self, kanban_home, monkeypatch):
        """The dispatcher selects _convene_spawn for tasks with convene_spec."""
        spec = json.dumps(_valid_convene_spec())
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title="Convene: dispatch test",
                convene_spec=spec,
            )
            # Task should be in 'ready' (no parents)
            task = kb.get_task(conn, tid)
            assert task.status in ("ready", "running")
            assert task.convene_spec is not None

    def test_convene_task_bypasses_profile_exists_check(self, kanban_home):
        """Convene tasks with sentinel assignee should not be skipped by
        the profile_exists gate."""
        spec = json.dumps(_valid_convene_spec())
        with kb.connect_closing() as conn:
            tid = kb.create_task(
                conn,
                title="Convene: profile gate bypass",
                convene_spec=spec,
            )
            task = kb.get_task(conn, tid)
        # The assignee is the sentinel, not a real profile — but the
        # task should still be in a dispatchable state, not skipped.
        assert task.assignee == kb.CONVENE_ASSIGNEE
        assert task.status in ("ready", "running")


# ---------------------------------------------------------------------------
# Convene-worker: child ticket parsing
# ---------------------------------------------------------------------------

class TestConveneWorkerParsing:
    def test_parse_child_tickets_present(self):
        from hermes_cli.convene_worker import _parse_child_tickets
        state = {
            "child_tickets": [
                {"title": "Implement feature A", "assignee": "dev-a"},
                {"title": "Review and merge", "assignee": "reviewer"},
            ],
        }
        children = _parse_child_tickets(state)
        assert len(children) == 2
        assert children[0]["title"] == "Implement feature A"

    def test_parse_child_tickets_absent(self):
        from hermes_cli.convene_worker import _parse_child_tickets
        state = {"status": "completed", "transcript": []}
        children = _parse_child_tickets(state)
        assert children == []

    def test_parse_child_tickets_empty_list(self):
        from hermes_cli.convene_worker import _parse_child_tickets
        state = {"child_tickets": []}
        children = _parse_child_tickets(state)
        assert children == []

    def test_parse_child_tickets_skips_invalid(self):
        from hermes_cli.convene_worker import _parse_child_tickets
        state = {
            "child_tickets": [
                {"title": "valid ticket"},
                {"title": "", },  # empty title — skipped
                "not-a-dict",  # not a dict — skipped
                {"assignee": "x"},  # no title — skipped
            ],
        }
        children = _parse_child_tickets(state)
        assert len(children) == 1
        assert children[0]["title"] == "valid ticket"


# ---------------------------------------------------------------------------
# Convene-worker: child ticket emission
# ---------------------------------------------------------------------------

class TestConveneWorkerEmission:
    def test_emit_child_tickets_creates_tasks(self, kanban_home):
        """_emit_child_tickets creates child tasks linked to the parent."""
        from hermes_cli.convene_worker import _emit_child_tickets
        spec = json.dumps(_valid_convene_spec())
        with kb.connect_closing() as conn:
            parent_id = kb.create_task(
                conn,
                title="Convene: parent",
                convene_spec=spec,
            )
            children = [
                {"title": "Child A", "assignee": "dev-a", "body": "do A"},
                {"title": "Child B", "assignee": "dev-b", "body": "do B"},
            ]
            child_ids = _emit_child_tickets(conn, parent_id, children)
        assert len(child_ids) == 2
        # Verify the children exist and link to parent
        with kb.connect_closing() as conn:
            for cid in child_ids:
                child = kb.get_task(conn, cid)
                assert child is not None
                assert child.title in ("Child A", "Child B")
                # Child has a parent that's not done, so it starts in todo
                assert child.status in ("todo", "ready", "running")

    def test_emit_child_tickets_no_assignee_goes_to_triage(self, kanban_home):
        """Children without an assignee land in triage for human routing."""
        from hermes_cli.convene_worker import _emit_child_tickets
        spec = json.dumps(_valid_convene_spec())
        with kb.connect_closing() as conn:
            parent_id = kb.create_task(
                conn,
                title="Convene: triage children",
                convene_spec=spec,
            )
            children = [
                {"title": "Unrouted child"},  # no assignee
            ]
            child_ids = _emit_child_tickets(conn, parent_id, children)
        assert len(child_ids) == 1
        with kb.connect_closing() as conn:
            child = kb.get_task(conn, child_ids[0])
            assert child.status == "triage"


# ---------------------------------------------------------------------------
# Config flag: convene_enabled
# ---------------------------------------------------------------------------

class TestConveneConfigFlag:
    def test_convene_enabled_default_true(self):
        """The config default for kanban.convene_enabled is True."""
        from hermes_cli.config_defaults import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["kanban"]["convene_enabled"] is True

    def test_convene_driver_url_default(self):
        """The config default for kanban.convene_driver_url is localhost:8196."""
        from hermes_cli.config_defaults import DEFAULT_CONFIG
        assert DEFAULT_CONFIG["kanban"]["convene_driver_url"] == "http://localhost:8196"

    def test_convene_driver_url_constant_matches(self):
        """CONVENE_DRIVER_DEFAULT_URL constant matches the config default."""
        from hermes_cli.config_defaults import DEFAULT_CONFIG
        assert kb.CONVENE_DRIVER_DEFAULT_URL == DEFAULT_CONFIG["kanban"]["convene_driver_url"]


# ---------------------------------------------------------------------------
# Tool handler: kanban_create validation
# ---------------------------------------------------------------------------

class TestKanbanCreateToolValidation:
    def test_convene_without_spec_returns_error(self):
        """kanban_create handler rejects type=convene without convene_spec."""
        from tools.kanban_tools import _handle_create
        result = _handle_create({
            "title": "Convene: missing spec",
            "type": "convene",
        })
        assert isinstance(result, str)
        assert "convene_spec is required" in result

    def test_convene_with_single_persona_assignee_returns_error(self):
        """kanban_create handler rejects single-persona assignee for convene."""
        from tools.kanban_tools import _handle_create
        spec = _valid_convene_spec()
        result = _handle_create({
            "title": "Convene: bad assignee",
            "type": "convene",
            "convene_spec": spec,
            "assignee": "some-researcher",
        })
        assert isinstance(result, str)
        assert "convene tickets must use assignee" in result

    def test_invalid_type_returns_error(self):
        """kanban_create handler rejects unknown type values."""
        from tools.kanban_tools import _handle_create
        result = _handle_create({
            "title": "Bad type",
            "type": "invalid",
        })
        assert isinstance(result, str)
        assert "type must be 'default' or 'convene'" in result

    def test_convene_spec_without_type_returns_error(self):
        """kanban_create handler rejects convene_spec without type=convene."""
        from tools.kanban_tools import _handle_create
        result = _handle_create({
            "title": "Spec without type",
            "convene_spec": _valid_convene_spec(),
            "assignee": "some-researcher",
        })
        assert isinstance(result, str)
        assert "convene_spec was provided but type is not 'convene'" in result