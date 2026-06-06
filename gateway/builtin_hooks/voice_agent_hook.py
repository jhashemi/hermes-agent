"""
Voice Agent Platform Adapter Hook — Channel-Agnostic HTTP Proxy (SOLID Design)

Intercepts voice-agent commands and audio messages across all platforms
(Telegram, WhatsApp, Discord, Signal, etc.) via the GatewayHookManager.

Routes through the voice_bridge_service HTTP API (port 8193) which provides:
- 8-system Park et al. memory pipeline (STM+LTM+ScratchPad+Semantic+ActivationField+TemporalTrace)
- AuthenticityMemoryStream with interview-grounded retrieval
- Conversation history tracking (per-session, 20-turn cap)
- Tool bridge (HermesToolBridge)
- LLM call with Bedrock + OpenAI fallback + Framework engine
- Resemble TTS synthesis (streaming + batch)
- Deepgram STT transcription
- NATS bridge for nervous system events
- LiveKit token generation + room management
- SessionManager with stale cleanup

This hook is a THIN PROXY — it doesn't duplicate any intelligence.
All heavy lifting happens in the voice_bridge_service process.

Architecture:
- VoiceAgentMessageInterceptor: Observer hook for pre-message-processing
- Session keys: (user_id, platform) tuples prevent cross-channel collisions
- Identity: Uses AccessManager.get_user_id() for consistent user resolution
- HTTP proxy: All /load, /chat, /synthesize, /disconnect calls go to port 8193
"""

import logging
import json
from typing import Optional, Union, Any, Tuple
from abc import ABC, abstractmethod

import aiohttp

from gateway.platforms.base import MessageEvent, MessageType
from gateway.error_response import ErrorResponse, ErrorCode, ErrorSeverity, create_access_denied_error
from gateway.access_control import get_access_manager

logger = logging.getLogger(__name__)

# Voice bridge HTTP service configuration
# The voice_bridge_service runs on port 8193 with the full 8-system memory pipeline.
BRIDGE_HOST = "localhost"
BRIDGE_PORT = 8193
BRIDGE_BASE_URL = f"http://{BRIDGE_HOST}:{BRIDGE_PORT}"


def _extract_platform(event: MessageEvent) -> str:
    """Extract platform identifier from a MessageEvent."""
    source = getattr(event, 'source', None)
    if source is not None:
        platform = getattr(source, 'platform', None)
        if platform is not None:
            return str(platform.value if hasattr(platform, 'value') else platform).lower()
    return 'unknown'


def _extract_user_id(event: MessageEvent) -> str:
    """Extract consistent user identity from a MessageEvent."""
    return get_access_manager().get_user_id(event)


def _extract_chat_id(event: MessageEvent) -> str:
    """Extract chat_id from MessageEvent for audio responses."""
    source = getattr(event, 'source', None)
    if source is not None:
        chat_id = getattr(source, 'chat_id', None)
        if chat_id:
            return str(chat_id)
    return getattr(event, 'chat_id', 'unknown')


# ============================================================================
# Voice Session Manager (platform-scoped, not in-process)
# ============================================================================

class VoiceSessionManager:
    """Manages voice agent sessions keyed by (user_id, platform).
    
    Tracks which users have active voice sessions on which platforms.
    The actual session state lives in the voice_bridge_service process;
    this manager only tracks the mapping locally for fast interception.
    """
    
    def __init__(self):
        self._sessions: dict[Tuple[str, str], dict] = {}
    
    def get(self, user_id: str, platform: str) -> Optional[dict]:
        return self._sessions.get((user_id, platform))
    
    def set(self, user_id: str, platform: str, session_data: dict) -> None:
        self._sessions[(user_id, platform)] = session_data
    
    def delete(self, user_id: str, platform: str) -> Optional[dict]:
        return self._sessions.pop((user_id, platform), None)
    
    def has_session(self, user_id: str, platform: str) -> bool:
        return (user_id, platform) in self._sessions


# ============================================================================
# Async HTTP Client for Voice Bridge Service
# ============================================================================

