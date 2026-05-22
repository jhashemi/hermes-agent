"""Unit tests for VCG dispatcher — health monitoring, task dispatch, resource allocation."""

import pytest
import time
import threading
from gateway.vcg_dispatcher import (
    VCGDispatcher, VCGAgent, VCGNode, TaskAllocationResult, HealthState
)
from gateway.vcg_gateway import VCGGateway, get_vcg_gateway, initialize_vcg_gateway


class TestVCGNode:
    """Tests for VCGNode health state and capacity tracking."""

    def test_node_initialization(self):
        """Node initializes with correct defaults."""
        node = VCGNode(agent_id="hermes-local")
        assert node.agent_id == "hermes-local"
        assert node.health_state == HealthState.UNKNOWN
        assert node.health_score == 1.0
        assert node.capacity_available == 5
        assert node.utilization == 0.0

    def test_node_utilization_calculation(self):
        """Utilization is correctly calculated."""
        node = VCGNode(agent_id="test", capacity_max=10, capacity_available=7)
        assert node.utilization == 0.3  # (10-7)/10

    def test_node_is_ready(self):
        """Node readiness checks health and capacity."""
        node = VCGNode(
            agent_id="test",
            health_state=HealthState.HEALTHY,
            health_score=0.9,
            capacity_available=2,
        )
        assert node.is_ready is True

        # Not ready if degraded capacity
        node.capacity_available = 0
        assert node.is_ready is False

        # Not ready if unhealthy
        node.capacity_available = 2
        node.health_state = HealthState.UNHEALTHY
        assert node.is_ready is False

        # Not ready if low health score
        node.health_state = HealthState.HEALTHY
        node.health_score = 0.2
        assert node.is_ready is False

    def test_node_mark_unhealthy(self):
        """Consecutive failures trigger unhealthy transition."""
        node = VCGNode(agent_id="test", health_state=HealthState.HEALTHY)
        node.mark_unhealthy("Connection timeout")
        assert node.consecutive_failures == 1
        assert node.health_state == HealthState.HEALTHY  # Not yet unhealthy

        node.mark_unhealthy("Connection timeout")
        assert node.consecutive_failures == 2
        assert node.health_state == HealthState.HEALTHY

        node.mark_unhealthy("Connection timeout")
        assert node.consecutive_failures == 3
        assert node.health_state == HealthState.UNHEALTHY


