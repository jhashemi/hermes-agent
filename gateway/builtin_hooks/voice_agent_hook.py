"""
Voice Agent Platform Adapter Hook - SOLID Design (No Core Modifications)

This module provides a pluggable hook into the Hermes Gateway message processing
without modifying core platform adapters.

Uses the Observer/Hook pattern:
- VoiceAgentMessageInterceptor: Intercepts messages before platform processing
- Registered via gateway configuration (builtin_hooks)
- Completely decoupled from WhatsApp adapter

This follows SOLID principles:
- S: Single Responsibility (voice handling only)
- O: Open/Closed (extensible via hooks, not modification)
- L: Liskov Substitution (compatible with base gateway hooks)
- I: Interface Segregation (minimal hook interface)
- D: Dependency Inversion (depends on abstractions, not concrete adapters)
"""

import logging
from typing import Optional, Union, Any
from abc import ABC, abstractmethod

from gateway.platforms.base import MessageEvent, MessageType
from gateway.error_response import ErrorResponse, ErrorCode, ErrorSeverity
from gateway.access_control import get_access_manager

logger = logging.getLogger(__name__)

# Import voice bridge
import sys
from pathlib import Path
sys.path.insert(0, '/home/ubuntu/executive_agents_platform')
from loader.whatsapp_voice_bridge import WhatsAppVoiceAgentBridge


# ============================================================================
# Gateway Hook Interface (SOLID: Dependency Inversion)
# ============================================================================

class GatewayMessageHook(ABC):
    """Abstract base for gateway message hooks.
    
    Implemented by platform adapters to provide extension points.
    Follows SOLID: depends on abstraction, not concrete adapters.
    """
    
    @abstractmethod
    async def before_message_processing(
        self,
        event: MessageEvent,
        gateway_runner: Any,
    ) -> Optional[Union[str, ErrorResponse]]:
        """
        Hook called before platform processes message.
        
        Args:
            event: Message event from platform
            gateway_runner: Gateway runner instance
        
        Returns:
            Response to send (intercepts message)
            OR None to continue normal processing
        """
        pass
    
    @abstractmethod
    async def after_message_processing(
        self,
        event: MessageEvent,
        response: Any,
        gateway_runner: Any,
    ) -> Any:
        """Hook called after platform processes message."""
        pass


# ============================================================================
# Voice Agent Message Interceptor (SOLID: Single Responsibility)
# ============================================================================

