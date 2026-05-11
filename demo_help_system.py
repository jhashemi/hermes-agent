#!/usr/bin/env python
"""Demonstration of the dynamic help system.

Shows:
- Loading help configuration from YAML
- Displaying all help topics
- Command handling
- Content delivery
"""

import sys
sys.path.insert(0, "/home/ubuntu/hermes-agent")

from gateway.help_menu import (
    get_help,
    get_help_by_topic,
    format_help_index,
    format_help_topic,
    format_quick_reference,
    is_help_command,
)


def print_section(title, content):
    """Print a formatted section."""
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}\n")
    print(content)


def main():
    """Run help system demonstration."""
    print("\n" + "="*70)
    print("  HERMES DYNAMIC HELP SYSTEM DEMONSTRATION")
    print("="*70)

    # Test 1: Load help index (main help)
    print_section(
        "TEST 1: /help command (show index)",
        get_help()
    )

    # Test 2: Get help by topic - agents
    print_section(
        "TEST 2: /help agents command",
        get_help_by_topic("agents")
    )

    # Test 3: Get help by topic - instances
    print_section(
        "TEST 3: /help instances command",
        get_help_by_topic("instances")
    )

    # Test 4: Get help by topic - general
    print_section(
        "TEST 4: /help general command",
        get_help_by_topic("general")
    )

    # Test 5: Quick reference
    print_section(
        "TEST 5: Quick Reference (for welcome message)",
        format_quick_reference()
    )

    # Test 6: Command detection
    print_section(
        "TEST 6: Help Command Detection",
        format_command_detection()
    )

    # Test 7: Error handling
    print_section(
        "TEST 7: Invalid Topic Handling",
        format_help_topic("nonexistent_topic")
    )

    print("\n" + "="*70)
    print("  DEMONSTRATION COMPLETE")
    print("="*70 + "\n")


def format_command_detection():
    """Test command detection logic."""
    lines = []
    test_commands = [
        "help",
        "?",
        "help-agents",
        "help-instances",
        "help-general",
        "/help",
        "/help agents",
        "not_a_help_command",
    ]

    for cmd in test_commands:
        is_help = is_help_command(cmd)
        status = "✓ DETECTED" if is_help else "✗ NOT DETECTED"
        lines.append(f"  {cmd:25} → {status}")

    return "\n".join(lines)


if __name__ == "__main__":
    main()
