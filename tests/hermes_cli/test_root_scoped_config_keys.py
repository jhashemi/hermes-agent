"""Regression tests for VFE-SAFE-02 — root-scoped config-key routing.

Kanban t_c43af288: ``hermes config set vfe.enabled X`` used to silently write
to ``<HERMES_HOME>/config.yaml`` when the CLI was running under an active
profile, but the VFE daemon polls the ROOT ``config.yaml`` (a per-host
service unscoped to any profile). Result: the kill-switch escape hatch
(INV-08, 30 s ceiling) was broken — an operator flipping the flag in a
panic would land in a profile config the daemon never reads.

This test suite locks in that:

  1. ``vfe.*`` writes always land in the root ``config.yaml`` regardless of
     the active profile (5 profile contexts + no-profile baseline).
  2. Non-``vfe`` writes keep the historical profile-scoped semantics.
  3. ``config get`` for ``vfe.*`` reads the root config directly, so
     verifying a flip returns the value the daemon will see.
  4. ``config unset vfe.*`` targets the root file too.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from hermes_cli.config import (
    _is_root_scoped_key,
    _resolve_config_path_for_key,
    get_config_value,
    get_root_config_path,
    set_config_value,
    unset_config_value,
)


PROFILE_NAMES = (
    "alan_kay",
    "margaret_hamilton",
    "jeff_dean",
    "elon_musk",
    "helios",
)


@pytest.fixture
def hermes_root(tmp_path):
    """Create a fake Hermes home layout with root + N profile subdirs.

    Layout mirrors production:

        <tmp>/config.yaml            <-- ROOT (what the VFE daemon polls)
        <tmp>/.env
        <tmp>/profiles/<name>/config.yaml
        <tmp>/profiles/<name>/.env

    Yields the root path so callers can point ``HERMES_HOME`` at any of the
    profile dirs and assert routing.
    """
    root = tmp_path
    (root / "config.yaml").write_text(
        yaml.safe_dump({"vfe": {"enabled": True, "kill_switch_poll_interval_seconds": 5}}),
        encoding="utf-8",
    )
    (root / ".env").touch()
    for name in PROFILE_NAMES:
        p = root / "profiles" / name
        p.mkdir(parents=True, exist_ok=True)
        (p / "config.yaml").write_text(
            yaml.safe_dump({"model": f"stub-{name}"}),
            encoding="utf-8",
        )
        (p / ".env").touch()
    return root


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


# ---------------------------------------------------------------------------
# Helper predicates
# ---------------------------------------------------------------------------


class TestRootScopedKeyPredicate:
    """The `_is_root_scoped_key` gate must match exactly the intended namespace."""

    @pytest.mark.parametrize("key", [
        "vfe",
        "vfe.enabled",
        "vfe.kill_switch_poll_interval_seconds",
        "vfe.subsection.nested",
        "VFE.enabled",  # case-insensitive on the head segment
    ])
    def test_vfe_prefix_is_root_scoped(self, key):
        assert _is_root_scoped_key(key) is True

    @pytest.mark.parametrize("key", [
        "model",
        "model.name",
        "terminal.backend",
        "tts.provider",
        "vfeenabled",           # no dot boundary, not the vfe namespace
        "prefix.vfe.enabled",   # vfe is not the head segment
        "",
    ])
    def test_non_vfe_is_profile_scoped(self, key):
        assert _is_root_scoped_key(key) is False


# ---------------------------------------------------------------------------
# `_resolve_config_path_for_key` returns root path + override flag correctly
# ---------------------------------------------------------------------------


class TestResolveConfigPathForKey:
    """Path resolver returns (root, True) for vfe.* under a profile."""

    @pytest.mark.parametrize("profile_name", PROFILE_NAMES)
    def test_vfe_key_routes_to_root_under_every_profile(
        self, profile_name, hermes_root
    ):
        profile_home = hermes_root / "profiles" / profile_name
        with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}):
            path, is_override = _resolve_config_path_for_key("vfe.enabled")
        assert path == hermes_root / "config.yaml"
        assert is_override is True

    def test_vfe_key_without_active_profile_stays_root_no_override_flag(
        self, hermes_root
    ):
        """When HERMES_HOME is already root, no reroute happens (flag=False)."""
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_root)}):
            path, is_override = _resolve_config_path_for_key("vfe.enabled")
        assert path == hermes_root / "config.yaml"
        assert is_override is False

    @pytest.mark.parametrize("profile_name", PROFILE_NAMES)
    def test_non_vfe_key_stays_profile_scoped(self, profile_name, hermes_root):
        profile_home = hermes_root / "profiles" / profile_name
        with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}):
            path, is_override = _resolve_config_path_for_key("model")
        assert path == profile_home / "config.yaml"
        assert is_override is False


# ---------------------------------------------------------------------------
# End-to-end: set_config_value / unset_config_value under every profile
# ---------------------------------------------------------------------------


class TestVfeSet_UnderActiveProfile_LandsInRoot:
    """The core DoD invariant: `hermes config set vfe.enabled X` from any of
    five profile contexts must write to the root config.yaml the daemon polls.
    """

    @pytest.mark.parametrize("profile_name", PROFILE_NAMES)
    def test_set_vfe_enabled_false_lands_in_root_config(
        self, profile_name, hermes_root, capsys
    ):
        profile_home = hermes_root / "profiles" / profile_name
        with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}):
            set_config_value("vfe.enabled", "false", force=True)

        root_cfg = _read_yaml(hermes_root / "config.yaml")
        assert root_cfg["vfe"]["enabled"] is False, (
            f"Expected root config vfe.enabled=False after CLI set under "
            f"profile={profile_name!r}; got: {root_cfg.get('vfe')}"
        )

        # Profile config must NOT have grown a vfe block (regression guard —
        # this was the exact bug: writes silently going to the profile).
        profile_cfg = _read_yaml(profile_home / "config.yaml")
        assert "vfe" not in profile_cfg, (
            f"Profile {profile_name!r} config leaked a vfe block: "
            f"{profile_cfg.get('vfe')}"
        )

        # UX: the CLI must announce the reroute so the operator sees which
        # file was actually touched. Without this the fix is invisible.
        out = capsys.readouterr().out
        assert "daemon-global key" in out, (
            f"Reroute notice missing from CLI output for profile "
            f"{profile_name!r}: {out!r}"
        )

    @pytest.mark.parametrize("profile_name", PROFILE_NAMES)
    def test_set_vfe_enabled_true_lands_in_root_config(
        self, profile_name, hermes_root
    ):
        # First set to False, then flip back to True — mirrors the operator
        # workflow of engaging then rescinding the kill-switch.
        profile_home = hermes_root / "profiles" / profile_name
        with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}):
            set_config_value("vfe.enabled", "false", force=True)
            set_config_value("vfe.enabled", "true", force=True)

        root_cfg = _read_yaml(hermes_root / "config.yaml")
        assert root_cfg["vfe"]["enabled"] is True

    @pytest.mark.parametrize("profile_name", PROFILE_NAMES)
    def test_set_vfe_poll_interval_lands_in_root(self, profile_name, hermes_root):
        profile_home = hermes_root / "profiles" / profile_name
        with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}):
            set_config_value(
                "vfe.kill_switch_poll_interval_seconds", "10", force=True
            )

        root_cfg = _read_yaml(hermes_root / "config.yaml")
        assert root_cfg["vfe"]["kill_switch_poll_interval_seconds"] == 10


class TestNonVfeSet_UnderActiveProfile_StaysProfileScoped:
    """Regression guard: only vfe.* is special-cased. Every other key must
    keep the historical profile-scoping semantics.
    """

    @pytest.mark.parametrize("profile_name", PROFILE_NAMES)
    @pytest.mark.parametrize("key,value", [
        ("model", "anthropic/claude-sonnet-4"),
        ("terminal.backend", "local"),
        ("tts.provider", "edge"),
    ])
    def test_non_vfe_stays_profile(self, profile_name, key, value, hermes_root):
        profile_home = hermes_root / "profiles" / profile_name
        with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}):
            set_config_value(key, value, force=True)

        profile_cfg = _read_yaml(profile_home / "config.yaml")
        # The dotted key should exist somewhere in the nested tree of the
        # PROFILE config (not the root).
        head = key.split(".", 1)[0]
        assert head in profile_cfg, (
            f"Non-vfe key {key!r} should be profile-scoped for "
            f"profile={profile_name!r}"
        )
        root_cfg = _read_yaml(hermes_root / "config.yaml")
        # Root config's vfe block must be untouched — no cross-contamination.
        assert head not in root_cfg or head == "vfe", (
            f"Non-vfe key {key!r} leaked into root config from profile "
            f"{profile_name!r}"
        )


class TestVfeGet_UnderActiveProfile_ReadsRoot:
    """`hermes config get vfe.X` under a profile must return the ROOT value —
    otherwise operators can't verify a flip they just made.
    """

    @pytest.mark.parametrize("profile_name", PROFILE_NAMES)
    def test_get_vfe_enabled_reads_root(
        self, profile_name, hermes_root, capsys
    ):
        # Seed root with a known value.
        root_path = hermes_root / "config.yaml"
        data = yaml.safe_load(root_path.read_text(encoding="utf-8")) or {}
        data.setdefault("vfe", {})["enabled"] = False
        root_path.write_text(yaml.safe_dump(data), encoding="utf-8")

        profile_home = hermes_root / "profiles" / profile_name
        with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}):
            get_config_value("vfe.enabled")

        # `get_config_value` prints the value; capture stdout.
        out = capsys.readouterr().out.strip()
        # Booleans are formatted lower-case ("false") by _format_config_get_value.
        assert out.lower() in ("false",), (
            f"Expected `get vfe.enabled` under profile={profile_name!r} to "
            f"print the root value 'false'; got: {out!r}"
        )


class TestVfeUnset_UnderActiveProfile_TargetsRoot:
    """`hermes config unset vfe.X` from a profile must remove the key from
    the root config, not silently succeed on a profile file the daemon never
    reads.
    """

    @pytest.mark.parametrize("profile_name", PROFILE_NAMES)
    def test_unset_vfe_removes_from_root(
        self, profile_name, hermes_root, capsys
    ):
        # Seed root with the key present.
        root_path = hermes_root / "config.yaml"
        data = yaml.safe_load(root_path.read_text(encoding="utf-8")) or {}
        data.setdefault("vfe", {})["temp_flag"] = "sentinel"
        root_path.write_text(yaml.safe_dump(data), encoding="utf-8")

        profile_home = hermes_root / "profiles" / profile_name
        with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}):
            unset_config_value("vfe.temp_flag")

        root_cfg = _read_yaml(root_path)
        assert "temp_flag" not in root_cfg.get("vfe", {}), (
            f"unset vfe.temp_flag under profile={profile_name!r} did not "
            f"reach the root config: {root_cfg.get('vfe')}"
        )


class TestGetRootConfigPath:
    """The `get_root_config_path` primitive must return the same file under
    every profile — that is the whole point of the reroute.
    """

    def test_root_path_stable_across_profiles(self, hermes_root):
        seen = set()
        for name in PROFILE_NAMES:
            profile_home = hermes_root / "profiles" / name
            with patch.dict(os.environ, {"HERMES_HOME": str(profile_home)}):
                seen.add(get_root_config_path())
        assert seen == {hermes_root / "config.yaml"}, (
            f"get_root_config_path drifted across profiles: {seen}"
        )