class TestVCGDispatcher:
    """Tests for VCGDispatcher allocation and health monitoring."""

    def test_dispatcher_initialization_empty(self):
        """Dispatcher initializes with no agents."""
        dispatcher = VCGDispatcher()
        assert len(dispatcher._agents) == 0
        assert len(dispatcher._nodes) == 0

    def test_dispatcher_initialization_with_agents(self):
        """Dispatcher initializes with agent specs."""
        agents = [
            VCGAgent(id="hermes-local", skills=["all"], capacity=5),
            VCGAgent(id="hermes2", skills=["vision", "code-review"], capacity=3),
        ]
        dispatcher = VCGDispatcher(agents=agents)
        assert len(dispatcher._agents) == 2
        assert len(dispatcher._nodes) == 2
        assert dispatcher._nodes["hermes-local"].health_state == HealthState.UNKNOWN

    def test_register_node_new(self):
        """Registering a new node creates correct state."""
        dispatcher = VCGDispatcher()
        dispatcher.register_node("hermes-local", health_score=0.95, capacity_available=4)

        node = dispatcher._nodes["hermes-local"]
        assert node.health_state == HealthState.HEALTHY
        assert node.health_score == 0.95
        assert node.capacity_available == 4
        assert node.consecutive_failures == 0

    def test_register_node_health_states(self):
        """Health score maps to correct health state."""
        dispatcher = VCGDispatcher()

        # Healthy
        dispatcher.register_node("a", health_score=0.95)
        assert dispatcher._nodes["a"].health_state == HealthState.HEALTHY

        # Degraded
        dispatcher.register_node("b", health_score=0.70)
        assert dispatcher._nodes["b"].health_state == HealthState.DEGRADED

        # Unhealthy
        dispatcher.register_node("c", health_score=0.3)
        assert dispatcher._nodes["c"].health_state == HealthState.UNHEALTHY

    def test_dispatch_no_agents_ready(self):
        """Dispatch returns None when no agents ready."""
        dispatcher = VCGDispatcher()
        result = dispatcher.dispatch_task(
            task_id="t_abc",
            title="Test task",
            required_skills=["vision"],
            reach=5,
            impact=5,
            confidence=0.5,
            effort=1.0,
        )
        assert result is None

    def test_dispatch_single_agent_healthy(self):
        """Dispatch selects single healthy agent."""
        agents = [VCGAgent(id="hermes-local", skills=["all"], capacity=5)]
        dispatcher = VCGDispatcher(agents=agents)
        dispatcher.register_node("hermes-local", health_score=0.95, capacity_available=5)

        result = dispatcher.dispatch_task(
            task_id="t_abc",
            title="Test task",
            required_skills=["vision"],
            reach=8,
            impact=9,
            confidence=0.9,
            effort=3.0,
        )

        assert result is not None
        assert result.agent_id == "hermes-local"
        assert result.task_id == "t_abc"
        assert result.skill_match == 1.0  # 'all' skill
        assert result.welfare > 0
        # With single agent and no competitors, clarke_tax = welfare (would be 0 if agent weren't available)
        assert result.clarke_tax == result.welfare

    def test_dispatch_skill_matching(self):
        """Task dispatch prefers agents with skill match."""
        agents = [
            VCGAgent(id="agent1", skills=["vision"], capacity=5),
            VCGAgent(id="agent2", skills=["vision", "code-review"], capacity=5),
        ]
        dispatcher = VCGDispatcher(agents=agents)
        dispatcher.register_node("agent1", health_score=0.95, capacity_available=5)
        dispatcher.register_node("agent2", health_score=0.95, capacity_available=5)

        result = dispatcher.dispatch_task(
            task_id="t_code",
            title="Code review",
            required_skills=["code-review"],
            reach=8,
            impact=9,
            confidence=0.9,
            effort=2.0,
        )

        assert result is not None
        assert result.agent_id == "agent2"  # agent2 has code-review
        assert result.skill_match == 1.0

    def test_dispatch_insufficient_skill_match(self):
        """Task dispatch skips agents with low skill match."""
        agents = [
            VCGAgent(id="agent1", skills=["vision"], capacity=5),
        ]
        dispatcher = VCGDispatcher(agents=agents)
        dispatcher.register_node("agent1", health_score=0.95, capacity_available=5)

        # agent1 only has vision, task requires code-review + testing (0/2 = 0 match)
        result = dispatcher.dispatch_task(
            task_id="t_code",
            title="Code review",
            required_skills=["code-review", "testing"],
            reach=8,
            impact=9,
            confidence=0.9,
            effort=2.0,
        )

        assert result is None  # agent1 skipped for low skill match

    def test_dispatch_capacity_constraint(self):
        """Dispatch respects capacity limits."""
        agents = [VCGAgent(id="hermes", skills=["all"], capacity=2)]
        dispatcher = VCGDispatcher(agents=agents)
        dispatcher.register_node("hermes", health_score=0.95, capacity_available=2)

        # Allocate two tasks
        r1 = dispatcher.dispatch_task(
            task_id="t1", title="Task 1", required_skills=[], effort=1.0
        )
        assert r1 is not None
        assert dispatcher._nodes["hermes"].capacity_available == 1

        r2 = dispatcher.dispatch_task(
            task_id="t2", title="Task 2", required_skills=[], effort=1.0
        )
        assert r2 is not None
        assert dispatcher._nodes["hermes"].capacity_available == 0

        # Third task should fail — no capacity
        r3 = dispatcher.dispatch_task(
            task_id="t3", title="Task 3", required_skills=[], effort=1.0
        )
        assert r3 is None

    def test_dispatch_rice_scoring(self):
        """Dispatch uses RICE scoring correctly."""
        agents = [
            VCGAgent(id="agent1", skills=["all"], capacity=5),
            VCGAgent(id="agent2", skills=["all"], capacity=5),
        ]
        dispatcher = VCGDispatcher(agents=agents)
        dispatcher.register_node("agent1", health_score=0.95, capacity_available=5)
        dispatcher.register_node("agent2", health_score=0.95, capacity_available=5)

        # Low-RICE task
        r1 = dispatcher.dispatch_task(
            task_id="t_low",
            title="Low RICE",
            required_skills=[],
            reach=2,
            impact=2,
            confidence=0.5,
            effort=5.0,
        )
        rice_low = (2 * 2 * 0.5) / 5.0  # = 0.4

        # High-RICE task
        r2 = dispatcher.dispatch_task(
            task_id="t_high",
            title="High RICE",
            required_skills=[],
            reach=10,
            impact=10,
            confidence=0.9,
            effort=2.0,
        )
        rice_high = (10 * 10 * 0.9) / 2.0  # = 45

        assert r2.welfare > r1.welfare

    def test_dispatch_health_score_impact(self):
        """Dispatch prefers healthier agents."""
        agents = [
            VCGAgent(id="agent-healthy", skills=["all"], capacity=5),
            VCGAgent(id="agent-degraded", skills=["all"], capacity=5),
        ]
        dispatcher = VCGDispatcher(agents=agents)
        dispatcher.register_node("agent-healthy", health_score=0.95, capacity_available=5)
        dispatcher.register_node("agent-degraded", health_score=0.6, capacity_available=5)

        result = dispatcher.dispatch_task(
            task_id="t1", title="Task", required_skills=[], effort=1.0
        )
        assert result.agent_id == "agent-healthy"

    def test_dispatch_utilization_impact(self):
        """Dispatch prefers less utilized agents."""
        agents = [
            VCGAgent(id="agent1", skills=["all"], capacity=5),
            VCGAgent(id="agent2", skills=["all"], capacity=5),
        ]
        dispatcher = VCGDispatcher(agents=agents)
        dispatcher.register_node("agent1", health_score=0.95, capacity_available=1)
        dispatcher.register_node("agent2", health_score=0.95, capacity_available=5)

        result = dispatcher.dispatch_task(
            task_id="t1", title="Task", required_skills=[], effort=1.0
        )
        # agent2 has lower utilization (0/5 vs 4/5)
        assert result.agent_id == "agent2"

    def test_clarke_tax_calculation(self):
        """Clarke tax is calculated as welfare difference."""
        agents = [
            VCGAgent(id="agent1", skills=["all"], capacity=5),
            VCGAgent(id="agent2", skills=["all"], capacity=5),
        ]
        dispatcher = VCGDispatcher(agents=agents)
        dispatcher.register_node("agent1", health_score=0.95, capacity_available=5)
        dispatcher.register_node("agent2", health_score=0.8, capacity_available=5)

        result = dispatcher.dispatch_task(
            task_id="t1", title="Task", required_skills=[], effort=1.0
        )

        # Clarke tax should be positive (winner gets best welfare, loser has lower)
        assert result.clarke_tax >= 0
        assert result.clarke_tax <= result.welfare

    def test_complete_task_frees_capacity(self):
        """Completing a task frees capacity."""
        agents = [VCGAgent(id="hermes", skills=["all"], capacity=5)]
        dispatcher = VCGDispatcher(agents=agents)
        dispatcher.register_node("hermes", health_score=0.95, capacity_available=5)

        result = dispatcher.dispatch_task(
            task_id="t1", title="Task", required_skills=[], effort=1.0
        )
        assert dispatcher._nodes["hermes"].capacity_available == 4

        success = dispatcher.complete_task("t1")
        assert success is True
        assert dispatcher._nodes["hermes"].capacity_available == 5

    def test_complete_task_nonexistent(self):
        """Completing nonexistent task returns False."""
        dispatcher = VCGDispatcher()
        success = dispatcher.complete_task("t_nonexistent")
        assert success is False

    def test_health_check_comprehensive(self):
        """Health check returns comprehensive monitoring data."""
        agents = [
            VCGAgent(id="hermes1", skills=["all"], capacity=5),
            VCGAgent(id="hermes2", skills=["vision"], capacity=3),
        ]
        dispatcher = VCGDispatcher(agents=agents)
        dispatcher.register_node("hermes1", health_score=0.95, capacity_available=5)
        dispatcher.register_node("hermes2", health_score=0.6, capacity_available=2)

        # Allocate a task
        dispatcher.dispatch_task(task_id="t1", title="Task", required_skills=[], effort=1.0)

        health = dispatcher.health_check()

        assert "timestamp" in health
        assert "nodes" in health
        assert "summary" in health
        assert health["summary"]["total_nodes"] == 2
        assert health["summary"]["healthy_nodes"] == 1
        assert health["summary"]["degraded_nodes"] == 1
        assert health["summary"]["capacity_utilization"] > 0

    def test_resource_report_accounting(self):
        """Resource report tracks allocation history and welfare."""
        agents = [VCGAgent(id="hermes", skills=["all"], capacity=5)]
        dispatcher = VCGDispatcher(agents=agents)
        dispatcher.register_node("hermes", health_score=0.95, capacity_available=5)

        # Allocate multiple tasks
        dispatcher.dispatch_task(task_id="t1", title="Task 1", required_skills=[], effort=1.0)
        dispatcher.dispatch_task(task_id="t2", title="Task 2", required_skills=[], effort=1.0)

        report = dispatcher.resource_report()

        assert "agents" in report
        assert "totals" in report
        assert report["totals"]["total_allocations"] == 2
        assert report["totals"]["total_welfare"] > 0

    def test_stale_node_detection(self):
        """Stale heartbeats trigger degradation and unhealthy states."""
        dispatcher = VCGDispatcher()
        dispatcher.register_node("hermes", health_score=0.95, capacity_available=5)

        node = dispatcher._nodes["hermes"]
        assert node.health_state == HealthState.HEALTHY

        # Simulate stale heartbeat in degraded range (30-60s)
        node.last_heartbeat = time.time() - 40  # 40s ago
        dispatcher._update_stale_nodes()
        assert node.health_state == HealthState.DEGRADED

        # Simulate even staler heartbeat (>60s) — triggers mark_unhealthy
        node.last_heartbeat = time.time() - 70  # 70s ago
        dispatcher._update_stale_nodes()
        # After first unhealthy mark, consecutive_failures = 1, state still DEGRADED
        assert node.consecutive_failures == 1
        assert node.health_state == HealthState.DEGRADED  # Not yet UNHEALTHY (need 3 failures)

        # Second call with stale state triggers second unhealthy mark
        node.last_heartbeat = time.time() - 70
        dispatcher._update_stale_nodes()
        assert node.consecutive_failures == 2

        # Third call reaches unhealthy threshold
        node.last_heartbeat = time.time() - 70
        dispatcher._update_stale_nodes()
        assert node.consecutive_failures == 3
        assert node.health_state == HealthState.UNHEALTHY


