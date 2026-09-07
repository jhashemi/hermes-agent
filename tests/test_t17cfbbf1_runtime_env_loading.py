"""Tests for t_17cfbbf1: env vars loaded at runtime (per-init), not at module import.

DoD acceptance:
  - load_instances_from_config() reads env at runtime
  - Changing HERMES_HTTP_KEY env var works on next InstanceOrchestrator init
  - Instances reloadable without Python restart via orchestrator.reload_instances()
  - HERMES_INSTANCES no longer built at import time from baked constants
"""

import os
from unittest.mock import patch

import pytest

from gateway.instance_orchestrator import (
    HERMES_INSTANCES,
    InstanceOrchestrator,
    RemoteHermesInstance,
    load_instances_from_config,
    reload_hermes_instances,
)


class TestLoadInstancesFromConfig:
    """load_instances_from_config() reads os.environ on every call."""

    def test_returns_dict_of_instances(self):
        instances = load_instances_from_config()
        assert isinstance(instances, dict)
        assert "local" in instances
        assert "hermes2" in instances
        assert all(isinstance(v, RemoteHermesInstance) for v in instances.values())

    def test_reads_hermes_http_key_from_env(self):
        """DoD: changing HERMES_HTTP_KEY env var works on next init."""
        with patch.dict(os.environ, {"HERMES_HTTP_KEY": "test-key-runtime-A"}, clear=False):
            # Ensure per-instance override is unset for this test.
            os.environ.pop("HERMES2_HTTP_KEY", None)
            instances = load_instances_from_config()
            assert instances["hermes2"].http_key == "test-key-runtime-A"

        # Change the env var — a new call reflects the new value.
        with patch.dict(os.environ, {"HERMES_HTTP_KEY": "test-key-runtime-B"}, clear=False):
            os.environ.pop("HERMES2_HTTP_KEY", None)
            instances = load_instances_from_config()
            assert instances["hermes2"].http_key == "test-key-runtime-B"

    def test_hermes2_http_key_overrides_fleet_wide_key(self):
        with patch.dict(
            os.environ,
            {"HERMES_HTTP_KEY": "fleet-wide", "HERMES2_HTTP_KEY": "instance-specific"},
            clear=False,
        ):
            instances = load_instances_from_config()
            assert instances["hermes2"].http_key == "instance-specific"

    def test_reads_hermes2_ip_from_env(self):
        with patch.dict(os.environ, {"HERMES2_IP": "10.0.0.42"}, clear=False):
            instances = load_instances_from_config()
            assert instances["hermes2"].ip == "10.0.0.42"

    def test_reads_hermes2_username_from_env(self):
        with patch.dict(os.environ, {"HERMES2_USERNAME": "custom-user"}, clear=False):
            instances = load_instances_from_config()
            assert instances["hermes2"].username == "custom-user"

    def test_default_username_is_ubuntu(self):
        env = {k: v for k, v in os.environ.items() if k != "HERMES2_USERNAME"}
        with patch.dict(os.environ, env, clear=True):
            instances = load_instances_from_config()
            assert instances["hermes2"].username == "ubuntu"

    def test_invalid_port_falls_back_to_8000(self):
        with patch.dict(os.environ, {"HERMES2_PORT": "not-an-int"}, clear=False):
            instances = load_instances_from_config()
            assert instances["hermes2"].http_port == 8000

    def test_out_of_range_port_falls_back_to_8000(self):
        with patch.dict(os.environ, {"HERMES2_PORT": "99999"}, clear=False):
            instances = load_instances_from_config()
            assert instances["hermes2"].http_port == 8000

    def test_each_call_returns_fresh_dict(self):
        """No caching: each call is an independent read of env."""
        a = load_instances_from_config()
        b = load_instances_from_config()
        assert a is not b  # different dict identities
        assert a["hermes2"] is not b["hermes2"]  # different instance identities


