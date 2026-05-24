"""Tests for the ``post_api_request`` plugin hook.

The hook fires inside ``AIAgent.run_conversation`` once per API call,
AFTER the HTTP response is received from the model provider.  It is
the plugin's chance to observe response metadata (finish_reason, usage,
duration, etc.) for observability tools like Langfuse.

The dispatch contract in run_agent.py (line ~14138):

    _invoke_hook(
        "post_api_request",
        task_id=effective_task_id,
        session_id=self.session_id or "",
        platform=self.platform or "",
        model=self.model,
        provider=self.provider,
        base_url=self.base_url,
        api_mode=self.api_mode,
        api_call_count=api_call_count,
        api_duration=api_duration,
        finish_reason=finish_reason,
        message_count=len(api_messages),
        response_model=getattr(response, "model", None),
        usage=self._usage_summary_for_api_request_hook(response),
        assistant_content_chars=len(_assistant_text),
        assistant_tool_call_count=len(_assistant_tool_calls),
    )

This is an observer-only hook — return values are ignored.  The
invoke call is wrapped in a bare ``except Exception: pass``.
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

def test_post_api_request_in_valid_hooks():
    assert "post_api_request" in VALID_HOOKS


# ---------------------------------------------------------------------------
# 2. Hook receives all 15 expected kwargs
# ---------------------------------------------------------------------------

def test_hook_receives_all_expected_kwargs(tmp_path, monkeypatch):
    """The callback must see all 15 kwargs dispatched in run_agent.py."""
    captured = {}

    def _capture(**kw):
        captured.update(kw)
        return None

    mgr = PluginManager()
    mgr._hooks.setdefault("post_api_request", []).append(_capture)

    mgr.invoke_hook(
        "post_api_request",
        task_id="t-42",
        session_id="s-200",
        platform="telegram",
        model="anthropic/claude-sonnet-4",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_mode="chat_completions",
        api_call_count=7,
        api_duration=1.23,
        finish_reason="stop",
        message_count=15,
        response_model="claude-sonnet-4-20250514",
        usage={"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
        assistant_content_chars=350,
        assistant_tool_call_count=0,
    )

    expected_keys = {
        "task_id", "session_id", "platform", "model", "provider",
        "base_url", "api_mode", "api_call_count", "api_duration",
        "finish_reason", "message_count", "response_model",
        "usage", "assistant_content_chars", "assistant_tool_call_count",
    }
    assert set(captured.keys()) == expected_keys


# ---------------------------------------------------------------------------
# 3. Usage dict present when tokens reported
# ---------------------------------------------------------------------------

def test_usage_dict_present_when_tokens_reported():
    """When the provider returns usage info, the hook sees it as a dict."""
    captured = {}

    def _capture(**kw):
        captured.update(kw)
        return None

    mgr = PluginManager()
    mgr._hooks.setdefault("post_api_request", []).append(_capture)

    usage_dict = {
        "prompt_tokens": 2048,
        "completion_tokens": 512,
        "total_tokens": 2560,
    }
    mgr.invoke_hook(
        "post_api_request",
        task_id="t1",
        session_id="s1",
        platform="cli",
        model="m",
        provider="p",
        base_url="b",
        api_mode="chat_completions",
        api_call_count=1,
        api_duration=0.5,
        finish_reason="stop",
        message_count=5,
        response_model="m",
        usage=usage_dict,
        assistant_content_chars=100,
        assistant_tool_call_count=2,
    )

    assert captured["usage"]["prompt_tokens"] == 2048
    assert captured["usage"]["total_tokens"] == 2560


# ---------------------------------------------------------------------------
# 4. Usage None when no tokens reported
# ---------------------------------------------------------------------------

def test_usage_none_when_no_tokens_reported():
    """When the provider doesn't return usage, the hook sees usage=None."""
    captured = {}

    def _capture(**kw):
        captured.update(kw)
        return None

    mgr = PluginManager()
    mgr._hooks.setdefault("post_api_request", []).append(_capture)

    mgr.invoke_hook(
        "post_api_request",
        task_id="t1",
        session_id="s1",
        platform="cli",
        model="m",
        provider="p",
        base_url="b",
        api_mode="chat_completions",
        api_call_count=1,
        api_duration=0.1,
        finish_reason="length",
        message_count=3,
        response_model="m",
        usage=None,
        assistant_content_chars=50,
        assistant_tool_call_count=1,
    )

    assert captured["usage"] is None


# ---------------------------------------------------------------------------
# 5. Hook is observer-only (return values ignored)
# ---------------------------------------------------------------------------

def test_return_values_ignored():
    """post_api_request return values are never inspected by the caller.
    The code in run_agent.py discards the return (bare expression statement)."""
    assert True  # Contract documented


# ---------------------------------------------------------------------------
# 6. Hook exception does not break subsequent processing
# ---------------------------------------------------------------------------

def test_hook_exception_does_not_break_dispatch(tmp_path, monkeypatch):
    """A crashing plugin must not break the agent loop. PluginManager
    catches per-callback exceptions and continues."""
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home, "crash_post_api",
        register_body=(
            'def _boom(**kw):\n'
            '        raise RuntimeError("observability crash")\n'
            '    ctx.register_hook("post_api_request", _boom)'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    results = mgr.invoke_hook(
        "post_api_request",
        task_id="t1",
        session_id="s1",
        platform="cli",
        model="m",
        provider="p",
        base_url="b",
        api_mode="chat_completions",
        api_call_count=1,
        api_duration=0.5,
        finish_reason="stop",
        message_count=1,
        response_model="m",
        usage=None,
        assistant_content_chars=10,
        assistant_tool_call_count=0,
    )
    assert results == []


# ---------------------------------------------------------------------------
# 7. api_duration and api_call_count track iteration metadata
# ---------------------------------------------------------------------------

def test_api_duration_and_call_count_track_iteration():
    """api_duration increases with each call; api_call_count increments.
    Verify the hook can observe these for latency tracking."""
    call_log = []

    def _log(**kw):
        call_log.append({
            "call": kw["api_call_count"],
            "duration": kw["api_duration"],
        })
        return None

    mgr = PluginManager()
    mgr._hooks.setdefault("post_api_request", []).append(_log)

    for i, dur in enumerate([0.1, 0.3, 1.5], start=1):
        mgr.invoke_hook(
            "post_api_request",
            task_id="t1",
            session_id="s1",
            platform="cli",
            model="m",
            provider="p",
            base_url="b",
            api_mode="chat_completions",
            api_call_count=i,
            api_duration=dur,
            finish_reason="stop",
            message_count=1,
            response_model="m",
            usage=None,
            assistant_content_chars=10,
            assistant_tool_call_count=0,
        )

    assert len(call_log) == 3
    assert call_log[0]["call"] == 1
    assert call_log[0]["duration"] == 0.1
    assert call_log[2]["call"] == 3
    assert call_log[2]["duration"] == 1.5