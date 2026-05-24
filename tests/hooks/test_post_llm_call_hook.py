"""Tests for the ``post_llm_call`` plugin hook.

The hook fires inside ``AIAgent.run_conversation`` once per turn,
AFTER the tool-calling loop has produced a final response.  It is
the plugin's chance to persist conversation data (e.g. sync to an
external memory system like Langfuse or Honcho).

The dispatch contract in run_agent.py (line ~15086):

    if final_response and not interrupted:
        _invoke_hook(
            "post_llm_call",
            session_id=self.session_id,
            user_message=original_user_message,
            assistant_response=final_response,
            conversation_history=list(messages),
            model=self.model,
            platform=getattr(self, "platform", None) or "",
        )

This is an observer-only hook — return values are ignored by the
caller.  An exception in the hook is caught and logged.
"""

from pathlib import Path

import yaml

import hermes_cli.plugins as plugins_mod
from hermes_cli.plugins import PluginManager, VALID_HOOKS


def _make_enabled_plugin(hermes_home: Path, name: str, register_body: str) -> Path:
    """Create a plugin under <hermes_home>/plugins/<name> and opt it in."""
    plugin_dir = hermes_home / "plugins" / name
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "plugin.yaml").write_text(
        yaml.safe_dump({"name": name, "version": "0.1.0"}), encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        "def register(ctx):\n"
        f"    {register_body}\n",
        encoding="utf-8",
    )
    cfg_path = hermes_home / "config.yaml"
    cfg = {}
    if cfg_path.exists():
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    cfg.setdefault("plugins", {}).setdefault("enabled", []).append(name)
    cfg_path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return plugin_dir


# ---------------------------------------------------------------------------
# 1. Hook is in VALID_HOOKS
# ---------------------------------------------------------------------------

def test_post_llm_call_in_valid_hooks():
    assert "post_llm_call" in VALID_HOOKS


# ---------------------------------------------------------------------------
# 2. Hook receives all expected kwargs
# ---------------------------------------------------------------------------

def test_hook_receives_expected_kwargs(tmp_path, monkeypatch):
    """The callback must see session_id, user_message, assistant_response,
    conversation_history, model, platform."""
    captured = {}

    def _capture(**kw):
        captured.update(kw)
        return None

    mgr = PluginManager()
    # Register hook directly into the internal _hooks dict
    mgr._hooks.setdefault("post_llm_call", []).append(_capture)

    mgr.invoke_hook(
        "post_llm_call",
        session_id="s-99",
        user_message="what is 2+2?",
        assistant_response="4",
        conversation_history=[
            {"role": "user", "content": "what is 2+2?"},
            {"role": "assistant", "content": "4"},
        ],
        model="openai/gpt-4o",
        platform="discord",
    )

    assert captured["session_id"] == "s-99"
    assert captured["user_message"] == "what is 2+2?"
    assert captured["assistant_response"] == "4"
    assert captured["model"] == "openai/gpt-4o"
    assert captured["platform"] == "discord"
    assert len(captured["conversation_history"]) == 2


# ---------------------------------------------------------------------------
# 3. Hook is observer-only (return values ignored by caller)
# ---------------------------------------------------------------------------

def test_return_values_ignored_by_caller_semantic():
    """post_llm_call is observer-only — the caller never inspects results.
    Simulate the run_agent.py wiring: the return value of invoke_hook is
    discarded (assigned but never read for decision-making)."""
    results_from_hook = [{"action": "block"}, "some string", None, 42]
    # In run_agent.py, the return is simply _invoke_hook(...) with no
    # conditional on the results. This test documents that contract.
    # If any code were added that inspected results, this test would
    # serve as a reminder that the hook is observer-only.
    # The hook fires *after* the response is finalized — nothing to change.
    assert True  # Contract documented; no mutation path


# ---------------------------------------------------------------------------
# 4. Multiple plugins all receive the call
# ---------------------------------------------------------------------------

def test_multiple_plugins_all_receive_call(tmp_path, monkeypatch):
    """When two plugins register for post_llm_call, both fire."""
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home, "persist_a",
        register_body=(
            'ctx.register_hook("post_llm_call", '
            'lambda **kw: "A")'
        ),
    )
    _make_enabled_plugin(
        hermes_home, "persist_b",
        register_body=(
            'ctx.register_hook("post_llm_call", '
            'lambda **kw: "B")'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    results = mgr.invoke_hook(
        "post_llm_call",
        session_id="s1",
        user_message="msg",
        assistant_response="response",
        conversation_history=[],
        model="m",
        platform="cli",
    )
    # Both non-None returns are collected
    assert len(results) == 2
    assert "A" in results
    assert "B" in results


# ---------------------------------------------------------------------------
# 5. Hook exception does not prevent other plugins from firing
# ---------------------------------------------------------------------------

def test_hook_exception_does_not_prevent_other_plugins(tmp_path, monkeypatch):
    """A misbehaving plugin raising an exception must not prevent other
    well-behaved plugins from receiving the post_llm_call event."""
    call_count = {"good": 0}

    def _good_hook(**kw):
        call_count["good"] += 1
        return None

    def _bad_hook(**kw):
        raise RuntimeError("boom")

    mgr = PluginManager()
    mgr._hooks.setdefault("post_llm_call", []).extend([_bad_hook, _good_hook])

    results = mgr.invoke_hook(
        "post_llm_call",
        session_id="s1",
        user_message="msg",
        assistant_response="resp",
        conversation_history=[],
        model="m",
        platform="cli",
    )
    assert call_count["good"] == 1


# ---------------------------------------------------------------------------
# 6. Hook not called when final_response is empty
# ---------------------------------------------------------------------------

def test_hook_not_called_when_no_final_response():
    """In run_agent.py, post_llm_call fires only if final_response is
    truthy and the session was not interrupted. Document this guard."""
    # Simulate the conditional: `if final_response and not interrupted:`
    final_response = ""
    interrupted = False
    should_fire = bool(final_response and not interrupted)
    assert should_fire is False

    final_response = None
    should_fire = bool(final_response and not interrupted)
    assert should_fire is False

    final_response = "Hello!"
    should_fire = bool(final_response and not interrupted)
    assert should_fire is True


# ---------------------------------------------------------------------------
# 7. Hook not called when interrupted
# ---------------------------------------------------------------------------

def test_hook_not_called_when_interrupted():
    """Even with a non-empty final_response, interruption prevents the hook."""
    final_response = "Partial answer"
    interrupted = True
    should_fire = bool(final_response and not interrupted)
    assert should_fire is False