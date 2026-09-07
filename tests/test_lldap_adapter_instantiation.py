"""test_lldap_adapter_instantiation.py — Unit tests for wire-011.

Tests:
  1. Import path verification (3)
  2. LldapConfig creation / precedence (4)
  3. get_lldap_adapter() factory (2)
  4. bootstrap_lldap() entry point (5)
  5. Config resolution precedence (4)
  6. .env file parsing (2)
  7. YAML config parsing (3)
"""
from __future__ import annotations

import os
import sys
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

# Ensure hermes-agent root is on sys.path so "agent.*" imports resolve.
_HERMES_AGENT_ROOT = str(Path(__file__).parent.parent)
if _HERMES_AGENT_ROOT not in sys.path:
    sys.path.insert(0, _HERMES_AGENT_ROOT)

# Also ensure executive_agents_framework is importable.
_EAF_SRC = "/home/ubuntu/executive_agents_framework/src"
if _EAF_SRC not in sys.path:
    sys.path.insert(0, _EAF_SRC)

# Default LLDAP admin password used in dev/local environments.
# The framework's resolve_admin_password() requires EAF_LLDAP_ADMIN_PASSWORD to be
# set (no source-code fallback). Tests that exercise live LLDAP connection use
# this constant via patch.dict so that the test environment works even when the
# variable is not exported into the shell.
_DEV_LLDAP_PASSWORD = "BootstrapAdminPassword123!@#"
_LLDAP_LIVE_ENV = {"EAF_LLDAP_ADMIN_PASSWORD": _DEV_LLDAP_PASSWORD}


# ===========================================================================
# 1. Import path verification (3 tests)
# ===========================================================================

class TestImportPaths(unittest.TestCase):

    def test_framework_wrapper_importable(self):
        """AC1: agent.framework_wrapper can be imported."""
        import agent.framework_wrapper as fw  # noqa: F401
        self.assertIsNotNone(fw)

    def test_agent_bootstrap_importable(self):
        """AC2: agent.agent_bootstrap can be imported."""
        import agent.agent_bootstrap as ab  # noqa: F401
        self.assertIsNotNone(ab)

    def test_lldap_adapter_importable_from_framework(self):
        """AC1: LldapAdapter importable from the framework via framework_wrapper."""
        from agent.framework_wrapper import get_lldap_adapter  # noqa: F401
        self.assertTrue(callable(get_lldap_adapter))


# ===========================================================================
# 2. LldapConfig creation / precedence (4 tests)
# ===========================================================================

class TestLldapConfig(unittest.TestCase):

    def test_default_host_and_port(self):
        """LldapConfig defaults to localhost:3890."""
        clean_env = {k: v for k, v in os.environ.items()
                     if not k.startswith("LLDAP_") and k != "EAF_LLDAP_BIND_DN"}
        with patch.dict(os.environ, clean_env, clear=True):
            # Temporarily remove LLDAP vars
            from agent.framework_wrapper import LldapConfig
            cfg = LldapConfig()
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.port, 3890)

    def test_explicit_kwargs_override_env(self):
        """Explicit kwargs take precedence over environment variables."""
        with patch.dict(os.environ, {"LLDAP_HOST": "ldap.example.com"}, clear=False):
            from agent.framework_wrapper import LldapConfig
            cfg = LldapConfig(host="my-host")
        self.assertEqual(cfg.host, "my-host")

    def test_env_var_overrides_default(self):
        """LLDAP_HOST env var overrides the compile-time default."""
        with patch.dict(os.environ, {"LLDAP_HOST": "env-host"}, clear=False):
            from agent.framework_wrapper import LldapConfig
            cfg = LldapConfig()
        self.assertEqual(cfg.host, "env-host")

    def test_port_coercion_to_int(self):
        """LldapConfig.port is always an int."""
        from agent.framework_wrapper import LldapConfig
        cfg = LldapConfig(port=3891)
        self.assertIsInstance(cfg.port, int)
        self.assertEqual(cfg.port, 3891)


# ===========================================================================
# 3. get_lldap_adapter() factory (2 tests)
# ===========================================================================