class TestOrchestratorInitLoadsAtRuntime:
    """DoD: InstanceOrchestrator.__init__() calls load_instances_from_config()."""

    def test_init_populates_from_current_env(self):
        """Constructing an orchestrator picks up current env vars."""
        with patch.dict(os.environ, {"HERMES_HTTP_KEY": "init-time-key"}, clear=False):
            os.environ.pop("HERMES2_HTTP_KEY", None)
            orch = InstanceOrchestrator()
            assert orch._instances["hermes2"].http_key == "init-time-key"

    def test_changing_env_between_inits_takes_effect(self):
        """DoD test: changing HERMES_HTTP_KEY env var works on next init."""
        os.environ.pop("HERMES2_HTTP_KEY", None)

        with patch.dict(os.environ, {"HERMES_HTTP_KEY": "key-first"}, clear=False):
            os.environ.pop("HERMES2_HTTP_KEY", None)
            orch1 = InstanceOrchestrator()
            assert orch1._instances["hermes2"].http_key == "key-first"

        with patch.dict(os.environ, {"HERMES_HTTP_KEY": "key-second"}, clear=False):
            os.environ.pop("HERMES2_HTTP_KEY", None)
            orch2 = InstanceOrchestrator()
            assert orch2._instances["hermes2"].http_key == "key-second"

        # Two orchestrators, two different env snapshots, no restart in between.
        assert orch1._instances["hermes2"].http_key != orch2._instances["hermes2"].http_key

    def test_init_accepts_injected_registry(self):
        """Callers can inject a registry for tests without touching env."""
        custom = {
            "test-only": RemoteHermesInstance(
                name="test-only",
                hostname="127.0.0.1",
                ip="127.0.0.1",
                http_port=9000,
            )
        }
        orch = InstanceOrchestrator(instances=custom)
        assert "test-only" in orch._instances
        # Injected registry stands alone — no local/hermes2 loaded from env.
        assert "local" not in orch._instances

    def test_get_instance_uses_per_orchestrator_registry(self):
        with patch.dict(os.environ, {"HERMES2_IP": "192.168.100.100"}, clear=False):
            orch = InstanceOrchestrator()
        # Even after the env changes back, the orchestrator holds the snapshot
        # it saw at init time — that IS the point (no accidental live-reload).
        with patch.dict(os.environ, {"HERMES2_IP": "10.0.0.1"}, clear=False):
            instance = orch.get_instance("hermes2")
            assert instance is not None
            assert instance.ip == "192.168.100.100"


class TestReloadWithoutRestart:
    """DoD: instances reloadable without Python restart."""

    def test_reload_instances_picks_up_new_env(self):
        os.environ.pop("HERMES2_HTTP_KEY", None)

        with patch.dict(os.environ, {"HERMES_HTTP_KEY": "before-reload"}, clear=False):
            os.environ.pop("HERMES2_HTTP_KEY", None)
            orch = InstanceOrchestrator()
            assert orch._instances["hermes2"].http_key == "before-reload"

        # Change env — orchestrator still holds the old value (as expected).
        with patch.dict(os.environ, {"HERMES_HTTP_KEY": "after-reload"}, clear=False):
            os.environ.pop("HERMES2_HTTP_KEY", None)
            assert orch._instances["hermes2"].http_key == "before-reload"

            # Reload — no restart, same Python process, same orchestrator instance.
            orch.reload_instances()
            assert orch._instances["hermes2"].http_key == "after-reload"

    def test_reload_returns_new_registry(self):
        orch = InstanceOrchestrator()
        result = orch.reload_instances()
        assert isinstance(result, dict)
        assert result is orch._instances

    def test_reload_invalidates_health_cache(self):
        """Stale hosts/keys mean stale health results must be dropped."""
        orch = InstanceOrchestrator()
        orch._health_cache["hermes2"] = (True, "fake-timestamp")
        assert "hermes2" in orch._health_cache
        orch.reload_instances()
        assert orch._health_cache == {}

    def test_module_level_reload_function_updates_symbol(self):
        """reload_hermes_instances() refreshes the module-level dict."""
        import gateway.instance_orchestrator as mod

        with patch.dict(os.environ, {"HERMES_HTTP_KEY": "module-reload-key"}, clear=False):
            os.environ.pop("HERMES2_HTTP_KEY", None)
            new_registry = reload_hermes_instances()
            assert mod.HERMES_INSTANCES["hermes2"].http_key == "module-reload-key"
            assert new_registry["hermes2"].http_key == "module-reload-key"


class TestHermesInstancesNotBakedAtImport:
    """DoD: HERMES_INSTANCES no longer built from hardcoded values at import.

    The old code hardcoded http_key='putty_key_here' as a placeholder at
    import time. The refactor must have moved that to a runtime env lookup.
    """

    def test_no_placeholder_key_bled_through(self):
        """The old 'putty_key_here' placeholder must not appear anywhere."""
        instances = load_instances_from_config()
        for inst in instances.values():
            assert inst.http_key != "putty_key_here", (
                f"Instance {inst.name} still uses import-time placeholder key. "
                "load_instances_from_config() must read the key from env at "
                "runtime, not carry a hardcoded placeholder."
            )

    def test_module_level_is_populated_by_loader(self):
        """The module-level HERMES_INSTANCES must be produced by the runtime
        loader, so it holds real env values (empty string if unset), never
        the old 'putty_key_here' constant."""
        assert HERMES_INSTANCES["hermes2"].http_key != "putty_key_here"
