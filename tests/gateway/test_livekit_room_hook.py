# tests/gateway/test_livekit_room_hook.py
import asyncio
from unittest import mock

import pytest

from gateway.builtin_hooks.livekit_room_hook import (
    parse_voice_command,
    LiveKitRoomController,
)


class TestParseVoiceCommand:
    def test_join(self):
        assert parse_voice_command("/voice join") == ("join", None)

    def test_join_with_voice(self):
        assert parse_voice_command("/voice join demis") == ("join", "demis")

    def test_leave(self):
        assert parse_voice_command("/voice leave") == ("leave", None)

    def test_status(self):
        assert parse_voice_command("/voice status") == ("status", None)

    def test_unknown(self):
        assert parse_voice_command("/voice frobnicate") == ("unknown", "frobnicate")

    def test_not_a_voice_command(self):
        assert parse_voice_command("hello world") is None
        assert parse_voice_command("/skills list") is None


@pytest.mark.asyncio
async def test_join_spawns_sidecar(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://x.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIabc")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret-must-be-long-enough-for-hs256-32b")

    fake_proc = mock.MagicMock()
    fake_proc.pid = 12345
    fake_proc.returncode = None

    with mock.patch(
        "asyncio.create_subprocess_exec",
        new=mock.AsyncMock(return_value=fake_proc),
    ) as spawn, mock.patch(
        "tools.livekit_room_manager.ensure_room", new=mock.AsyncMock()
    ):
        ctrl = LiveKitRoomController()
        result = await ctrl.join(chat_id="445462521", voice="default")

    assert result["pid"] == 12345
    assert result["room"].startswith("hermes-")
    spawn.assert_called_once()


@pytest.mark.asyncio
async def test_leave_kills_sidecar():
    fake_proc = mock.MagicMock()
    fake_proc.pid = 12345
    fake_proc.returncode = None
    fake_proc.terminate = mock.MagicMock()
    fake_proc.wait = mock.AsyncMock()

    ctrl = LiveKitRoomController()
    ctrl._procs["445462521"] = fake_proc

    result = await ctrl.leave(chat_id="445462521")

    assert result["status"] == "left"
    fake_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_join_idempotent_when_already_joined():
    fake_proc = mock.MagicMock()
    fake_proc.pid = 99999
    fake_proc.returncode = None  # still running

    ctrl = LiveKitRoomController()
    ctrl._procs["445462521"] = fake_proc

    result = await ctrl.join(chat_id="445462521", voice="default")
    assert result["pid"] == 99999
    assert result.get("already_joined") is True


@pytest.mark.asyncio
async def test_status_reports_active_room():
    fake_proc = mock.MagicMock()
    fake_proc.pid = 555
    fake_proc.returncode = None

    ctrl = LiveKitRoomController()
    ctrl._procs["445462521"] = fake_proc

    result = await ctrl.status(chat_id="445462521")
    assert result["active"] is True
    assert result["pid"] == 555

    other = await ctrl.status(chat_id="999")
    assert other["active"] is False


@pytest.mark.asyncio
async def test_hook_returns_config_error_when_env_missing(monkeypatch):
    """When /voice join arrives but LIVEKIT_URL etc. are unset, the hook
    must return a 'LiveKit not configured' string (not raise).
    """
    from gateway.builtin_hooks.livekit_room_hook import LiveKitRoomHook

    for var in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(var, raising=False)

    fake_event = mock.MagicMock()
    fake_event.text_content = "/voice join"
    fake_event.chat_id = "445462521"

    hook = LiveKitRoomHook()
    result = await hook.before_message_processing(fake_event, gateway_runner=None)

    assert isinstance(result, str)
    assert result.startswith("LiveKit not configured"), result


@pytest.mark.asyncio
async def test_hook_passes_through_non_voice_messages():
    """Plain user messages must NOT be intercepted (return None = pass through)."""
    from gateway.builtin_hooks.livekit_room_hook import LiveKitRoomHook

    fake_event = mock.MagicMock()
    fake_event.text_content = "hello world"
    fake_event.chat_id = "445462521"

    hook = LiveKitRoomHook()
    result = await hook.before_message_processing(fake_event, gateway_runner=None)
    assert result is None
