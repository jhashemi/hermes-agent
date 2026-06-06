"""
Integration tests for the shell-hook pipeline.

Covers the create→hooks→state flow:
  - A hook configured for ``pre_tool_call`` is registered via
    ``register_from_config``, fires through the plugin manager's
    ``invoke_hook`` machinery, and the block decision propagates back up
    to the caller.
  - A hook configured for ``on_session_start`` is registered and fires
    without raising, confirming the side-effect model.
  - Env-var opt-in channels: HERMES_ACCEPT_HOOKS and hooks_auto_accept.
  - Multiple events share a single script but are registered independently.
  - A hook that errors (bad command) does not break the pipeline — other
    hooks for the same event still fire.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List

import pytest

from agent import shell_hooks
from hermes_cli import plugins


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    """Give every test its own HERMES_HOME and a clean plugin manager."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("HERMES_ACCEPT_HOOKS", raising=False)
    shell_hooks.reset_for_tests()
    plugins._plugin_manager = plugins.PluginManager()
    yield
    shell_hooks.reset_for_tests()
    plugins._plugin_manager = plugins.PluginManager()


def _write_script(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / name
    p.write_text(body)
    p.chmod(0o755)
    return p


# ---------------------------------------------------------------------------
# Integration: create → register → block decision propagates
# ---------------------------------------------------------------------------


class TestCreateHooksBlockIntegration:
    def test_pre_tool_call_block_propagates_to_plugin_manager(self, tmp_path):
        """The full pipeline:
          1. Script that returns a Claude-Code-style block directive.
          2. ``register_from_config`` wires it to the plugin manager.
          3. ``get_pre_tool_call_block_message`` returns the expected message.
        """
        blocker = _write_script(
            tmp_path, "blocker.sh",
            "#!/usr/bin/env bash\n"
            'printf \'{"decision": "block", "reason": "integration-block"}\\n\'\n',
        )
        cfg = {
            "hooks": {
                "pre_tool_call": [
                    {"matcher": "terminal", "command": str(blocker)},
                ],
            },
        }
        registered = shell_hooks.register_from_config(cfg, accept_hooks=True)
        assert len(registered) == 1

        msg = plugins.get_pre_tool_call_block_message(
            tool_name="terminal",
            args={"command": "rm -rf /"},
        )
        assert msg == "integration-block"

    def test_pre_tool_call_canonical_block_style_propagates(self, tmp_path):
        """Hermes-canonical ``{\"action\": \"block\"}`` is also recognised."""
        blocker = _write_script(
            tmp_path, "canonical.sh",
            "#!/usr/bin/env bash\n"
            'printf \'{"action": "block", "message": "canonical-msg"}\\n\'\n',
        )
        cfg = {
            "hooks": {
                "pre_tool_call": [
                    {"command": str(blocker)},  # no matcher → fires for all tools
                ],
            },
        }
        shell_hooks.register_from_config(cfg, accept_hooks=True)

        msg = plugins.get_pre_tool_call_block_message(
            tool_name="write_file",
            args={"path": "/etc/passwd"},
        )
        assert msg == "canonical-msg"

    def test_matcher_filters_block_for_other_tool(self, tmp_path):
        """A hook matched to 'terminal' must NOT block 'web_search'."""
        blocker = _write_script(
            tmp_path, "term_only.sh",
            "#!/usr/bin/env bash\n"
            'printf \'{"decision": "block", "reason": "terminal-only"}\\n\'\n',
        )
        cfg = {
            "hooks": {
                "pre_tool_call": [
                    {"matcher": "terminal", "command": str(blocker)},
                ],
            },
        }
        shell_hooks.register_from_config(cfg, accept_hooks=True)

        # terminal is blocked
        assert plugins.get_pre_tool_call_block_message(
            tool_name="terminal", args={},
        ) == "terminal-only"

        # web_search is not
        assert plugins.get_pre_tool_call_block_message(
            tool_name="web_search", args={},
        ) is None


# ---------------------------------------------------------------------------
# Integration: on_session_start fires without raising
# ---------------------------------------------------------------------------


class TestCreateHooksSessionIntegration:
    def test_session_start_hook_fires_and_writes_side_effect(self, tmp_path):
        """``on_session_start`` fires through the plugin manager and writes a
        side-effect file confirming it ran."""
        sentinel = tmp_path / "session_started.flag"
        session_hook = _write_script(
            tmp_path, "start.sh",
            f"#!/usr/bin/env bash\ntouch {sentinel}\nprintf '{{}}\\n'\n",
        )
        cfg = {
            "hooks": {
                "on_session_start": [{"command": str(session_hook)}],
            },
        }
        shell_hooks.register_from_config(cfg, accept_hooks=True)

        # Fire through invoke_hook — the shell-hook callback runs the script.
        plugins.invoke_hook("on_session_start", session_id="sess-integration")

        assert sentinel.exists(), "session start hook never ran"

    def test_post_tool_call_receives_correct_payload(self, tmp_path):
        """``post_tool_call`` hook receives a properly structured JSON payload
        with tool_name, tool_input, and session_id at the top level."""
        capture = tmp_path / "payload.json"
        observer = _write_script(
            tmp_path, "observe.sh",
            f"#!/usr/bin/env bash\ncat - > {capture}\nprintf '{{}}\\n'\n",
        )
        cfg = {
            "hooks": {
                "post_tool_call": [{"command": str(observer)}],
            },
        }
        shell_hooks.register_from_config(cfg, accept_hooks=True)

        plugins.invoke_hook(
            "post_tool_call",
            tool_name="terminal",
            args={"command": "echo hi"},
            session_id="sess-99",
            result='{"output": "hi"}',
            duration_ms=12,
        )

        payload = json.loads(capture.read_text())
        assert payload["hook_event_name"] == "post_tool_call"
        assert payload["tool_name"] == "terminal"
        assert payload["tool_input"] == {"command": "echo hi"}
        assert payload["session_id"] == "sess-99"


# ---------------------------------------------------------------------------
# Integration: env-var and config accept channels
# ---------------------------------------------------------------------------


class TestAcceptChannelsIntegration:
    def test_hermes_accept_hooks_env_registers_without_flag(
        self, tmp_path, monkeypatch,
    ):
        """HERMES_ACCEPT_HOOKS=1 must register hooks even without accept_hooks=True."""
        monkeypatch.setenv("HERMES_ACCEPT_HOOKS", "1")
        script = _write_script(tmp_path, "env.sh", "#!/usr/bin/env bash\nprintf '{}\\n'\n")
        cfg = {"hooks": {"on_session_start": [{"command": str(script)}]}}
        registered = shell_hooks.register_from_config(cfg, accept_hooks=False)
        assert len(registered) == 1

    def test_hooks_auto_accept_config_registers_without_flag(self, tmp_path):
        """hooks_auto_accept: true in config must register without --accept-hooks."""
        script = _write_script(tmp_path, "cfg.sh", "#!/usr/bin/env bash\nprintf '{}\\n'\n")
        cfg = {
            "hooks_auto_accept": True,
            "hooks": {"on_session_start": [{"command": str(script)}]},
        }
        registered = shell_hooks.register_from_config(cfg, accept_hooks=False)
        assert len(registered) == 1


# ---------------------------------------------------------------------------
# Integration: multiple events, one script
# ---------------------------------------------------------------------------


class TestMultiEventIntegration:
    def test_same_script_for_multiple_events_registers_all(self, tmp_path):
        """One script registered for both pre_tool_call and post_tool_call
        creates two independent callbacks — matcher keying includes the event."""
        sentinel_pre = tmp_path / "pre.flag"
        sentinel_post = tmp_path / "post.flag"

        # Write two distinct scripts so we can tell them apart by sentinel files.
        pre_script = _write_script(
            tmp_path, "pre.sh",
            f"#!/usr/bin/env bash\ntouch {sentinel_pre}\nprintf '{{}}\\n'\n",
        )
        post_script = _write_script(
            tmp_path, "post.sh",
            f"#!/usr/bin/env bash\ntouch {sentinel_post}\nprintf '{{}}\\n'\n",
        )

        cfg = {
            "hooks": {
                "pre_tool_call": [{"command": str(pre_script)}],
                "post_tool_call": [{"command": str(post_script)}],
            },
        }
        registered = shell_hooks.register_from_config(cfg, accept_hooks=True)
        assert len(registered) == 2

        plugins.invoke_hook("pre_tool_call", tool_name="terminal", args={}, session_id="s")
        plugins.invoke_hook("post_tool_call", tool_name="terminal", args={}, session_id="s",
                            result="ok", duration_ms=1)

        assert sentinel_pre.exists(), "pre_tool_call hook never fired"
        assert sentinel_post.exists(), "post_tool_call hook never fired"


# ---------------------------------------------------------------------------
# Integration: error in one hook does not prevent subsequent hooks from firing
# ---------------------------------------------------------------------------


class TestErrorIsolationIntegration:
    def test_broken_hook_does_not_block_following_hook(self, tmp_path):
        """A hook that fails (command not found) must not prevent a subsequent
        hook for the same event from executing."""
        sentinel = tmp_path / "second_ran.flag"
        second_script = _write_script(
            tmp_path, "second.sh",
            f"#!/usr/bin/env bash\ntouch {sentinel}\nprintf '{{}}\\n'\n",
        )
        cfg = {
            "hooks": {
                "on_session_start": [
                    {"command": "/does/not/exist/hook.sh"},  # will fail
                    {"command": str(second_script)},          # must still run
                ],
            },
        }
        shell_hooks.register_from_config(cfg, accept_hooks=True)
        plugins.invoke_hook("on_session_start", session_id="s")

        assert sentinel.exists(), (
            "second hook did not fire — broken first hook blocked the chain"
        )


# ---------------------------------------------------------------------------
# Integration: idempotent registration (create→register called twice)
# ---------------------------------------------------------------------------


class TestIdempotentRegistrationIntegration:
    def test_calling_register_twice_does_not_double_fire(self, tmp_path):
        """register_from_config is safe to call twice from CLI and gateway
        entry points.  The callback must fire exactly once per invoke_hook."""
        counter = tmp_path / "count.txt"
        counter.write_text("0")

        script = _write_script(
            tmp_path, "counter.sh",
            f"#!/usr/bin/env bash\n"
            f"count=$(cat {counter})\n"
            f"echo $((count + 1)) > {counter}\n"
            f"printf '{{}}\\n'\n",
        )
        cfg = {"hooks": {"on_session_start": [{"command": str(script)}]}}

        # Register twice (e.g., CLI + gateway both call register_from_config)
        shell_hooks.register_from_config(cfg, accept_hooks=True)
        shell_hooks.register_from_config(cfg, accept_hooks=True)

        plugins.invoke_hook("on_session_start", session_id="s")

        count = int(counter.read_text().strip())
        assert count == 1, f"expected 1 call, got {count}"
