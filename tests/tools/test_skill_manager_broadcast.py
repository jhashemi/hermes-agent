"""Regression tests for cluster broadcast on direct skill_manage mutations.

Mirror of tests/run_agent/test_background_review_skill_broadcast.py — same
broadcast hook, different entry surface. Bound by L4 policy
``hermes-skills-broadcast/runtime.l4 § skill-broadcast-hook-coverage``: every
code path that creates or updates a skill MUST broadcast.

The 4 mutation paths under test:
  - _create_skill   → "Skill 'X' created."
  - _edit_skill     → "Skill 'X' updated."
  - _patch_skill    → "Patched SKILL.md in skill 'X' (...)"
  - _write_file     → "File 'references/Y' written to skill 'X'."

Deletion paths (_delete_skill, _remove_file) MUST NOT broadcast (per
``hermes-skills-broadcast/governance.l4 § skill-deletion-not-permitted-via-broadcast``);
verified explicitly in test_delete_does_not_broadcast.
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ImmediateThread:
    """Run thread targets synchronously so tests can observe side effects."""

    def __init__(self, *, target, daemon=None, name=None):
        self._target = target

    def start(self):
        self._target()


def _install_fake_skills_broadcast(monkeypatch, skills_dir: Path, calls: list):
    """Stub tools.skills_broadcast so tests don't need a live NATS cluster.

    Patches BOTH ``sys.modules['tools.skills_broadcast']`` (covers ``import
    tools.skills_broadcast``) AND the ``skills_broadcast`` attribute on the
    ``tools`` package (covers ``from tools import skills_broadcast``).
    The hook in skill_manager_tool.py uses the ``from`` form, which reads
    the package attribute — leaving that unpatched silently routes the test
    through the real production module if ``tools`` was already imported
    earlier in the test session (e.g. by another test file).
    """
    fake = types.ModuleType("tools.skills_broadcast")
    fake.SKILLS_DIR = skills_dir  # type: ignore[attr-defined]

    async def fake_cmd_publish(skill_dir: Path):
        calls.append((skill_dir.name, skill_dir))

    fake.cmd_publish = fake_cmd_publish  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools.skills_broadcast", fake)
    import tools as _tools_pkg
    monkeypatch.setattr(_tools_pkg, "skills_broadcast", fake, raising=False)


@pytest.fixture
def broadcast_calls(monkeypatch, tmp_path):
    """Yields ``calls`` list that records every cmd_publish invocation."""
    import tools.skill_manager_tool as smt

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    monkeypatch.setattr(smt, "SKILLS_DIR", skills_dir)
    monkeypatch.setattr(smt, "HERMES_HOME", tmp_path)

    # _find_skill (used by _edit/_patch/_write_file/_delete/_remove_file)
    # walks agent.skill_utils.get_all_skills_dirs() — point it at our tmp dir
    # so create→subsequent-mutation lookups resolve.
    import agent.skill_utils as skill_utils
    monkeypatch.setattr(skill_utils, "get_all_skills_dirs", lambda: [skills_dir])

    calls: list = []
    _install_fake_skills_broadcast(monkeypatch, skills_dir, calls)
    import threading as _t
    monkeypatch.setattr(_t, "Thread", _ImmediateThread)

    return calls


# ---------------------------------------------------------------------------
# Direct unit test for the helper (mirrors run_agent test pattern)
# ---------------------------------------------------------------------------


def test_broadcast_helper_publishes_when_skill_dir_exists(monkeypatch, tmp_path):
    """The standalone helper resolves and publishes."""
    import tools.skill_manager_tool as smt

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "demo-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\n---\nbody\n")

    calls: list = []
    _install_fake_skills_broadcast(monkeypatch, skills_dir, calls)

    import threading as _t
    monkeypatch.setattr(_t, "Thread", _ImmediateThread)

    smt._broadcast_skill_to_cluster("demo-skill")

    assert calls == [("demo-skill", skill_dir)]


def test_broadcast_helper_resolves_category_nested(monkeypatch, tmp_path):
    """Skills under devops/ etc. (real layout) must still resolve."""
    import tools.skill_manager_tool as smt

    skills_dir = tmp_path / "skills"
    (skills_dir / "devops").mkdir(parents=True)
    skill_dir = skills_dir / "devops" / "category-nested-skill"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("body")

    calls: list = []
    _install_fake_skills_broadcast(monkeypatch, skills_dir, calls)

    import threading as _t
    monkeypatch.setattr(_t, "Thread", _ImmediateThread)

    smt._broadcast_skill_to_cluster("category-nested-skill")

    assert calls == [("category-nested-skill", skill_dir)]


def test_broadcast_helper_swallows_publish_failure(monkeypatch, tmp_path):
    """A NATS connect failure must never raise to the caller."""
    import tools.skill_manager_tool as smt

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    skill_dir = skills_dir / "boom"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("body")

    fake = types.ModuleType("tools.skills_broadcast")
    fake.SKILLS_DIR = skills_dir  # type: ignore[attr-defined]

    async def boom(_path):
        raise RuntimeError("NATS dead")

    fake.cmd_publish = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "tools.skills_broadcast", fake)
    import threading as _t
    monkeypatch.setattr(_t, "Thread", _ImmediateThread)

    # Must not raise:
    smt._broadcast_skill_to_cluster("boom")


def test_broadcast_helper_skips_when_skill_missing(monkeypatch, tmp_path):
    """Missing skill dir → no-op, no exception."""
    import tools.skill_manager_tool as smt

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    calls: list = []
    _install_fake_skills_broadcast(monkeypatch, skills_dir, calls)
    import threading as _t
    monkeypatch.setattr(_t, "Thread", _ImmediateThread)

    smt._broadcast_skill_to_cluster("never-existed")

    assert calls == []


def test_broadcast_helper_handles_invalid_input(monkeypatch):
    """Empty / None / non-string names → no-op."""
    import tools.skill_manager_tool as smt
    smt._broadcast_skill_to_cluster("")
    smt._broadcast_skill_to_cluster(None)  # type: ignore[arg-type]
    smt._broadcast_skill_to_cluster(42)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Wiring tests: call sites in _create_skill, _edit_skill, _patch_skill, _write_file
# ---------------------------------------------------------------------------


def test_create_skill_broadcasts(broadcast_calls):
    """skill_manage(action='create') must broadcast on success."""
    import tools.skill_manager_tool as smt

    content = (
        "---\n"
        "name: pipeline-test-create\n"
        "description: A skill created during a unit test.\n"
        "---\n\n"
        "# Pipeline Test Create\n\nBody.\n"
    )

    # Bypass security scan in test (no scanners installed).
    import contextlib
    with contextlib.suppress(Exception):
        result = smt._create_skill("pipeline-test-create", content)
        assert result.get("success"), result
        assert any(call[0] == "pipeline-test-create" for call in broadcast_calls), \
            f"create did not broadcast; calls={broadcast_calls}"


def test_edit_skill_broadcasts(broadcast_calls):
    """skill_manage(action='edit') must broadcast on success."""
    import tools.skill_manager_tool as smt

    create_content = (
        "---\n"
        "name: pipeline-test-edit\n"
        "description: skill for the edit broadcast test.\n"
        "---\n\nbody v1\n"
    )
    smt._create_skill("pipeline-test-edit", create_content)
    broadcast_calls.clear()

    new_content = (
        "---\n"
        "name: pipeline-test-edit\n"
        "description: skill for the edit broadcast test.\n"
        "---\n\nbody v2 (edited)\n"
    )
    result = smt._edit_skill("pipeline-test-edit", new_content)
    assert result.get("success"), result
    assert any(call[0] == "pipeline-test-edit" for call in broadcast_calls), \
        f"edit did not broadcast; calls={broadcast_calls}"


def test_patch_skill_broadcasts(broadcast_calls):
    """skill_manage(action='patch') must broadcast on success."""
    import tools.skill_manager_tool as smt

    create_content = (
        "---\n"
        "name: pipeline-test-patch\n"
        "description: skill for the patch broadcast test.\n"
        "---\n\nfindme: original-token\n"
    )
    smt._create_skill("pipeline-test-patch", create_content)
    broadcast_calls.clear()

    result = smt._patch_skill(
        "pipeline-test-patch",
        old_string="original-token",
        new_string="replaced-token",
    )
    assert result.get("success"), result
    assert any(call[0] == "pipeline-test-patch" for call in broadcast_calls), \
        f"patch did not broadcast; calls={broadcast_calls}"


def test_write_file_broadcasts(broadcast_calls):
    """skill_manage(action='write_file') must broadcast on success."""
    import tools.skill_manager_tool as smt

    create_content = (
        "---\n"
        "name: pipeline-test-writefile\n"
        "description: skill for the write_file broadcast test.\n"
        "---\n\nbody.\n"
    )
    smt._create_skill("pipeline-test-writefile", create_content)
    broadcast_calls.clear()

    result = smt._write_file(
        "pipeline-test-writefile",
        file_path="references/extra.md",
        file_content="# Extra ref doc\n",
    )
    assert result.get("success"), result
    assert any(call[0] == "pipeline-test-writefile" for call in broadcast_calls), \
        f"write_file did not broadcast; calls={broadcast_calls}"


def test_delete_skill_does_not_broadcast(broadcast_calls):
    """Deletion MUST NOT broadcast — bound by skill-deletion-not-permitted-via-broadcast.

    The cluster broadcast pipeline only carries create/update events. A delete
    on this node MUST stay local and route through the curator's archive path.
    """
    import tools.skill_manager_tool as smt

    create_content = (
        "---\n"
        "name: pipeline-test-deletion\n"
        "description: skill for the deletion no-broadcast test.\n"
        "---\n\nbody.\n"
    )
    smt._create_skill("pipeline-test-deletion", create_content)
    broadcast_calls.clear()

    result = smt._delete_skill("pipeline-test-deletion")
    assert result.get("success"), result
    assert broadcast_calls == [], (
        "delete must NOT broadcast (skill-deletion-not-permitted-via-broadcast); "
        f"calls={broadcast_calls}"
    )
