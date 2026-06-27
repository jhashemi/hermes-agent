"""Thin JetStream pub/sub helper for the voice<->gateway bridge.

Subjects (per ADR-008):
  voice_bridge.context.<room>      — snapshot push at /voice-connect
  voice_bridge.gateway_out.<room>  — gateway → voice (user/assistant turns)
  voice_bridge.voice_out.<room>    — voice → gateway (analog-text typer)
  voice_bridge.mode.<room>         — FSM transition audit

This module is intentionally side-effect free at import time. The plugin
calls `start_async()` once on the running gateway event loop. If NATS is
unreachable, `_jet` stays None and publish/subscribe become no-ops.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger("voice_agents_plugin.jetstream")

NATS_URL = "nats://localhost:4222"
STREAM_NAME = "VOICE_BRIDGE"
SUBJECTS = ["voice_bridge.>"]


class JetStreamBridge:
    """Lazy, fire-and-forget JetStream client for voice-bridge subjects."""

    def __init__(self) -> None:
        self._nc = None
        self._js = None
        self._subs: list = []
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    async def start(self) -> None:
        if self._connected:
            return
        try:
            import nats as _nats_mod  # type: ignore
            self._nc = await _nats_mod.connect(NATS_URL, name="voice-agents-plugin")
            self._js = self._nc.jetstream()
            try:
                await self._js.stream_info(STREAM_NAME)
            except Exception:
                from nats.js.api import RetentionPolicy, StreamConfig  # type: ignore
                await self._js.add_stream(StreamConfig(
                    name=STREAM_NAME,
                    subjects=SUBJECTS,
                    retention=RetentionPolicy.LIMITS,
                    max_age=3600,
                    max_msgs=10_000,
                    duplicate_window=120,
                ))
            self._loop = asyncio.get_running_loop()
            self._connected = True
            logger.info("[voice-agents.jet] JetStream connected — subjects=%s", SUBJECTS)
        except Exception as exc:
            logger.warning("[voice-agents.jet] JetStream unavailable: %s", exc)

    def publish_sync(self, subject: str, payload: dict) -> None:
        """Schedule async publish from a sync caller (the gateway hook).

        Drops silently if not connected — this is fire-and-forget audit.
        """
        if not self._connected or self._js is None or self._loop is None:
            return
        body = json.dumps(payload, default=str).encode("utf-8")
        js = self._js  # capture for closure (mypy/pyright)

        async def _do() -> None:
            try:
                await js.publish(subject, body)
            except Exception as exc:
                logger.debug("[voice-agents.jet] publish %s failed: %s", subject, exc)

        try:
            asyncio.run_coroutine_threadsafe(_do(), self._loop)
        except Exception as exc:
            logger.debug("[voice-agents.jet] schedule failed: %s", exc)

    async def subscribe(self, subject: str, handler: Callable[[dict], Any]) -> None:
        """Subscribe to a wildcard subject; invoke handler(payload_dict) per msg."""
        if not self._connected or self._js is None:
            return

        async def _cb(msg) -> None:
            try:
                payload = json.loads(msg.data.decode("utf-8"))
            except Exception:
                await msg.ack()
                return
            try:
                res = handler(payload)
                if asyncio.iscoroutine(res):
                    await res
            except Exception as exc:
                logger.warning("[voice-agents.jet] handler error %s: %s", subject, exc)
            await msg.ack()

        sub = await self._js.subscribe(subject, cb=_cb)
        self._subs.append(sub)
        logger.info("[voice-agents.jet] subscribed: %s", subject)

    async def close(self) -> None:
        for s in self._subs:
            try:
                await s.unsubscribe()
            except Exception:
                pass
        if self._nc is not None:
            try:
                await self._nc.drain()
            except Exception:
                pass
        self._connected = False


# Module-level singleton — initialized on plugin load, used by hook
_jet = JetStreamBridge()


def get_bridge() -> JetStreamBridge:
    return _jet


# ---------------------------------------------------------------------------
# Publisher helpers (sync, called from pre_gateway_dispatch_hook)
# ---------------------------------------------------------------------------


def publish_gateway_delta(*, room: str, role: str, text: str,
                          in_flight_tool_call: bool = False) -> dict:
    """Mirror a gateway turn onto voice_bridge.gateway_out.<room>."""
    payload = {
        "event_id": str(uuid.uuid4()),
        "room": room,
        "role": role,
        "text": text,
        "ts": time.time(),
        "in_flight_tool_call": bool(in_flight_tool_call),
    }
    _jet.publish_sync(f"voice_bridge.gateway_out.{room}", payload)
    return payload


def publish_mode_transition(*, room: str, prior: str, target: str, trigger: str) -> dict:
    payload = {
        "event_id": str(uuid.uuid4()),
        "room": room,
        "prior": prior,
        "target": target,
        "trigger": trigger,
        "ts": time.time(),
    }
    _jet.publish_sync(f"voice_bridge.mode.{room}", payload)
    return payload


def derive_room(agent_id: str, user_id: str) -> str:
    """Canonical room-name format used by both ends of the bridge."""
    return f"voice_{agent_id}_{user_id}"


# ---------------------------------------------------------------------------
# Async start helper + per-room FSM registry (used by /voice-handoff)
# ---------------------------------------------------------------------------

_room_fsm_registry: dict[str, Any] = {}


def get_or_start_bridge() -> JetStreamBridge:
    """Schedule start() on the running loop if connected loop is missing.

    Returns the singleton; safe to call repeatedly. If there is no running
    asyncio loop (sync invocation), the start is deferred — callers can
    `await _jet.start()` themselves on the gateway loop.
    """
    if not _jet._connected:
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_jet.start())
            else:
                # No running loop yet — best effort start in current context
                try:
                    loop.run_until_complete(_jet.start())
                except Exception:
                    pass
        except RuntimeError:
            pass
    return _jet


def get_room_fsm(room: str):
    """Return (and lazily create) a GatewayBridgeMode for the given room.

    Imports executive_agents_framework's bridge module lazily so this file
    has no hard dependency on it at import time.
    """
    fsm = _room_fsm_registry.get(room)
    if fsm is not None:
        return fsm
    try:
        from voice_gateway_bridge import GatewayBridgeMode  # type: ignore
    except ImportError:
        # Try absolute path injection (gateway plugins run with path set up)
        import sys as _sys
        from pathlib import Path as _Path
        framework_src = _Path("/home/ubuntu/executive_agents_framework/src")
        if framework_src.exists() and str(framework_src) not in _sys.path:
            _sys.path.insert(0, str(framework_src))
        try:
            from voice_gateway_bridge import GatewayBridgeMode  # type: ignore
        except Exception as exc:
            logger.warning("[voice-agents.jet] FSM unavailable: %s", exc)
            return None

    def _on_transition(prior: str, target: str, trigger: str) -> None:
        publish_mode_transition(room=room, prior=prior, target=target, trigger=trigger)

    fsm = GatewayBridgeMode(on_transition=_on_transition)
    _room_fsm_registry[room] = fsm
    return fsm


def list_rooms() -> list[str]:
    return list(_room_fsm_registry.keys())
