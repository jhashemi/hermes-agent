"""Help menu system for WhatsApp gateway commands.

Provides hierarchical help organization:
  /help or /?                     → Show all command categories
  /help agents                    → Show executive agent commands
  /help instances                 → Show instance switching commands
  /help general                   → Show general commands
  /help-agents (alias)
  /help-instances (alias)
"""

from typing import Optional, Dict, List
from gateway.platforms.base import MessageEvent


# ============================================================================
# Help Content Registry
# ============================================================================

HELP_TOPICS: Dict[str, Dict[str, str]] = {
    "agents": {
        "title": "🤖 Executive Agent Commands",
        "description": "Connect to expert personas with specialized knowledge.",
        "commands": {
            "load-demis": "Connect to Demis Hassabis (DeepMind co-founder, AI researcher)",
            "load-jony": "Connect to Jony Ive (Apple design chief, product visionary)",
            "load-jeff": "Connect to Jeff Dean (Google AI/systems researcher)",
            "load-knuth": "Connect to Donald Knuth (Computer science pioneer, TAOCP)",
            "load-tigani": "Connect to Jordan Tigani (BigQuery architect, data warehouse)",
            "load-turing": "Connect to Alan Turing (Computing theory pioneer)",
            "agents-list": "List all available executive agents",
            "agents-disconnect": "Disconnect from current agent and return to default",
        },
        "example": (
            "Example:\n"
            "  User: /load-demis\n"
            "  System: Switched to Demis Hassabis\n"
            "  User: How should we approach AGI safety?\n"
            "  Agent: [Responds as Demis Hassabis]"
        ),
    },
    "instances": {
        "title": "🌐 Multi-Instance Orchestration",
        "description": "Control which Hermes instance executes your requests.",
        "commands": {
            "switch-local": "Switch to local Hermes instance (WhatsApp gateway)",
            "switch-hermes2": "Switch to remote Hermes instance (agent execution layer)",
            "hermes-list": "List all available Hermes instances and status",
            "hermes-status": "Show current active instance and connection health",
        },
        "example": (
            "Example:\n"
            "  User: /hermes-list\n"
            "  System: Shows 🟢 LOCAL and 🔵 REMOTE (hermes2) instances\n"
            "  User: /switch-hermes2\n"
            "  System: Switched to remote instance (hermes2)\n"
            "  User: What is the meaning of life?\n"
            "  Agent: [Executes on hermes2]"
        ),
    },
    "general": {
        "title": "📋 General Commands",
        "description": "Common utility commands.",
        "commands": {
            "help": "Show this help menu (or /help <topic>)",
            "status": "Show current Hermes gateway status",
            "clear": "Clear conversation history",
            "models": "Show available AI models",
            "settings": "View or change user settings",
        },
        "example": (
            "Example:\n"
            "  User: /help agents\n"
            "  System: Shows all agent loading commands\n"
            "  User: /status\n"
            "  System: Shows gateway health and model info"
        ),
    },
}

COMMAND_CATEGORIES = [
    "agents",
    "instances",
    "general",
]


# ============================================================================
# Help Formatting
# ============================================================================

def format_help_topic(topic: str) -> str:
    """Format detailed help for a specific topic."""
    if topic not in HELP_TOPICS:
        return f"❌ Help topic '{topic}' not found. Try: /help"

    data = HELP_TOPICS[topic]
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
    """Format the help index (top-level menu)."""
    lines = [
        "📚 **Hermes WhatsApp Gateway — Command Help**\n",
        "Pick a topic to learn more:\n",
    ]

    for topic in COMMAND_CATEGORIES:
        data = HELP_TOPICS[topic]
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
    """Format a quick reference card (for welcome message)."""
    lines = [
        "🎯 **Quick Command Reference**",
        "",
        "Instance Control:",
        "  /hermes-list      List instances",
        "  /switch-hermes2   Route to agent layer",
        "",
        "Agent Personas:",
        "  /load-demis       Chat as Demis Hassabis",
        "  /load-jony        Chat as Jony Ive",
        "  /agents-list      All personas",
        "",
        "Help:",
        "  /help             Command help",
        "  /help agents      Agent commands",
        "",
    ]
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
