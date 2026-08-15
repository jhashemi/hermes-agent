"""wire-004: Verify all framework modules accessible from hermes-agent.

Tests that all critical framework components are importable via the
hermes_agent.framework_wrapper re-export surface. Validates the full
import chain (hermes_agent -> framework_wrapper -> executive_agents.*)
before wire-005 removes duplicate copies.

Run:
    python -m pytest tests/test_framework_imports.py -v
"""
from __future__ import annotations

import importlib
import inspect
import sys
import types

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _framework_src_on_path() -> bool:
    """Return True if the framework src dir is resolvable."""
    import os
    framework_src = os.environ.get(
        "EXECUTIVE_AGENTS_FRAMEWORK_SRC",
        "/home/ubuntu/executive_agents_framework/src",
    )
    return framework_src in sys.path or any(
        p.startswith("/home/ubuntu/executive_agents_framework") for p in sys.path
    )


# ---------------------------------------------------------------------------
# 0. Wrapper module itself
# ---------------------------------------------------------------------------

class TestFrameworkWrapperModule:
    """The wrapper module must be importable and set up sys.path correctly."""

    def test_wrapper_importable(self):
        """hermes_agent.framework_wrapper must be importable."""
        mod = importlib.import_module("hermes_agent.framework_wrapper")
        assert mod is not None

    def test_wrapper_adds_framework_src_to_sys_path(self):
        """Importing the wrapper must add the framework src directory to sys.path."""
        importlib.import_module("hermes_agent.framework_wrapper")
        assert _framework_src_on_path(), (
            "Expected executive_agents_framework/src to be on sys.path after importing wrapper"
        )

    def test_wrapper_all_exports_defined(self):
        """__all__ must list all 6 expected exports."""
        from hermes_agent import framework_wrapper
        expected = {
            "LldapAdapter",
            "LDAPAgentLocator",
            "NATSEventBus",
            "ExecutiveAgentActor",
            "Container",
            "KanbanWorkerExecutiveAgentActor",
        }
        assert expected == set(framework_wrapper.__all__)


# ---------------------------------------------------------------------------
# 1. LldapAdapter — via wrapper re-export
# ---------------------------------------------------------------------------

class TestLldapAdapterViaWrapper:
    """Verify LldapAdapter is accessible via hermes_agent.framework_wrapper."""

    def test_import_via_wrapper(self):
        """Import LldapAdapter through the wrapper."""
        from hermes_agent.framework_wrapper import LldapAdapter
        assert LldapAdapter is not None

    def test_is_class(self):
        from hermes_agent.framework_wrapper import LldapAdapter
        assert isinstance(LldapAdapter, type)

    def test_same_object_as_direct_import(self):
        """Wrapper re-export must be the identical class object as the direct import."""
        from hermes_agent.framework_wrapper import LldapAdapter as via_wrapper
        from executive_agents.infrastructure.adapters.directory.lldap_adapter import (
            LldapAdapter as direct,
        )
        assert via_wrapper is direct

    def test_init_signature_has_config_param(self):
        """LldapAdapter.__init__ should accept a 'config' parameter."""
        from hermes_agent.framework_wrapper import LldapAdapter
        sig = inspect.signature(LldapAdapter.__init__)
        assert "config" in sig.parameters

    def test_implements_ldap_directory_port(self):
        """LldapAdapter must implement LdapDirectoryPort."""
        from hermes_agent.framework_wrapper import LldapAdapter
        from executive_agents.ports.directory.ldap_directory_port import LdapDirectoryPort
        assert issubclass(LldapAdapter, LdapDirectoryPort)


# ---------------------------------------------------------------------------
# 2. LDAPAgentLocator — via wrapper re-export
# ---------------------------------------------------------------------------

class TestLDAPAgentLocatorViaWrapper:
    """Verify LDAPAgentLocator is accessible via hermes_agent.framework_wrapper."""

    def test_import_via_wrapper(self):
        from hermes_agent.framework_wrapper import LDAPAgentLocator
        assert LDAPAgentLocator is not None

    def test_is_class(self):
        from hermes_agent.framework_wrapper import LDAPAgentLocator
        assert isinstance(LDAPAgentLocator, type)

    def test_same_object_as_direct_import(self):
        from hermes_agent.framework_wrapper import LDAPAgentLocator as via_wrapper
        from executive_agents.infrastructure.adapters.directory.ldap_agent_locator import (
            LDAPAgentLocator as direct,
        )
        assert via_wrapper is direct


# ---------------------------------------------------------------------------
# 3. NATSEventBus — via wrapper re-export
# ---------------------------------------------------------------------------

class TestNATSEventBusViaWrapper:
    """Verify NATSEventBus is accessible via hermes_agent.framework_wrapper."""

    def test_import_via_wrapper(self):
        from hermes_agent.framework_wrapper import NATSEventBus
        assert NATSEventBus is not None

    def test_is_class(self):
        from hermes_agent.framework_wrapper import NATSEventBus
        assert isinstance(NATSEventBus, type)

    def test_same_object_as_direct_import(self):
        from hermes_agent.framework_wrapper import NATSEventBus as via_wrapper
        from executive_agents.infrastructure.adapters.nats_event_bus import (
            NATSEventBus as direct,
        )
        assert via_wrapper is direct


# ---------------------------------------------------------------------------
# 4. ExecutiveAgentActor — via wrapper re-export
# ---------------------------------------------------------------------------

