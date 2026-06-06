"""Tests for hermes_cli.adaptive_model_selector — task-class → model routing.

Covers the rule-precedence ladder and the env-override hooks.
"""
from __future__ import annotations

import os

import pytest

from hermes_cli.adaptive_model_selector import (
    TaskSignal,
    TaskTier,
    adaptive_args_for_task,
    classify,
)


# ---------------------------------------------------------------------------
# Rule precedence (skill > priority > title patterns > default)
# ---------------------------------------------------------------------------

class TestClassifierPrecedence:
    def test_default_is_glm(self):
        choice = classify(TaskSignal(title="implement feature X"))
        assert choice.tier is TaskTier.DEFAULT
        assert choice.model == "glm-5.1"
        assert choice.provider == "openrouter"

    def test_high_stakes_skill_wins_over_default_title(self):
        choice = classify(TaskSignal(
            title="implement feature X",
            skills=("adr",),
        ))
        assert choice.tier is TaskTier.HIGH_STAKES
        assert choice.model == "us.anthropic.claude-sonnet-4-6"
        assert choice.provider == "bedrock"
        assert "adr" in choice.reason

    def test_high_stakes_skill_wins_over_priority(self):
        # Both high-stakes — but skill rule fires first
        choice = classify(TaskSignal(
            title="cleanup",
            priority=9,
            skills=("safety-critical-patterns",),
        ))
        assert choice.tier is TaskTier.HIGH_STAKES
        assert "safety-critical-patterns" in choice.reason

    def test_priority_8_triggers_high_stakes(self):
        choice = classify(TaskSignal(title="generic task", priority=8))
        assert choice.tier is TaskTier.HIGH_STAKES
        assert "priority>=8" in choice.reason

    def test_priority_7_does_not_trigger(self):
        choice = classify(TaskSignal(title="generic task", priority=7))
        assert choice.tier is TaskTier.DEFAULT

    def test_title_critical_triggers_high_stakes(self):
        choice = classify(TaskSignal(title="fix critical bug in auth"))
        assert choice.tier is TaskTier.HIGH_STAKES

    def test_title_security_triggers_high_stakes(self):
        choice = classify(TaskSignal(title="rotate security tokens"))
        assert choice.tier is TaskTier.HIGH_STAKES

    def test_title_architecture_triggers_high_stakes(self):
        choice = classify(TaskSignal(title="ADR: split monolith"))
        assert choice.tier is TaskTier.HIGH_STAKES

    def test_title_typo_triggers_trivial(self):
        choice = classify(TaskSignal(title="fix typo in README"))
        assert choice.tier is TaskTier.TRIVIAL
        assert choice.model == "us.anthropic.claude-haiku-4-5"
        assert choice.provider == "bedrock"

    def test_title_label_triggers_trivial(self):
        choice = classify(TaskSignal(title="label task with priority tag"))
        assert choice.tier is TaskTier.TRIVIAL

    def test_priority_overrides_trivial_title(self):
        # priority wins: a "label" task at p9 is still high-stakes
        choice = classify(TaskSignal(title="label tasks", priority=9))
        assert choice.tier is TaskTier.HIGH_STAKES


# ---------------------------------------------------------------------------
# adaptive_args_for_task helper used by the dispatcher
# ---------------------------------------------------------------------------

class TestSpawnArgs:
    def test_args_emit_minus_m_and_provider(self, monkeypatch):
        monkeypatch.delenv("HERMES_KANBAN_ADAPTIVE", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_FORCE_MODEL", raising=False)
        args, choice = adaptive_args_for_task(
            title="benign task", priority=3, skills=()
        )
        assert "-m" in args
        assert "--provider" in args
        assert choice.tier is TaskTier.DEFAULT

    def test_kill_switch_returns_empty(self, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_ADAPTIVE", "0")
        args, choice = adaptive_args_for_task(
            title="critical bug", priority=9, skills=()
        )
        assert args == []
        assert choice is None

    def test_force_model_env_override(self, monkeypatch):
        monkeypatch.setenv("HERMES_KANBAN_FORCE_MODEL", "test-mock-model")
        monkeypatch.setenv("HERMES_KANBAN_FORCE_PROVIDER", "test-mock")
        args, choice = adaptive_args_for_task(
            title="critical safety issue", priority=9, skills=("adr",)
        )
        assert args == ["-m", "test-mock-model", "--provider", "test-mock"]
        assert choice.reason == "env-forced"

    def test_high_stakes_routes_to_sonnet_bedrock(self, monkeypatch):
        monkeypatch.delenv("HERMES_KANBAN_ADAPTIVE", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_FORCE_MODEL", raising=False)
        args, choice = adaptive_args_for_task(
            title="critical migration", priority=9, skills=()
        )
        assert "us.anthropic.claude-sonnet-4-6" in args
        assert "bedrock" in args
        assert choice.tier is TaskTier.HIGH_STAKES


# ---------------------------------------------------------------------------
# Determinism (same input → same output)
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_same_signal_same_choice(self):
        sig = TaskSignal(title="add new endpoint", priority=5,
                         skills=("api-design",))
        a = classify(sig)
        b = classify(sig)
        assert a == b

    @pytest.mark.parametrize("title,expected_tier", [
        ("fix typo", TaskTier.TRIVIAL),
        ("normal feature work", TaskTier.DEFAULT),
        ("CRITICAL outage repair", TaskTier.HIGH_STAKES),
        ("Auth migration", TaskTier.HIGH_STAKES),
        ("rename variable", TaskTier.TRIVIAL),
    ])
    def test_table_driven(self, title, expected_tier):
        choice = classify(TaskSignal(title=title))
        assert choice.tier is expected_tier