class TestSkillMatching:
    """Tests for skill matching logic."""

    def test_empty_required_skills(self):
        """Empty required skills matches all agents perfectly."""
        dispatcher = VCGDispatcher()
        match = dispatcher._calculate_skill_match([], ["vision"])
        assert match == 1.0

    def test_empty_agent_skills(self):
        """Agent with no skills matches nothing."""
        dispatcher = VCGDispatcher()
        match = dispatcher._calculate_skill_match(["vision"], [])
        assert match == 0.0

    def test_all_skill_wildcard(self):
        """'all' skill matches all required skills."""
        dispatcher = VCGDispatcher()
        match = dispatcher._calculate_skill_match(["vision", "code-review"], ["all"])
        assert match == 1.0

    def test_partial_skill_match(self):
        """Partial skill match returns proportion."""
        dispatcher = VCGDispatcher()
        match = dispatcher._calculate_skill_match(["a", "b", "c"], ["a", "b"])
        assert match == 2.0 / 3.0  # ~0.667

    def test_perfect_skill_match(self):
        """Perfect skill match returns 1.0."""
        dispatcher = VCGDispatcher()
        match = dispatcher._calculate_skill_match(["a", "b"], ["a", "b", "c"])
        assert match == 1.0


class TestThreadSafety:
    """Tests for thread-safe concurrent access to VCGDispatcher."""

    def test_concurrent_dispatch_and_register(self):
        """Concurrent dispatch_task and register_node don't corrupt state."""
        agents = [
            VCGAgent(id="agent1", skills=["all"], capacity=10),
            VCGAgent(id="agent2", skills=["all"], capacity=10),
        ]
        dispatcher = VCGDispatcher(agents=agents)
        dispatcher.register_node("agent1", health_score=0.95, capacity_available=10)
        dispatcher.register_node("agent2", health_score=0.95, capacity_available=10)

        errors = []
        results = []
        results_lock = threading.Lock()

        def dispatch_many(name):
            try:
                for i in range(20):
                    r = dispatcher.dispatch_task(
                        task_id=f"t_{name}_{i}",
                        title=f"Concurrent task {i}",
                        required_skills=[],
                        effort=1.0,
                    )
                    if r:
                        with results_lock:
                            results.append(r)
            except Exception as e:
                errors.append(e)

        def register_loop():
            try:
                for i in range(20):
                    dispatcher.register_node(
                        "agent1",
                        health_score=0.9 + (i % 5) * 0.02,
                        # Don't reset capacity -- just update health score
                    )
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        def health_check_loop():
            try:
                for i in range(20):
                    dispatcher.health_check()
                    time.sleep(0.001)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=dispatch_many, args=("d1",)),
            threading.Thread(target=dispatch_many, args=("d2",)),
            threading.Thread(target=register_loop),
            threading.Thread(target=health_check_loop),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(errors) == 0, f"Thread errors: {errors}"
        # Verify no data corruption: total allocated tasks = sum of allocated lists
        total_in_lists = sum(len(n.allocated_tasks) for n in dispatcher._nodes.values())
        assert len(results) == total_in_lists

    def test_concurrent_complete_task(self):
        """Concurrent complete_task calls don't double-free capacity."""
        agents = [VCGAgent(id="hermes", skills=["all"], capacity=5)]
        dispatcher = VCGDispatcher(agents=agents)
        dispatcher.register_node("hermes", health_score=0.95, capacity_available=5)

        # Allocate 5 tasks
        task_ids = []
        for i in range(5):
            r = dispatcher.dispatch_task(
                task_id=f"t_{i}", title=f"Task {i}", required_skills=[], effort=1.0
            )
            assert r is not None
            task_ids.append(f"t_{i}")

        assert dispatcher._nodes["hermes"].capacity_available == 0

        # Concurrently complete all tasks
        errors = []
        completed = []
        lock = threading.Lock()

        def complete_task(tid):
            try:
                ok = dispatcher.complete_task(tid)
                with lock:
                    completed.append(ok)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=complete_task, args=(tid,)) for tid in task_ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0, f"Thread errors: {errors}"
        # All tasks completed, capacity back to 5
        assert dispatcher._nodes["hermes"].capacity_available == 5
        # Each task should return True exactly once
        assert sum(1 for c in completed if c) == 5


