"""Slash commands /voice join, /voice leave, /voice status.

Routes a Telegram (or any platform) chat into a LiveKit rendezvous room.
Spawns one ``tools.livekit_room_agent`` subprocess per active chat.

ADR-013 step 3.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
from typing import Any, Dict, Optional, Tuple, Union

logger = logging.getLogger("livekit_room_hook")


def parse_voice_command(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """Parse '/voice <subcommand> [arg]' into (subcommand, arg) or None.

    Returns None if the input is not a /voice slash command at all.
    """
    if not text or not text.strip().startswith("/voice"):
        return None
    parts = shlex.split(text.strip())
    if parts[0] != "/voice":
        return None
    if len(parts) == 1:
        return ("status", None)  # bare /voice → status
    sub = parts[1]
    arg = parts[2] if len(parts) > 2 else None
    if sub in ("join", "leave", "status"):
        return (sub, arg)
    return ("unknown", sub)


class LiveKitRoomController:
    """Per-process registry of running room-agent subprocesses keyed by chat_id."""

    def __init__(self) -> None:
        self._procs: Dict[str, asyncio.subprocess.Process] = {}
        self._lock = asyncio.Lock()

    async def join(self, chat_id: str, voice: str = "default") -> dict:
        from tools.livekit_room_manager import (
            LiveKitConfig,
            derive_room_name,
            ensure_room,
            mint_participant_token,
        )

        async with self._lock:
            existing = self._procs.get(chat_id)
            if existing and existing.returncode is None:
                return {
                    "pid": existing.pid,
                    "room": derive_room_name(chat_id),
                    "already_joined": True,
                }

            cfg = LiveKitConfig.from_env()
            room = derive_room_name(chat_id)
            await ensure_room(cfg, room)
            token = mint_participant_token(
                cfg,
                room_name=room,
                identity=f"hermes-bot-{chat_id[:16]}",
                ttl_seconds=3600,
            )

            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tools.livekit_room_agent",
                "--room",
                room,
                "--token",
                token,
                "--url",
                cfg.url,
                "--voice",
                voice,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ},
            )
            self._procs[chat_id] = proc
            logger.info(
                "LiveKit sidecar pid=%s room=%s chat=%s", proc.pid, room, chat_id
            )
            return {"pid": proc.pid, "room": room, "already_joined": False}

    async def leave(self, chat_id: str) -> dict:
        async with self._lock:
            proc = self._procs.pop(chat_id, None)
            if not proc or proc.returncode is not None:
                return {"status": "not_active"}
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            return {"status": "left", "pid": proc.pid}

    async def status(self, chat_id: str) -> dict:
        from tools.livekit_room_manager import derive_room_name

        proc = self._procs.get(chat_id)
        if proc and proc.returncode is None:
            return {"active": True, "pid": proc.pid, "room": derive_room_name(chat_id)}
        return {"active": False}


# Module-level singleton — one controller per gateway process
_controller: Optional[LiveKitRoomController] = None


def _get_controller() -> LiveKitRoomController:
    global _controller
    if _controller is None:
        _controller = LiveKitRoomController()
    return _controller


# ─── OPTION A: GatewayMessageHook subclass wiring ─────────────────────────

from gateway.builtin_hooks.voice_agent_hook import (  # noqa: E402
    GatewayMessageHook,
    get_hook_manager,
)
from gateway.platforms.base import MessageEvent  # noqa: E402


class LiveKitRoomHook(GatewayMessageHook):
    """GatewayMessageHook adapter: dispatch /voice commands to LiveKitRoomController."""

    async def before_message_processing(
        self, event: MessageEvent, gateway_runner: Any
    ) -> Optional[Union[str, Any]]:
        text = (
            getattr(event, "text_content", None)
            or getattr(event, "text", None)
            or getattr(event, "content", "")
            or ""
        )
        parsed = parse_voice_command(text)
        if parsed is None:
            return None  # pass through

        sub, arg = parsed
        chat_id = str(
            getattr(event, "chat_id", "")
            or getattr(getattr(event, "source", None), "chat_id", "")
            or ""
        )
        if not chat_id:
            return "/voice requires a chat context"

        ctrl = _get_controller()
        try:
            if sub == "join":
                res = await ctrl.join(chat_id, voice=arg or "default")
                if res.get("already_joined"):
                    return (
                        f"Already in voice room `{res['room']}` (pid {res['pid']})"
                    )
                return (
                    f"Joined LiveKit room `{res['room']}` (pid {res['pid']}). "
                    f"Connect from your LiveKit client to chat by voice."
                )
            elif sub == "leave":
                res = await ctrl.leave(chat_id)
                if res["status"] == "not_active":
                    return "No active voice room for this chat."
                return f"Left voice room (pid {res['pid']})."
            elif sub == "status":
                res = await ctrl.status(chat_id)
                if res["active"]:
                    return f"Voice room `{res['room']}` active (pid {res['pid']})."
                return "No active voice room for this chat."
            else:
                return f"Unknown /voice subcommand `{arg}`. Try: join, leave, status."
        except RuntimeError as e:
            return f"LiveKit not configured: {e}"
        except Exception as e:
            logger.exception("livekit hook crashed")
            return f"Voice room error: {type(e).__name__}: {e}"

    async def after_message_processing(
        self, event: MessageEvent, response: Any, gateway_runner: Any
    ) -> Any:
        return response


def register_builtin_hooks() -> None:
    """Register the LiveKit room hook with the gateway hook manager."""
    manager = get_hook_manager()
    manager.register_hook(LiveKitRoomHook())
    logger.info(
        "[hooks] LiveKit room hook registered (channel-agnostic /voice commands)"
    )
