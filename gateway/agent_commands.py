"""Command handlers for executive agent loading and multi-instance orchestration.

This module contains handlers for:
  /load-demis, /load-jony, /load-jeff, /load-knuth, /load-tigani, /load-turing
  /agents-list, /agents-disconnect
  /switch-hermes, /switch-local, /hermes-list, /hermes-status

These handlers:
  - Switch agent personas by injecting system prompt override
  - Dispatch requests to different Hermes instances
"""

from typing import Optional, Union
from gateway.platforms.base import MessageEvent, EphemeralReply
from agent.persona_manager import PersonaManager, EXECUTIVE_PERSONAS
from gateway.instance_orchestrator import InstanceOrchestrator
from gateway.access_control import (
    get_access_manager,
    check_access_and_execute,
    is_access_command,
    get_access_command_handler,
    handle_access_list_command,
    handle_access_status_command,
    handle_access_grant_command,
    handle_access_revoke_command,
)
from gateway.help_menu import (
    HELP_COMMAND_HANDLERS,
    handle_help_command,
    handle_help_agents_command,
    handle_help_instances_command,
)


async def handle_load_agent_command(
    gateway_runner,
    event: MessageEvent,
    persona_key: str,
) -> Union[str, EphemeralReply]:
    """Generic handler for /load-<agent> commands.

    Args:
        gateway_runner: The GatewayRunner instance
        event: The message event
        persona_key: The persona to load (e.g., "demis_hassabis")

    Returns:
        Confirmation message or ephemeral reply
    """
    # Check access first
    access_mgr = get_access_manager()
    if not access_mgr.has_access(event):
        user_id = access_mgr.get_user_id(event)
        return (
            f"🚫 Access Denied\n\n"
            f"You don't have permission to load agent personas.\n"
            f"Your ID: {user_id}\n\n"
            f"Status: /access-status"
        )

    # Validate persona exists
    if persona_key not in EXECUTIVE_PERSONAS:
        return f"❌ Agent '{persona_key}' not found. Try /agents-list"

    persona_data = EXECUTIVE_PERSONAS[persona_key]

    # Check if voice clone is available
    if not persona_data.get("voice_uuid"):
        return (
            f"⚠️  {persona_data['name']} voice clone not ready yet. "
            f"Available agents: /agents-list"
        )

    # Get or create persona manager on the agent
    if not hasattr(gateway_runner, "_persona_manager"):
        gateway_runner._persona_manager = PersonaManager()

    persona_mgr: PersonaManager = gateway_runner._persona_manager

    # Set the persona
    success = persona_mgr.set_persona(persona_key)
    if not success:
        return f"❌ Could not load agent: {persona_key}"

    persona_name = persona_data["name"]
    return (
        f"🎤 Switched to **{persona_name}**\n\n"
        f"{persona_data['title']}\n\n"
        f"I'm ready to chat. What would you like to know?"
    )


async def handle_agents_list_command(
    gateway_runner,
    event: MessageEvent,
) -> str:
    """Handle /agents-list or /list-agents command."""
    lines = ["🤖 **Available Executive Agents:**\n"]

    for key, persona in EXECUTIVE_PERSONAS.items():
        status = "✓" if persona.get("voice_uuid") else "⏳"
        agent_name = key.replace("_", " ").title()
        lines.append(
            f"  {status} /load-{agent_name.lower().split()[0]:8} "
            f"{persona['name']:20} — {persona['description']}"
        )

    return "\n".join(lines)


async def handle_agents_disconnect_command(
    gateway_runner,
    event: MessageEvent,
) -> str:
    """Handle /agents-disconnect or /disconnect command."""
    if not hasattr(gateway_runner, "_persona_manager"):
        return "ℹ️ Not connected to any agent (using default)."

    persona_mgr: PersonaManager = gateway_runner._persona_manager
    current = persona_mgr.get_persona_name()

    if not current:
        return "ℹ️ Not connected to any agent (using default)."

    persona_mgr.reset_persona()
    return f"✓ Disconnected from **{current}**. Switched back to default."


# ============================================================================
# Instance Orchestration Commands
# ============================================================================


