"""Unit tests for LldapAdapter instantiation via framework_wrapper + agent_bootstrap.

Covers:
- LldapConfig creation (defaults, env vars, explicit values)
- framework_wrapper.get_lldap_adapter() lazy import behavior
- agent_bootstrap._resolve_config() precedence (env > .env > yaml > defaults)
- agent_bootstrap.bootstrap_lldap() with skip_connection=True
- Import path: from agent.framework_wrapper import ...
- No import errors or missing dependencies
"""
from __future__ import annotations

import importlib
import os
import sys
import types
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# 1. Import path verification
# ---------------------------------------------------------------------------


class TestImportPaths:
    """Verify that all acceptance-criteria imports work."""

    def test_framework_wrapper_importable(self):
        """AC: LldapAdapter imported from agent.framework_wrapper."""
        mod = importlib.import_module("agent.framework_wrapper")
        assert hasattr(mod, "get_lldap_adapter"), "Missing get_lldap_adapter"
        assert hasattr(mod, "LldapConfig"), "Missing LldapConfig"
        assert hasattr(mod, "get_directory_port_type"), "Missing get_directory_port_type"

    def test_agent_bootstrap_importable(self):
        """AC: agent_bootstrap module exists and is importable."""
        mod = importlib.import_module("agent.agent_bootstrap")
        assert hasattr(mod, "bootstrap_lldap"), "Missing bootstrap_lldap"
        assert hasattr(mod, "test_lldap_connection"), "Missing test_lldap_connection"

    def test_framework_lldap_adapter_importable(self):
        """Underlying framework class is importable."""
        from executive_agents.infrastructure.adapters.directory.lldap_adapter import (
            LldapAdapter,
            LldapConfig as FwLldapConfig,
        )
        assert LldapAdapter is not None
        assert FwLldapConfig is not None


# ---------------------------------------------------------------------------
# 2. LldapConfig (hermes-facing)
# ---------------------------------------------------------------------------


class TestLldapConfig:
    """Test hermes-agent's own LldapConfig wrapper."""

    def _clean_env(self):
        """Remove LLDAP_ env vars, return backup dict."""
        backup = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("LLDAP_")}
        return backup

    def test_defaults(self):
        from agent.framework_wrapper import LldapConfig

        backup = self._clean_env()
        try:
            cfg = LldapConfig()
            assert cfg.host == "127.0.0.1"
            assert cfg.port == 3890
            assert cfg.base_dn == "dc=eaf,dc=hermes2,dc=internal"
        finally:
            os.environ.update(backup)

    def test_explicit_values(self):
        from agent.framework_wrapper import LldapConfig

        backup = self._clean_env()
        try:
            cfg = LldapConfig(host="10.0.0.1", port=636, base_dn="dc=test,dc=org")
            assert cfg.host == "10.0.0.1"
            assert cfg.port == 636
            assert cfg.base_dn == "dc=test,dc=org"
        finally:
            os.environ.update(backup)

    def test_env_vars_override_explicit_kwargs(self):
        """12-factor: runtime env vars override even explicit kwargs."""
        from agent.framework_wrapper import LldapConfig

        backup = self._clean_env()
        os.environ["LLDAP_HOST"] = "env-host"
        try:
            cfg = LldapConfig(host="kwarg-host")
            assert cfg.host == "env-host"
        finally:
            os.environ.pop("LLDAP_HOST", None)
            os.environ.update(backup)

    def test_to_framework_config(self):
        from agent.framework_wrapper import LldapConfig

        backup = self._clean_env()
        try:
            cfg = LldapConfig(host="10.0.0.5", port=3900)
            fw_cfg = cfg.to_framework_config()
            assert fw_cfg.host == "10.0.0.5"
            assert fw_cfg.port == 3900
            from executive_agents.infrastructure.adapters.directory.lldap_adapter import (
                LldapConfig as FwConfig,
            )
            assert isinstance(fw_cfg, FwConfig)
        finally:
            os.environ.update(backup)


# ---------------------------------------------------------------------------
# 3. framework_wrapper.get_lldap_adapter()
# ---------------------------------------------------------------------------


