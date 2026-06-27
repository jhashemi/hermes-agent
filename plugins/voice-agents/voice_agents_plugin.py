"""
Executive Voice Agents - Hermes Gateway Plugin

Native plugin that registers a 'pre_gateway_dispatch' hook to intercept
WhatsApp messages for executive voice agents without modifying core gateway code.

Commands:
  /load-{agent}     - Load and connect to an executive agent
  /voice-agents     - List available agents
  /voice-disconnect - End current agent session
  /voice-info {id}  - Get agent info

Flow for active sessions:
  - Voice note  → /transcribe (Deepgram) → /query (LLM) → send text + optional audio
  - Text message → /query (LLM) → send text reply directly
  - Uses action="skip" to bypass Hermes LLM entirely for persona sessions
"""

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Optional

import requests

from .jetstream_bridge import (
    derive_room,
    get_bridge,
    get_or_start_bridge,
    get_room_fsm,
    publish_gateway_delta,
)

logger = logging.getLogger("voice_agents_plugin")

# Bridge service URL
VOICE_BRIDGE_URL = "http://localhost:8193"

# Add executive agents platform to path
PLATFORM_DIR = Path("/home/ubuntu/executive_agents_platform")
if PLATFORM_DIR.exists() and str(PLATFORM_DIR) not in sys.path:
    sys.path.insert(0, str(PLATFORM_DIR))


# Agent ID aliases: short name -> bridge agent_id (underscore format)
AGENT_ALIASES = {
    "demis": "demis_hassabis",
    "hassabis": "demis_hassabis",
    "steve": "steve_jobs",
    "jobs": "steve_jobs",
    "knuth": "donald_knuth",
    "dean": "jeff_dean",
    "jony": "jony_ive",
    "ive": "jony_ive",
    "jordan": "jordan_tigani",
    "tigani": "jordan_tigani",
}

# Directory-style aliases (dashes) → bridge format (underscores)
DASH_TO_UNDERSCORE = {
    "demis-hassabis": "demis_hassabis",
    "steve-jobs": "steve_jobs",
    "donald-knuth": "donald_knuth",
    "jeff-dean": "jeff_dean",
    "jony-ive": "jony_ive",
    "jordan-tigani": "jordan_tigani",
}


class VoiceAgentSession:
    """Per-user session tracking."""

    def __init__(self, user_id: str, agent_id: str, session_id: str, chat_id: str = ""):
        self.user_id = user_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.chat_id = chat_id


class VoiceAgentRegistry:
    """Agent registry loaded from YAML config."""

    def __init__(self, agents_dir: Path):
        self.agents_dir = agents_dir
        self._agents: Dict[str, Dict] = {}

    def load(self) -> None:
        import yaml

        if not self.agents_dir.exists():
            logger.warning("[voice-agents] Agents directory not found: %s", self.agents_dir)
            return

        for agent_path in sorted(self.agents_dir.iterdir()):
            if not agent_path.is_dir():
                continue

            profile_file = agent_path / "agent_profile.yaml"
            if not profile_file.exists():
                continue

            try:
                with open(profile_file) as f:
                    profile = yaml.safe_load(f)

                agent_id = agent_path.name.lower().replace("-", "_")

                interview_dir = agent_path / "interview_data"
                question_count = 0
                if interview_dir.exists():
                    for jf in interview_dir.glob("*.json"):
                        try:
                            import json
                            with open(jf) as f:
                                data = json.load(f)
                                if isinstance(data, dict):
                                    question_count += len(data.get("questions", data.get("responses", [])))
                                elif isinstance(data, list):
                                    question_count += len(data)
                        except Exception:
                            pass

                self._agents[agent_id] = {
                    "id": agent_id,
                    "name": profile.get("name", agent_path.name),
                    "title": profile.get("title", ""),
                    "bio": profile.get("bio", "")[:200],
                    "profile": profile,
                    "question_count": question_count,
                    "path": str(agent_path),
                }
                logger.info("[voice-agents] Registered agent: %s (%d Q)", agent_id, question_count)
            except Exception as e:
                logger.error("[voice-agents] Failed to load %s: %s", agent_path.name, e)

    def get(self, agent_id: str) -> Optional[Dict]:
        return self._agents.get(agent_id)

    def list_all(self) -> list:
        return sorted(self._agents.values(), key=lambda a: a["id"])


