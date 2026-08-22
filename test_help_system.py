"""Tests for dynamic help system.

Verifies:
- YAML config loading and validation
- Runtime help content delivery
- Help command handlers
- All supported command formats
"""

import pytest
import tempfile
from pathlib import Path
import sys

# Ensure this repo (not any editable install of hermes-agent living
# elsewhere on the filesystem) is used when the test file is invoked
# directly.  Previously this file hardcoded "/home/ubuntu/hermes-agent"
# which shadowed the local checkout during CI runs on developer machines
# that keep a sibling clone under that path.  We now resolve the repo
# root relative to this file.
_REPO_ROOT = str(Path(__file__).resolve().parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gateway.help_config import (
    HelpConfigLoader,
    HelpConfigError,
    load_help_config,
    get_help_config,
)
from gateway.help_menu import (
    get_help_topics,
    get_command_categories,
    format_help_topic,
    format_help_index,
    format_quick_reference,
    get_help,
    get_help_by_topic,
)


class TestHelpConfigLoader:
    """Test configuration loading and validation."""

    def test_load_default_config(self):
        """Test loading default help.yaml config."""
        loader = HelpConfigLoader()
        config = loader.load()
        
        # Verify structure
        assert isinstance(config, dict)
        assert "agents" in config
        assert "instances" in config
        assert "general" in config
        assert "categories" in config
        assert "quick_reference" in config

    def test_config_has_required_topic_keys(self):
        """Test that each topic has required keys."""
        config = get_help_config()
        
        for topic_name in ["agents", "instances", "general"]:
            topic = config[topic_name]
            assert "title" in topic
            assert "description" in topic
            assert "example" in topic
            # ``commands`` was intentionally removed from help.yaml — command
            # descriptions now come from ``hermes_cli.commands.COMMAND_REGISTRY``
            # (see tests/gateway/test_help_topics_from_registry.py). We
            # therefore assert its ABSENCE here as a regression guard.
            assert "commands" not in topic, (
                f"help.yaml topic '{topic_name}' must not carry a hardcoded "
                f"'commands' block — descriptions live in COMMAND_REGISTRY."
            )

            # Verify types of remaining keys
            assert isinstance(topic["title"], str)
            assert isinstance(topic["description"], str)
            assert isinstance(topic["example"], str)

    def test_commands_are_non_empty(self):
        """Each topic's rendered ``commands`` (derived from COMMAND_REGISTRY)
        must be non-empty."""
        # After the P2 refactor, ``config[topic]["commands"]`` is no longer
        # populated from yaml; the runtime shape lives on
        # ``help_menu.get_help_topics()`` which merges yaml metadata with
        # registry-derived commands.
        from gateway.help_menu import get_help_topics
        topics = get_help_topics()

        for topic_name in ["agents", "instances", "general"]:
            topic = topics[topic_name]
            commands = topic["commands"]
            assert len(commands) > 0, f"Topic {topic_name} has no commands"

            # Verify command format
            for cmd_name, cmd_desc in commands.items():
                assert isinstance(cmd_name, str)
                assert isinstance(cmd_desc, str)
                assert len(cmd_name) > 0
                assert len(cmd_desc) > 0

    def test_categories_order(self):
        """Test that categories are ordered."""
        config = get_help_config()
        categories = config.get("categories", [])
        
        assert isinstance(categories, list)
        assert len(categories) > 0
        assert all(cat in ["agents", "instances", "general"] for cat in categories)

    def test_quick_reference_structure(self):
        """Test quick reference structure."""
        config = get_help_config()
        quick_ref = config.get("quick_reference", [])
        
        assert isinstance(quick_ref, list)
        assert len(quick_ref) > 0
        
        for section in quick_ref:
            assert "section" in section
            assert "commands" in section
            assert isinstance(section["commands"], list)
            assert len(section["commands"]) > 0

    def test_invalid_yaml_path(self):
        """Test error handling for missing config."""
        loader = HelpConfigLoader("/nonexistent/help.yaml")
        
        with pytest.raises(HelpConfigError):
            loader.load()

    def test_invalid_yaml_content(self):
        """Test error handling for invalid YAML."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("invalid: yaml: content: [")
            f.flush()
            
            try:
                loader = HelpConfigLoader(f.name)
                with pytest.raises(HelpConfigError):
                    loader.load()
            finally:
                Path(f.name).unlink()

    def test_missing_required_section(self):
        """Test validation of required sections."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("""
agents:
  title: "Test"
  description: "Test"
  commands: {}
  example: "Test"
categories: []
quick_reference: []
""")
            f.flush()
            
            try:
                loader = HelpConfigLoader(f.name)
                with pytest.raises(HelpConfigError, match="Missing required help sections"):
                    loader.load()
            finally:
                Path(f.name).unlink()

    def test_config_reload(self):
        """Test reloading configuration."""
        loader = HelpConfigLoader()
        config1 = loader.load()
        config2 = loader.reload()
        
        assert config1 is not None
        assert config2 is not None
        # Content should be the same
        assert config1.keys() == config2.keys()


class TestHelpTopics:
    """Test help topic retrieval."""

    def test_get_help_topics(self):
        """Test getting all help topics."""
        topics = get_help_topics()
        
        assert isinstance(topics, dict)
        assert "agents" in topics
        assert "instances" in topics
        assert "general" in topics

    def test_get_command_categories(self):
        """Test getting command categories."""
        categories = get_command_categories()
        
        assert isinstance(categories, list)
        assert len(categories) > 0
        assert "agents" in categories


class TestHelpFormatting:
    """Test help text formatting."""

    def test_format_help_index(self):
        """Test formatting help index."""
        text = format_help_index()
        
        assert isinstance(text, str)
        assert len(text) > 0
        assert "Hermes WhatsApp Gateway" in text
        assert "/help agents" in text or "agents" in text.lower()
        assert "/help instances" in text or "instances" in text.lower()

    def test_format_agents_help(self):
        """Test formatting agents help topic."""
        text = format_help_topic("agents")
        
        assert isinstance(text, str)
        assert len(text) > 0
        assert "🤖" in text or "Executive Agent" in text
        assert "/load-demis" in text or "load-demis" in text
        assert "Example:" in text

    def test_format_instances_help(self):
        """Test formatting instances help topic."""
        text = format_help_topic("instances")
        
        assert isinstance(text, str)
        assert len(text) > 0
        assert "🌐" in text or "Instance" in text.lower()
        assert "/switch-hermes2" in text or "switch-hermes2" in text
        assert "Example:" in text

    def test_format_general_help(self):
        """Test formatting general help topic."""
        text = format_help_topic("general")
        
        assert isinstance(text, str)
        assert len(text) > 0
        assert "📋" in text or "General" in text
        assert "/help" in text or "help" in text.lower()
        assert "Example:" in text

    def test_format_invalid_topic(self):
        """Test error handling for invalid topic."""
        text = format_help_topic("nonexistent")
        
        assert "not found" in text.lower()

    def test_format_quick_reference(self):
        """Test formatting quick reference."""
        text = format_quick_reference()
        
        assert isinstance(text, str)
        assert len(text) > 0
        assert "Quick Command Reference" in text

    def test_help_functions_api(self):
        """Test public help API functions."""
        # Test get_help() with no topic
        help_index = get_help()
        assert isinstance(help_index, str)
        assert len(help_index) > 0

        # Test get_help() with topic
        help_agents = get_help("agents")
        assert isinstance(help_agents, str)
        assert len(help_agents) > 0

        # Test get_help_by_topic()
        help_instances = get_help_by_topic("instances")
        assert isinstance(help_instances, str)
        assert len(help_instances) > 0


class TestHelpContent:
    """Test specific help content."""

    def test_all_agents_listed(self):
        """Test that all agents are documented."""
        text = format_help_topic("agents")
        
        # Check for key agents
        assert "demis" in text.lower()
        assert "jony" in text.lower()
        assert "jeff" in text.lower()

    def test_all_instances_documented(self):
        """Test that instance commands are documented."""
        text = format_help_topic("instances")
        
        # Check for key instance commands
        assert "switch-local" in text.lower()
        assert "switch-hermes2" in text.lower()
        assert "hermes-list" in text.lower()

    def test_general_commands_present(self):
        """Test that general commands are documented.

        After the P2 refactor the 'general' topic is populated from
        COMMAND_REGISTRY entries with category in {"Help", "Info"} — the
        previous yaml-authored list included fictional entries like
        /clear and /models that were never real gateway commands.  We
        now assert against commands that actually exist in the registry.
        """
        text = format_help_topic("general").lower()

        # Real gateway-available commands from the "Info" / "Help" categories
        assert "help" in text
        assert "whoami" in text
        assert "version" in text

    def test_commands_have_descriptions(self):
        """Test that all commands have descriptions."""
        for topic_name in ["agents", "instances", "general"]:
            topics = get_help_topics()
            commands = topics[topic_name]["commands"]
            
            for cmd_name, cmd_desc in commands.items():
                assert len(cmd_desc) > 0, f"Command {cmd_name} has no description"


class TestConfigIntegration:
    """Test configuration integration."""

    def test_multiple_load_calls_consistent(self):
        """Test that multiple loads return consistent data."""
        config1 = get_help_config()
        config2 = get_help_config()
        
        # Should return same data
        assert config1.keys() == config2.keys()
        assert config1["agents"]["title"] == config2["agents"]["title"]

    def test_all_sections_accessible(self):
        """Test that all config sections are accessible."""
        config = get_help_config()
        
        # Test topics
        # ``commands`` no longer lives inside config (yaml) — it is
        # merged in at read time by ``help_menu.get_help_topics()``.
        from gateway.help_menu import get_help_topics
        topics = get_help_topics()
        assert topics["agents"]["commands"]["load-demis"]
        assert topics["instances"]["commands"]["switch-local"]
        assert topics["general"]["commands"]["help"]
        
        # Test metadata
        assert len(config["categories"]) > 0
        assert len(config["quick_reference"]) > 0


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v", "--tb=short"])