class TestVCGGateway:
    """Tests for VCGGateway integration layer."""

    def test_gateway_from_config(self):
        """Gateway creates correctly from config dict."""
        config = {
            "agents": [
                {"id": "hermes-local", "skills": ["all"], "capacity": 5},
                {"id": "hermes2", "skills": ["vision"], "capacity": 3},
            ],
            "nats_servers": ["nats://localhost:4222"],
            "heartbeat_interval_sec": 5.0,
        }
        gateway = VCGGateway.from_config(config)

        assert gateway is not None
        assert len(gateway._dispatcher._agents) == 2
        assert gateway._heartbeat_interval_sec == 5.0

    def test_gateway_report_node_health(self):
        """Gateway relays health reports to dispatcher."""
        config = {"agents": [{"id": "hermes-local", "skills": ["all"], "capacity": 5}]}
        gateway = VCGGateway.from_config(config)

        gateway.report_node_health("hermes-local", health_score=0.9, capacity_available=4)

        node = gateway._dispatcher._nodes["hermes-local"]
        assert node.health_score == 0.9
        assert node.health_state == HealthState.HEALTHY

    def test_gateway_report_task_completion(self):
        """Gateway relays task completion to dispatcher."""
        config = {"agents": [{"id": "hermes-local", "skills": ["all"], "capacity": 5}]}
        gateway = VCGGateway.from_config(config)
        gateway.report_node_health("hermes-local", health_score=0.95, capacity_available=5)

        result = gateway._dispatcher.dispatch_task(
            task_id="t_test", title="Test", required_skills=[], effort=1.0
        )
        assert result is not None

        success = gateway.report_task_completion("t_test")
        assert success is True

    def test_gateway_get_health_status(self):
        """Gateway returns health status from dispatcher."""
        config = {"agents": [{"id": "hermes-local", "skills": ["all"], "capacity": 5}]}
        gateway = VCGGateway.from_config(config)
        gateway.report_node_health("hermes-local", health_score=0.95, capacity_available=5)

        health = gateway.get_health_status()
        assert "nodes" in health
        assert "summary" in health
        assert health["summary"]["total_nodes"] == 1

    def test_gateway_get_resource_accounting(self):
        """Gateway returns resource accounting from dispatcher."""
        config = {"agents": [{"id": "hermes-local", "skills": ["all"], "capacity": 5}]}
        gateway = VCGGateway.from_config(config)
        gateway.report_node_health("hermes-local", health_score=0.95, capacity_available=5)

        gateway._dispatcher.dispatch_task(
            task_id="t1", title="Task", required_skills=[], effort=1.0
        )

        report = gateway.get_resource_accounting()
        assert "agents" in report
        assert "totals" in report
        assert report["totals"]["total_allocations"] == 1

    def test_singleton_init_and_get(self):
        """Global singleton initialize/get cycle works."""
        # Reset singleton
        import gateway.vcg_gateway as vg_mod
        vg_mod._vcg_gateway = None

        assert get_vcg_gateway() is None

        config = {"agents": [{"id": "hermes-local", "skills": ["all"], "capacity": 5}]}
        gw = initialize_vcg_gateway(config)
        assert gw is not None
        assert get_vcg_gateway() is gw

        # Cleanup
        vg_mod._vcg_gateway = None

    def test_gateway_graceful_without_nats(self):
        """Gateway works correctly when NATS is unavailable."""
        config = {"agents": [{"id": "hermes-local", "skills": ["all"], "capacity": 5}]}
        gateway = VCGGateway.from_config(config)
        gateway.report_node_health("hermes-local", health_score=0.95, capacity_available=5)

        # allocate_task should work fine without NATS (async, but we test sync dispatcher)
        result = gateway._dispatcher.dispatch_task(
            task_id="t_no_nats", title="No NATS", required_skills=[], effort=1.0
        )
        assert result is not None
        assert result.agent_id == "hermes-local"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
