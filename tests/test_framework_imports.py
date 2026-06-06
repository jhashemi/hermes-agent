"""Wire-002: Test import paths for executive-agents-framework modules.

Validates that all key framework entry points are accessible from the
hermes-agent environment. This test confirms the wire-001 pyproject.toml
dependency promotion (executive-agents>=0.1.0,<1 as core dep) works
correctly and documents the actual import paths.

Run:  python -m pytest tests/test_framework_imports.py -v
"""
import pytest


# ---------------------------------------------------------------------------
# 1. LldapAdapter — real LDAP directory adapter via LLDAP server
# ---------------------------------------------------------------------------
class TestLldapAdapter:
    """Verify LldapAdapter is importable from the framework."""

    def test_import_from_full_path(self):
        """Import from the full module path (most specific)."""
        from executive_agents.infrastructure.adapters.directory.lldap_adapter import (
            LldapAdapter,
        )
        assert LldapAdapter is not None

    def test_class_is_type(self):
        """LldapAdapter should be a class."""
        from executive_agents.infrastructure.adapters.directory.lldap_adapter import (
            LldapAdapter,
        )
        assert isinstance(LldapAdapter, type)

    def test_implements_ldap_directory_port(self):
        """LldapAdapter should implement the LdapDirectoryPort protocol."""
        from executive_agents.infrastructure.adapters.directory.lldap_adapter import (
            LldapAdapter,
        )
        from executive_agents.ports.directory.ldap_directory_port import (
            LdapDirectoryPort,
        )
        assert issubclass(LldapAdapter, LdapDirectoryPort)


# ---------------------------------------------------------------------------
# 2. LDAPAgentLocator — discovers eligible agents via LLDAP directory
# ---------------------------------------------------------------------------
class TestLDAPAgentLocator:
    """Verify LDAPAgentLocator is importable from the framework."""

    def test_import_from_full_path(self):
        """Import from the full module path."""
        from executive_agents.infrastructure.adapters.directory.ldap_agent_locator import (
            LDAPAgentLocator,
        )
        assert LDAPAgentLocator is not None

    def test_class_is_type(self):
        """LDAPAgentLocator should be a class."""
        from executive_agents.infrastructure.adapters.directory.ldap_agent_locator import (
            LDAPAgentLocator,
        )
        assert isinstance(LDAPAgentLocator, type)


# ---------------------------------------------------------------------------
# 3. NATSEventBus — cross-service pub/sub backed by NATS JetStream
# ---------------------------------------------------------------------------
class TestNATSEventBus:
    """Verify NATSEventBus is importable from the framework."""

    def test_import_from_adapters_package(self):
        """Import from the adapters subpackage (re-exported in __init__)."""
        from executive_agents.infrastructure.adapters import NATSEventBus
        assert NATSEventBus is not None

    def test_import_from_full_path(self):
        """Import from the full module path."""
        from executive_agents.infrastructure.adapters.nats_event_bus import NATSEventBus
        assert NATSEventBus is not None

    def test_class_is_type(self):
        """NATSEventBus should be a class."""
        from executive_agents.infrastructure.adapters.nats_event_bus import NATSEventBus
        assert isinstance(NATSEventBus, type)


# ---------------------------------------------------------------------------
# 4. ExecutiveAgentActor — abstract base class for all executive agents
# ---------------------------------------------------------------------------
class TestExecutiveAgentActor:
    """Verify ExecutiveAgentActor is importable from the framework."""

    def test_import_from_agents_package(self):
        """Import from the agents subpackage (re-exported in __init__)."""
        from executive_agents.agents import ExecutiveAgentActor
        assert ExecutiveAgentActor is not None

    def test_import_from_full_path(self):
        """Import from the full module path."""
        from executive_agents.agents.kanban_worker_executive_agent_actor import (
            ExecutiveAgentActor,
        )
        assert ExecutiveAgentActor is not None

    def test_class_is_type(self):
        """ExecutiveAgentActor should be a class."""
        from executive_agents.agents import ExecutiveAgentActor
        assert isinstance(ExecutiveAgentActor, type)


# ---------------------------------------------------------------------------
# 5. AgentContainer — DI container (note: task says "Container", actual name is "AgentContainer")
# ---------------------------------------------------------------------------
class TestAgentContainer:
    """Verify AgentContainer is importable from the framework.

    NOTE: The task specification refers to "Container" but the actual class
    name in the framework is "AgentContainer". The composition/__init__.py
    re-exports it as "AgentContainer" and also provides a "build()" factory.
    There is no bare "Container" class in the framework — "Container" in the
    task spec is an alias/shortcut for AgentContainer.
    """

    def test_import_from_composition_package(self):
        """Import from the composition subpackage (re-exported in __init__)."""
        from executive_agents.composition import AgentContainer
        assert AgentContainer is not None

    def test_import_from_full_path(self):
        """Import from the full module path."""
        from executive_agents.composition.container import AgentContainer
        assert AgentContainer is not None

    def test_class_is_type(self):
        """AgentContainer should be a class (dataclass-based)."""
        from executive_agents.composition.container import AgentContainer
        assert isinstance(AgentContainer, type)

    def test_build_function_importable(self):
        """The build() factory function should also be importable."""
        from executive_agents.composition import build
        assert callable(build)


# ---------------------------------------------------------------------------
# 6. Additional framework entry points worth verifying
# ---------------------------------------------------------------------------
class TestAdditionalFrameworkImports:
    """Sanity-check other important framework entry points."""

    def test_okr_domain_objects(self):
        """OKR domain objects are re-exported from the top-level package."""
        from executive_agents import OKR, Goal, KeyResult
        assert OKR is not None
        assert Goal is not None
        assert KeyResult is not None

    def test_kanban_worker_actor_importable(self):
        """KanbanWorkerActor extends ExecutiveAgentActor — verify it imports."""
        from executive_agents.agents import KanbanWorkerActor
        assert KanbanWorkerActor is not None

    def test_event_classes_importable(self):
        """CQRS event types should be importable."""
        from executive_agents.agents import TaskCompletedEvent, TaskBlockedEvent
        assert TaskCompletedEvent is not None
        assert TaskBlockedEvent is not None

    def test_framework_version(self):
        """Framework should report version 0.1.0."""
        import executive_agents
        assert executive_agents.__version__ == "0.1.0"


# ---------------------------------------------------------------------------
# Import path mapping — documents the canonical paths for wire-003
# ---------------------------------------------------------------------------
IMPORT_PATH_MAP = {
    "LldapAdapter": "executive_agents.infrastructure.adapters.directory.lldap_adapter:LldapAdapter",
    "LDAPAgentLocator": "executive_agents.infrastructure.adapters.directory.ldap_agent_locator:LDAPAgentLocator",
    "NATSEventBus": "executive_agents.infrastructure.adapters:NATSEventBus",
    "ExecutiveAgentActor": "executive_agents.agents:ExecutiveAgentActor",
    "AgentContainer": "executive_agents.composition:AgentContainer",
}
