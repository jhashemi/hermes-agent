"""
Voice Agent Command Handlers - Hermes Tool System Compatible

Handles /load-{agent}, /voice-agents, /voice-disconnect commands
with audio message support and voice synthesis.

Follows Hermes command handler dispatch pattern:
- Each handler: async def handler(gateway_runner, event) -> Union[str, ErrorResponse]
- Registered in COMMAND_HANDLERS dict
- Dispatched from gateway/run.py
"""

from typing import Optional, Union
import logging

from gateway.platforms.base import MessageEvent, EphemeralReply, cache_audio_from_url, MessageType
from gateway.error_response import (
    ErrorResponse,
    ErrorCode,
    ErrorSeverity,
    create_access_denied_error,
    create_not_found_error,
)
from gateway.access_control import get_access_manager

logger = logging.getLogger(__name__)

# Import voice bridge (from executive agents platform)
import sys
from pathlib import Path
sys.path.insert(0, '/home/ubuntu/executive_agents_platform')
from loader.whatsapp_voice_bridge import WhatsAppVoiceAgentBridge


# ============================================================================
# Global Voice Bridge Instance (Singleton)
# ============================================================================

_voice_bridge: Optional[WhatsAppVoiceAgentBridge] = None


def _get_voice_bridge() -> WhatsAppVoiceAgentBridge:
    """Get or create singleton voice bridge instance."""
    global _voice_bridge
    if _voice_bridge is None:
        logger.info("[voice] Initializing voice bridge")
        try:
            _voice_bridge = WhatsAppVoiceAgentBridge()
        except Exception as e:
            logger.error(f"[voice] Failed to initialize voice bridge: {e}")
            raise
    return _voice_bridge


# ============================================================================
# Voice Command Handlers (Hermes Tool System Pattern)
# ============================================================================

async def handle_voice_load_command(
    gateway_runner,
    event: MessageEvent,
    agent_id: str,
) -> Union[str, EphemeralReply, ErrorResponse]:
    """Handle /load-{agent} command with voice bridge.
    
    Signature follows Hermes pattern: (gateway_runner, event) -> response
    
    Args:
        gateway_runner: The GatewayRunner instance
        event: The MessageEvent from platform adapter
        agent_id: Agent to load (e.g., "demis_hassabis")
    
    Returns:
        Response string, error response, or ephemeral reply
    """
    # Access control check
    access_mgr = get_access_manager()
    if not access_mgr.has_access(event):
        error = create_access_denied_error(
            user_id=access_mgr.get_user_id(event),
            command=f"load-voice-{agent_id}",
            reason="You don't have permission to load voice agents.",
        )
        return error.to_emoji_response()
    
    try:
        bridge = _get_voice_bridge()
        result = await bridge.handle_load_command(event.user_id, agent_id)
        
        if "error" in result:
            error = ErrorResponse(
                code=ErrorCode.NOT_FOUND,
                message=result["error"],
                severity=ErrorSeverity.MEDIUM.value,
                user_id=access_mgr.get_user_id(event),
            )
            return error.to_emoji_response()
        
        # Store session on gateway runner
        if not hasattr(gateway_runner, "_voice_sessions"):
            gateway_runner._voice_sessions = {}
        
        gateway_runner._voice_sessions[event.user_id] = {
            "session_id": result["session_id"],
            "agent_id": agent_id,
            "chat_id": event.chat_id,
        }
        
        logger.info(f"[voice] User {event.user_id} loaded agent {agent_id}")
        
        # Return confirmation message
        return result.get("message", f"✅ Loaded {agent_id}")
    
    except Exception as e:
        logger.error(f"[voice] Load command failed: {e}", exc_info=True)
        error = ErrorResponse(
            code=ErrorCode.OPERATION_FAILED,
            message=f"Failed to load voice agent: {str(e)}",
            severity=ErrorSeverity.HIGH.value,
            user_id=access_mgr.get_user_id(event),
        )
        return error.to_emoji_response()


async def handle_voice_agents_list_command(
    gateway_runner,
    event: MessageEvent,
) -> str:
    """Handle /voice-agents command to list available agents."""
    try:
        bridge = _get_voice_bridge()
        agents = bridge.loader.list_agents()
        
        if not agents:
            return "❌ No voice agents configured"
        
        lines = ["🤖 **Voice Agents Available:**\n"]
        for agent in agents:
            status = "✅" if agent["status"] == "ready" else "⏳"
            lines.append(
                f"{status} /load-{agent['id']:15} "
                f"{agent['name']:20} "
                f"({agent['questions']} questions)"
            )
        
        return "\n".join(lines)
    
    except Exception as e:
        logger.error(f"[voice] List agents failed: {e}", exc_info=True)
        return f"❌ Error listing agents: {str(e)}"


