"""Agent internals -- extracted modules from run_agent.py.

These modules contain pure utility functions and self-contained classes
that were previously embedded in the 3,600-line run_agent.py. Extracting
them makes run_agent.py focused on the AIAgent orchestrator class.

Wire-012 (LDAP): Agent discovery via LLDAP directory
Wire-016 (NATS): Event bus via NATS JetStream
"""
from agent.framework_wrapper import (
    # LDAP (wire-012)
    LDAPAgentLocatorConfig,
    LldapConfig,  # backward compat
    load_ldap_agent_locator,
    load_ldap_agent_locator_or_fixture,
    get_lldap_adapter,  # backward compat
    get_directory_port_type,  # backward compat
    # NATS (wire-016)
    NATSEventBusConfig,
    OfflineEventBus,
    load_nats_event_bus,
    load_nats_event_bus_or_offline,
    load_nats_connection_pool,
)

__all__ = [
    # LDAP
    "LDAPAgentLocatorConfig",
    "LldapConfig",
    "load_ldap_agent_locator",
    "load_ldap_agent_locator_or_fixture",
    "get_lldap_adapter",
    "get_directory_port_type",
    # NATS
    "NATSEventBusConfig",
    "OfflineEventBus",
    "load_nats_event_bus",
    "load_nats_event_bus_or_offline",
    "load_nats_connection_pool",
]