class TestGetLldapAdapterFactory(unittest.TestCase):

    def test_returns_adapter_instance(self):
        """get_lldap_adapter() returns a framework LldapAdapter."""
        with patch.dict(os.environ, _LLDAP_LIVE_ENV, clear=False):
            from agent.framework_wrapper import LldapConfig, get_lldap_adapter
            from executive_agents.infrastructure.adapters.directory.lldap_adapter import (
                LldapAdapter,
            )
            cfg = LldapConfig()
            adapter = get_lldap_adapter(cfg)
        self.assertIsInstance(adapter, LldapAdapter)

    def test_adapter_has_config_attribute(self):
        """Returned adapter exposes a config attribute."""
        with patch.dict(os.environ, _LLDAP_LIVE_ENV, clear=False):
            from agent.framework_wrapper import LldapConfig, get_lldap_adapter
            cfg = LldapConfig()
            adapter = get_lldap_adapter(cfg)
        self.assertTrue(hasattr(adapter, "config"))


# ===========================================================================
# 4. bootstrap_lldap() entry point (5 tests)
# ===========================================================================

class TestBootstrapLldap(unittest.TestCase):

    def test_bootstrap_returns_adapter(self):
        """bootstrap_lldap() returns a non-None adapter when LLDAP is running."""
        with patch.dict(os.environ, _LLDAP_LIVE_ENV, clear=False):
            from agent.agent_bootstrap import bootstrap_lldap
            adapter = bootstrap_lldap()
        self.assertIsNotNone(adapter)

    def test_bootstrap_with_explicit_config(self):
        """bootstrap_lldap(config=...) uses the provided config."""
        with patch.dict(os.environ, _LLDAP_LIVE_ENV, clear=False):
            from agent.agent_bootstrap import bootstrap_lldap
            from agent.framework_wrapper import LldapConfig
            cfg = LldapConfig()
            adapter = bootstrap_lldap(config=cfg)
        self.assertIsNotNone(adapter)

    def test_bootstrap_skip_connection_returns_none_on_bad_host(self):
        """bootstrap_lldap(skip_connection=True) returns None for unreachable host."""
        with patch.dict(os.environ, _LLDAP_LIVE_ENV, clear=False):
            from agent.agent_bootstrap import bootstrap_lldap
            from agent.framework_wrapper import LldapConfig
            cfg = LldapConfig(host="127.0.0.1", port=19999)  # closed port, fast refusal
            result = bootstrap_lldap(config=cfg, skip_connection=True)
        self.assertIsNone(result)

    def test_bootstrap_skip_connection_false_raises_on_bad_host(self):
        """bootstrap_lldap(skip_connection=False) raises on unreachable host."""
        # Use localhost on a port that is definitively not open (avoided 192.0.2.x
        # because TCP to TEST-NET can hang indefinitely on some kernels).
        with patch.dict(os.environ, _LLDAP_LIVE_ENV, clear=False):
            from agent.agent_bootstrap import bootstrap_lldap
            from agent.framework_wrapper import LldapConfig
            cfg = LldapConfig(host="127.0.0.1", port=19999)  # port 19999 should be closed
            with self.assertRaises(Exception):
                bootstrap_lldap(config=cfg, skip_connection=False)

    def test_bootstrap_default_config_resolves(self):
        """bootstrap_lldap() with no args resolves config from environment."""
        with patch.dict(os.environ, _LLDAP_LIVE_ENV, clear=False):
            from agent.agent_bootstrap import bootstrap_lldap
            adapter = bootstrap_lldap()
        self.assertIsNotNone(adapter)


# ===========================================================================
# 5. Config resolution precedence (4 tests)
# ===========================================================================

class TestConfigResolutionPrecedence(unittest.TestCase):

    def test_runtime_env_beats_defaults(self):
        """Runtime env LLDAP_HOST overrides hardcoded default."""
        with patch.dict(os.environ, {"LLDAP_HOST": "runtime-host"}, clear=False):
            from agent.agent_bootstrap import _resolve_config
            cfg = _resolve_config()
        self.assertEqual(cfg.host, "runtime-host")

    def test_env_file_used_when_no_runtime_env(self):
        """~/.hermes/.env is read when runtime env lacks LLDAP_HOST."""
        import tempfile
        env_content = "LLDAP_HOST=env-file-host\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(env_content)
            tmp_path = Path(f.name)
        try:
            clean_env = {k: v for k, v in os.environ.items()
                         if k != "LLDAP_HOST"}
            with patch.dict(os.environ, clean_env, clear=True):
                with patch("agent.agent_bootstrap._ENV_FILE", tmp_path):
                    from agent.agent_bootstrap import _resolve_config
                    cfg = _resolve_config()
            self.assertEqual(cfg.host, "env-file-host")
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_yaml_used_as_fallback(self):
        """config.yaml lldap.host is used when env and .env don't set LLDAP_HOST."""
        import tempfile
        yaml_content = "lldap:\n  host: yaml-host\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            tmp_yaml = Path(f.name)
        try:
            clean_env = {k: v for k, v in os.environ.items()
                         if k != "LLDAP_HOST"}
            with patch.dict(os.environ, clean_env, clear=True):
                with patch("agent.agent_bootstrap._ENV_FILE", Path("/nonexistent/.env")):
                    with patch("agent.agent_bootstrap._CONFIG_YAML", tmp_yaml):
                        from agent.agent_bootstrap import _resolve_config
                        cfg = _resolve_config()
            self.assertEqual(cfg.host, "yaml-host")
        finally:
            tmp_yaml.unlink(missing_ok=True)

    def test_default_host_when_no_overrides(self):
        """Default host 127.0.0.1 is used when no overrides are present."""
        clean_env = {k: v for k, v in os.environ.items()
                     if k != "LLDAP_HOST"}
        with patch.dict(os.environ, clean_env, clear=True):
            with patch("agent.agent_bootstrap._ENV_FILE", Path("/nonexistent/.env")):
                with patch("agent.agent_bootstrap._CONFIG_YAML", Path("/nonexistent/config.yaml")):
                    from agent.agent_bootstrap import _resolve_config
                    cfg = _resolve_config()
        self.assertEqual(cfg.host, "127.0.0.1")


