"""
Builtin Gateway Hooks - Extensible Message Processing

Provides a hook mechanism for message interception without modifying
core platform adapters. Follows SOLID design principles.

Hooks are registered at gateway startup and can be added/removed dynamically.
Each hook implements GatewayMessageHook interface and is responsible for
deciding whether to intercept or pass through messages.

Example hook:
    class MyHook(GatewayMessageHook):
        async def before_message_processing(self, event, gateway_runner):
            if my_condition(event):
                return my_response
            return None
    
    manager = get_hook_manager()
    manager.register_hook(MyHook())
"""

import logging

logger = logging.getLogger(__name__)


async def initialize_builtin_hooks():
    """
    Initialize all builtin gateway hooks.
    
    Called once at gateway startup in gateway/run.py.
    
    Registers:
    - Voice agent message interceptor
    - (Future hooks can be added here without modifying core code)
    """
    from gateway.builtin_hooks.voice_agent_hook import (
        register_builtin_hooks as register_voice_hooks,
    )
    
    logger.info("[hooks] Initializing builtin gateway hooks...")
    
    # Register voice agent hook
    try:
        register_voice_hooks()
        logger.info("[hooks] ✓ Voice agent hook registered")
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
]