class VoiceAgentMessageInterceptor(GatewayMessageHook):
    """
    Message interceptor for voice agent handling.
    
    SOLID principles:
    - Single Responsibility: Only handles voice messages
    - Open/Closed: Extends via hooks, doesn't modify adapters
    - Liskov: Implements GatewayMessageHook interface
    - Interface Segregation: Minimal interface
    - Dependency Inversion: Depends on abstractions
    """
    
    def __init__(self):
        """Initialize voice interceptor with bridge."""
        self._voice_bridge: Optional[WhatsAppVoiceAgentBridge] = None
        self._initialized = False
    
    def _get_voice_bridge(self) -> Optional[WhatsAppVoiceAgentBridge]:
        """Get or create voice bridge (lazy initialization)."""
        if self._initialized:
            return self._voice_bridge
        
        try:
            self._voice_bridge = WhatsAppVoiceAgentBridge()
            self._initialized = True
            logger.info("[voice-hook] Voice bridge initialized")
        except Exception as e:
            logger.error(f"[voice-hook] Failed to initialize voice bridge: {e}")
            self._initialized = True  # Don't retry
            return None
        
        return self._voice_bridge
    
    async def before_message_processing(
        self,
        event: MessageEvent,
        gateway_runner: Any,
    ) -> Optional[Union[str, ErrorResponse]]:
        """
        Intercept message before platform processes it.
        
        Returns response to send OR None to continue normal flow.
        """
        # SOLID: Single responsibility - only handle voice commands + audio
        
        # Check for voice commands
        text = (event.text or "").strip()
        if text:
            if text.startswith("/load-"):
                return await self._handle_voice_load(text, event, gateway_runner)
            elif text == "/voice-agents":
                return await self._handle_voice_list(event, gateway_runner)
            elif text == "/voice-disconnect":
                return await self._handle_voice_disconnect(event, gateway_runner)
        
        # Check for audio messages with active voice session
        if event.message_type == MessageType.AUDIO:
            return await self._handle_voice_audio(event, gateway_runner)
        
        # Continue normal processing
        return None
    
    async def after_message_processing(
        self,
        event: MessageEvent,
        response: Any,
        gateway_runner: Any,
    ) -> Any:
        """Hook after processing (no-op for voice)."""
        return response
    
    # ========================================================================
    # Voice Command Handlers (Private - Implementation Details)
    # ========================================================================
    
    async def _handle_voice_load(
        self,
        text: str,
        event: MessageEvent,
        gateway_runner: Any,
    ) -> Union[str, ErrorResponse]:
        """Handle /load-{agent} command."""
        agent_id = text[6:].strip().lower()
        
        try:
            bridge = self._get_voice_bridge()
            if not bridge:
                return ErrorResponse(
                    code=ErrorCode.OPERATION_FAILED,
                    message="Voice bridge not available",
                    severity=ErrorSeverity.HIGH.value,
                ).to_emoji_response()
            
            result = await bridge.handle_load_command(event.user_id, agent_id)
            
            if "error" in result:
                return ErrorResponse(
                    code=ErrorCode.NOT_FOUND,
                    message=result["error"],
                    severity=ErrorSeverity.MEDIUM.value,
                ).to_emoji_response()
            
            # Store session on gateway runner
            if not hasattr(gateway_runner, "_voice_sessions"):
                gateway_runner._voice_sessions = {}
            
            gateway_runner._voice_sessions[event.user_id] = {
                "session_id": result["session_id"],
                "agent_id": agent_id,
                "chat_id": event.chat_id,
            }
            
            logger.info(f"[voice-hook] User {event.user_id} loaded {agent_id}")
            return result.get("message", f"✅ Loaded {agent_id}")
        
        except Exception as e:
            logger.error(f"[voice-hook] Load failed: {e}", exc_info=True)
            return ErrorResponse(
                code=ErrorCode.OPERATION_FAILED,
                message=f"Failed to load agent: {str(e)}",
                severity=ErrorSeverity.HIGH.value,
            ).to_emoji_response()
    
    async def _handle_voice_list(
        self,
        event: MessageEvent,
        gateway_runner: Any,
    ) -> str:
        """Handle /voice-agents command."""
        try:
            bridge = self._get_voice_bridge()
            if not bridge:
                return "❌ Voice bridge not available"
            
            agents = bridge.loader.list_agents()
            
            if not agents:
                return "❌ No voice agents configured"
            
            lines = ["🤖 **Voice Agents:**\n"]
            for agent in agents:
                status = "✅" if agent["status"] == "ready" else "⏳"
                lines.append(
                    f"{status} /load-{agent['id']:15} "
                    f"{agent['name']:20} "
                    f"({agent['questions']} Q)"
                )
            
            return "\n".join(lines)
        
        except Exception as e:
            logger.error(f"[voice-hook] List failed: {e}")
            return f"❌ Error: {str(e)}"
    
    async def _handle_voice_disconnect(
        self,
        event: MessageEvent,
        gateway_runner: Any,
    ) -> str:
        """Handle /voice-disconnect command."""
        if not hasattr(gateway_runner, "_voice_sessions"):
            return "ℹ️ No voice agent connected"
        
        session = gateway_runner._voice_sessions.get(event.user_id)
        if not session:
            return "ℹ️ No voice agent connected"
        
        agent_id = session["agent_id"]
        del gateway_runner._voice_sessions[event.user_id]
        
        logger.info(f"[voice-hook] User {event.user_id} disconnected from {agent_id}")
        return f"✅ Disconnected from {agent_id}"
    
    async def _handle_voice_audio(
        self,
        event: MessageEvent,
        gateway_runner: Any,
    ) -> Optional[str]:
        """Handle audio message with active voice session."""
        if not hasattr(gateway_runner, "_voice_sessions"):
            return None
        
        session = gateway_runner._voice_sessions.get(event.user_id)
        if not session:
            return None
        
        session_id = session["session_id"]
        agent_id = session["agent_id"]
        
        try:
            # Download audio
            from gateway.platforms.base import cache_audio_from_url
            audio_bytes = await cache_audio_from_url(event.media_url)
            
            # Process via voice bridge
            bridge = self._get_voice_bridge()
            if not bridge:
                return "❌ Voice bridge not available"
            
            response = await bridge.handle_audio_message(
                user_id=event.user_id,
                audio_bytes=audio_bytes,
                session_id=session_id
            )
            
            if "error" in response:
                logger.error(f"[voice-hook] Audio error: {response['error']}")
                return f"❌ {response['error']}"
            
            user_input = response.get("user_input", "[voice]")
            response_text = response.get("response_text", "")
            
            logger.info(f"[voice-hook] Processed audio for {event.user_id}")
            
            return (
                f"📝 You: {user_input}\n\n"
                f"🤖 Agent: {response_text}"
            )
        
        except Exception as e:
            logger.error(f"[voice-hook] Audio failed: {e}", exc_info=True)
            return f"❌ Processing failed: {str(e)}"