class TestGetLldapAdapter:
    """Test the adapter factory function (without real server)."""

    def test_raises_on_unreachable_server(self):
        """get_lldap_adapter() should raise RuntimeError when server is down."""
        from agent.framework_wrapper import get_lldap_adapter, LldapConfig

        backup = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("LLDAP_")}
        try:
            cfg = LldapConfig(host="127.0.0.1", port=19999)
            with pytest.raises(Exception):
                get_lldap_adapter(cfg)
        finally:
            os.environ.update(backup)

    def test_lazy_import_mechanism(self):
        """Framework classes are loaded on first call, not at import time."""
        mod = importlib.import_module("agent.framework_wrapper")
        mod.get_directory_port_type()
        assert mod._LdapDirectoryPort is not None


# ---------------------------------------------------------------------------
# 4. agent_bootstrap config resolution
# ---------------------------------------------------------------------------


class TestConfigResolution:
    """Test _resolve_config() precedence rules."""

    def _clean_env(self):
        backup = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("LLDAP_")}
        return backup

    def test_defaults_only(self):
        from agent.agent_bootstrap import _resolve_config
        from agent.framework_wrapper import LldapConfig

        backup = self._clean_env()
        try:
            cfg = _resolve_config(env_file_vars={}, yaml_vars={})
            assert isinstance(cfg, LldapConfig)
            assert cfg.host == "127.0.0.1"
        finally:
            os.environ.update(backup)

    def test_yaml_overrides_defaults(self):
        from agent.agent_bootstrap import _resolve_config

        backup = self._clean_env()
        try:
            cfg = _resolve_config(env_file_vars={}, yaml_vars={"host": "yaml-host", "port": 636})
            assert cfg.host == "yaml-host"
            assert cfg.port == 636
        finally:
            os.environ.update(backup)

    def test_env_file_overrides_yaml(self):
        from agent.agent_bootstrap import _resolve_config

        backup = self._clean_env()
        try:
            cfg = _resolve_config(
                env_file_vars={"LLDAP_HOST": "dotenv-host"},
                yaml_vars={"host": "yaml-host"},
            )
            assert cfg.host == "dotenv-host"
        finally:
            os.environ.update(backup)

    def test_runtime_env_overrides_all(self):
        """12-factor: runtime env vars always win."""
        from agent.agent_bootstrap import _resolve_config

        backup = self._clean_env()
        os.environ["LLDAP_HOST"] = "runtime-host"
        try:
            cfg = _resolve_config(
                env_file_vars={"LLDAP_HOST": "dotenv-host"},
                yaml_vars={"host": "yaml-host"},
            )
            assert cfg.host == "runtime-host"
        finally:
            os.environ.pop("LLDAP_HOST", None)
            os.environ.update(backup)


# ---------------------------------------------------------------------------
# 5. agent_bootstrap.bootstrap_lldap()
# ---------------------------------------------------------------------------


class TestBootstrapLldap:
    """Test the main bootstrap entry point."""

    def _clean_env(self):
        return {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("LLDAP_")}

    def test_skip_connection_returns_none_on_failure(self):
        """With skip_connection=True, unreachable server returns None."""
        from agent.agent_bootstrap import bootstrap_lldap
        from agent.framework_wrapper import LldapConfig

        backup = self._clean_env()
        try:
            cfg = LldapConfig(host="127.0.0.1", port=19999)
            result = bootstrap_lldap(config=cfg, skip_connection=True)
            assert result is None
        finally:
            os.environ.update(backup)

    def test_skip_connection_false_raises_on_failure(self):
        """With skip_connection=False, unreachable server raises."""
        from agent.agent_bootstrap import bootstrap_lldap
        from agent.framework_wrapper import LldapConfig

        backup = self._clean_env()
        try:
            cfg = LldapConfig(host="127.0.0.1", port=19999)
            with pytest.raises(Exception):
                bootstrap_lldap(config=cfg, skip_connection=False)
        finally:
            os.environ.update(backup)

    def test_explicit_config_used(self):
        """Explicit config is passed through without file/env resolution."""
        from agent.agent_bootstrap import bootstrap_lldap
        from agent.framework_wrapper import LldapConfig

        backup = self._clean_env()
        try:
            cfg = LldapConfig(host="10.9.9.9", port=12345)
            with pytest.raises(Exception):
                bootstrap_lldap(config=cfg, skip_connection=False)
        finally:
            os.environ.update(backup)


