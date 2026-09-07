"""Tests for EventBus factory wiring (P1 — DIP EventBus injection).

Verifies:
  1. create_event_bus() returns a usable bus instance
  2. NullEventBus implements the same interface as NATSEventBus
  3. ExecutiveAgentActor with event_bus publishes events
  4. ExecutiveAgentActor without event_bus falls back to logging
  5. The factory and NullEventBus are exported from framework_wrapper
"""

import pytest
from unittest.mock import MagicMock, patch
from agent.framework_wrapper import (
    create_event_bus,
    NullEventBus,
    NATSEventBus,
    ExecutiveAgentActor,
    KanbanWorkerExecutiveAgentActor,
)


class TestCreateEventBus:
    """Tests for the create_event_bus factory function."""

    def test_factory_returns_bus_instance(self):
        """Factory must return a bus instance (NATSEventBus or NullEventBus)."""
        bus = create_event_bus("nats://127.0.0.1:4222")
        assert bus is not None
        assert hasattr(bus, "publish")
        assert hasattr(bus, "publish_async")

    def test_factory_reads_env_url(self):
        """Factory reads NATS_URL from environment when url is None."""
        with patch.dict("os.environ", {"NATS_URL": "nats://127.0.0.1:4222"}):
            bus = create_event_bus()
            assert bus is not None

    def test_factory_default_url(self):
        """Factory uses default URL when env var not set."""
        with patch.dict("os.environ", {}, clear=False):
            import os
            os.environ.pop("NATS_URL", None)
            bus = create_event_bus()
            assert bus is not None


class TestNullEventBus:
    """Tests for the NullEventBus fallback."""

    def test_null_bus_publish_returns_synthetic_id(self):
        """NullEventBus.publish returns a string starting with 'null-'."""
        bus = NullEventBus()
        result = bus.publish("test.subject", b"payload")
        assert isinstance(result, str)
        assert result.startswith("null-")

    def test_null_bus_async_publish(self):
        """NullEventBus.publish_async returns same as publish."""
        import asyncio
        bus = NullEventBus()
        result = asyncio.run(bus.publish_async("test.subject", b"payload"))
        assert result.startswith("null-")

    def test_null_bus_matches_pattern(self):
        """NullEventBus.matches_pattern handles NATS wildcards."""
        bus = NullEventBus()
        assert bus.matches_pattern("exec.agent.helios.task_completed", "exec.agent.>")
        assert bus.matches_pattern("exec.agent.helios.task_completed", "exec.agent.*.task_completed")
        assert not bus.matches_pattern("exec.voice.task.create", "exec.agent.>")

    def test_null_bus_tracks_published(self):
        """NullEventBus tracks published messages for debugging."""
        bus = NullEventBus()
        bus.publish("subject1", b"data1")
        bus.publish("subject2", b"data2")
        assert len(bus._published) == 2


class TestActorEventBusWiring:
    """Tests for EventBus injection into actors."""

    def _make_test_actor(self, event_bus=None):
        """Create a minimal test actor subclass."""
        class TestActor(ExecutiveAgentActor):
            def deliberate(self, context=None):
                """Return test deliberation."""
                return "test deliberation"

            def execute(self, context=None):
                """Return success."""
                return 0

        return TestActor(agent_id="test_agent", event_bus=event_bus)

    def test_actor_with_mock_bus_emits_event(self):
        """Actor with injected bus publishes via bus.publish()."""
        mock_bus = MagicMock()
        mock_bus.publish.return_value = "seq-001"
        actor = self._make_test_actor(event_bus=mock_bus)

        result = actor.emit_event("task_completed", {"task_id": "T1"})

        assert result == 0
        mock_bus.publish.assert_called_once()
        # Verify subject pattern
        call_args = mock_bus.publish.call_args
        subject = call_args[0][0]
        assert "exec.agent.test_agent.task_completed" in subject

    def test_actor_without_bus_does_not_raise(self):
        """Actor without event_bus falls back to logging (no crash)."""
        actor = self._make_test_actor(event_bus=None)

        result = actor.emit_event("task_completed", {"task_id": "T2"})

        assert result == 0  # success even without bus

    def test_kanban_worker_accepts_event_bus(self):
        """KanbanWorkerExecutiveAgentActor accepts event_bus in constructor."""
        mock_bus = MagicMock()
        mock_bus.publish.return_value = "seq-002"
        worker = KanbanWorkerExecutiveAgentActor(
            task_id="T1",
            agent_id="test_worker",
            event_bus=mock_bus,
        )
        assert worker._event_bus is mock_bus

    def test_kanban_worker_without_bus(self):
        """KanbanWorkerActor works without event_bus (backward compat)."""
        worker = KanbanWorkerExecutiveAgentActor(
            task_id="T2",
            agent_id="test_worker2",
        )
        assert worker._event_bus is None


class TestFrameworkWrapperExports:
    """Tests that framework_wrapper exports the new symbols."""

    def test_create_event_bus_exported(self):
        """create_event_bus must be importable from framework_wrapper."""
        from agent.framework_wrapper import create_event_bus as ceb
        assert callable(ceb)

    def test_null_event_bus_exported(self):
        """NullEventBus must be importable from framework_wrapper."""
        from agent.framework_wrapper import NullEventBus as NEB
        assert isinstance(NEB, type)

    def test_exports_in_all(self):
        """create_event_bus and NullEventBus must be in __all__."""
        import agent.framework_wrapper as fw
        assert "create_event_bus" in fw.__all__
        assert "NullEventBus" in fw.__all__