async def handle_switch_instance_command(
    gateway_runner,
    event: MessageEvent,
    instance_name: str,
) -> str:
    """Handle /switch-<instance> commands to change execution target.
    
    Args:
        gateway_runner: The GatewayRunner instance
        event: The message event
        instance_name: The instance to switch to (e.g., "local", "hermes2")
    
    Returns:
        Confirmation message
    """
    # Check access first
    access_mgr = get_access_manager()
    if not access_mgr.has_access(event):
        user_id = access_mgr.get_user_id(event)
        return (
            f"🚫 Access Denied\n\n"
            f"You don't have permission to switch instances.\n"
            f"Your ID: {user_id}\n\n"
            f"Status: /access-status"
        )

    # Initialize orchestrator if needed
    if not hasattr(gateway_runner, "_instance_orchestrator"):
        gateway_runner._instance_orchestrator = InstanceOrchestrator()
    
    orchestrator: InstanceOrchestrator = gateway_runner._instance_orchestrator
    
    # Attempt switch
    success = orchestrator.set_current_instance(
        instance_name,
        chat_id=event.chat_id,
    )
    
    if not success:
        return f"❌ Instance '{instance_name}' not found. Try /hermes-list"
    
    instance = orchestrator.get_instance(instance_name)
    status = "🟢 LOCAL" if instance.is_local else "🔵 REMOTE"
    
    return (
        f"{status} Switched to **{instance.name}**\n\n"
        f"{instance.description}\n\n"
        f"Next message will be routed here."
    )


async def handle_hermes_list_command(
    gateway_runner,
    event: MessageEvent,
) -> str:
    """Handle /hermes-list command to show available instances."""
    if not hasattr(gateway_runner, "_instance_orchestrator"):
        gateway_runner._instance_orchestrator = InstanceOrchestrator()
    
    orchestrator: InstanceOrchestrator = gateway_runner._instance_orchestrator
    return orchestrator.list_instances()


async def handle_hermes_status_command(
    gateway_runner,
    event: MessageEvent,
) -> str:
    """Handle /hermes-status command to show current instance."""
    if not hasattr(gateway_runner, "_instance_orchestrator"):
        gateway_runner._instance_orchestrator = InstanceOrchestrator()
    
    orchestrator: InstanceOrchestrator = gateway_runner._instance_orchestrator
    return await orchestrator.get_status(chat_id=event.chat_id)


# Mapping of canonical command names to handlers
AGENT_COMMAND_HANDLERS = {
    # Agent persona switching
    "load-demis": lambda gr, ev: handle_load_agent_command(gr, ev, "demis_hassabis"),
    "load-jony": lambda gr, ev: handle_load_agent_command(gr, ev, "jony_ive"),
    "load-jeff": lambda gr, ev: handle_load_agent_command(gr, ev, "jeff_dean"),
    "load-knuth": lambda gr, ev: handle_load_agent_command(gr, ev, "donald_knuth"),
    "load-tigani": lambda gr, ev: handle_load_agent_command(gr, ev, "jordan_tigani"),
    "load-turing": lambda gr, ev: handle_load_agent_command(gr, ev, "alan_turing"),
    "agents-list": handle_agents_list_command,
    "agents-disconnect": handle_agents_disconnect_command,
    # Instance orchestration
    "switch-local": lambda gr, ev: handle_switch_instance_command(gr, ev, "local"),
    "switch-hermes2": lambda gr, ev: handle_switch_instance_command(gr, ev, "hermes2"),
    "hermes-list": handle_hermes_list_command,
    "hermes-status": handle_hermes_status_command,
    # Help menu
    "help": handle_help_command,
    "?": handle_help_command,  # Alias: /?
    "help-agents": handle_help_agents_command,
    "help-instances": handle_help_instances_command,
    # Access control
    "access-list": handle_access_list_command,
    "access-status": handle_access_status_command,
    "access-grant": handle_access_grant_command,
    "access-revoke": handle_access_revoke_command,
}


def get_agent_command_handler(command_name: str):
    """Get handler for an agent-related command."""
    canonical = command_name.lower().lstrip("/")
    return AGENT_COMMAND_HANDLERS.get(canonical)


def is_agent_command(command_name: Optional[str]) -> bool:
    """Check if a command is an agent-related command."""
    if not command_name:
        return False
    return get_agent_command_handler(command_name) is not None
