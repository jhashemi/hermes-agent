"""Tests for t_fc932369: unknown-but-well-formatted instance names return a
helpful error that lists the actually-available instances.

The pre-existing P3-002 tests cover FORMAT rejection (bad chars, length,
leading/trailing hyphens). This suite covers the orthogonal case: name is
well-formatted but not registered in the orchestrator.
"""
import pytest
from unittest.mock import MagicMock, patch

from gateway.agent_commands import handle_switch_instance_command
from gateway.platforms.base import MessageEvent


def _make_event():
    event = MagicMock(spec=MessageEvent)
    event.chat_id = "test_chat"
    event.get_command_args = MagicMock(return_value="")
    return event


def _mock_access_manager():
    mock_mgr = MagicMock()
    mock_mgr.has_access = MagicMock(return_value=True)
    mock_mgr.get_user_id = MagicMock(return_value="user123")
    return mock_mgr


@pytest.mark.asyncio
async def test_unknown_instance_returns_error_listing_available():
    """/switch-<unknown> rejects with a clear message that lists real instances."""
    gateway_runner = MagicMock()
    event = _make_event()

    with patch("gateway.agent_commands.get_access_manager") as mock_access:
        mock_access.return_value = _mock_access_manager()

        # Orchestrator with two registered instances, neither is 'nope'
        mock_orch = MagicMock()
        mock_orch._get_registry = MagicMock(return_value={
            "local": MagicMock(),
            "hermes2": MagicMock(),
        })
        # If pre-check works we should NEVER hit set_current_instance for 'nope'
        mock_orch.set_current_instance = MagicMock(
            side_effect=AssertionError(
                "set_current_instance called for unknown instance — "
                "pre-check should have rejected it"
            )
        )
        gateway_runner._instance_orchestrator = mock_orch

        result = await handle_switch_instance_command(
            gateway_runner, event, "nope"
        )

    result_str = str(result)
    # Rejects the bad name
    assert "nope" in result_str
    assert "not found" in result_str.lower()
    # Lists the real instances so the user can recover
    assert "local" in result_str
    assert "hermes2" in result_str


@pytest.mark.asyncio
async def test_unknown_instance_error_includes_command_hints():
    """Error body should suggest the /switch-<name> form for each available."""
    gateway_runner = MagicMock()
    event = _make_event()

    with patch("gateway.agent_commands.get_access_manager") as mock_access:
        mock_access.return_value = _mock_access_manager()

        mock_orch = MagicMock()
        mock_orch._get_registry = MagicMock(return_value={
            "prod-us-west": MagicMock(),
        })
        mock_orch.set_current_instance = MagicMock(return_value=False)
        gateway_runner._instance_orchestrator = mock_orch

        result = await handle_switch_instance_command(
            gateway_runner, event, "prod-eu"
        )

    result_str = str(result)
    assert "/switch-prod-us-west" in result_str


@pytest.mark.asyncio
async def test_empty_registry_returns_readable_error():
    """When no instances are configured, still return a clean message."""
    gateway_runner = MagicMock()
    event = _make_event()

    with patch("gateway.agent_commands.get_access_manager") as mock_access:
        mock_access.return_value = _mock_access_manager()

        mock_orch = MagicMock()
        mock_orch._get_registry = MagicMock(return_value={})
        gateway_runner._instance_orchestrator = mock_orch

        result = await handle_switch_instance_command(
            gateway_runner, event, "anything"
        )

    result_str = str(result)
    assert "not found" in result_str.lower()
    assert (
        "No instances" in result_str
        or "(none)" in result_str
    )


@pytest.mark.asyncio
async def test_known_instance_still_switches_successfully():
    """Sanity: a valid known instance still succeeds after the pre-check."""
    gateway_runner = MagicMock()
    event = _make_event()

    with patch("gateway.agent_commands.get_access_manager") as mock_access:
        mock_access.return_value = _mock_access_manager()

        instance_obj = MagicMock()
        instance_obj.name = "local"
        instance_obj.is_local = True
        instance_obj.description = "Local instance"

        mock_orch = MagicMock()
        mock_orch._get_registry = MagicMock(return_value={"local": instance_obj})
        mock_orch.set_current_instance = MagicMock(return_value=True)
        mock_orch.get_instance = MagicMock(return_value=instance_obj)
        gateway_runner._instance_orchestrator = mock_orch

        result = await handle_switch_instance_command(
            gateway_runner, event, "local"
        )

    result_str = str(result)
    assert "Switched to" in result_str or "🟢" in result_str
