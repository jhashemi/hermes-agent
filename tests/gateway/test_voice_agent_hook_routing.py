"""Tests for the voice agent hook routing.

The voice agent hook MUST proxy /load-* (and /load_*) commands through the
voice bridge service (port 8193) on every invocation — including the very
first one — so the executive AgentContainer is spawned in the bridge
process, not just have a system-prompt swap in persona_manager.

Regression: the previous behaviour gated /load-* on
`self._sessions.has_session(user_id, platform)` which meant first-time loads
silently fell through to agent_commands.py → persona_manager (system-prompt
only, no AgentContainer). That made /load-demis a costume change, not an
actor swap.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

# Re-use the discord mock helper so the gateway.platforms imports succeed.
from tests.gateway.test_voice_command import _ensure_discord_mock  # noqa: F401

from gateway.platforms.base import MessageEvent, MessageType, SessionSource
from gateway.builtin_hooks.voice_agent_hook import VoiceAgentMessageInterceptor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(text: str, *, platform: str = "telegram", user_id: str = "u1") -> MessageEvent:
    """Build a minimal MessageEvent for hook testing."""
    source = SessionSource(
        platform=platform,
        chat_id=f"chat-{user_id}",
        user_id=user_id,
    )
    return MessageEvent(
        source=source,
        text=text,
        message_type=MessageType.TEXT,
    )


def _make_interceptor() -> VoiceAgentMessageInterceptor:
    """Build a hook with all I/O stubbed."""
    hook = VoiceAgentMessageInterceptor()
    # Stub the bridge HTTP client
    hook._bridge.post = AsyncMock(return_value={
        "session_id": "vs_test_123",
        "message": "✅ Loaded demis_hassabis",
    })
    hook._bridge.get = AsyncMock(return_value={"agents": [
        {"id": "demis_hassabis", "name": "Demis Hassabis", "has_voice": True},
    ]})
    hook._bridge.health = AsyncMock(return_value={"status": "ok"})
    # Pretend the bridge is healthy without round-tripping
    hook._bridge_healthy = True
    return hook


def _stub_access(monkeypatch, user_id: str = "u1") -> None:
    """Stub get_access_manager so has_access(event) returns True."""
    fake_mgr = MagicMock()
    fake_mgr.has_access.return_value = True
    fake_mgr.get_user_id.return_value = user_id
    import gateway.builtin_hooks.voice_agent_hook as mod
    monkeypatch.setattr(mod, "get_access_manager", lambda: fake_mgr)


# ---------------------------------------------------------------------------
# Regression: first-time /load-* must hit the bridge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_time_load_dash_routes_to_bridge(monkeypatch):
    """/load-demis on a fresh user MUST POST to bridge /load (no has_session gate)."""
    _stub_access(monkeypatch)
    hook = _make_interceptor()
    event = _make_event("/load-demis")

    # Pre-condition: user has no session
    assert not hook._sessions.has_session("u1", "telegram")

    result = await hook.before_message_processing(event, gateway_runner=None)

    hook._bridge.post.assert_awaited_once_with("/load", {
        "agent_id": "demis",
        "user_id": "u1",
        "platform": "telegram",
    })
    # Session must be cached so follow-up text routes to /chat
    assert hook._sessions.has_session("u1", "telegram")
    assert "Loaded" in result or "✅" in result


@pytest.mark.asyncio
async def test_first_time_load_underscore_routes_to_bridge(monkeypatch):
    """/load_demis (Telegram dash→underscore autocorrect) MUST hit the same path."""
    _stub_access(monkeypatch)
    hook = _make_interceptor()
    event = _make_event("/load_demis")

    result = await hook.before_message_processing(event, gateway_runner=None)

    hook._bridge.post.assert_awaited_once()
    args, _ = hook._bridge.post.call_args
    assert args[0] == "/load"
    assert args[1]["agent_id"] == "demis"
    assert hook._sessions.has_session("u1", "telegram")
    assert result is not None


@pytest.mark.asyncio
async def test_load_voice_twin_hyphenated(monkeypatch):
    """/load-demis-hassabis preserves the inner hyphen as part of the agent id."""
    _stub_access(monkeypatch)
    hook = _make_interceptor()
    event = _make_event("/load-demis-hassabis")

    await hook.before_message_processing(event, gateway_runner=None)

    args, _ = hook._bridge.post.call_args
    assert args[1]["agent_id"] == "demis-hassabis"


# ---------------------------------------------------------------------------
# Text-when-connected routes to /chat
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_text_after_load_routes_to_bridge_chat(monkeypatch):
    """Plain text from a user who already loaded an agent MUST proxy to /chat."""
    _stub_access(monkeypatch)
    hook = _make_interceptor()
    hook._sessions.set("u1", "telegram", {
        "session_id": "vs_test_123",
        "agent_id": "demis_hassabis",
        "chat_id": "chat-u1",
    })
    hook._bridge.post = AsyncMock(return_value={"response": "Hello from Demis"})
    event = _make_event("how do you think about AGI safety?")

    result = await hook.before_message_processing(event, gateway_runner=None)

    hook._bridge.post.assert_awaited_once()
    args, _ = hook._bridge.post.call_args
    assert args[0] == "/chat"
    assert args[1]["agent_id"] == "demis_hassabis"
    assert args[1]["user_id"] == "u1"
    assert args[1]["message"] == "how do you think about AGI safety?"
    assert result == "Hello from Demis"


@pytest.mark.asyncio
async def test_text_without_session_passes_through(monkeypatch):
    """Plain text from a user with NO session must NOT touch the bridge."""
    _stub_access(monkeypatch)
    hook = _make_interceptor()
    event = _make_event("hello there")

    result = await hook.before_message_processing(event, gateway_runner=None)

    hook._bridge.post.assert_not_awaited()
    assert result is None  # falls through to default Hermes agent


@pytest.mark.asyncio
async def test_slash_command_with_session_does_not_intercept_text(monkeypatch):
    """A slash command (other than the recognized voice ones) while connected
    must NOT be re-routed to /chat — it should pass through to the normal
    command pipeline so /memo, /todo, etc. still work."""
    _stub_access(monkeypatch)
    hook = _make_interceptor()
    hook._sessions.set("u1", "telegram", {
        "session_id": "vs_test_123",
        "agent_id": "demis_hassabis",
    })
    event = _make_event("/help")

    result = await hook.before_message_processing(event, gateway_runner=None)

    hook._bridge.post.assert_not_awaited()
    assert result is None


# ---------------------------------------------------------------------------
# Cross-platform parity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform", ["telegram", "whatsapp", "discord", "signal"])
@pytest.mark.asyncio
async def test_load_routes_through_bridge_on_every_platform(monkeypatch, platform):
    """Channel-agnostic invariant: every platform routes /load- through the bridge."""
    _stub_access(monkeypatch)
    hook = _make_interceptor()
    event = _make_event("/load-demis", platform=platform)

    await hook.before_message_processing(event, gateway_runner=None)

    args, _ = hook._bridge.post.call_args
    assert args[1]["platform"] == platform


# ---------------------------------------------------------------------------
# Disconnect parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_voice_disconnect_underscore_alias(monkeypatch):
    """/voice_disconnect (underscore) and /voice-disconnect (dash) both work."""
    _stub_access(monkeypatch)
    hook = _make_interceptor()
    hook._sessions.set("u1", "telegram", {
        "session_id": "vs_test_123",
        "agent_id": "demis_hassabis",
    })
    event = _make_event("/voice_disconnect")
    hook._bridge.post = AsyncMock(return_value={"ok": True})

    result = await hook.before_message_processing(event, gateway_runner=None)

    args, _ = hook._bridge.post.call_args
    assert args[0] == "/disconnect"
    assert "Disconnected" in result
    assert not hook._sessions.has_session("u1", "telegram")
