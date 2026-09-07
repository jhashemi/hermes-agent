"""Help menu system for WhatsApp gateway commands.

Provides hierarchical help organization:
  /help or /?                     → Show all command categories
  /help agents                    → Show executive agent commands
  /help instances                 → Show instance switching commands
  /help general                   → Show general commands
  /help-agents (alias)
  /help-instances (alias)

Command descriptions are the single source of truth in
``hermes_cli.commands.COMMAND_REGISTRY`` — every ``CommandDef`` provides its
own description and category.  This module groups commands by
:attr:`CommandDef.category` and renders them into the per-topic help text.

Presentation-only metadata (topic titles, prose descriptions, examples,
the quick-reference welcome card) still lives in ``help.yaml`` because it
is authored copy, not machine-generated command documentation.  The two
are joined at read time so there is exactly one place to edit each fact:

  * Change what a command *does* → edit its ``CommandDef``
  * Change how a topic is *introduced* → edit ``help.yaml``
"""

from typing import Any, Optional, Dict, List
from gateway.platforms.base import MessageEvent
from gateway.help_config import get_help_config, HelpConfigError
import logging

logger = logging.getLogger(__name__)


# ============================================================================
# Category → topic mapping
# ============================================================================
#
# COMMAND_REGISTRY groups commands by ``CommandDef.category`` (a semantic
# label like "Agents", "Instances", "Help", "Session"). The gateway help
# system organises them into three user-facing topics: agents, instances,
# general. This mapping is the bridge.
#
# Adding a new CommandDef with one of these categories will automatically
# make it appear in the corresponding /help topic — no help.yaml edit
# required. That's the acceptance criterion "adding new command auto-adds
# to help".
CATEGORY_TO_TOPIC: Dict[str, str] = {
    "Agents": "agents",
    "Instances": "instances",
    "Help": "general",
    "Info": "general",
}

# Topics for which the command list is derived from COMMAND_REGISTRY.
# help.yaml still supplies title / description / example for these.
_DYNAMIC_TOPICS = frozenset({"agents", "instances", "general"})


# ============================================================================
# Registry-driven command list
# ============================================================================

def _registry_commands_by_topic() -> Dict[str, Dict[str, str]]:
    """Return {topic: {command_name: description}} derived from COMMAND_REGISTRY.

    Only commands available on gateway surfaces are included:
      - ``cli_only=True`` commands are excluded (never surfaced in gateway
        help).
      - ``gateway_only=True`` commands are included.
      - Default (both surfaces) commands are included.

    Aliases are not listed as separate entries — the canonical name wins.
    """
    # Local import to avoid a hard module-level dependency during test
    # collection when hermes_cli isn't on the path.  ``COMMAND_REGISTRY`` is
    # always available at runtime because the gateway ships alongside
    # hermes_cli.
    from hermes_cli.commands import COMMAND_REGISTRY

    topics: Dict[str, Dict[str, str]] = {topic: {} for topic in _DYNAMIC_TOPICS}
    for cmd in COMMAND_REGISTRY:
        if cmd.cli_only:
            # ``cli_only`` commands never appear on gateway help surfaces.
            # (``gateway_config_gate`` isn't consulted here — help output
            # advertises the general shape of the surface, not per-config
            # runtime availability.)
            continue
        topic = CATEGORY_TO_TOPIC.get(cmd.category)
        if topic is None:
            continue
        topics[topic][cmd.name] = cmd.description
    return topics


# ============================================================================
# Help Content Loading (metadata from YAML, commands from registry)
# ============================================================================

def get_help_topics() -> Dict[str, Dict[str, Any]]:
    """Get help topics with commands derived from COMMAND_REGISTRY.

    For each topic (agents, instances, general) the topic *metadata*
    (``title``, ``description``, ``example``) is loaded from ``help.yaml``
    and the ``commands`` map is built from ``COMMAND_REGISTRY`` grouped by
    :attr:`CommandDef.category`.

    Returns:
        Dictionary of help topics: ``{topic_name: {title, description,
        commands: {name: desc}, example}}``.

    Raises:
        HelpConfigError: If the yaml metadata cannot be loaded.
    """
    try:
        config = get_help_config()
    except HelpConfigError as e:
        logger.error(f"Failed to load help configuration: {e}")
        raise

    registry_commands = _registry_commands_by_topic()

    topics: Dict[str, Dict[str, Any]] = {}
    for topic_name in _DYNAMIC_TOPICS:
        meta = config.get(topic_name)
        if not isinstance(meta, dict):
            continue
        topics[topic_name] = {
            "title": meta.get("title", ""),
            "description": meta.get("description", ""),
            "commands": registry_commands.get(topic_name, {}),
            "example": meta.get("example", ""),
        }
    return topics


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

    commands = data["commands"] or {}
    for cmd, desc in commands.items():
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