class VoiceBridgeClient:
    """Async HTTP client for the voice_bridge_service API.
    
    Routes all calls to the standalone aiohttp service on port 8193
    which has the full 8-system Park et al. memory pipeline, LLM calls,
    TTS, STT, NATS events, and LiveKit integration.
    """
    
    def __init__(self, base_url: str = BRIDGE_BASE_URL):
        self._base_url = base_url
        self._session: Optional[aiohttp.ClientSession] = None
    
    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create the HTTP session (lazy, reused across calls)."""
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=5)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session
    
    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
    
    async def post(self, endpoint: str, payload: dict) -> dict:
        """POST to a voice bridge endpoint. Returns parsed JSON response."""
        session = await self._get_session()
        url = f"{self._base_url}{endpoint}"
        try:
            async with session.post(url, json=payload) as resp:
                return await resp.json()
        except aiohttp.ClientError as e:
            logger.error(f"[voice-bridge] HTTP POST {endpoint} failed: {e}")
            return {"error": f"Voice bridge unavailable: {str(e)}"}
        except json.JSONDecodeError as e:
            logger.error(f"[voice-bridge] Invalid JSON from {endpoint}: {e}")
            return {"error": f"Voice bridge returned invalid response"}
    
    async def get(self, endpoint: str) -> dict:
        """GET from a voice bridge endpoint. Returns parsed JSON response."""
        session = await self._get_session()
        url = f"{self._base_url}{endpoint}"
        try:
            async with session.get(url) as resp:
                return await resp.json()
        except aiohttp.ClientError as e:
            logger.error(f"[voice-bridge] HTTP GET {endpoint} failed: {e}")
            return {"error": f"Voice bridge unavailable: {str(e)}"}
    
    async def health(self) -> dict:
        """Check if the voice bridge service is alive."""
        return await self.get("/health")


# ============================================================================
# Gateway Hook Interface (SOLID: Dependency Inversion)
# ============================================================================

class GatewayMessageHook(ABC):
    """Abstract base for gateway message hooks."""
    
    @abstractmethod
    async def before_message_processing(
        self, event: MessageEvent, gateway_runner: Any,
    ) -> Optional[Union[str, ErrorResponse]]:
        pass
    
    @abstractmethod
    async def after_message_processing(
        self, event: MessageEvent, response: Any, gateway_runner: Any,
    ) -> Any:
        pass


# ============================================================================
# Voice Agent Message Interceptor — HTTP Proxy to voice_bridge_service
# ============================================================================

class VoiceAgentMessageInterceptor(GatewayMessageHook):
    """
    Channel-agnostic voice agent interceptor.
    
    Routes all voice commands and audio messages through the
    voice_bridge_service HTTP API (port 8193) which provides the full
    8-system Park et al. memory pipeline, LLM calls, TTS, STT,
    NATS events, and LiveKit integration.
    
    Session routing: (user_id, platform) to prevent cross-channel collisions.
    """
    
    def __init__(self):
        self._bridge = VoiceBridgeClient()
        self._sessions = VoiceSessionManager()
        self._bridge_healthy: Optional[bool] = None
    
    async def _check_bridge(self) -> bool:
        """Check if the voice bridge service is reachable."""
        if self._bridge_healthy is not None:
            return self._bridge_healthy
        try:
            result = await self._bridge.health()
            self._bridge_healthy = result.get("status") == "ok"
            return self._bridge_healthy
        except Exception:
            self._bridge_healthy = False
            return False
    
    async def before_message_processing(
        self, event: MessageEvent, gateway_runner: Any,
    ) -> Optional[Union[str, ErrorResponse]]:
        """Intercept message before platform processes it."""
        platform = _extract_platform(event)
        user_id = _extract_user_id(event)
        text = (event.text or "").strip()
        
        # Voice hook intercepts voice-specific commands and audio:
        #  1. /voice-agents, /voice-list, /voice-disconnect (always)
        #  2. /load-<agent> or /load_<agent> — ALWAYS proxy through the voice
        #     bridge so the first-time load actually spawns the full executive
        #     AgentContainer in the bridge process (port 8193). The previous
        #     behaviour gated this on has_session() which meant first-time
        #     loads silently fell through to agent_commands.py →
        #     persona_manager (system-prompt-only, no AgentContainer wiring).
        #     That bug is the reason /load-demis was a costume change instead
        #     of an actor swap. We accept both `-` and `_` separators because
        #     Telegram clients often auto-correct the dash to underscore.
        #  3. Text messages from a user who already has an active voice
        #     session route to the bridge /chat endpoint so the executive
        #     AgentContainer (with its 8-system memory pipeline) replies,
        #     not the lightweight Hermes agent.
        #  4. Audio/voice messages when user has an active voice session.

        if text:
            if text.startswith("/load-") or text.startswith("/load_"):
                return await self._handle_voice_load(text, event, platform, user_id)
            elif text in ("/voice-agents", "/voice-list", "/voice_agents", "/voice_list"):
                return await self._handle_voice_list()
            elif text in ("/voice-disconnect", "/voice_disconnect"):
                return await self._handle_voice_disconnect(event, platform, user_id)
            elif self._sessions.has_session(user_id, platform) and not text.startswith("/"):
                # Text reply while connected — route through bridge /chat so the
                # full AgentContainer answers, not the default Hermes agent.
                return await self._handle_voice_text(text, event, platform, user_id)

        if event.message_type in (MessageType.AUDIO, MessageType.VOICE):
            return await self._handle_voice_audio(event, platform, user_id)

        return None
    
    async def after_message_processing(
        self, event: MessageEvent, response: Any, gateway_runner: Any,
    ) -> Any:
        """Hook after processing (no-op for voice)."""
        return response
    
    # ========================================================================
    # Voice Command Handlers — HTTP Proxy to Bridge Service
    # ========================================================================
    
    async def _handle_voice_load(
        self, text: str, event: MessageEvent, platform: str, user_id: str,
    ) -> Union[str, ErrorResponse]:
        """Handle /load-{agent} or /load_{agent} by proxying to voice bridge /load endpoint."""
        # Strip the 6-char prefix (/load- or /load_); both are length 6 and
        # the agent id can contain dashes (`demis-hassabis`).
        agent_id = text[6:].strip().lower()
        
        access_mgr = get_access_manager()
        if not access_mgr.has_access(event):
            error = create_access_denied_error(
                user_id=user_id,
                command=f"load-voice-{agent_id}",
                reason="You don't have permission to load voice agents.",
            )
            return error.to_emoji_response()
        
        result = await self._bridge.post("/load", {
            "agent_id": agent_id,
            "user_id": user_id,
            "platform": platform,
        })
        
        if "error" in result:
            return ErrorResponse(
                code=ErrorCode.NOT_FOUND,
                message=result["error"],
                severity=ErrorSeverity.MEDIUM.value,
            ).to_emoji_response()
        
        # Cache session locally for fast audio routing
        self._sessions.set(user_id, platform, {
            "session_id": result.get("session_id", ""),
            "agent_id": agent_id,
            "chat_id": _extract_chat_id(event),
        })
        
        logger.info(f"[voice-hook] User {user_id} on {platform} loaded {agent_id}")
        return result.get("message", f"✅ Loaded {agent_id}")
    
    async def _handle_voice_list(self) -> str:
        """Handle /voice-agents by proxying to bridge /list endpoint."""
        result = await self._bridge.get("/list")
        
        if "error" in result:
            return f"❌ Error: {result['error']}"
        
        agents = result.get("agents", [])
        if not agents:
            return "❌ No voice agents configured"
        
        lines = ["🤖 **Voice Agents:**\n"]
        for agent in agents:
            status = "🎙️" if agent.get("has_voice") else "📝"
            lines.append(
                f"{status} /load-{agent['id']:15} "
                f"{agent['name']:20}"
            )
        return "\n".join(lines)
    
    async def _handle_voice_disconnect(
        self, event: MessageEvent, platform: str, user_id: str,
    ) -> str:
        """Handle /voice-disconnect by proxying to bridge and clearing local session."""
        session = self._sessions.get(user_id, platform)
        if not session:
            return "ℹ️ No voice agent connected"
        
        session_id = session.get("session_id", "")
        await self._bridge.post("/disconnect", {
            "session_id": session_id,
            "user_id": user_id,
        })
        
        agent_id = session.get("agent_id", "unknown")
        self._sessions.delete(user_id, platform)
        logger.info(f"[voice-hook] User {user_id} on {platform} disconnected from {agent_id}")
        return f"✅ Disconnected from {agent_id}"
    
    async def _handle_voice_text(
        self, text: str, event: MessageEvent, platform: str, user_id: str,
    ) -> Optional[str]:
        """Handle text message with active voice session.

        Mirrors _handle_voice_audio for text-channel inputs (Telegram chat,
        WhatsApp text, etc.). Proxies to the voice bridge /chat endpoint so
        the full executive AgentContainer (8-system memory pipeline +
        conversation history + tool bridge) generates the reply, not the
        default Hermes agent. The bridge decides whether to synthesize TTS
        based on agent capability + caller preference; we ask for it on but
        the bridge will downgrade gracefully if no voice clone is available.
        """
        session = self._sessions.get(user_id, platform)
        if not session:
            return None  # No active voice session — pass through

        agent_id = session.get("agent_id", "")

        result = await self._bridge.post("/chat", {
            "agent_id": agent_id,
            "user_id": user_id,
            "message": text,
            "synthesize": False,  # text channel — no TTS by default
        })

        if "error" in result:
            logger.error(f"[voice-hook] Chat failed: {result['error']}")
            return f"❌ {result['error']}"

        response_text = result.get("response", "") or result.get("message", "")
        logger.info(f"[voice-hook] Processed text for {user_id} on {platform}")
        return response_text

    async def _handle_voice_audio(
        self, event: MessageEvent, platform: str, user_id: str,
    ) -> Optional[str]:
        """Handle audio message with active voice session.
        
        Proxies to voice bridge /chat endpoint which provides the full
        8-system memory pipeline, conversation history, and tool access.
        """
        session = self._sessions.get(user_id, platform)
        if not session:
            return None  # No active voice session — pass through
        
        session_id = session.get("session_id", "")
        agent_id = session.get("agent_id", "")
        
        # Get audio URL from event
        audio_url = (event.media_urls or [None])[0]
        if not audio_url:
            return "❌ No audio data received"
        
        # If it's a voice message (just text after transcription), route through /chat
        # The bridge service handles STT transcription + memory pipeline + LLM + TTS
        text = (event.text or "").strip()
        
        result = await self._bridge.post("/chat", {
            "agent_id": agent_id,
            "user_id": user_id,
            "message": text if text else "[audio]",
            "synthesize": True,
        })
        
        if "error" in result:
            logger.error(f"[voice-hook] Chat failed: {result['error']}")
            return f"❌ {result['error']}"
        
        response_text = result.get("response", "") or result.get("message", "")
        
        # Build response — include transcription if available
        user_input = result.get("user_input", text or "[voice]")
        logger.info(f"[voice-hook] Processed audio for {user_id} on {platform}")
        
        return f"📝 You: {user_input}\n\n🤖 Agent: {response_text}"


# ============================================================================
# Gateway Hook Manager
# ============================================================================

class GatewayHookManager:
    """Manages builtin hooks for gateway message processing."""
    
    def __init__(self):
        self._hooks: list[GatewayMessageHook] = []
    
    def register_hook(self, hook: GatewayMessageHook) -> None:
        if hook not in self._hooks:
            self._hooks.append(hook)
            logger.info(f"[hooks] Registered {hook.__class__.__name__}")
    
    def unregister_hook(self, hook: GatewayMessageHook) -> None:
        if hook in self._hooks:
            self._hooks.remove(hook)
            logger.info(f"[hooks] Unregistered {hook.__class__.__name__}")
    
    async def before_message_processing(
        self, event: MessageEvent, gateway_runner: Any,
    ) -> Optional[Union[str, ErrorResponse]]:
        for hook in self._hooks:
            result = await hook.before_message_processing(event, gateway_runner)
            if result is not None:
                return result
        return None
    
    async def after_message_processing(
        self, event: MessageEvent, response: Any, gateway_runner: Any,
    ) -> Any:
        for hook in self._hooks:
            response = await hook.after_message_processing(event, response, gateway_runner)
        return response


# ============================================================================
# Global Hook Manager Instance
# ============================================================================

_hook_manager: Optional[GatewayHookManager] = None

def get_hook_manager() -> GatewayHookManager:
    global _hook_manager
    if _hook_manager is None:
        _hook_manager = GatewayHookManager()
    return _hook_manager


def register_builtin_hooks() -> None:
    """Register all builtin gateway hooks. Called once at startup."""
    manager = get_hook_manager()
    voice_interceptor = VoiceAgentMessageInterceptor()
    manager.register_hook(voice_interceptor)
    logger.info("[hooks] Builtin hooks registered (voice agent HTTP proxy)")


# ============================================================================
# Integration Point for run.py
# ============================================================================

async def process_message_with_hooks(
    event: MessageEvent,
    gateway_runner: Any,
    original_handler: callable,
) -> Any:
    """Process message through hooks before platform adapter."""
    manager = get_hook_manager()
    result = await manager.before_message_processing(event, gateway_runner)
    if result is not None:
        return result
    return await original_handler(event)