class TestExecutiveAgentActorViaWrapper:
    """Verify ExecutiveAgentActor is accessible via hermes_agent.framework_wrapper."""

    def test_import_via_wrapper(self):
        from hermes_agent.framework_wrapper import ExecutiveAgentActor
        assert ExecutiveAgentActor is not None

    def test_is_class(self):
        from hermes_agent.framework_wrapper import ExecutiveAgentActor
        assert isinstance(ExecutiveAgentActor, type)

    def test_same_object_as_direct_import(self):
        from hermes_agent.framework_wrapper import ExecutiveAgentActor as via_wrapper
        from executive_agents.agents.kanban_worker_executive_agent_actor import (
            ExecutiveAgentActor as direct,
        )
        assert via_wrapper is direct


# ---------------------------------------------------------------------------
# 5. Container (AgentContainer alias) — via wrapper re-export
# ---------------------------------------------------------------------------

class TestContainerViaWrapper:
    """Verify Container is accessible via hermes_agent.framework_wrapper."""

    def test_import_via_wrapper(self):
        from hermes_agent.framework_wrapper import Container
        assert Container is not None

    def test_is_class(self):
        from hermes_agent.framework_wrapper import Container
        assert isinstance(Container, type)

    def test_aliases_agent_container(self):
        """Container must be the AgentContainer class from the framework."""
        from hermes_agent.framework_wrapper import Container
        from executive_agents.composition.container import AgentContainer
        assert Container is AgentContainer


# ---------------------------------------------------------------------------
# 6. KanbanWorkerExecutiveAgentActor (KanbanWorkerActor alias) — via wrapper
# ---------------------------------------------------------------------------

class TestKanbanWorkerActorViaWrapper:
    """Verify KanbanWorkerExecutiveAgentActor is accessible via wrapper."""

    def test_import_via_wrapper(self):
        from hermes_agent.framework_wrapper import KanbanWorkerExecutiveAgentActor
        assert KanbanWorkerExecutiveAgentActor is not None

    def test_is_class(self):
        from hermes_agent.framework_wrapper import KanbanWorkerExecutiveAgentActor
        assert isinstance(KanbanWorkerExecutiveAgentActor, type)

    def test_same_object_as_kanban_worker_actor(self):
        from hermes_agent.framework_wrapper import KanbanWorkerExecutiveAgentActor
        from executive_agents.agents.kanban_worker_executive_agent_actor import (
            KanbanWorkerActor,
        )
        assert KanbanWorkerExecutiveAgentActor is KanbanWorkerActor

    def test_subclass_of_executive_agent_actor(self):
        """KanbanWorkerExecutiveAgentActor must extend ExecutiveAgentActor."""
        from hermes_agent.framework_wrapper import (
            KanbanWorkerExecutiveAgentActor,
            ExecutiveAgentActor,
        )
        assert issubclass(KanbanWorkerExecutiveAgentActor, ExecutiveAgentActor)


# ---------------------------------------------------------------------------
# 7. Version / dependency compatibility
# ---------------------------------------------------------------------------

class TestVersionCompatibility:
    """Verify framework version and no dependency conflicts."""

    def test_framework_version_is_0_1_0(self):
        """Framework must report version 0.1.0 (matches requirements)."""
        import executive_agents
        assert executive_agents.__version__ == "0.1.0"

    def test_no_import_errors_on_wildcard(self):
        """Wildcard import of the wrapper must not raise."""
        import importlib
        wrapper = importlib.import_module("hermes_agent.framework_wrapper")
        for name in wrapper.__all__:
            assert hasattr(wrapper, name), f"__all__ member {name!r} missing from module"

    def test_executive_agents_importable_after_wrapper(self):
        """After importing wrapper, the executive_agents package is on sys.path."""
        import hermes_agent.framework_wrapper  # noqa: F401 (side-effect: path setup)
        import executive_agents  # must not ImportError
        assert executive_agents is not None


# ---------------------------------------------------------------------------
# 8. Integration test — hermes_agent imports LldapAdapter and instantiates
# ---------------------------------------------------------------------------

class TestIntegration:
    """Integration: hermes_agent consumes LldapAdapter via wrapper."""

    def test_import_lldap_adapter_from_wrapper(self):
        """hermes_agent code can import LldapAdapter from the wrapper surface."""
        # Simulate what hermes_agent bootstrap code does
        from hermes_agent.framework_wrapper import LldapAdapter
        assert LldapAdapter is not None

    def test_lldap_adapter_has_expected_methods(self):
        """LldapAdapter exposes the directory-query API needed by hermes_agent."""
        from hermes_agent.framework_wrapper import LldapAdapter
        # These are the port methods expected by the hermes-agent bootstrap
        from executive_agents.ports.directory.ldap_directory_port import LdapDirectoryPort
        port_methods = [
            m for m in dir(LdapDirectoryPort)
            if not m.startswith("_")
        ]
        for method in port_methods:
            assert hasattr(LldapAdapter, method), (
                f"LldapAdapter is missing port method {method!r}"
            )

    def test_dedicated_subject_writer_importable(self):
        """DedicatedSubjectWriter companion class must also be accessible."""
        from executive_agents.infrastructure.adapters.nats_event_bus import (
            DedicatedSubjectWriter,
        )
        assert DedicatedSubjectWriter is not None

    def test_all_wrapper_exports_are_types(self):
        """Every name in __all__ must be a class (type), not a module or function."""
        from hermes_agent import framework_wrapper
        for name in framework_wrapper.__all__:
            obj = getattr(framework_wrapper, name)
            assert isinstance(obj, type), (
                f"Expected {name!r} to be a class, got {type(obj)!r}"
            )
