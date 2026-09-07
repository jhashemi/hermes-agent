"""TDD tests for policy_state — enable/disable lifecycle state management."""
from __future__ import annotations

import datetime as _dt
import json
import os
from pathlib import Path

import pytest

from tools.l4_agt_compiler.policy_state import (
    check_auto_revert,
    get_policy_state,
    load_state,
    save_state,
    set_policy_state,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _state_path(tmp_path: Path) -> Path:
    return tmp_path / ".hermes" / "policies" / ".state.json"


# ── load / save ───────────────────────────────────────────────────────────────


def test_load_state_returns_empty_when_missing(tmp_path):
    state = load_state(_state_path(tmp_path))
    assert state == {"policies": {}}


def test_save_and_reload_roundtrip(tmp_path):
    sp = _state_path(tmp_path)
    original = {
        "policies": {
            "hermes.governance": {
                "enabled": True,
                "enforcement": "monitor",
                "last_changed_at": "2025-06-01T00:00:00+00:00",
                "changed_by": "alice",
            }
        }
    }
    save_state(original, sp)
    reloaded = load_state(sp)
    assert reloaded == original


def test_load_state_handles_corrupt_file(tmp_path):
    sp = _state_path(tmp_path)
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text("NOT JSON", encoding="utf-8")
    state = load_state(sp)
    assert state == {"policies": {}}


def test_save_state_creates_parent_dirs(tmp_path):
    sp = tmp_path / "deep" / "nested" / "dir" / ".state.json"
    save_state({"policies": {}}, sp)
    assert sp.is_file()


def test_atomic_write_no_partial_state(tmp_path, monkeypatch):
    """If json.dump raises mid-write, the original file should remain intact."""
    sp = _state_path(tmp_path)
    original = {"policies": {"p.axis": {"enabled": True, "enforcement": "enforce"}}}
    save_state(original, sp)

    # Monkey-patch os.replace to simulate a crash after write but before rename
    original_replace = os.replace
    calls = []

    def exploding_replace(src, dst):
        calls.append((src, dst))
        raise OSError("simulated disk full")

    monkeypatch.setattr(os, "replace", exploding_replace)
    with pytest.raises(OSError):
        save_state({"policies": {"p.axis": {"enabled": False}}}, sp)

    monkeypatch.setattr(os, "replace", original_replace)
    # Original file must still contain the original data
    reloaded = load_state(sp)
    assert reloaded == original


# ── set_policy_state ──────────────────────────────────────────────────────────


def test_set_policy_state_creates_entry(tmp_path):
    sp = _state_path(tmp_path)
    entry = set_policy_state(
        "app.governance",
        enabled=True,
        enforcement="monitor",
        changed_by="dev",
        state_path=sp,
    )
    assert entry["enabled"] is True
    assert entry["enforcement"] == "monitor"
    assert entry["changed_by"] == "dev"
    assert "last_changed_at" in entry


def test_set_policy_state_validates_enforcement(tmp_path):
    sp = _state_path(tmp_path)
    with pytest.raises(ValueError, match="enforcement must be"):
        set_policy_state(
            "app.governance",
            enabled=True,
            enforcement="BADVALUE",
            changed_by="dev",
            state_path=sp,
        )


def test_set_policy_state_disable_sets_auto_revert(tmp_path):
    sp = _state_path(tmp_path)
    before = _dt.datetime.now(_dt.timezone.utc)
    entry = set_policy_state(
        "app.runtime",
        enabled=False,
        enforcement="disabled",
        changed_by="ops",
        reason="emergency break-glass",
        state_path=sp,
    )
    assert entry["enabled"] is False
    assert entry["enforcement"] == "disabled"
    assert entry["reason"] == "emergency break-glass"
    assert "auto_revert_at" in entry

    # auto_revert_at should be approximately 24h from now
    revert_dt = _dt.datetime.fromisoformat(entry["auto_revert_at"])
    if revert_dt.tzinfo is None:
        revert_dt = revert_dt.replace(tzinfo=_dt.timezone.utc)
    delta = revert_dt - before
    assert 23 * 3600 < delta.total_seconds() < 25 * 3600


def test_set_policy_state_enable_clears_auto_revert(tmp_path):
    sp = _state_path(tmp_path)
    # First disable
    set_policy_state(
        "app.runtime",
        enabled=False,
        enforcement="disabled",
        changed_by="ops",
        reason="break-glass",
        state_path=sp,
    )
    # Then re-enable
    entry = set_policy_state(
        "app.runtime",
        enabled=True,
        enforcement="monitor",
        changed_by="ops",
        state_path=sp,
    )
    assert "auto_revert_at" not in entry


def test_set_policy_state_persists_to_file(tmp_path):
    sp = _state_path(tmp_path)
    set_policy_state(
        "myapp.usage",
        enabled=True,
        enforcement="enforce",
        changed_by="ci-bot",
        state_path=sp,
    )
    state = load_state(sp)
    assert "myapp.usage" in state["policies"]
    assert state["policies"]["myapp.usage"]["enforcement"] == "enforce"


# ── get_policy_state ──────────────────────────────────────────────────────────


def test_get_policy_state_returns_runtime_state(tmp_path):
    sp = _state_path(tmp_path)
    set_policy_state(
        "app.integration",
        enabled=True,
        enforcement="enforce",
        changed_by="alice",
        state_path=sp,
    )
    result = get_policy_state("app.integration", state_path=sp)
    assert result["enabled"] is True
    assert result["enforcement"] == "enforce"


def test_get_policy_state_falls_back_to_header_defaults(tmp_path):
    sp = _state_path(tmp_path)  # empty state
    defaults = {"enabled": True, "enforcement": "monitor"}
    result = get_policy_state("app.governance", state_path=sp, header_defaults=defaults)
    assert result["enabled"] is True
    assert result["enforcement"] == "monitor"


def test_get_policy_state_returns_safe_defaults_when_no_state_no_header(tmp_path):
    sp = _state_path(tmp_path)
    result = get_policy_state("unknown.policy", state_path=sp)
    assert result["enabled"] is False
    assert result["enforcement"] == "disabled"


# ── check_auto_revert ─────────────────────────────────────────────────────────


def test_check_auto_revert_returns_expired(tmp_path):
    """Policies whose auto_revert_at is in the past should be returned."""
    past = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=1)).isoformat()
    future = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=23)).isoformat()

    state = {
        "policies": {
            "app.governance": {
                "enabled": False,
                "enforcement": "disabled",
                "auto_revert_at": past,
            },
            "app.runtime": {
                "enabled": False,
                "enforcement": "disabled",
                "auto_revert_at": future,
            },
            "app.usage": {
                "enabled": True,
                "enforcement": "monitor",
                # no auto_revert_at
            },
        }
    }
    expired = check_auto_revert(state)
    assert expired == ["app.governance"]


def test_check_auto_revert_returns_empty_when_none_expired():
    future = (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(hours=23)).isoformat()
    state = {
        "policies": {
            "a.b": {"enabled": False, "enforcement": "disabled", "auto_revert_at": future}
        }
    }
    assert check_auto_revert(state) == []


def test_check_auto_revert_empty_state():
    assert check_auto_revert({"policies": {}}) == []