# ---------------------------------------------------------------------------
# 6. test_lldap_connection()
# ---------------------------------------------------------------------------


class TestLldapConnection:
    """Test the connection test helper."""

    def test_none_adapter_returns_false(self):
        from agent.agent_bootstrap import test_lldap_connection

        assert test_lldap_connection(None) is False

    def test_adapter_with_failed_search_returns_false(self):
        from agent.agent_bootstrap import test_lldap_connection

        mock_adapter = mock.MagicMock()
        mock_adapter.config.base_dn = "dc=test"
        mock_adapter.list_agents_by_ou.side_effect = Exception("search failed")
        assert test_lldap_connection(mock_adapter) is False

    def test_adapter_with_successful_search_returns_true(self):
        from agent.agent_bootstrap import test_lldap_connection

        mock_adapter = mock.MagicMock()
        mock_adapter.config.base_dn = "dc=test"
        mock_adapter.list_agents_by_ou.return_value = [
            {"agent_id": "test_agent", "cn": "Test Agent"},
        ]
        assert test_lldap_connection(mock_adapter) is True


# ---------------------------------------------------------------------------
# 7. .env file parsing
# ---------------------------------------------------------------------------


class TestEnvFileParsing:
    """Test _load_config_from_env_file()."""

    def test_nonexistent_file_returns_empty(self):
        from agent.agent_bootstrap import _load_config_from_env_file

        with mock.patch("os.path.isfile", return_value=False):
            result = _load_config_from_env_file()
            assert result == {}

    def test_parses_lldap_vars(self, tmp_path):
        from agent.agent_bootstrap import _load_config_from_env_file

        env_file = tmp_path / ".env"
        env_file.write_text(
            "# LLDAP config\n"
            "LLDAP_HOST=custom-host\n"
            "LLDAP_PORT=636\n"
            "SOME_OTHER_VAR=ignore\n"
        )
        with mock.patch("os.path.expanduser", return_value=str(env_file)):
            with mock.patch("os.path.isfile", return_value=True):
                result = _load_config_from_env_file()
                assert result["LLDAP_HOST"] == "custom-host"
                assert result["LLDAP_PORT"] == "636"
                assert "SOME_OTHER_VAR" not in result


# ---------------------------------------------------------------------------
# 8. YAML config parsing
# ---------------------------------------------------------------------------


class TestYamlConfigParsing:
    """Test _load_config_from_yaml()."""

    def test_nonexistent_file_returns_empty(self):
        from agent.agent_bootstrap import _load_config_from_yaml

        with mock.patch("os.path.isfile", return_value=False):
            result = _load_config_from_yaml()
            assert result == {}

    def test_parses_lldap_section(self, tmp_path):
        from agent.agent_bootstrap import _load_config_from_yaml

        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(
            "lldap:\n"
            "  host: yaml-host\n"
            "  port: 3900\n"
            "  base_dn: dc=yaml,dc=test\n"
        )
        with mock.patch("os.path.expanduser", return_value=str(yaml_file)):
            with mock.patch("os.path.isfile", return_value=True):
                result = _load_config_from_yaml()
                assert result["host"] == "yaml-host"
                assert result["port"] == 3900
                assert result["base_dn"] == "dc=yaml,dc=test"

    def test_missing_lldap_section_returns_empty(self, tmp_path):
        from agent.agent_bootstrap import _load_config_from_yaml

        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text("other_key: other_value\n")
        with mock.patch("os.path.expanduser", return_value=str(yaml_file)):
            with mock.patch("os.path.isfile", return_value=True):
                result = _load_config_from_yaml()
                assert result == {}
