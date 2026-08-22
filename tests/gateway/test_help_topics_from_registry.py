"""Tests for the registry-driven dynamic help system.

Verifies the P2 refactor (task t_9441f3da): help topics are built from
``hermes_cli.commands.COMMAND_REGISTRY`` — the single source of truth for
command descriptions — with only presentation metadata (titles, prose,
examples) coming from ``help.yaml``.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
import yaml

from gateway import help_menu
from gateway.help_config import HelpConfigLoader
from hermes_cli.commands import COMMAND_REGISTRY, CommandDef


class TestHelpTopicsFromRegistry:
    """HELP_TOPICS' command lists are derived from COMMAND_REGISTRY."""

    def test_agents_topic_lists_all_agents_category_commands(self):
        """Every CommandDef with category='Agents' must appear in the agents topic."""
        topics = help_menu.get_help_topics()
        agents_commands = topics["agents"]["commands"]

        expected = {
            cmd.name: cmd.description
            for cmd in COMMAND_REGISTRY
            if cmd.category == "Agents" and not cmd.cli_only
        }
        assert expected, "sanity: registry has at least one Agents command"
        assert agents_commands == expected

    def test_instances_topic_lists_all_instances_category_commands(self):
        """Every CommandDef with category='Instances' must appear in the instances topic."""
        topics = help_menu.get_help_topics()
        instances_commands = topics["instances"]["commands"]

        expected = {
            cmd.name: cmd.description
            for cmd in COMMAND_REGISTRY
            if cmd.category == "Instances" and not cmd.cli_only
        }
        assert expected, "sanity: registry has at least one Instances command"
        assert instances_commands == expected

    def test_general_topic_lists_help_and_info_category_commands(self):
        """Help/Info-categorised gateway commands appear in the general topic."""
        topics = help_menu.get_help_topics()
        general_commands = topics["general"]["commands"]

        expected = {
            cmd.name: cmd.description
            for cmd in COMMAND_REGISTRY
            if cmd.category in {"Help", "Info"} and not cmd.cli_only
        }
        assert expected
        assert general_commands == expected

    def test_cli_only_commands_never_in_gateway_help(self):
        """cli_only=True commands must not surface in any /help topic."""
        topics = help_menu.get_help_topics()

        cli_only_names = {c.name for c in COMMAND_REGISTRY if c.cli_only}
        for topic_name, data in topics.items():
            for cmd_name in data["commands"]:
                assert cmd_name not in cli_only_names, (
                    f"cli_only command '{cmd_name}' leaked into "
                    f"gateway /help {topic_name}"
                )


class TestNoHardcodedDescriptions:
    """Descriptions live in the registry — help.yaml no longer authors them."""

    def test_yaml_topic_blocks_do_not_carry_a_commands_dict(self):
        """After the refactor, help.yaml topic sections drop the redundant
        ``commands`` block — the registry is the sole source.
        """
        loader = HelpConfigLoader()
        raw = yaml.safe_load(loader.config_path.read_text(encoding="utf-8"))
        for topic in ("agents", "instances", "general"):
            assert topic in raw
            assert "commands" not in raw[topic], (
                f"help.yaml still hardcodes a 'commands' block under "
                f"'{topic}' — descriptions must come from COMMAND_REGISTRY, "
                f"not yaml."
            )

    def test_get_help_topics_returns_registry_descriptions_verbatim(self):
        """Descriptions rendered by help_menu MUST equal registry.description
        for at least one representative command per topic — proves the
        registry is the source (a divergent yaml value would fail).
        """
        topics = help_menu.get_help_topics()
        sample = {
            "agents": "load-demis",
            "instances": "switch-hermes2",
            "general": "help",
        }
        for topic_name, cmd_name in sample.items():
            reg = next(c for c in COMMAND_REGISTRY if c.name == cmd_name)
            assert topics[topic_name]["commands"][cmd_name] == reg.description


class TestNewCommandAutoAppears:
    """DoD: adding a new command auto-adds to help.

    We can't actually mutate COMMAND_REGISTRY (frozen semantically), so we
    monkeypatch the module-level import in help_menu's internal accessor to
    simulate a registry with one extra CommandDef.  This is the same
    contract the real registry exposes — if today it composes correctly,
    tomorrow's edit to COMMAND_REGISTRY will too.
    """

    def test_new_agents_command_appears_in_help_without_yaml_edit(self, monkeypatch):
        fake_cmd = CommandDef(
            name="load-fake-persona",
            description="Connect to the Fake Persona (unit-test only)",
            category="Agents",
            gateway_only=True,
        )
        extended = list(COMMAND_REGISTRY) + [fake_cmd]

        # Patch the exact import site used by _registry_commands_by_topic.
        monkeypatch.setattr(
            "hermes_cli.commands.COMMAND_REGISTRY",
            extended,
            raising=True,
        )

        topics = help_menu.get_help_topics()
        agents = topics["agents"]["commands"]

        assert "load-fake-persona" in agents, (
            "New CommandDef with category='Agents' did not auto-appear in "
            "the agents /help topic — the registry is not driving the help "
            "system."
        )
        assert (
            agents["load-fake-persona"]
            == "Connect to the Fake Persona (unit-test only)"
        )

    def test_new_instances_command_appears_in_help_without_yaml_edit(self, monkeypatch):
        fake_cmd = CommandDef(
            name="switch-fake-node",
            description="Route to fake node (unit-test only)",
            category="Instances",
            gateway_only=True,
        )
        extended = list(COMMAND_REGISTRY) + [fake_cmd]
        monkeypatch.setattr(
            "hermes_cli.commands.COMMAND_REGISTRY",
            extended,
            raising=True,
        )

        topics = help_menu.get_help_topics()
        assert "switch-fake-node" in topics["instances"]["commands"]

    def test_uncategorised_command_does_not_appear(self, monkeypatch):
        """Categories outside CATEGORY_TO_TOPIC do not leak into /help."""
        fake_cmd = CommandDef(
            name="internal-only-thing",
            description="Would leak if the filter is broken",
            category="Session",  # not mapped to any /help topic
            gateway_only=True,
        )
        extended = list(COMMAND_REGISTRY) + [fake_cmd]
        monkeypatch.setattr(
            "hermes_cli.commands.COMMAND_REGISTRY",
            extended,
            raising=True,
        )

        topics = help_menu.get_help_topics()
        for data in topics.values():
            assert "internal-only-thing" not in data["commands"]


class TestFunctionalEquivalence:
    """/help still returns sensible content for every topic (no crashes,
    every topic well-formed).
    """

    def test_format_help_index_renders(self):
        out = help_menu.format_help_index()
        assert "Executive Agent Commands" in out
        assert "Multi-Instance Orchestration" in out
        assert "General Commands" in out

    @pytest.mark.parametrize("topic", ["agents", "instances", "general"])
    def test_format_help_topic_renders_with_commands_and_example(self, topic):
        out = help_menu.format_help_topic(topic)
        assert "Commands:" in out
        assert "Example:" in out
        # At least one slash-command line
        assert "  /" in out
        # No 'commands' YAML block leakage or error state
        assert "❌" not in out

    def test_unknown_topic_returns_error_hint(self):
        out = help_menu.format_help_topic("does-not-exist")
        assert "not found" in out.lower()