# ============================================================================
# Module-level state
# ============================================================================

_registry: Optional[VoiceAgentRegistry] = None
_sessions: Dict[str, VoiceAgentSession] = {}


def _get_registry() -> VoiceAgentRegistry:
    global _registry
    if _registry is None:
        _registry = VoiceAgentRegistry(PLATFORM_DIR / "agents")
        _registry.load()
    return _registry


def _sync_sessions_from_bridge() -> None:
    """Restore in-memory sessions from the bridge's active session list (called on plugin load)."""
    try:
        resp = requests.get(f"{VOICE_BRIDGE_URL}/list", timeout=5)
        if resp.status_code != 200:
            return
        active = resp.json().get("active_sessions", [])
        for s in active:
            user_id = s.get("user_id", "")
            agent_id = s.get("agent_id", "")
            session_id = s.get("session_id", "")
            if user_id and agent_id and user_id not in _sessions:
                _sessions[user_id] = VoiceAgentSession(
                    user_id=user_id,
                    agent_id=agent_id,
                    session_id=session_id,
                )
                logger.info("[voice-agents] Restored session: user=%s agent=%s", user_id, agent_id)
    except Exception as e:
        logger.warning("[voice-agents] Could not sync sessions from bridge: %s", e)


def _resolve_agent_id(raw: str) -> str:
    """Normalize any alias/format to bridge agent_id (underscore format)."""
    raw = raw.strip().lower()
    if raw in AGENT_ALIASES:
        return AGENT_ALIASES[raw]
    if raw in DASH_TO_UNDERSCORE:
        return DASH_TO_UNDERSCORE[raw]
    return raw.replace("-", "_")


# ============================================================================
# Bridge HTTP helpers (sync, called from sync hook)
# ============================================================================

def _bridge_load(user_id: str, agent_id: str) -> Optional[Dict]:
    """Call /load to create a bridge session."""
    try:
        resp = requests.post(
            f"{VOICE_BRIDGE_URL}/load",
            json={"user_id": user_id, "agent_id": agent_id},
            timeout=15,
        )
        return resp.json() if resp.status_code == 200 else None
    except Exception as e:
        logger.warning("[voice-agents] Bridge /load error: %s", e)
        return None


def _bridge_query(user_id: str, query: str, synthesize: bool = False) -> Optional[Dict]:
    """Call /query for LLM response, optionally with TTS synthesis."""
    try:
        resp = requests.post(
            f"{VOICE_BRIDGE_URL}/query",
            json={"user_id": user_id, "query": query, "synthesize": synthesize},
            timeout=60,
        )
        return resp.json() if resp.status_code == 200 else None
    except Exception as e:
        logger.warning("[voice-agents] Bridge /query error: %s", e)
        return None


def _bridge_transcribe(audio_url: str) -> Optional[str]:
    """Call /transcribe to get STT text from an audio file."""
    try:
        resp = requests.post(
            f"{VOICE_BRIDGE_URL}/transcribe",
            json={"audio_url": audio_url},
            timeout=30,
        )
        if resp.status_code == 200:
            data = resp.json()
            return data.get("text") or data.get("transcript")
        return None
    except Exception as e:
        logger.warning("[voice-agents] Bridge /transcribe error: %s", e)
        return None


def _bridge_disconnect(user_id: str) -> None:
    """Call /disconnect to clean up bridge session."""
    try:
        requests.post(
            f"{VOICE_BRIDGE_URL}/disconnect",
            json={"user_id": user_id},
            timeout=5,
        )
    except Exception:
        pass


# ============================================================================
# Async sender — run in event loop from sync hook
# ============================================================================

def _send_reply_async(gateway: Any, event: Any, text: str, audio_path: Optional[str] = None) -> None:
    """Fire-and-forget: send text + optional audio back to user via gateway adapter."""
    source = event.source
    platform = source.platform
    chat_id = source.chat_id

    async def _do_send():
        try:
            adapter = gateway.adapters.get(platform)
            if not adapter:
                logger.error("[voice-agents] No adapter for platform %s", platform)
                return
            # Send text response
            if text:
                await adapter.send(chat_id, text)
            # Send audio as voice note if available
            if audio_path and os.path.exists(audio_path):
                if hasattr(adapter, "send_voice"):
                    await adapter.send_voice(chat_id, audio_path)
                else:
                    await adapter.send_document(chat_id, audio_path, caption="🔊 Voice response")
        except Exception as e:
            logger.error("[voice-agents] Send reply error: %s", e)

    # Schedule on the gateway's running event loop
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_do_send())
        else:
            loop.run_until_complete(_do_send())
    except RuntimeError:
        # Fallback: run in a new thread with its own loop
        def _in_thread():
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            try:
                new_loop.run_until_complete(_do_send())
            finally:
                new_loop.close()
        threading.Thread(target=_in_thread, daemon=True).start()


