"""Tests for the ``pre_llm_call`` plugin hook.

The hook fires inside ``AIAgent.run_conversation`` once per turn,
BEFORE the tool-calling loop begins.  It is the plugin's chance to
inject ephemeral context into the user message (never the system
prompt, to preserve prompt-cache prefix).

The dispatch contract in run_agent.py (line ~11798):

    _pre_results = _invoke_hook(
        "pre_llm_call",
        session_id=self.session_id,
        user_message=original_user_message,
        conversation_history=list(messages),
        is_first_turn=(not bool(conversation_history)),
        model=self.model,
        platform=getattr(self, "platform", None) or "",
        sender_id=getattr(self, "_user_id", None) or "",
    )
    _ctx_parts: list[str] = []
    for r in _pre_results:
        if isinstance(r, dict) and r.get("context"):
            _ctx_parts.append(str(r["context"]))
        elif isinstance(r, str) and r.strip():
            _ctx_parts.append(r)
    if _ctx_parts:
        _plugin_user_context = "\\n\\n".join(_ctx_parts)

Context strings are appended to the user message.  An exception in the
hook is caught and logged — it must not break the agent loop.
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

def test_pre_llm_call_in_valid_hooks():
    assert "pre_llm_call" in VALID_HOOKS


# ---------------------------------------------------------------------------
# 2. Hook receives all expected kwargs
# ---------------------------------------------------------------------------

def test_hook_receives_expected_kwargs(tmp_path, monkeypatch):
    """The callback must see session_id, user_message, conversation_history,
    is_first_turn, model, platform, sender_id."""
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home, "capture_pre_llm",
        register_body=(
            'ctx.register_hook("pre_llm_call", '
            'lambda **kw: f"{kw[\'session_id\']}|{kw[\'user_message\']}|'
            '{kw[\'is_first_turn\']}|{kw[\'model\']}|{kw[\'platform\']}|'
            '{kw[\'sender_id\']}")'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    results = mgr.invoke_hook(
        "pre_llm_call",
        session_id="s-42",
        user_message="hello agent",
        conversation_history=[{"role": "user", "content": "prior"}],
        is_first_turn=False,
        model="anthropic/claude-sonnet-4",
        platform="telegram",
        sender_id="user-999",
    )
    assert results == ["s-42|hello agent|False|anthropic/claude-sonnet-4|telegram|user-999"]


# ---------------------------------------------------------------------------
# 3. Dict with "context" key is extracted
# ---------------------------------------------------------------------------

def test_dict_with_context_key_is_extracted(tmp_path, monkeypatch):
    """Plugin returns {"context": "..."} and the caller extracts the string."""
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home, "ctx_injector",
        register_body=(
            'ctx.register_hook("pre_llm_call", '
            'lambda **kw: {"context": "recalled: important fact"})'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    results = mgr.invoke_hook(
        "pre_llm_call",
        session_id="s1",
        user_message="hi",
        conversation_history=[],
        is_first_turn=True,
        model="m",
        platform="cli",
        sender_id="",
    )
    # Simulate the run_agent.py extraction loop
    ctx_parts: list[str] = []
    for r in results:
        if isinstance(r, dict) and r.get("context"):
            ctx_parts.append(str(r["context"]))
        elif isinstance(r, str) and r.strip():
            ctx_parts.append(r)
    assert ctx_parts == ["recalled: important fact"]


# ---------------------------------------------------------------------------
# 4. Plain string return is also valid context
# ---------------------------------------------------------------------------

def test_plain_string_return_is_valid_context(tmp_path, monkeypatch):
    """A plugin returning a bare string (not wrapped in a dict) is also
    treated as injectable context."""
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home, "string_injector",
        register_body=(
            'ctx.register_hook("pre_llm_call", '
            'lambda **kw: "plain context string")'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    results = mgr.invoke_hook(
        "pre_llm_call",
        session_id="s1",
        user_message="msg",
        conversation_history=[],
        is_first_turn=True,
        model="m",
        platform="cli",
        sender_id="",
    )
    ctx_parts: list[str] = []
    for r in results:
        if isinstance(r, dict) and r.get("context"):
            ctx_parts.append(str(r["context"]))
        elif isinstance(r, str) and r.strip():
            ctx_parts.append(r)
    assert ctx_parts == ["plain context string"]


# ---------------------------------------------------------------------------
# 5. Multiple plugins contribute concatenated context
# ---------------------------------------------------------------------------

def test_multiple_plugins_contribute_concatenated_context(tmp_path, monkeypatch):
    """Two plugins each return context; both are joined with \\n\\n."""
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home, "mem_plugin",
        register_body=(
            'ctx.register_hook("pre_llm_call", '
            'lambda **kw: {"context": "memory: last topic was X"})'
        ),
    )
    _make_enabled_plugin(
        hermes_home, "rule_plugin",
        register_body=(
            'ctx.register_hook("pre_llm_call", '
            'lambda **kw: "rule: always greet politely")'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    results = mgr.invoke_hook(
        "pre_llm_call",
        session_id="s1",
        user_message="msg",
        conversation_history=[],
        is_first_turn=True,
        model="m",
        platform="cli",
        sender_id="",
    )
    ctx_parts: list[str] = []
    for r in results:
        if isinstance(r, dict) and r.get("context"):
            ctx_parts.append(str(r["context"]))
        elif isinstance(r, str) and r.strip():
            ctx_parts.append(r)
    assert "memory: last topic was X" in ctx_parts
    assert "rule: always greet politely" in ctx_parts
    joined = "\n\n".join(ctx_parts)
    assert "memory: last topic was X" in joined
    assert "rule: always greet politely" in joined


# ---------------------------------------------------------------------------
# 6. Hook exception does not break agent loop
# ---------------------------------------------------------------------------

def test_hook_exception_does_not_break_dispatch(tmp_path, monkeypatch):
    """A plugin raising an exception must not prevent other plugins
    from contributing context, and invoke_hook must return only
    successful results."""
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home, "crash_plugin",
        register_body=(
            'def _boom(**kw):\n'
            '        raise RuntimeError("plugin crashed")\n'
            '    ctx.register_hook("pre_llm_call", _boom)'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    # The crashing plugin should produce empty results (caught internally)
    results = mgr.invoke_hook(
        "pre_llm_call",
        session_id="s1",
        user_message="msg",
        conversation_history=[],
        is_first_turn=True,
        model="m",
        platform="cli",
        sender_id="",
    )
    assert results == []


# ---------------------------------------------------------------------------
# 7. None / empty-string returns are not treated as context
# ---------------------------------------------------------------------------

def test_none_and_empty_returns_not_treated_as_context(tmp_path, monkeypatch):
    """Plugin callbacks returning None or empty string must not be
    included in the caller's context concatenation."""
    hermes_home = tmp_path / "hermes_test"
    hermes_home.mkdir(exist_ok=True)
    _make_enabled_plugin(
        hermes_home, "noop_plugin",
        register_body=(
            'def _noop(**kw):\n'
            '        return None\n'
            '    ctx.register_hook("pre_llm_call", _noop)'
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    mgr = PluginManager()
    mgr.discover_and_load()

    results = mgr.invoke_hook(
        "pre_llm_call",
        session_id="s1",
        user_message="msg",
        conversation_history=[],
        is_first_turn=True,
        model="m",
        platform="cli",
        sender_id="",
    )
    # None returns are filtered out by invoke_hook (only non-None added)
    assert results == []