# ===========================================================================
# 6. .env file parsing (2 tests)
# ===========================================================================

class TestEnvFileParsing(unittest.TestCase):

    def test_parses_lldap_vars_from_env_file(self):
        """_load_config_from_env_file returns LLDAP_* vars from the file."""
        import tempfile
        env_content = textwrap.dedent("""\
            # comment
            LLDAP_HOST=my-ldap-host
            LLDAP_PORT=3891
            UNRELATED_VAR=ignore_me
            EAF_LLDAP_BIND_DN=uid=admin,ou=people,dc=test
        """)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".env", delete=False) as f:
            f.write(env_content)
            tmp = Path(f.name)
        try:
            with patch("agent.agent_bootstrap._ENV_FILE", tmp):
                from agent.agent_bootstrap import _load_config_from_env_file
                result = _load_config_from_env_file()
            self.assertEqual(result.get("LLDAP_HOST"), "my-ldap-host")
            self.assertEqual(result.get("LLDAP_PORT"), "3891")
            self.assertNotIn("UNRELATED_VAR", result)
            self.assertIn("EAF_LLDAP_BIND_DN", result)
        finally:
            tmp.unlink(missing_ok=True)

    def test_returns_empty_dict_when_env_file_missing(self):
        """_load_config_from_env_file returns {} when file does not exist."""
        with patch("agent.agent_bootstrap._ENV_FILE", Path("/nonexistent/.env")):
            from agent.agent_bootstrap import _load_config_from_env_file
            result = _load_config_from_env_file()
        self.assertEqual(result, {})


# ===========================================================================
# 7. YAML config parsing (3 tests)
# ===========================================================================

class TestYamlConfigParsing(unittest.TestCase):

    def test_reads_lldap_section(self):
        """_load_config_from_yaml returns the lldap: block."""
        import tempfile
        yaml_content = textwrap.dedent("""\
            some_other_key: value
            lldap:
              host: yaml-host
              port: 3892
              base_dn: dc=test,dc=internal
        """)
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            tmp = Path(f.name)
        try:
            with patch("agent.agent_bootstrap._CONFIG_YAML", tmp):
                from agent.agent_bootstrap import _load_config_from_yaml
                result = _load_config_from_yaml()
            self.assertEqual(result.get("host"), "yaml-host")
            self.assertEqual(result.get("port"), 3892)
            self.assertEqual(result.get("base_dn"), "dc=test,dc=internal")
        finally:
            tmp.unlink(missing_ok=True)

    def test_returns_empty_when_no_lldap_section(self):
        """_load_config_from_yaml returns {} when lldap: key absent."""
        import tempfile
        yaml_content = "other_config:\n  key: value\n"
        with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
            f.write(yaml_content)
            tmp = Path(f.name)
        try:
            with patch("agent.agent_bootstrap._CONFIG_YAML", tmp):
                from agent.agent_bootstrap import _load_config_from_yaml
                result = _load_config_from_yaml()
            self.assertEqual(result, {})
        finally:
            tmp.unlink(missing_ok=True)

    def test_returns_empty_when_yaml_missing(self):
        """_load_config_from_yaml returns {} when config.yaml does not exist."""
        with patch("agent.agent_bootstrap._CONFIG_YAML", Path("/nonexistent/config.yaml")):
            from agent.agent_bootstrap import _load_config_from_yaml
            result = _load_config_from_yaml()
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