# ============================================================================
# Command handlers
# ============================================================================

def _handle_load_agent(text: str, event: Any, gateway: Any) -> Optional[Dict]:
    """Handle /load-{agent} command."""
    raw = text[6:].strip()
    agent_id = _resolve_agent_id(raw)

    # Check bridge list first
    try:
        resp = requests.get(f"{VOICE_BRIDGE_URL}/list", timeout=5)
        if resp.status_code == 200:
            bridge_agents = {a["id"] for a in resp.json().get("agents", [])}
            if agent_id not in bridge_agents:
                available = ", ".join(sorted(bridge_agents))
                return {
                    "action": "rewrite",
                    "text": f"❌ Agent '{agent_id}' not found.\nAvailable: {available}\n\nType /voice-agents to see all agents.",
                }
    except Exception:
        pass

    user_id = getattr(event.source, "user_id", "")
    chat_id = getattr(event.source, "chat_id", "") or ""

    # Call bridge /load
    bridge_resp = _bridge_load(user_id, agent_id)
    if not bridge_resp or bridge_resp.get("status") != "ok":
        err = bridge_resp.get("message", "unknown error") if bridge_resp else "bridge unreachable"
        return {"action": "rewrite", "text": f"❌ Failed to connect to {agent_id}: {err}"}

    # Store session
    session = VoiceAgentSession(
        user_id=user_id,
        agent_id=agent_id,
        session_id=bridge_resp.get("session_id", ""),
        chat_id=chat_id,
    )
    _sessions[user_id] = session

    voice_uuid = bridge_resp.get("voice_uuid", "")
    voice_status = "🎙️ Voice ready" if voice_uuid else "📝 Text only"

    # Get display name from registry or bridge
    registry = _get_registry()
    agent_info = registry.get(agent_id)
    name = agent_info["name"] if agent_info else agent_id.replace("_", " ").title()
    title = agent_info["title"] if agent_info else ""

    return {
        "action": "rewrite",
        "text": (
            f"✅ Connected to **{name}**\n"
            f"   {title}\n"
            f"   {voice_status} | LiveKit room: {bridge_resp.get('room_name', '')}\n\n"
            f"Send a text message or voice note to start talking.\n"
            f"Type /voice-disconnect to end."
        ),
    }


def _handle_list_agents(event: Any) -> Optional[Dict]:
    """Handle /voice-agents command."""
    user_id = getattr(event.source, "user_id", "")
    active = _sessions.get(user_id)

    try:
        resp = requests.get(f"{VOICE_BRIDGE_URL}/list", timeout=5)
        if resp.status_code != 200:
            return {"action": "rewrite", "text": "❌ Could not reach voice bridge."}
        data = resp.json()
        agents = data.get("agents", [])
    except Exception as e:
        return {"action": "rewrite", "text": f"❌ Bridge error: {e}"}

    lines = ["🤖 **Voice Agents:**\n"]
    for a in agents:
        star = "⭐" if active and active.agent_id == a["id"] else "○"
        voice_icon = "🎙️" if a.get("has_voice") else "📝"
        lines.append(f"  {star} {voice_icon} /load-{a['id'].replace('_', '-'):18} {a.get('name', a['id'])}")

    if active:
        lines.append(f"\n🔵 Active: {active.agent_id}")
    lines.append("\n_Type /load-{name} to connect, /voice-disconnect to end._")

    return {"action": "rewrite", "text": "\n".join(lines)}


def _handle_disconnect(event: Any) -> Optional[Dict]:
    """Handle /voice-disconnect command."""
    user_id = getattr(event.source, "user_id", "")
    session = _sessions.pop(user_id, None)

    if not session:
        return {"action": "rewrite", "text": "ℹ️ No voice agent connected.\n\nType /voice-agents to see available agents."}

    _bridge_disconnect(user_id)
    return {"action": "rewrite", "text": f"✅ Disconnected from {session.agent_id}.\n\nType /voice-agents to connect to a different agent."}


