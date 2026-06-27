"""Regression tests for cluster broadcast on bg-review skill creation.

When the background self-improvement review creates or updates a skill, the
agent MUST broadcast that skill over NATS JetStream to every other cluster
node. The broadcast is fire-and-forget on a daemon thread so a NATS hiccup
or missing skill dir cannot break the review flow or the user-facing summary.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

import run_agent as run_agent_module
from run_agent import AIAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ImmediateThread:
    """Run thread targets synchronously so tests can observe side effects."""

    def __init__(self, *, target, daemon=None, name=None):
        self._target = target

    def start(self):
        self._target()


def _install_fake_skills_broadcast(monkeypatch, tmp_path: Path, calls: list):
    """Stub tools.skills_broadcast with a fake module that records publishes.

    Returns nothing — populates ``calls`` with ``(skill_name, skill_dir)`` for
    every cmd_publish invocation the broadcast hook makes.
    """

    fake = types.ModuleType("tools.skills_broadcast")
    fake.SKILLS_DIR = tmp_path  # type: ignore[attr-defined]

    async def fake_cmd_publish(skill_dir: Path):
        calls.append((skill_dir.name, skill_dir))

    fake.cmd_publish = fake_cmd_publish  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools.skills_broadcast", fake)
    # Also replace the attribute on the ``tools`` package so that
    # ``from tools import skills_broadcast`` (used inside the production
    # broadcast hook) resolves to our fake even when ``tools`` was already
    # imported earlier in the test session.
    import tools as _tools_pkg
    monkeypatch.setattr(_tools_pkg, "skills_broadcast", fake, raising=False)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_broadcast_skill_actions_publishes_created_skill(monkeypatch, tmp_path):
    """Skill 'X' created. → publish X to cluster via NATS."""
    calls: list = []
    _install_fake_skills_broadcast(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(run_agent_module.threading, "Thread", _ImmediateThread)

    skill_dir = tmp_path / "l4-governance-compilation"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: l4-governance-compilation\n---\nbody\n")

    AIAgent._broadcast_skill_actions(["Skill 'l4-governance-compilation' created."])

    assert calls == [("l4-governance-compilation", skill_dir)]


def test_broadcast_skill_actions_publishes_updated_skill(monkeypatch, tmp_path):
    """Skill 'X' updated. → publish X (same path; updates count too)."""
    calls: list = []
    _install_fake_skills_broadcast(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(run_agent_module.threading, "Thread", _ImmediateThread)

    skill_dir = tmp_path / "post-mortem-l4-loop"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: post-mortem-l4-loop\n---\nbody\n")

    AIAgent._broadcast_skill_actions(["Skill 'post-mortem-l4-loop' updated."])

    assert calls == [("post-mortem-l4-loop", skill_dir)]


def test_broadcast_skill_actions_dedupes_same_skill(monkeypatch, tmp_path):
    """If the summary mentions the same skill twice (e.g. created+patched), publish once."""
    calls: list = []
    _install_fake_skills_broadcast(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(run_agent_module.threading, "Thread", _ImmediateThread)

    skill_dir = tmp_path / "double-mention"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("body")

    AIAgent._broadcast_skill_actions([
        "Skill 'double-mention' created.",
        "Skill 'double-mention' updated.",
    ])

    assert len(calls) == 1
    assert calls[0][0] == "double-mention"


def test_broadcast_skill_actions_skips_when_skill_dir_missing(monkeypatch, tmp_path):
    """Missing local SKILL.md must not crash and must not publish."""
    calls: list = []
    _install_fake_skills_broadcast(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(run_agent_module.threading, "Thread", _ImmediateThread)

    AIAgent._broadcast_skill_actions(["Skill 'never-existed' created."])

    assert calls == []


def test_broadcast_skill_actions_ignores_non_skill_actions(monkeypatch, tmp_path):
    """'Memory updated' / 'User profile updated' must not trigger any broadcast."""
    calls: list = []
    _install_fake_skills_broadcast(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(run_agent_module.threading, "Thread", _ImmediateThread)

    AIAgent._broadcast_skill_actions([
        "Memory updated",
        "User profile updated",
    ])

    assert calls == []


def test_broadcast_skill_actions_swallows_publish_failure(monkeypatch, tmp_path):
    """A NATS connect failure inside cmd_publish must not raise to the caller."""
    fake = types.ModuleType("tools.skills_broadcast")
    fake.SKILLS_DIR = tmp_path  # type: ignore[attr-defined]

    async def boom(_skill_dir):
        raise RuntimeError("NATS connect failed")

    fake.cmd_publish = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools.skills_broadcast", fake)
    # Also replace the attribute on the ``tools`` package so that
    # ``from tools import skills_broadcast`` (used inside the production
    # broadcast hook) resolves to our fake even when ``tools`` was already
    # imported earlier in the test session.
    import tools as _tools_pkg
    monkeypatch.setattr(_tools_pkg, "skills_broadcast", fake, raising=False)
    monkeypatch.setattr(run_agent_module.threading, "Thread", _ImmediateThread)

    skill_dir = tmp_path / "explody"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("body")

    # Must not raise:
    AIAgent._broadcast_skill_actions(["Skill 'explody' created."])


def test_broadcast_skill_actions_handles_empty_input():
    """Empty / None / non-string entries must be tolerated silently."""
    AIAgent._broadcast_skill_actions([])
    AIAgent._broadcast_skill_actions([None, 42, {"not": "a string"}])  # type: ignore[list-item]


def test_broadcast_skill_actions_finds_category_nested_skill(monkeypatch, tmp_path):
    """Skills are organized under category subdirs (devops/, mlops/, etc.).

    The bg-review creates skills like ``~/.hermes/skills/devops/l4-governance-compilation/``.
    The broadcast resolver must walk one level of subdirectory, otherwise the
    just-created skill never reaches the cluster (the original bug behind this
    test file).
    """
    calls: list = []
    _install_fake_skills_broadcast(monkeypatch, tmp_path, calls)
    monkeypatch.setattr(run_agent_module.threading, "Thread", _ImmediateThread)

    category = tmp_path / "devops"
    category.mkdir()
    skill_dir = category / "l4-governance-compilation"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: l4-governance-compilation\n---\nbody")

    AIAgent._broadcast_skill_actions(["Skill 'l4-governance-compilation' created."])

    assert calls == [("l4-governance-compilation", skill_dir)]