# ============================================================================
# Gateway Hook Manager (SOLID: Single Responsibility)
# ============================================================================

class GatewayHookManager:
    """
    Manages builtin hooks for gateway message processing.
    
    Allows plugins to register hooks without modifying core adapters.
    SOLID: Dependency Inversion - depends on abstract hook interface.
    """
    
    def __init__(self):
        """Initialize hook manager."""
        self._hooks: list[GatewayMessageHook] = []
    
    def register_hook(self, hook: GatewayMessageHook) -> None:
        """Register a gateway message hook."""
        if hook not in self._hooks:
            self._hooks.append(hook)
            logger.info(f"[hooks] Registered {hook.__class__.__name__}")
    
    def unregister_hook(self, hook: GatewayMessageHook) -> None:
        """Unregister a gateway message hook."""
        if hook in self._hooks:
            self._hooks.remove(hook)
            logger.info(f"[hooks] Unregistered {hook.__class__.__name__}")
    
    async def before_message_processing(
        self,
        event: MessageEvent,
        gateway_runner: Any,
    ) -> Optional[Union[str, ErrorResponse]]:
        """
        Run all before_message_processing hooks.
        
        First hook to return non-None intercepts the message.
        """
        for hook in self._hooks:
            result = await hook.before_message_processing(event, gateway_runner)
            if result is not None:
                return result
        
        return None
    
    async def after_message_processing(
        self,
        event: MessageEvent,
        response: Any,
        gateway_runner: Any,
    ) -> Any:
        """Run all after_message_processing hooks."""
        for hook in self._hooks:
            response = await hook.after_message_processing(event, response, gateway_runner)
        
        return response


# ============================================================================
# Global Hook Manager Instance
# ============================================================================

_hook_manager: Optional[GatewayHookManager] = None


def get_hook_manager() -> GatewayHookManager:
    """Get or create global hook manager."""
    global _hook_manager
    if _hook_manager is None:
        _hook_manager = GatewayHookManager()
    return _hook_manager


def register_builtin_hooks() -> None:
    """Register all builtin gateway hooks.
    
    Called once at gateway startup.
    """
    manager = get_hook_manager()
    
    # Register voice agent interceptor
    voice_interceptor = VoiceAgentMessageInterceptor()
    manager.register_hook(voice_interceptor)
    
    logger.info("[hooks] Builtin hooks registered (voice agent)")


# ============================================================================
# Integration Points (Gateway Core Modifications Minimal)
# ============================================================================

async def process_message_with_hooks(
    event: MessageEvent,
    gateway_runner: Any,
    original_handler: callable,
) -> Any:
    """
    Process message through hooks before platform adapter.
    
    SOLID: Minimal integration point (strategy pattern).
    This is the only modification needed to gateway/run.py.
    
    Args:
        event: Message event
        gateway_runner: Gateway runner
        original_handler: Original platform _handle_message method
    
    Returns:
        Response or None to continue normal flow
    """
    manager = get_hook_manager()
    
    # Try hooks first
    result = await manager.before_message_processing(event, gateway_runner)
    if result is not None:
        return result
    
    # Continue normal processing
    return await original_handler(event)