def _handle_agent_info(text: str, event: Any) -> Optional[Dict]:
    """Handle /voice-info {agent_id} command."""
    raw = text[12:].strip()
    agent_id = _resolve_agent_id(raw)

    registry = _get_registry()
    agent = registry.get(agent_id)
    if not agent:
        return {"action": "rewrite", "text": f"❌ Agent '{agent_id}' not found.\n\nType /voice-agents to see all agents."}

    voice_cfg = agent["profile"].get("voice", {})
    voice_uuid = voice_cfg.get("voice_uuid", "Not configured")

    return {
        "action": "rewrite",
        "text": (
            f"📋 **{agent['name']}**\n"
            f"   Title: {agent['title']}\n"
            f"   Bio: {agent['bio'][:150]}...\n"
            f"   Interview Questions: {agent['question_count']}\n"
            f"   Voice UUID: {voice_uuid}\n\n"
            f"Type /load-{agent['id'].replace('_', '-')} to connect."
        ),
    }


# ============================================================================
# Main hook
# ============================================================================

def _handle_voice_handoff(text: str, event: Any) -> Optional[Dict]:
    """Handle /voice-handoff on|off|auto — drives ModeFSM for current room."""
    user_id = getattr(event.source, "user_id", "")
    arg = text[len("/voice-handoff"):].strip().lower() or "auto"
    if arg not in ("on", "off", "auto"):
        return {"action": "rewrite", "text": "Usage: /voice-handoff on|off|auto"}

    session = _sessions.get(user_id)
    if not session:
        return {
            "action": "rewrite",
            "text": "ℹ️ /voice-handoff requires an active voice session. Type /voice-agents.",
        }

    room = derive_room(session.agent_id, user_id)
    fsm = get_room_fsm(room)
    if fsm is None:
        return {"action": "rewrite", "text": "⚠️ Voice bridge unavailable (FSM not loaded)."}

    try:
        fsm.manual_override(arg)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("[voice-agents] FSM override failed: %s", exc)
        return {"action": "rewrite", "text": f"⚠️ Override failed: {exc}"}

    return {
        "action": "rewrite",
        "text": f"🎚️ /voice-handoff {arg} → state={fsm.state} (room={room})",
    }


