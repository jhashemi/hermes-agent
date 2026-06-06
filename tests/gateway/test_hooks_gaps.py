"""
Coverage gap tests for gateway/hooks.py

Targets lines not covered by the existing test suite:
  81, 92-93, 113-114, 120-122, 142-143
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.hooks import HookRegistry


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _patch_no_builtins(reg):
    return patch.object(reg, "_register_builtin_hooks")


def _create_hook(hooks_dir, hook_name, events_yaml, handler_code):
    hook_dir = hooks_dir / hook_name
    hook_dir.mkdir(parents=True)
    (hook_dir / "HOOK.yaml").write_text(
        f"name: {hook_name}\n"
        f"description: Test hook\n"
        f"events: {events_yaml}\n"
    )
    (hook_dir / "handler.py").write_text(handler_code)
    return hook_dir


# ---------------------------------------------------------------------------
# discover_and_load edge cases (lines 81, 92-93, 113-114, 120-122, 142-143)
# ---------------------------------------------------------------------------


class TestDiscoverAndLoadEdgeCases:
    def test_non_directory_entries_in_hooks_dir_are_skipped(self, tmp_path):
        """Line 81: a plain file inside the hooks dir must be silently skipped."""
        # Create a valid hook alongside a stray file
        _create_hook(tmp_path, "good-hook", '["agent:start"]',
                     "def handle(e, c): pass\n")
        (tmp_path / "stray-file.txt").write_text("I am not a hook directory\n")

        reg = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path), _patch_no_builtins(reg):
            reg.discover_and_load()

        # Only the real hook dir is loaded; the stray file is silently ignored.
        assert len(reg.loaded_hooks) == 1
        assert reg.loaded_hooks[0]["name"] == "good-hook"

    def test_invalid_hook_yaml_content_is_skipped(self, tmp_path, capsys):
        """Lines 92-93: HOOK.yaml is valid YAML but not a dict → skip with message."""
        hook_dir = tmp_path / "bad-yaml-hook"
        hook_dir.mkdir()
        # A YAML file that parses to a list, not a dict.
        (hook_dir / "HOOK.yaml").write_text("- item1\n- item2\n")
        (hook_dir / "handler.py").write_text("def handle(e, c): pass\n")

        reg = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path), _patch_no_builtins(reg):
            reg.discover_and_load()

        assert len(reg.loaded_hooks) == 0
        captured = capsys.readouterr()
        assert "invalid HOOK.yaml" in captured.out or "Skipping" in captured.out

    def test_null_hook_yaml_content_is_skipped(self, tmp_path, capsys):
        """Lines 92-93: HOOK.yaml is empty (parses to None) → skip."""
        hook_dir = tmp_path / "empty-yaml-hook"
        hook_dir.mkdir()
        (hook_dir / "HOOK.yaml").write_text("")
        (hook_dir / "handler.py").write_text("def handle(e, c): pass\n")

        reg = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path), _patch_no_builtins(reg):
            reg.discover_and_load()

        assert len(reg.loaded_hooks) == 0

    def test_handler_with_no_spec_is_skipped(self, tmp_path, capsys):
        """Lines 113-114: importlib.util.spec_from_file_location returns None → skip."""
        hook_dir = tmp_path / "no-spec-hook"
        hook_dir.mkdir()
        (hook_dir / "HOOK.yaml").write_text(
            "name: no-spec-hook\nevents: ['agent:start']\n"
        )
        (hook_dir / "handler.py").write_text("def handle(e, c): pass\n")

        import importlib.util as _ilu

        real_spec_from_file = _ilu.spec_from_file_location

        def patched_spec(name, path, **kw):
            if "no-spec-hook" in str(path):
                return None
            return real_spec_from_file(name, path, **kw)

        reg = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path), \
             _patch_no_builtins(reg), \
             patch("importlib.util.spec_from_file_location", side_effect=patched_spec):
            reg.discover_and_load()

        assert len(reg.loaded_hooks) == 0
        captured = capsys.readouterr()
        assert "could not load" in captured.out

    def test_handler_module_exec_error_is_caught(self, tmp_path, capsys):
        """Lines 120-122: handler.py raises on import → skip hook, clean sys.modules."""
        import sys

        hook_dir = tmp_path / "bad-exec-hook"
        hook_dir.mkdir()
        (hook_dir / "HOOK.yaml").write_text(
            "name: bad-exec-hook\nevents: ['agent:start']\n"
        )
        # A handler that raises at module-load time.
        (hook_dir / "handler.py").write_text(
            "raise RuntimeError('intentional import failure')\n"
        )

        module_name = "hermes_hook_bad-exec-hook"
        before_keys = set(sys.modules.keys())

        reg = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path), _patch_no_builtins(reg):
            reg.discover_and_load()

        assert len(reg.loaded_hooks) == 0
        # Module must be cleaned from sys.modules after the exec failure.
        assert module_name not in sys.modules
        captured = capsys.readouterr()
        assert "Error loading hook" in captured.out

    def test_general_exception_during_hook_load_is_caught(self, tmp_path, capsys):
        """Lines 142-143: a completely unexpected exception during load is caught
        and logged, without crashing discover_and_load."""
        hook_dir = tmp_path / "crash-hook"
        hook_dir.mkdir()
        (hook_dir / "HOOK.yaml").write_text(
            "name: crash-hook\nevents: ['agent:start']\n"
        )
        (hook_dir / "handler.py").write_text("def handle(e, c): pass\n")

        import yaml as _yaml

        real_safe_load = _yaml.safe_load

        def exploding_safe_load(stream):
            content = stream if isinstance(stream, str) else stream.read() if hasattr(stream, 'read') else str(stream)
            if "crash-hook" in str(content):
                raise RuntimeError("unexpected YAML crash")
            return real_safe_load(stream)

        reg = HookRegistry()
        with patch("gateway.hooks.HOOKS_DIR", tmp_path), \
             _patch_no_builtins(reg), \
             patch("yaml.safe_load", side_effect=exploding_safe_load):
            reg.discover_and_load()

        assert len(reg.loaded_hooks) == 0
        captured = capsys.readouterr()
        assert "Error loading hook" in captured.out
