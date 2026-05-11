"""Help menu system for WhatsApp gateway commands.

Provides hierarchical help organization:
  /help or /?                     → Show all command categories
  /help agents                    → Show executive agent commands
  /help instances                 → Show instance switching commands
  /help general                   → Show general commands
  /help-agents (alias)
  /help-instances (alias)

Help content is loaded from help.yaml at runtime with validation.
"""

from typing import Optional, Dict, List
from gateway.platforms.base import MessageEvent
from gateway.help_config import get_help_config, HelpConfigError
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# Help Content Loading (Dynamic from YAML)
# ============================================================================

def get_help_topics() -> Dict[str, Dict[str, str]]:
    """Get help topics from loaded configuration.
    
    Returns:
        Dictionary of help topics (agents, instances, general).
        
    Raises:
        HelpConfigError: If configuration cannot be loaded.
    """
    try:
        config = get_help_config()
        # Extract help topics (all keys except metadata)
        topics = {
            key: value
            for key, value in config.items()
            if key in ("agents", "instances", "general")
        }
        return topics
    except HelpConfigError as e:
        logger.error(f"Failed to load help configuration: {e}")
        raise


def get_command_categories() -> List[str]:
    """Get ordered list of help categories from config.
    
    Returns:
        List of category names in order.
        
    Raises:
        HelpConfigError: If configuration cannot be loaded.
    """
    try:
        config = get_help_config()
        return config.get("categories", ["agents", "instances", "general"])
    except HelpConfigError as e:
        logger.error(f"Failed to load help configuration: {e}")
        raise


# ============================================================================
# Help Formatting
# ============================================================================

def format_help_topic(topic: str) -> str:
    """Format detailed help for a specific topic.
    
    Args:
        topic: Name of the help topic (agents, instances, general).
        
    Returns:
        Formatted help text for the topic.
    """
    try:
        help_topics = get_help_topics()
    except HelpConfigError as e:
        return f"❌ Error loading help: {e}"

    if topic not in help_topics:
        return f"❌ Help topic '{topic}' not found. Try: /help"

    data = help_topics[topic]
    lines = [
        f"\n{data['title']}",
        "=" * 50,
        f"{data['description']}\n",
        "Commands:",
    ]

    for cmd, desc in data["commands"].items():
        lines.append(f"  /{cmd:20} {desc}")

    lines.extend([
        "",
        data["example"],
        "",
    ])

    return "\n".join(lines)


def format_help_index() -> str:
    """Format the help index (top-level menu).
    
    Returns:
        Formatted help index text.
    """
    try:
        help_topics = get_help_topics()
        categories = get_command_categories()
    except HelpConfigError as e:
        return f"❌ Error loading help: {e}"

    lines = [
        "📚 **Hermes WhatsApp Gateway — Command Help**\n",
        "Pick a topic to learn more:\n",
    ]

    for topic in categories:
        if topic in help_topics:
            data = help_topics[topic]
            lines.append(f"  /help {topic:15} → {data['title']}")

    lines.extend([
        "",
        "💡 Quick Start:",
        "  /switch-hermes2     → Route to agent instance",
        "  /load-demis         → Chat as Demis Hassabis",
        "  /hermes-list        → See available instances",
        "",
        "Need more info?",
        "  /help agents        → All persona commands",
        "  /help instances     → Instance switching",
        "",
    ])

    return "\n".join(lines)


def format_quick_reference() -> str:
    """Format a quick reference card (for welcome message).
    
    Returns:
        Formatted quick reference text.
    """
    try:
        config = get_help_config()
        quick_ref = config.get("quick_reference", [])
    except HelpConfigError as e:
        logger.error(f"Failed to load quick reference: {e}")
        quick_ref = []

    lines = ["🎯 **Quick Command Reference**", ""]

    for section in quick_ref:
        section_name = section.get("section", "")
        commands = section.get("commands", [])

        if section_name:
            lines.append(f"{section_name}:")
            for cmd in commands:
                lines.append(f"  {cmd}")
            lines.append("")

    return "\n".join(lines)


# ============================================================================
# Help Command Handlers
# ============================================================================

async def handle_help_command(
    gateway_runner,
    event: MessageEvent,
    topic: Optional[str] = None,
) -> str:
    """Handle /help or /help <topic> command.

    Args:
        gateway_runner: The GatewayRunner instance
        event: The message event
        topic: Optional help topic (agents, instances, general)

    Returns:
        Formatted help text
    """
    if not topic:
        return format_help_index()

    topic = topic.lower().strip("/")
    return format_help_topic(topic)


async def handle_help_agents_command(
    gateway_runner,
    event: MessageEvent,
) -> str:
    """Handle /help-agents or /help agents command."""
    return format_help_topic("agents")


async def handle_help_instances_command(
    gateway_runner,
    event: MessageEvent,
) -> str:
    """Handle /help-instances or /help instances command."""
    return format_help_topic("instances")


async def handle_help_general_command(
    gateway_runner,
    event: MessageEvent,
) -> str:
    """Handle /help-general or /help general command."""
    return format_help_topic("general")


# Help command registry (integrate with agent_commands.py)
HELP_COMMAND_HANDLERS = {
    "help": handle_help_command,
    "?": handle_help_command,  # Alias: /?
    "help-agents": handle_help_agents_command,
    "help-instances": handle_help_instances_command,
    "help-general": handle_help_general_command,
}


def is_help_command(command_name: Optional[str]) -> bool:
    """Check if a command is a help-related command."""
    if not command_name:
        return False
    canonical = command_name.lower().lstrip("/")
    return canonical in HELP_COMMAND_HANDLERS


def get_help_command_handler(command_name: str):
    """Get handler for a help command."""
    canonical = command_name.lower().lstrip("/")
    return HELP_COMMAND_HANDLERS.get(canonical)


# ============================================================================
# Public API (backward compatible)
# ============================================================================

def get_help(topic: Optional[str] = None) -> str:
    """Get help text for a topic or show index.
    
    Args:
        topic: Optional topic name. If None, shows index.
        
    Returns:
        Formatted help text.
    """
    if not topic:
        return format_help_index()
    return format_help_topic(topic.lower().strip("/"))


def get_help_by_topic(topic: str) -> str:
    """Get help text for a specific topic.
    
    Args:
        topic: Topic name (agents, instances, general).
        
    Returns:
        Formatted help text for the topic.
    """
    return format_help_topic(topic.lower().strip("/"))