def pre_gateway_dispatch_hook(event: Any, gateway: Any = None, session_store: Any = None, **kwargs) -> Optional[Dict]:
    """
    Pre-dispatch hook. Returns:
      {"action": "skip"}     → we handled it, no Hermes LLM needed
      {"action": "rewrite", "text": "..."} → replace text, pass to Hermes
      None                   → normal dispatch
    """
    text = (getattr(event, "text", "") or "").strip()
    user_id = getattr(event.source, "user_id", "")
    msg_type = getattr(event, "message_type", None)
    msg_type_str = str(msg_type) if msg_type else ""

    # ── Command routing (always runs regardless of session) ──────────────────
    if text.startswith("/load-"):
        return _handle_load_agent(text, event, gateway)
    if text == "/voice-agents":
        return _handle_list_agents(event)
    if text == "/voice-disconnect":
        return _handle_disconnect(event)
    if text.startswith("/voice-info "):
        return _handle_agent_info(text, event)
    if text == "/voice-handoff" or text.startswith("/voice-handoff "):
        return _handle_voice_handoff(text, event)

    # ── Active session handling ──────────────────────────────────────────────
    session = _sessions.get(user_id)
    if not session:
        return None  # No active session — normal Hermes dispatch

    # Mirror inbound user turn onto JetStream for the voice twin
    try:
        if text:
            publish_gateway_delta(
                room=derive_room(session.agent_id, user_id),
                role="user",
                text=text,
                in_flight_tool_call=False,
            )
    except Exception as exc:  # pragma: no cover - audit-only
        logger.debug("[voice-agents] gateway_delta user publish failed: %s", exc)

    # Skip other slash commands (let Hermes handle them)
    if text.startswith("/"):
        return None

    # ── Voice note: transcribe then query ────────────────────────────────────
    is_audio = (
        "AUDIO" in msg_type_str.upper()
        or "VOICE" in msg_type_str.upper()
        or (not text and getattr(event, "media_urls", None))
    )
    if is_audio:
        media_urls = getattr(event, "media_urls", []) or []
        if not media_urls:
            # No media URL — can't transcribe
            return {"action": "skip", "reason": "voice note without media_url"}

        audio_url = media_urls[0]
        # Run transcription synchronously (bridge handles Deepgram)
        transcript = _bridge_transcribe(audio_url)
        if not transcript:
            _send_reply_async(
                gateway, event,
                "🎤 Sorry, I couldn't transcribe your voice note. Please try again or type your message.",
            )
            return {"action": "skip", "reason": "transcription failed"}

        # Query bridge with synthesize=True (voice reply)
        voice_uuid = ""
        try:
            br = requests.get(f"{VOICE_BRIDGE_URL}/list", timeout=3).json()
            for a in br.get("agents", []):
                if a["id"] == session.agent_id:
                    voice_uuid = a.get("voice_uuid", "")
                    break
        except Exception:
            pass

        bridge_resp = _bridge_query(user_id, transcript, synthesize=bool(voice_uuid))
        if not bridge_resp or bridge_resp.get("status") != "ok":
            _send_reply_async(gateway, event, "⚠️ I had trouble responding. Please try again.")
            return {"action": "skip", "reason": "bridge query failed"}

        llm_text = bridge_resp.get("llm_response", "")
        # audio_url is actually a local file path from synthesize_speech
        audio_path = bridge_resp.get("audio_url") or bridge_resp.get("audio_path")
        if audio_path and (audio_path.startswith("http") or not os.path.exists(audio_path)):
            audio_path = None  # Only send existing local file paths

        reply = f"_{transcript}_\n\n{llm_text}" if llm_text else "⚠️ No response generated."
        _send_reply_async(gateway, event, reply, audio_path if audio_path else None)
        # Mirror assistant reply onto JetStream
        try:
            if llm_text:
                publish_gateway_delta(
                    room=derive_room(session.agent_id, user_id),
                    role="assistant",
                    text=llm_text,
                )
        except Exception as exc:  # pragma: no cover - audit-only
            logger.debug("[voice-agents] gateway_delta assistant publish failed: %s", exc)
        return {"action": "skip", "reason": "voice note handled by voice agent"}

    # ── Text message: query bridge ───────────────────────────────────────────
    # Always synthesize when a voice session is active — voice agents respond in voice.
    if text:
        # Determine if this agent has a voice (synthesize only when voice_uuid exists)
        voice_uuid = ""
        try:
            br = requests.get(f"{VOICE_BRIDGE_URL}/list", timeout=3).json()
            for a in br.get("agents", []):
                if a["id"] == session.agent_id:
                    voice_uuid = a.get("voice_uuid", "")
                    break
        except Exception:
            pass

        bridge_resp = _bridge_query(user_id, text, synthesize=bool(voice_uuid))
        if bridge_resp and bridge_resp.get("status") == "ok":
            llm_text = bridge_resp.get("llm_response", "")
            # audio_url holds local path when synthesize=True
            audio_path = bridge_resp.get("audio_url") or bridge_resp.get("audio_path")
            if audio_path and (audio_path.startswith("http") or not os.path.exists(audio_path)):
                audio_path = None  # Only send existing local file paths
            if llm_text:
                _send_reply_async(gateway, event, llm_text, audio_path if audio_path else None)
                # Mirror assistant reply onto JetStream
                try:
                    publish_gateway_delta(
                        room=derive_room(session.agent_id, user_id),
                        role="assistant",
                        text=llm_text,
                    )
                except Exception as exc:  # pragma: no cover - audit-only
                    logger.debug("[voice-agents] gateway_delta assistant publish failed: %s", exc)
                return {"action": "skip", "reason": "text handled by voice agent"}

        # Bridge failed — fall through to normal Hermes (better than silence)
        logger.warning("[voice-agents] Bridge query failed for user %s, falling through", user_id)

    return None


# ============================================================================
# Voice→gateway inbound injection (subscriber on voice_bridge.voice_out.>)
# ============================================================================

_voice_out_injector: Optional[Any] = None
_inbound_chat_bindings: Dict[str, Dict[str, str]] = {}  # room → {chat_id, platform}


