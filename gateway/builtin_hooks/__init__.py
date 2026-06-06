"""
Builtin Gateway Hooks - Channel-Agnostic Message Interception

Provides a hook mechanism for message interception without modifying
core platform adapters. Follows SOLID design principles.

Hooks are registered at gateway startup and can be added/removed dynamically.
Each hook implements GatewayMessageHook interface and is responsible for
deciding whether to intercept or pass through messages.

The voice agent interceptor handles:
- /load-{agent} commands → create voice session
- /voice-agents → list available agents  
- /voice-disconnect → disconnect from agent
- Audio messages with active voice session → transcribe + respond

All hooks are channel-agnostic: work identically across Telegram,
WhatsApp, Discord, Signal, and other platforms.
"""

import logging

logger = logging.getLogger(__name__)


async def initialize_builtin_hooks():
    """
    Initialize all builtin gateway hooks.
    
    Called once at gateway startup in GatewayRunner.__init__().
    
    Registers:
    - Voice agent message interceptor (channel-agnostic)
    - (Future hooks can be added here without modifying core code)
    """
    from gateway.builtin_hooks.voice_agent_hook import (
        register_builtin_hooks as register_voice_hooks,
    )
    
    logger.info("[hooks] Initializing builtin gateway hooks...")
    
    # Register voice agent hook
    try:
        register_voice_hooks()
        logger.info("[hooks] ✓ Voice agent hook registered (channel-agnostic)")
    except Exception as e:
        logger.error(f"[hooks] ✗ Failed to register voice hook: {e}")
    
    # Future hooks can be registered here
    # try:
    #     register_some_other_hook()
    #     logger.info("[hooks] ✓ Other hook registered")
    # except Exception as e:
    #     logger.error(f"[hooks] ✗ Failed to register other hook: {e}")
    
    logger.info("[hooks] Initialization complete")


__all__ = [
    "initialize_builtin_hooks",
    "get_hook_manager",
]


def get_hook_manager():
    """Convenience re-export of the hook manager for run.py."""
    from gateway.builtin_hooks.voice_agent_hook import get_hook_manager as _gm
    return _gm()