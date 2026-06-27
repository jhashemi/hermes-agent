"""TDD RED PHASE: Tests for UNIFY-IM-002 — Gateway WhatsApp → Framework Event Bridge.

These tests MUST FAIL initially — the framework_bridge built-in hook does not exist yet.

Architecture (from PL-002 design):
- ONE built-in hook registered in gateway/hooks.py:_register_builtin_hooks()
- Subscribes to 3 existing gateway hook events: agent:start, agent:end, on_processing_complete
- Translates them to framework DomainEvents and POSTs to nervous system endpoint
- ZERO changes to whatsapp.py, run.py, or base.py
- Conditional: only active when EXECUTIVE_AGENTS_NERVOUS_URL is set
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── RED-1: FrameworkBridgeHook receives agent:start event ──────────

def test_framework_bridge_imports():
    """RED PHASE: framework_bridge module must exist and export handle function."""
    try:
        from gateway.framework_bridge import handle
        assert callable(handle), "handle must be callable"
    except ImportError as e:
        pytest.fail(f"framework_bridge module not found: {e}")


# ── RED-2: agent:start → gateway.chat_received translation ─────────

@pytest.mark.asyncio
async def test_agent_start_to_chat_received():
    """RED PHASE: hook handler must translate agent:start to gateway.chat_received.

    The handler receives a hook context from gateway hooks.emit("agent:start", ctx)
    and should POST to the framework nervous system endpoint.
    """
    from gateway.framework_bridge import handle

    # Simulate the context that run.py sends on agent:start
    event_type = "agent:start"
    context = {
        "platform": "whatsapp",
        "user_id": "1234567890@c.us",
        "session_id": "test-session-uuid",
        "message": "Hello, Demis! What's the latest on AlphaFold?",
    }

    # Mock the aiohttp POST to framework nervous system
    with patch.dict(os.environ, {"EXECUTIVE_AGENTS_NERVOUS_URL": "http://localhost:8193/nervous/emit"}):
        with patch("gateway.framework_bridge._post_to_nervous", new_callable=AsyncMock) as mock_post:
            result = await handle(event_type, context)

            # Should call _post_to_nervous with gateway.chat_received event
            mock_post.assert_called_once()
            call_args = mock_post.call_args[0]
            assert len(call_args) >= 2
            assert call_args[0] == "gateway.chat_received"  # event type
            payload = call_args[1]
            assert payload["platform"] == "whatsapp"
            assert payload["user_id"] == "1234567890@c.us"
            assert payload["session_id"] == "test-session-uuid"
            assert "Hello, Demis!" in payload["message"]


# ── RED-3: agent:end → gateway.agent_response translation ──────────

@pytest.mark.asyncio
async def test_agent_end_to_agent_response():
    """RED PHASE: hook handler must translate agent:end to gateway.agent_response."""
    from gateway.framework_bridge import handle

    event_type = "agent:end"
    context = {
        "platform": "whatsapp",
        "user_id": "1234567890@c.us",
        "session_id": "test-session-uuid",
        "message": "Hello, Demis!",
        "response": "Hello! AlphaFold 3 just predicted structures for all known proteins.",
    }

    with patch.dict(os.environ, {"EXECUTIVE_AGENTS_NERVOUS_URL": "http://localhost:8193/nervous/emit"}):
        with patch("gateway.framework_bridge._post_to_nervous", new_callable=AsyncMock) as mock_post:
            await handle(event_type, context)

            mock_post.assert_called_once()
            call_args = mock_post.call_args[0]
            assert call_args[0] == "gateway.agent_response"
            payload = call_args[1]
            assert payload["platform"] == "whatsapp"
            assert "AlphaFold 3" in payload["response"]


# ── RED-4: on_processing_complete → gateway.delivery_sent ──────────

@pytest.mark.asyncio
async def test_processing_complete_to_delivery_sent():
    """RED PHASE: on_processing_complete → gateway.delivery_sent."""
    from gateway.framework_bridge import handle

    event_type = "on_processing_complete"
    context = {
        "platform": "whatsapp",
        "target": "1234567890@c.us",
        "success": True,
        "content_length": 200,
    }

    with patch.dict(os.environ, {"EXECUTIVE_AGENTS_NERVOUS_URL": "http://localhost:8193/nervous/emit"}):
        with patch("gateway.framework_bridge._post_to_nervous", new_callable=AsyncMock) as mock_post:
            await handle(event_type, context)

            mock_post.assert_called_once()
            call_args = mock_post.call_args[0]
            assert call_args[0] == "gateway.delivery_sent"
            payload = call_args[1]
            assert payload["success"] is True


# ── RED-5: No-op when nervous URL not configured ──────────────────

@pytest.mark.asyncio
async def test_noop_when_url_not_configured():
    """RED PHASE: handle() must be a no-op when EXECUTIVE_AGENTS_NERVOUS_URL is not set."""
    from gateway.framework_bridge import handle

    # Ensure env var is not set
    with patch.dict(os.environ, {}, clear=True):
        with patch("gateway.framework_bridge._post_to_nervous", new_callable=AsyncMock) as mock_post:
            await handle("agent:start", {"platform": "whatsapp"})

            # Should NOT have called the HTTP POST
            mock_post.assert_not_called()


# ── RED-6: Graceful degradation when framework is down ────────────

@pytest.mark.asyncio
async def test_graceful_degradation_on_framework_down():
    """RED PHASE: If framework nervous endpoint is unreachable, handle must not raise.

    The hook should log a warning and continue — gateway must never be blocked
    by framework availability.
    """
    from gateway.framework_bridge import handle

    with patch.dict(os.environ, {"EXECUTIVE_AGENTS_NERVOUS_URL": "http://localhost:9999"}):
        # The POST will fail because nothing is listening on 9999
        # handle() should catch this and not propagate the exception
        result = await handle("agent:start", {
            "platform": "whatsapp",
            "user_id": "test",
            "session_id": "test",
            "message": "test",
        })

        # Should return None (no-op on failure)
        assert result is None


# ── RED-7: Unknown event types are silently ignored ───────────────

@pytest.mark.asyncio
async def test_unknown_events_ignored():
    """RED PHASE: handle() ignores events it doesn't care about."""
    from gateway.framework_bridge import handle

    with patch.dict(os.environ, {"EXECUTIVE_AGENTS_NERVOUS_URL": "http://localhost:8193/nervous/emit"}):
        with patch("gateway.framework_bridge._post_to_nervous", new_callable=AsyncMock) as mock_post:
            await handle("session:start", {"platform": "whatsapp"})

            # Should not have POSTed — unknown event type
            mock_post.assert_not_called()
