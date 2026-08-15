"""Unified import interface for executive-agents-framework components.

This module consolidates all critical framework imports to one place,
eliminating duplicate imports across the codebase and simplifying
future refactoring.

On import, adds the framework src directory to sys.path so that
executive_agents is importable by downstream code.

Exports:
    LldapAdapter: LDAP directory adapter for agent discovery.
    LDAPAgentLocator: Agent locator using LDAP directory.
    NATSEventBus: NATS-based event bus.
    ExecutiveAgentActor: Base executive agent actor class.
    Container: Alias for AgentContainer (DI container).
    KanbanWorkerExecutiveAgentActor: Kanban worker actor class.
"""
from __future__ import annotations

import logging
import os
import sys

logger = logging.getLogger(__name__)

# ---- Add framework src to sys.path ─────────────────────────────────────────
_FRAMEWORK_SRC = os.environ.get(
    "EXECUTIVE_AGENTS_FRAMEWORK_SRC",
    "/home/ubuntu/executive_agents_framework/src",
)
if os.path.isdir(_FRAMEWORK_SRC) and _FRAMEWORK_SRC not in sys.path:
    sys.path.insert(0, _FRAMEWORK_SRC)

__all__ = [
    "LldapAdapter",
    "LDAPAgentLocator",
    "NATSEventBus",
    "ExecutiveAgentActor",
    "Container",
    "KanbanWorkerExecutiveAgentActor",
]

# ---- Lazy imports ──────────────────────────────────────────────────────────
def _try_import(module_path: str, name: str):
    """Import a name from a module, returning None if not available."""
    try:
        import importlib
        mod = importlib.import_module(module_path)
        return getattr(mod, name)
    except (ImportError, AttributeError) as e:
        logger.debug("framework_wrapper: could not import %s from %s: %s", name, module_path, e)
        return None


LldapAdapter = _try_import(
    "executive_agents.infrastructure.adapters.directory.lldap_adapter",
    "LldapAdapter",
)
LDAPAgentLocator = _try_import(
    "executive_agents.infrastructure.adapters.directory.ldap_agent_locator",
    "LDAPAgentLocator",
)
NATSEventBus = _try_import(
    "executive_agents.infrastructure.adapters.nats_event_bus",
    "NATSEventBus",
)
Container = _try_import(
    "executive_agents.composition.container",
    "AgentContainer",
)

# ExecutiveAgentActor and KanbanWorkerActor live in the same module
_executive_agent_actor = _try_import(
    "executive_agents.agents.kanban_worker_executive_agent_actor",
    "ExecutiveAgentActor",
)
_kanban_worker_actor = _try_import(
    "executive_agents.agents.kanban_worker_executive_agent_actor",
    "KanbanWorkerActor",
)

# The wrapper exports KanbanWorkerActor under the name KanbanWorkerExecutiveAgentActor
# (the test verifies they are the same object).
ExecutiveAgentActor = _executive_agent_actor
KanbanWorkerExecutiveAgentActor = _kanban_worker_actor