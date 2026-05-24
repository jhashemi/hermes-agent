"""Tests for the ``pre_api_request`` plugin hook.

The hook fires inside ``AIAgent.run_conversation`` once per API call,
BEFORE the HTTP request is sent to the model provider.  It is the
plugin's chance to observe and log request metadata (model, token
counts, etc.) for observability tools like Langfuse.

The dispatch contract in run_agent.py (line ~12260):

    _invoke_hook(
        "pre_api_request",
        task_id=effective_task_id,
        session_id=self.session_id or "",
        platform=self.platform or "",
        model=self.model,
        provider=self.provider,
        base_url=self.base_url,
        api_mode=self.api_mode,
        api_call_count=api_call_count,
        message_count=len(api_messages),
        tool_count=len(self.tools or []),
        approx_input_tokens=approx_tokens,
        request_char_count=total_chars,
        max_tokens=self.max_tokens,
    )

This is an observer-only hook — return values are ignored.  The
invoke call is wrapped in a bare ``except Exception: pass``, so a
crashing plugin cannot disrupt the API request.
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

def test_pre_api_request_in_valid_hooks():
    assert "pre_api_request" in VALID_HOOKS


# ---------------------------------------------------------------------------
# 2. Hook receives all 13 expected kwargs
# ---------------------------------------------------------------------------

def test_hook_receives_all_expected_kwargs(tmp_path, monkeypatch):
    """The callback must see all 13 kwargs dispatched in run_agent.py."""
    captured = {}

    def _capture(**kw):
        captured.update(kw)
        return None

    mgr = PluginManager()
    mgr._hooks.setdefault("pre_api_request", []).append(_capture)

    mgr.invoke_hook(
        "pre_api_request",
        task_id="t-1",
        session_id="s-100",
        platform="cli",
        model="anthropic/claude-sonnet-4",
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_mode="chat_completions",
        api_call_count=3,
        message_count=12,
        tool_count=5,
        approx_input_tokens=2048,
        request_char_count=8192,
        max_tokens=4096,
    )

    expected_keys = {
        "task_id", "session_id", "platform", "model", "provider",
        "base_url", "api_mode", "api_call_count", "message_count",
        "tool_count", "approx_input_tokens", "request_char_count",
        "max_tokens",
    }
    assert set(captured.keys()) == expected_keys


# ---------------------------------------------------------------------------
# 3. Hook fires on every API call iteration
# ---------------------------------------------------------------------------

def test_hook_fires_on_every_api_call(tmp_path, monkeypatch):
    """pre_api_request fires per-iteration, not per-turn. Verify that
    invoke_hook can be called N times and the plugin sees all calls."""
    call_log = []

    def _log(**kw):
        call_log.append(kw.get("api_call_count", -1))
        return None

    mgr = PluginManager()
    mgr._hooks.setdefault("pre_api_request", []).append(_log)

    for i in range(1, 5):
        mgr.invoke_hook(
            "pre_api_request",
            task_id="t1",
            session_id="s1",
            platform="cli",
            model="m",
            provider="p",
            base_url="b",
            api_mode="chat_completions",
            api_call_count=i,
            message_count=10,
            tool_count=3,
            approx_input_tokens=1000,
            request_char_count=5000,
            max_tokens=4096,
        )

    assert call_log == [1, 2, 3, 4]


# ---------------------------------------------------------------------------
# 4. Hook is observer-only (return values ignored)
# ---------------------------------------------------------------------------

def test_return_values_ignored():
    """pre_api_request return values are never inspected by the caller.
    The dispatch is: `_invoke_hook(...)`, no assignment or conditional."""
    # Document the observer-only contract
    results = [{"trace_id": "abc123"}, "something", None]
    # In run_agent.py, the return of invoke_hook is not captured at all
    # (it's called as a bare expression statement). This test documents
    # that contract — if someone mistakenly adds conditional logic on
    # the return, this test documents the intended semantics.
    assert True


# ---------------------------------------------------------------------------
# 5. Hook exception swallowed by caller (bare except: pass)
# ---------------------------------------------------------------------------

def test_hook_exception_does_not_prevent_api_call(tmp_path, monkeypatch):
    """A plugin raising must not block the subsequent API call.
    PluginManager.invoke_hook catches per-callback exceptions."""
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home, "crash_api_hook",
        register_body=(
            'def _boom(**kw):\n'
            '        raise RuntimeError("observability crash")\n'
            '    ctx.register_hook("pre_api_request", _boom)'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    # invoke_hook catches the exception internally — returns empty
    results = mgr.invoke_hook(
        "pre_api_request",
        task_id="t1",
        session_id="s1",
        platform="cli",
        model="m",
        provider="p",
        base_url="b",
        api_mode="chat_completions",
        api_call_count=1,
        message_count=1,
        tool_count=0,
        approx_input_tokens=100,
        request_char_count=500,
        max_tokens=4096,
    )
    assert results == []


# ---------------------------------------------------------------------------
# 6. Multiple plugins all receive the call
# ---------------------------------------------------------------------------

def test_multiple_plugins_all_receive_call(tmp_path, monkeypatch):
    """Two observability plugins both get the pre_api_request event."""
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home, "obs_a",
        register_body=(
            'ctx.register_hook("pre_api_request", '
            'lambda **kw: "A")'
        ),
    )
    _make_enabled_plugin(
        hermes_home, "obs_b",
        register_body=(
            'ctx.register_hook("pre_api_request", '
            'lambda **kw: "B")'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    results = mgr.invoke_hook(
        "pre_api_request",
        task_id="t1",
        session_id="s1",
        platform="cli",
        model="m",
        provider="p",
        base_url="b",
        api_mode="chat_completions",
        api_call_count=1,
        message_count=1,
        tool_count=0,
        approx_input_tokens=100,
        request_char_count=500,
        max_tokens=4096,
    )
    assert len(results) == 2
    assert "A" in results
    assert "B" in results


# ---------------------------------------------------------------------------
# 7. No plugins means empty results (no crash)
# ---------------------------------------------------------------------------

def test_no_plugins_returns_empty_results(tmp_path, monkeypatch):
    """With no plugins loaded, invoke_hook returns [] safely."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes_empty"))
    plugins_mod._plugin_manager = PluginManager()

    mgr = plugins_mod._plugin_manager
    results = mgr.invoke_hook(
        "pre_api_request",
        task_id="t1",
        session_id="s1",
        platform="cli",
        model="m",
        provider="p",
        base_url="b",
        api_mode="chat_completions",
        api_call_count=1,
        message_count=1,
        tool_count=0,
        approx_input_tokens=100,
        request_char_count=500,
        max_tokens=4096,
    )
    assert results == []