async def handle_voice_disconnect_command(
    gateway_runner,
    event: MessageEvent,
) -> str:
    """Handle /voice-disconnect command to disconnect from voice agent."""
    if not hasattr(gateway_runner, "_voice_sessions"):
        return "ℹ️ No voice agent connected"
    
    session = gateway_runner._voice_sessions.get(event.user_id)
    if not session:
        return "ℹ️ No voice agent connected"
    
    agent_id = session["agent_id"]
    del gateway_runner._voice_sessions[event.user_id]
    
    logger.info(f"[voice] User {event.user_id} disconnected from {agent_id}")
    
    return f"✅ Disconnected from {agent_id}. Use /voice-agents to reconnect."


async def handle_voice_audio_message(
    gateway_runner,
    event: MessageEvent,
) -> Union[str, EphemeralReply, None]:
    """Handle audio messages for connected voice agents.
    
    Called from WhatsApp platform adapter when audio message detected.
    
    Args:
        gateway_runner: The GatewayRunner instance
        event: The MessageEvent (must have media_url for audio)
    
    Returns:
        Response string or None if no voice session active
    """
    # Check if user has active voice session
    if not hasattr(gateway_runner, "_voice_sessions"):
        return None
    
    session = gateway_runner._voice_sessions.get(event.user_id)
    if not session:
        return None
    
    session_id = session["session_id"]
    agent_id = session["agent_id"]
    
    try:
        # Download audio file
        audio_bytes = await cache_audio_from_url(event.media_url)
        
        # Process via voice bridge
        bridge = _get_voice_bridge()
        response = await bridge.handle_audio_message(
            user_id=event.user_id,
            audio_bytes=audio_bytes,
            session_id=session_id
        )
        
        if "error" in response:
            logger.error(f"[voice] Audio processing failed: {response['error']}")
            return f"❌ {response['error']}"
        
        # Return text response with transcription
        user_input = response.get("user_input", "[voice]")
        response_text = response.get("response_text", "")
        
        logger.info(f"[voice] Processed audio for {event.user_id}, agent {agent_id}")
        
        return (
            f"📝 You: {user_input}\n\n"
            f"🤖 Agent: {response_text}"
        )
    
    except Exception as e:
        logger.error(f"[voice] Audio message failed: {e}", exc_info=True)
        return f"❌ Audio processing failed: {str(e)}"


# ============================================================================
# Command Handler Registry (Hermes Tool System)
# ============================================================================

VOICE_COMMAND_HANDLERS = {
    # Voice agent loading - /load-{agent} commands
    "load-demis": lambda gr, ev: handle_voice_load_command(gr, ev, "demis_hassabis"),
    "load-steve-jobs": lambda gr, ev: handle_voice_load_command(gr, ev, "steve_jobs"),
    "load-steve": lambda gr, ev: handle_voice_load_command(gr, ev, "steve_jobs"),  # Alias
    "load-jony": lambda gr, ev: handle_voice_load_command(gr, ev, "jony_ive"),
    "load-jeff": lambda gr, ev: handle_voice_load_command(gr, ev, "jeff_dean"),
    "load-knuth": lambda gr, ev: handle_voice_load_command(gr, ev, "donald_knuth"),
    "load-tigani": lambda gr, ev: handle_voice_load_command(gr, ev, "jordan_tigani"),
    "load-turing": lambda gr, ev: handle_voice_load_command(gr, ev, "alan_turing"),
    
    # Voice agent management
    "voice-agents": handle_voice_agents_list_command,
    "voice-list": handle_voice_agents_list_command,  # Alias
    "voice-disconnect": handle_voice_disconnect_command,
}


def get_voice_command_handler(command_name: str):
    """Get handler for a voice command.
    
    Follows Hermes pattern: returns callable or None
    """
    if not command_name:
        return None
    canonical = command_name.lower().lstrip("/")
    return VOICE_COMMAND_HANDLERS.get(canonical)


def is_voice_command(command_name: Optional[str]) -> bool:
    """Check if a command is a voice-related command."""
    if not command_name:
        return False
    return get_voice_command_handler(command_name) is not None
