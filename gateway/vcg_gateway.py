"""Integration between VCGDispatcher and hermes-agent gateway.

This module provides hooks to connect the VCG runtime dispatcher to the existing
instance orchestrator and remote agent API, enabling game-theoretic task allocation
across multi-instance deployments.

Key responsibilities:
  - VCGGateway: Singleton gateway adapter combining dispatcher + NATS integration
  - Heartbeat thread: Periodic health reports from remote instances
  - Task allocation hook: Intercept before dispatch_task calls
  - Event publishing: NATS JetStream pub/sub for monitoring
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Optional, Dict, List
from contextlib import asynccontextmanager

from gateway.vcg_dispatcher import (
    VCGDispatcher, VCGAgent, TaskAllocationResult, HealthState
)

logger = logging.getLogger(__name__)


class VCGGateway:
    """Integration layer between VCG dispatcher and hermes-agent gateway.

    Manages the dispatcher lifecycle, coordinates heartbeats from remote instances,
    and publishes allocation events to NATS for monitoring and accounting.

    Usage:
        gateway = VCGGateway.from_config(config_dict)
        async with gateway.connect():
            allocation = gateway.allocate_task(...)
    """

    def __init__(
        self,
        dispatcher: VCGDispatcher,
        nats_servers: List[str] = None,
        heartbeat_interval_sec: float = 10.0,
    ):
        """Initialize VCG gateway.

        Args:
            dispatcher: Initialized VCGDispatcher instance
            nats_servers: NATS server URLs for event publishing
            heartbeat_interval_sec: Interval for periodic health checks
        """
        self._dispatcher = dispatcher
        self._nats_servers = nats_servers or ["nats://localhost:4222"]
        self._heartbeat_interval_sec = heartbeat_interval_sec
        self._nats = None
        self._js = None  # JetStream context
        self._heartbeat_thread: Optional[threading.Thread] = None
        self._stop_heartbeat = threading.Event()

    @classmethod
    def from_config(cls, config: Dict) -> VCGGateway:
        """Create gateway from configuration dict.

        Args:
            config: Dict with keys:
                - agents: List[Dict] with id, skills, capacity
                - nats_servers: List[str] server URLs (default: localhost:4222)
                - heartbeat_interval_sec: Float (default: 10.0)

        Returns:
            Initialized VCGGateway instance
        """
        agents = []
        for agent_cfg in config.get("agents", []):
            agent = VCGAgent(
                id=agent_cfg["id"],
                skills=agent_cfg.get("skills", ["all"]),
                capacity=agent_cfg.get("capacity", 5),
                reliability=agent_cfg.get("reliability", 0.95),
            )
            agents.append(agent)

        dispatcher = VCGDispatcher(agents=agents)

        gateway = cls(
            dispatcher=dispatcher,
            nats_servers=config.get("nats_servers", ["nats://localhost:4222"]),
            heartbeat_interval_sec=config.get("heartbeat_interval_sec", 10.0),
        )
        return gateway

    @asynccontextmanager
    async def connect(self):
        """Connect to NATS and start heartbeat monitoring.

        Yields the gateway instance. On exit, cleanly closes NATS connection.
        """
        try:
            # Optional: connect to NATS for event publishing
            # await self._connect_nats()
            self._start_heartbeat_thread()
            yield self
        finally:
            self._stop_heartbeat.set()
            if self._heartbeat_thread:
                self._heartbeat_thread.join(timeout=5.0)
            # await self._close_nats()

    async def allocate_task(
        self,
        task_id: str,
        title: str,
        required_skills: List[str],
        reach: int = 5,
        impact: int = 5,
        confidence: float = 0.5,
        effort: float = 1.0,
    ) -> Optional[TaskAllocationResult]:
        """Allocate a task to an agent using game-theoretic principles.

        This is the main entry point for task dispatch. It coordinates with
        the dispatcher and publishes allocation events to NATS if available.

        Args:
            task_id: Unique task identifier
            title: Human-readable task title
            required_skills: Skills required to complete task
            reach: RICE reach score (1-10)
            impact: RICE impact score (1-10)
            confidence: RICE confidence score (0-1)
            effort: RICE effort score (days, >= 0.5)

        Returns:
            TaskAllocationResult with allocated agent, welfare, and Clarke tax,
            or None if no agent is ready for dispatch.
        """
        result = self._dispatcher.dispatch_task(
            task_id=task_id,
            title=title,
            required_skills=required_skills,
            reach=reach,
            impact=impact,
            confidence=confidence,
            effort=effort,
        )

        if result:
            await self._publish_allocation_event(result, title)

        return result

    def report_task_completion(self, task_id: str) -> bool:
        """Report that a task has completed and free up capacity.

        Args:
            task_id: Task ID to mark complete

        Returns:
            True if task was found and completed, False otherwise.
        """
        return self._dispatcher.complete_task(task_id)

    def report_node_health(
        self,
        agent_id: str,
        health_score: float = 1.0,
        capacity_available: int = None,
        response_time_ms: float = 0.0,
    ) -> None:
        """Report health status from a remote node.

        Called periodically (or on-demand) by remote agent instances to update
        their health and capacity state in the dispatcher.

        Args:
            agent_id: Agent identifier
            health_score: 0.0-1.0 health score
            capacity_available: Available task slots (None = no change)
            response_time_ms: Recent response time in ms
        """
        self._dispatcher.register_node(
            agent_id=agent_id,
            health_score=health_score,
            capacity_available=capacity_available,
            response_time_ms=response_time_ms,
        )

    def get_health_status(self) -> dict:
        """Return comprehensive health report for monitoring dashboards.

        Returns:
            Dict with nodes status, capacity utilization, and degradation metrics.
        """
        return self._dispatcher.health_check()

    def get_resource_accounting(self) -> dict:
        """Return resource allocation accounting for billing/reporting.

        Returns:
            Dict with task allocation history, agent workload, and welfare stats.
        """
        return self._dispatcher.resource_report()

    # ─────────────────────────────────────────────────────────────────────
    # Private methods
    # ─────────────────────────────────────────────────────────────────────

    def _start_heartbeat_thread(self) -> None:
        """Start background thread for periodic health monitoring.

        This thread wakes up every N seconds and calls health_check() to detect
        stale nodes and update their health states based on missed heartbeats.
        """
        def heartbeat_loop():
            while not self._stop_heartbeat.is_set():
                time.sleep(self._heartbeat_interval_sec)
                if self._stop_heartbeat.is_set():
                    break
                try:
                    # Trigger stale node detection
                    self._dispatcher._update_stale_nodes()
                except Exception as e:
                    logger.error(f"[VCG] Heartbeat thread error: {e}")

        self._heartbeat_thread = threading.Thread(target=heartbeat_loop, daemon=True)
        self._heartbeat_thread.start()
        logger.info(
            f"[VCG] Heartbeat thread started (interval={self._heartbeat_interval_sec}s)"
        )

    async def _connect_nats(self) -> None:
        """Connect to NATS and set up JetStream context."""
        try:
            import nats
        except ImportError:
            logger.warning("[VCG] NATS not available; event publishing disabled")
            return

        try:
            self._nats = await nats.connect(servers=self._nats_servers)
            self._js = self._nats.jetstream()
            logger.info(f"[VCG] Connected to NATS: {self._nats_servers}")
        except Exception as e:
            logger.warning(f"[VCG] Failed to connect to NATS: {e}")

    async def _close_nats(self) -> None:
        """Close NATS connection."""
        if self._nats:
            await self._nats.close()

    async def _publish_allocation_event(
        self, allocation: TaskAllocationResult, title: str
    ) -> None:
        """Publish task allocation event to NATS JetStream.

        Subject taxonomy:
            exec.vcg.allocation -> task allocated
            exec.vcg.health -> health status
            exec.vcg.accounting -> resource accounting

        Args:
            allocation: Task allocation result
            title: Human-readable task title
        """
        if not self._js:
            return

        try:
            import json
            subject = "exec.vcg.allocation"
            payload = {
                "event_type": "task_allocated",
                "timestamp": allocation.timestamp,
                "task_id": allocation.task_id,
                "agent_id": allocation.agent_id,
                "task_title": title,
                "welfare": allocation.welfare,
                "clarke_tax": allocation.clarke_tax,
                "skill_match": allocation.skill_match,
            }
            await self._js.publish(subject, json.dumps(payload).encode())
            logger.debug(f"[VCG] Published allocation event: {allocation.task_id}")
        except Exception as e:
            logger.warning(f"[VCG] Failed to publish allocation event: {e}")


# ─────────────────────────────────────────────────────────────────────────
# Singleton gateway instance (for use by gateway/run.py)
# ─────────────────────────────────────────────────────────────────────────

_vcg_gateway: Optional[VCGGateway] = None


def initialize_vcg_gateway(config: Dict) -> VCGGateway:
    """Initialize the global VCG gateway from config.

    Args:
        config: Configuration dict

    Returns:
        Initialized VCGGateway instance
    """
    global _vcg_gateway
    _vcg_gateway = VCGGateway.from_config(config)
    logger.info("[VCG] Gateway initialized with %d agents", len(config.get("agents", [])))
    return _vcg_gateway


def get_vcg_gateway() -> Optional[VCGGateway]:
    """Get the global VCG gateway instance.

    Returns:
        VCGGateway if initialized, None otherwise.
    """
    return _vcg_gateway
