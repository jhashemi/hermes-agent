"""Executive Voice Agents Plugin for Hermes Gateway."""
from .voice_agents_plugin import (
    pre_gateway_dispatch_hook,
    register,
    _get_registry,
    _sessions,
    VoiceAgentSession,
    VoiceAgentRegistry,
)
__all__ = ["register", "pre_gateway_dispatch_hook"]
