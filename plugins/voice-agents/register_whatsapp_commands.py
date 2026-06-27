"""
WhatsApp Voice Agent Command Registration

Registers all voice agent commands with Hermes Gateway.
These commands become available in WhatsApp after gateway restart.
"""

import json
import yaml
from pathlib import Path


VOICE_COMMANDS = {
    "load_agent": {
        "command": "/load-{agent_id}",
        "description": "Load and connect to an executive voice agent",
        "examples": [
            "/load-demis (connect to Demis Hassabis)",
            "/load-steve-jobs (connect to Steve Jobs)"
        ],
        "handler": "VoiceAgentMessageInterceptor.handle_voice_load_command"
    },
    "list_agents": {
        "command": "/voice-agents",
        "description": "List all available executive voice agents",
        "handler": "VoiceAgentMessageInterceptor.handle_voice_agents_list_command"
    },
    "disconnect": {
        "command": "/voice-disconnect",
        "description": "End current voice agent session",
        "handler": "VoiceAgentMessageInterceptor.handle_voice_disconnect_command"
    },
    "agent_info": {
        "command": "/voice-info {agent_id}",
        "description": "Get info about a specific agent",
        "examples": [
            "/voice-info demis (get Demis profile)",
            "/voice-info steve-jobs (get Steve Jobs profile)"
        ],
        "handler": "VoiceAgentMessageInterceptor.handle_voice_agent_info_command"
    }
}


def register_voice_commands():
    """Register all voice commands with the gateway"""
    gateway_path = Path("/home/ubuntu/hermes-agent/gateway")
    commands_file = gateway_path / "builtin_hooks" / "voice_commands_registry.json"
    
    with open(commands_file, 'w') as f:
        json.dump(VOICE_COMMANDS, f, indent=2)
    
    print(f"✓ Registered {len(VOICE_COMMANDS)} voice commands")
    print("\nAvailable commands:")
    for cmd_name, cmd_info in VOICE_COMMANDS.items():
        print(f"  {cmd_info['command']:<25} - {cmd_info['description']}")


if __name__ == "__main__":
    register_voice_commands()
