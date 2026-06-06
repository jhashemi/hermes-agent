"""Unit tests for NATS Event Bus wiring (wire-016).

Tests verify:
- NATSEventBusConfig resolves servers from config/env/fallback
- FrameworkWrapper instantiates NATSEventBus + NATSConnectionPool
- Offline mode fallback when NATS server unavailable
- EventBus protocol compliance (publish/subscribe/checkpoint)
- Health check returns correct status in offline mode
- load_nats_event_bus() factory returns working bus
- load_nats_event_bus_or_offline() returns OfflineEventBus when framework unavailable
- load_nats_connection_pool() factory returns pool
- Integration: hermes-agent startup includes NATSEventBus initialization
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

# Ensure workspace is on sys.path for imports
WORKSPACE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(WORKSPACE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

# Ensure framework is importable
FRAMEWORK_SRC = os.environ.get(
    "EXECUTIVE_AGENTS_FRAMEWORK_SRC",
    "/home/ubuntu/executive_agents_framework/src",
)
if FRAMEWORK_SRC not in sys.path:
    sys.path.insert(0, FRAMEWORK_SRC)


class TestNATSEventBusConfig(unittest.TestCase):
    """Test NATSEventBusConfig dataclass and server resolution."""

    def test_default_config(self):
        from agent.framework_wrapper import NATSEventBusConfig
        config = NATSEventBusConfig()
        self.assertEqual(config.servers, ())
        self.assertEqual(config.client_name, "hermes-eventbus")
        self.assertEqual(config.rate_limit_per_sec, 0.0)
        self.assertTrue(config.auto_connect)

    def test_explicit_servers(self):
        from agent.framework_wrapper import NATSEventBusConfig
        config = NATSEventBusConfig(
            servers=("nats://10.0.1.50:4222", "nats://10.0.1.51:4222"),
        )
        resolved = config.get_servers()
        self.assertEqual(resolved, ["nats://10.0.1.50:4222", "nats://10.0.1.51:4222"])

    def test_servers_from_nats_servers_env(self):
        from agent.framework_wrapper import NATSEventBusConfig
        config = NATSEventBusConfig()
        with patch.dict(os.environ, {"NATS_SERVERS": "nats://a:4222, nats://b:4222"}):
            resolved = config.get_servers()
            self.assertEqual(resolved, ["nats://a:4222", "nats://b:4222"])

    def test_servers_from_numbered_env(self):
        from agent.framework_wrapper import NATSEventBusConfig
        config = NATSEventBusConfig()
        env = os.environ.copy()
        env.pop("NATS_SERVERS", None)
        env["NATS_SERVER_1"] = "nats://x:4222"
        env["NATS_SERVER_2"] = "nats://y:4222"
        with patch.dict(os.environ, env, clear=True):
            resolved = config.get_servers()
            self.assertIn("nats://x:4222", resolved)

    def test_fallback_to_localhost(self):
        from agent.framework_wrapper import NATSEventBusConfig
        config = NATSEventBusConfig()
        env = {k: v for k, v in os.environ.items() if not k.startswith("NATS")}
        with patch.dict(os.environ, env, clear=True):
            resolved = config.get_servers()
            self.assertEqual(resolved, ["nats://localhost:4222"])

    def test_primary_secondary_env(self):
        from agent.framework_wrapper import NATSEventBusConfig
        config = NATSEventBusConfig()
        env = {
            "NATS_PRIMARY_SERVER": "nats://primary:4222",
            "NATS_SECONDARY_SERVER": "nats://secondary:4222",
        }
        full_env = os.environ.copy()
        full_env.pop("NATS_SERVERS", None)
        for i in range(1, 10):
            full_env.pop(f"NATS_SERVER_{i}", None)
        full_env.update(env)
        with patch.dict(os.environ, full_env, clear=True):
            resolved = config.get_servers()
            self.assertEqual(resolved[0], "nats://primary:4222")

    def test_frozen_dataclass(self):
        from agent.framework_wrapper import NATSEventBusConfig
        config = NATSEventBusConfig()
        with self.assertRaises(AttributeError):
            config.client_name = "changed"

    def test_custom_config(self):
        from agent.framework_wrapper import NATSEventBusConfig
        config = NATSEventBusConfig(
            servers=("nats://prod:4222",),
            client_name="custom-bus",
            rate_limit_per_sec=50.0,
            auto_connect=False,
            max_reconnect_attempts=5,
            offline_buffer_size=500,
        )
        self.assertEqual(config.servers, ("nats://prod:4222",))
        self.assertEqual(config.client_name, "custom-bus")
        self.assertEqual(config.rate_limit_per_sec, 50.0)
        self.assertFalse(config.auto_connect)
        self.assertEqual(config.max_reconnect_attempts, 5)
        self.assertEqual(config.offline_buffer_size, 500)


class TestFrameworkWrapperNATS(unittest.TestCase):
    """Test FrameworkWrapper NATS wiring."""

    def test_wrapper_creates_nats_event_bus(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        self.assertIsNotNone(wrapper.nats_event_bus)
        self.assertTrue(wrapper.nats_event_bus.is_degraded)

    def test_wrapper_creates_nats_connection_pool(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        self.assertIsNotNone(wrapper.nats_connection_pool)

    def test_wrapper_nats_health(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        health = wrapper.nats_health()
        self.assertIn("connected", health)
        self.assertIn("degraded", health)
        self.assertIn("mode", health)
        self.assertTrue(health["degraded"])

    def test_wrapper_nats_pool_stats(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        stats = wrapper.nats_pool_stats()
        self.assertIn("connected", stats)
        self.assertIn("healthy", stats)
        self.assertIn("offline_mode", stats)

    def test_can_publish_nats(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        self.assertTrue(wrapper.can_publish_nats())

    def test_wrapper_nats_connected_property(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        self.assertFalse(wrapper.nats_connected)

    def test_wrapper_with_custom_servers(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(
            servers=("nats://custom:4222",),
            client_name="test-wrapper",
            auto_connect=False,
        )
        wrapper = FrameworkWrapper(nats_config=config)
        self.assertIsNotNone(wrapper.nats_event_bus)
        self.assertEqual(wrapper.nats_event_bus._servers, ["nats://custom:4222"])

    def test_wrapper_both_ldap_and_nats(self):
        from agent.framework_wrapper import (
            FrameworkWrapper, LDAPAgentLocatorConfig, NATSEventBusConfig,
        )
        ldap_config = LDAPAgentLocatorConfig()
        nats_config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(ldap_config=ldap_config, nats_config=nats_config)
        self.assertIsNotNone(wrapper.nats_event_bus)
        self.assertTrue(wrapper.can_publish_nats())
        self.assertIsNotNone(wrapper.ldap_config)
        self.assertIsNotNone(wrapper.nats_config)


class TestNATSEventBusPublish(unittest.TestCase):
    """Test NATSEventBus publish in offline mode."""

    def test_publish_offline(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        msg_id = bus.publish("exec.task.dispatched", b'{"task_id": "T1"}')
        self.assertIsNotNone(msg_id)
        self.assertTrue(len(msg_id) > 0)

    def test_publish_multiple(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        for i in range(5):
            bus.publish(f"exec.task.event_{i}", b'payload')
        health = bus.health()
        self.assertEqual(health["published_count"], 5)

    def test_subscribe(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        sub = bus.subscribe("test-subscriber", "exec.task.>")
        self.assertEqual(sub["subscriber_id"], "test-subscriber")
        self.assertEqual(sub["pattern"], "exec.task.>")
        self.assertTrue(sub["active"])

    def test_checkpoint_noop(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        bus.checkpoint("exec.task.dispatched")  # Should not raise

    def test_subject_for(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        subject = bus.subject_for("task", "dispatched")
        self.assertEqual(subject, "exec.task.dispatched")

    def test_matches_pattern(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        self.assertTrue(bus.matches_pattern("exec.task.dispatched", "exec.>"))
        self.assertTrue(bus.matches_pattern("exec.task.dispatched", "exec.task.>"))
        self.assertTrue(bus.matches_pattern("exec.task.dispatched", "exec.task.*"))
        self.assertFalse(bus.matches_pattern("exec.task.dispatched", "exec.*"))
        self.assertTrue(bus.matches_pattern("exec.task.dispatched", "exec.task.dispatched"))


class TestNATSEventBusHealthCheck(unittest.TestCase):
    """Test NATSEventBus health check endpoint."""

    def test_health_offline_mode(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        health = wrapper.nats_health()
        self.assertFalse(health["connected"])
        self.assertTrue(health["degraded"])
        self.assertEqual(health["mode"], "offline")
        self.assertEqual(health["published_count"], 0)

    def test_health_after_publish(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        bus.publish("exec.agent.booted", b'{"agent_id": "test"}')
        health = wrapper.nats_health()
        self.assertEqual(health["published_count"], 1)

    def test_pool_stats_offline(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        stats = wrapper.nats_pool_stats()
        self.assertFalse(stats["connected"])
        self.assertTrue(stats["offline_mode"])


class TestNATSFactories(unittest.TestCase):
    """Test NATS convenience factory functions."""

    def test_load_nats_event_bus(self):
        from agent.framework_wrapper import load_nats_event_bus, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        bus = load_nats_event_bus(config)
        self.assertIsNotNone(bus)

    def test_load_nats_event_bus_or_offline_with_framework(self):
        from agent.framework_wrapper import load_nats_event_bus_or_offline, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        bus = load_nats_event_bus_or_offline(config)
        self.assertIsNotNone(bus)
        self.assertEqual(type(bus).__name__, "NATSEventBus")

    def test_load_nats_connection_pool(self):
        from agent.framework_wrapper import load_nats_connection_pool, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        pool = load_nats_connection_pool(config)
        self.assertIsNotNone(pool)

    def test_load_nats_event_bus_default_config(self):
        from agent.framework_wrapper import load_nats_event_bus, NATSEventBusConfig
        bus = load_nats_event_bus(NATSEventBusConfig(auto_connect=False))
        self.assertIsNotNone(bus)


class TestOfflineEventBus(unittest.TestCase):
    """Test OfflineEventBus fallback."""

    def test_offline_bus_creation(self):
        from agent.framework_wrapper import OfflineEventBus
        bus = OfflineEventBus()
        self.assertIsNotNone(bus)
        self.assertTrue(bus.is_degraded)

    def test_offline_bus_publish(self):
        from agent.framework_wrapper import OfflineEventBus
        bus = OfflineEventBus()
        msg_id = bus.publish("exec.test.event", b'payload')
        self.assertIsNotNone(msg_id)
        self.assertTrue(len(msg_id) > 0)

    def test_offline_bus_subscribe(self):
        from agent.framework_wrapper import OfflineEventBus
        bus = OfflineEventBus()
        sub = bus.subscribe("test-sub", "exec.>")
        self.assertEqual(sub["subscriber_id"], "test-sub")
        self.assertTrue(sub["active"])

    def test_offline_bus_checkpoint(self):
        from agent.framework_wrapper import OfflineEventBus
        bus = OfflineEventBus()
        bus.checkpoint("exec.test")  # Should not raise

    def test_offline_bus_health(self):
        from agent.framework_wrapper import OfflineEventBus
        bus = OfflineEventBus()
        health = bus.health()
        self.assertFalse(health["connected"])
        self.assertTrue(health["degraded"])
        self.assertEqual(health["mode"], "offline_fallback")

    def test_offline_bus_subject_for(self):
        from agent.framework_wrapper import OfflineEventBus
        bus = OfflineEventBus()
        subject = bus.subject_for("task", "dispatched")
        self.assertEqual(subject, "exec.task.dispatched")


class TestNATSEventBusProtocolCompliance(unittest.TestCase):
    """Test that wired NATSEventBus implements EventBus protocol."""

    def test_implements_event_bus_protocol(self):
        try:
            from executive_agents.ports.storage.event_bus import EventBus
        except ImportError:
            self.skipTest("EventBus port not importable")
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        self.assertIsInstance(bus, EventBus)

    def test_offline_bus_protocol_compatible(self):
        from agent.framework_wrapper import OfflineEventBus
        bus = OfflineEventBus()
        self.assertTrue(hasattr(bus, "publish"))
        self.assertTrue(hasattr(bus, "subscribe"))
        self.assertTrue(hasattr(bus, "checkpoint"))
        self.assertTrue(callable(bus.publish))
        self.assertTrue(callable(bus.subscribe))
        self.assertTrue(callable(bus.checkpoint))


class TestNATSRateLimiting(unittest.TestCase):
    """Test NATSEventBus rate limiting in offline mode."""

    def test_rate_limiting_zero_means_unlimited(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(rate_limit_per_sec=0.0, auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        for _ in range(100):
            msg_id = bus.publish("exec.test", b'payload')
            self.assertFalse(msg_id.startswith("dropped-"))

    def test_rate_limiting_active(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(rate_limit_per_sec=5.0, auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        dropped = 0
        for _ in range(20):
            msg_id = bus.publish("exec.test", b'payload')
            if msg_id.startswith("dropped-"):
                dropped += 1
        self.assertGreater(dropped, 0)


class TestNATSEventBusConsumerGroups(unittest.TestCase):
    """Test NATSEventBus consumer group management."""

    def test_create_consumer_group(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        group = bus.create_consumer_group("test-group", "EXEC_STREAM", filter_subjects=["exec.task.>"])
        self.assertEqual(group["group_id"], "test-group")
        self.assertEqual(group["stream"], "EXEC_STREAM")

    def test_list_consumer_groups(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        bus.create_consumer_group("g1", "S1")
        bus.create_consumer_group("g2", "S2")
        groups = bus.list_consumer_groups()
        self.assertEqual(len(groups), 2)

    def test_delete_consumer_group(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        bus.create_consumer_group("g1", "S1")
        deleted = bus.delete_consumer_group("g1")
        self.assertTrue(deleted)
        self.assertEqual(len(bus.list_consumer_groups()), 0)


class TestNATSEventBusTenantScoping(unittest.TestCase):
    """Test NATSEventBus tenant-scoped subjects."""

    def test_tenant_subject(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        subject = bus.tenant_subject("tenantA", "task", "dispatched")
        self.assertEqual(subject, "exec.tenantA.task.dispatched")

    def test_tenant_pattern(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        pattern = bus.tenant_pattern("tenantA")
        self.assertEqual(pattern, "exec.tenantA.>")


class TestNATSDeadLetterQueue(unittest.TestCase):
    """Test NATSEventBus dead letter queue and retry."""

    def test_publish_with_retry_offline(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        msg_id = bus.publish_with_retry("exec.test", b'payload', max_retries=3)
        self.assertIsNotNone(msg_id)
        self.assertFalse(msg_id.startswith("dlq-"))

    def test_dead_letter_queue(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        bus = wrapper.nats_event_bus
        dlq = bus.get_dead_letter_queue()
        self.assertIsInstance(dlq, list)


class TestNATSConnectionPoolProperties(unittest.TestCase):
    """Test NATSConnectionPool from wrapper."""

    def test_pool_offline_mode(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        pool = wrapper.nats_connection_pool
        self.assertTrue(pool.offline_mode)
        self.assertFalse(pool.is_connected)

    def test_pool_stats_keys(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        pool = wrapper.nats_connection_pool
        stats = pool.get_stats()
        self.assertIn("connected", stats)
        self.assertIn("healthy", stats)
        self.assertIn("offline_mode", stats)
        self.assertIn("offline_buffer_size", stats)
        self.assertIn("offline_buffer_capacity", stats)


class TestIntegrationStartup(unittest.TestCase):
    """Integration tests for NATSEventBus startup initialization."""

    def test_startup_initializes_nats_event_bus(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=False)
        wrapper = FrameworkWrapper(nats_config=config)
        self.assertIsNotNone(wrapper.nats_event_bus)
        self.assertIsNotNone(wrapper.nats_connection_pool)
        self.assertTrue(wrapper.can_publish_nats())
        health = wrapper.nats_health()
        self.assertIn("connected", health)
        self.assertIn("mode", health)

    def test_startup_no_connection_failures(self):
        from agent.framework_wrapper import FrameworkWrapper, NATSEventBusConfig
        config = NATSEventBusConfig(auto_connect=True)
        try:
            wrapper = FrameworkWrapper(nats_config=config)
            self.assertIsNotNone(wrapper.nats_event_bus)
        except Exception as e:
            self.fail(f"FrameworkWrapper raised unexpected exception: {e}")

    def test_startup_with_config_yaml_values(self):
        from agent.framework_wrapper import NATSEventBusConfig
        config = NATSEventBusConfig(
            servers=("nats://localhost:4222",),
            client_name="hermes-eventbus",
            rate_limit_per_sec=0,
            auto_connect=True,
            connection_timeout=10,
            max_reconnect_attempts=10,
            reconnect_wait_base=1.0,
            reconnect_wait_max=30.0,
            offline_buffer_size=1000,
        )
        self.assertEqual(config.client_name, "hermes-eventbus")
        self.assertEqual(config.connection_timeout, 10)
        self.assertEqual(config.max_reconnect_attempts, 10)

    def test_import_from_hermes_agent_package(self):
        from agent import (
            NATSEventBusConfig,
            load_nats_event_bus,
            load_nats_event_bus_or_offline,
            load_nats_connection_pool,
            OfflineEventBus,
        )
        self.assertTrue(callable(load_nats_event_bus))
        self.assertTrue(callable(load_nats_event_bus_or_offline))
        self.assertTrue(callable(load_nats_connection_pool))
        self.assertIsNotNone(OfflineEventBus)

    def test_event_types_importable(self):
        try:
            from executive_agents.infrastructure.nervous_system import (
                AGENT_BOOTED,
                TASK_DISPATCHED,
                TASK_COMPLETED,
                GOAL_COMPLETED,
                ERROR_DETECTED,
                ALL_EVENT_TYPES,
            )
            self.assertEqual(AGENT_BOOTED, "agent.booted")
            self.assertEqual(TASK_DISPATCHED, "task.dispatched")
            self.assertEqual(GOAL_COMPLETED, "goal.completed")
        except ImportError:
            self.skipTest("Nervous system event types not importable")


if __name__ == "__main__":
    unittest.main()
