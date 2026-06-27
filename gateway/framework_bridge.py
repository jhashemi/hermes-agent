"""Framework Bridge — Gateway built-in hook for executive agents nervous system.

Translates the gateway's lifecycle hook events into DomainEvent emissions
that are POSTed to the executive agents framework nervous system endpoint.

Architecture (from PL-002 design):
  - ONE built-in hook registered in gateway/hooks.py:_register_builtin_hooks()
  - Subscribes to 3 existing gateway hook events:
      agent:start           → gateway.chat_received
      agent:end             → gateway.agent_response
      on_processing_complete → gateway.delivery_sent
  - Conditional: only active when EXECUTIVE_AGENTS_NERVOUS_URL is set
  - ZERO changes to whatsapp.py, run.py, or base.py
  - Fire-and-forget: errors are logged but never block the gateway pipeline

Environment:
  EXECUTIVE_AGENTS_NERVOUS_URL  — http://localhost:8193/nervous/emit
                                   When unset, the hook is a silent no-op.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional
from uuid import uuid4

logger = logging.getLogger(__name__)


def _get_nervous_url() -> str:
    """Return the framework nervous system endpoint URL, or empty string if not configured."""
    return os.environ.get("EXECUTIVE_AGENTS_NERVOUS_URL", "")


async def handle(event_type: str, context: Optional[Dict[str, Any]] = None) -> Optional[bool]:
    """Gateway hook handler — translate lifecycle events to framework DomainEvents.

    This function signature matches the gateway hook protocol:
    handle(event_type: str, context: dict) → Any

    Args:
        event_type: Gateway hook event (e.g. "agent:start").
        context:    Event context dict from the gateway emitter.

    Returns:
        True if event was successfully forwarded, None if skipped or failed.
    """
    nervous_url = _get_nervous_url()
    if not nervous_url:
        return None

    if context is None:
        context = {}

    # ── Route event types ──────────────────────────────────────
    if event_type == "agent:start":
        payload = _build_chat_received(context)
        result = await _post_to_nervous("gateway.chat_received", payload)
        return result

    elif event_type == "agent:end":
        payload = _build_agent_response(context)
        result = await _post_to_nervous("gateway.agent_response", payload)
        return result

    elif event_type == "on_processing_complete":
        payload = _build_delivery_sent(context)
        result = await _post_to_nervous("gateway.delivery_sent", payload)
        return result

    else:
        # Unknown event type — silently ignore
        return None

    return None


# ── Payload builders ──────────────────────────────────────────

def _build_chat_received(context: Dict[str, Any]) -> Dict[str, Any]:
    """Build gateway.chat_received payload from agent:start context."""
    return {
        "source": context.get("platform", ""),
        "platform": context.get("platform", ""),
        "user_id": context.get("user_id", ""),
        "user_name": context.get("user_name"),
        "chat_id": context.get("chat_id", ""),
        "chat_type": context.get("chat_type", "dm"),
        "chat_name": context.get("chat_name"),
        "message": context.get("message", ""),
        "message_id": context.get("message_id"),
        "message_type": context.get("message_type", "text"),
        "has_media": context.get("has_media", False),
        "media_count": context.get("media_count", 0),
        "timestamp": context.get("timestamp", _now_iso()),
        "session_key": context.get("session_key"),
        "session_id": context.get("session_id", ""),
        "is_new_session": context.get("is_new_session", False),
        "agent_id": "hermes_gateway",
        "correlation_id": context.get("message_id"),
    }


def _build_agent_response(context: Dict[str, Any]) -> Dict[str, Any]:
    """Build gateway.agent_response payload from agent:end context."""
    return {
        "source": context.get("platform", ""),
        "platform": context.get("platform", ""),
        "user_id": context.get("user_id", ""),
        "session_id": context.get("session_id", ""),
        "message": context.get("message", ""),
        "response": context.get("response", ""),
        "response_length": len(context.get("response", "")),
        "timestamp": _now_iso(),
        "agent_id": "hermes_gateway",
        "correlation_id": context.get("message_id"),
    }


def _build_delivery_sent(context: Dict[str, Any]) -> Dict[str, Any]:
    """Build gateway.delivery_sent payload from on_processing_complete context."""
    return {
        "platform": context.get("platform", ""),
        "target": context.get("target", ""),
        "success": context.get("success", False),
        "content_length": context.get("content_length", 0),
        "timestamp": _now_iso(),
        "agent_id": "hermes_gateway",
    }


# ── HTTP transport ────────────────────────────────────────────

async def _post_to_nervous(event_type: str, payload: Dict[str, Any]) -> Optional[bool]:
    """POST a DomainEvent to the framework nervous system endpoint.

    Fire-and-forget: errors are logged but never raised.
    The gateway must never block on framework availability.

    Args:
        event_type: Framework DomainEvent type (e.g. "gateway.chat_received").
        payload:    Event payload dict.

    Returns:
        True on success, None on any failure.
    """
    nervous_url = _get_nervous_url()
    if not nervous_url:
        return None

    try:
        import aiohttp

        payload["event_type"] = event_type
        payload["event_id"] = str(uuid4())
        payload["timestamp"] = payload.get("timestamp", _now_iso())

        timeout = aiohttp.ClientTimeout(total=3.0)  # 3-second timeout
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(nervous_url, json=payload) as resp:
                if resp.status < 400:
                    logger.debug("Framework bridge: %s → %s OK", event_type, nervous_url)
                    return True
                else:
                    logger.warning(
                        "Framework bridge: %s → %s returned %d",
                        event_type, nervous_url, resp.status,
                    )
                    return None

    except ImportError:
        logger.warning("Framework bridge: aiohttp not available — events not forwarded")
        return None
    except Exception as exc:
        logger.warning(
            "Framework bridge: %s → %s failed: %s",
            event_type, nervous_url, exc,
        )
        return None


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()