def _bind_inbound_room(*, room: str, chat_id: str, platform: str) -> None:
    """Record where to inject voice_out events for this room."""
    _inbound_chat_bindings[room] = {"chat_id": chat_id, "platform": platform}


async def _start_voice_out_subscriber(gateway: Any) -> None:
    """Subscribe to voice_bridge.voice_out.> and inject text into the bound chat."""
    global _voice_out_injector
    bridge = get_bridge()
    if not bridge.connected:
        await bridge.start()
    if not bridge.connected:
        logger.info("[voice-agents] JetStream offline; voice_out subscription skipped")
        return

    # Build a sync send-callable that schedules adapter.send on the gateway loop.
    def _send(chat_id: str, text: str) -> None:
        binding = None
        for b in _inbound_chat_bindings.values():
            if b.get("chat_id") == chat_id:
                binding = b
                break
        platform = binding["platform"] if binding else "whatsapp"
        adapter = getattr(gateway, "adapters", {}).get(platform)
        if adapter is None:
            logger.warning("[voice-agents] inbound: no adapter for %s", platform)
            return

        async def _do() -> None:
            try:
                await adapter.send(chat_id, text)
            except Exception as exc:
                logger.warning("[voice-agents] inbound send failed: %s", exc)

        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                asyncio.ensure_future(_do())
        except RuntimeError:
            pass

    # Lazy import the dedup injector
    try:
        from voice_gateway_bridge import AnalogTextInjector  # type: ignore
    except ImportError:
        framework_src = Path("/home/ubuntu/executive_agents_framework/src")
        if framework_src.exists() and str(framework_src) not in sys.path:
            sys.path.insert(0, str(framework_src))
        try:
            from voice_gateway_bridge import AnalogTextInjector  # type: ignore
        except Exception as exc:
            logger.warning("[voice-agents] AnalogTextInjector unavailable: %s", exc)
            return

    injector = AnalogTextInjector(send=_send)
    _voice_out_injector = injector

    def _on_voice_out(payload: dict) -> None:
        room = payload.get("room", "")
        binding = _inbound_chat_bindings.get(room)
        if binding and room not in injector._room_to_chat:
            injector.bind_room(room, chat_id=binding["chat_id"])
        injector.handle(payload)

    await bridge.subscribe("voice_bridge.voice_out.>", _on_voice_out)


def _schedule_subscriber(gateway: Any) -> None:
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.ensure_future(_start_voice_out_subscriber(gateway))
        else:
            # Defer: try via a thread (the gateway will create its own loop)
            def _runner() -> None:
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    new_loop.run_until_complete(_start_voice_out_subscriber(gateway))
                except Exception as exc:
                    logger.debug("[voice-agents] subscriber thread error: %s", exc)

            threading.Thread(target=_runner, daemon=True).start()
    except RuntimeError:
        pass


# ============================================================================
# Plugin entry points
# ============================================================================

def on_load(plugin_interface):
    logger.info("[voice-agents] Plugin loaded — registering pre_gateway_dispatch hook")
    _sync_sessions_from_bridge()
    plugin_interface.register_hook("pre_gateway_dispatch", pre_gateway_dispatch_hook)
    # Start JetStream bridge + voice_out subscriber
    try:
        get_or_start_bridge()
        gateway = getattr(plugin_interface, "gateway", None)
        if gateway is not None:
            _schedule_subscriber(gateway)
    except Exception as exc:
        logger.warning("[voice-agents] bridge start skipped: %s", exc)


def register(ctx):
    logger.info("[voice-agents] Registering pre_gateway_dispatch hook")
    _sync_sessions_from_bridge()
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch_hook)
    try:
        get_or_start_bridge()
        gateway = getattr(ctx, "gateway", None)
        if gateway is not None:
            _schedule_subscriber(gateway)
    except Exception as exc:
        logger.warning("[voice-agents] bridge start skipped: %s", exc)
    logger.info("[voice-agents] ✅ Hook registered.")


if __name__ == "__main__":
    print("Voice Agent Plugin — Commands:")
    print("  /load-demis-hassabis  Connect to Demis Hassabis")
    print("  /load-steve-jobs      Connect to Steve Jobs")
    print("  /voice-agents         List available agents")
    print("  /voice-info demis     Get agent details")
    print("  /voice-disconnect     End session")
