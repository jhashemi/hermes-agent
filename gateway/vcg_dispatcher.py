"""VCG (Vickrey-Clarke-Groves) runtime dispatcher for task allocation.

Implements health monitoring, task dispatch, and resource allocation using
game-theoretic principles. Nodes register with their capabilities, receive
periodic health checks, and tasks are allocated based on skill match, capacity,
and Clarke tax welfare calculations.

Architecture:
  - VCGDispatcher: Central registry and allocation engine
  - VCGNode: Represents a Hermes instance with health + capacity tracking
  - TaskAllocationResult: Game-theoretic allocation with welfare + Clarke tax
  - HealthState: State machine for node health (healthy, degraded, unhealthy)

Integration points:
  - instance_orchestrator: VCGDispatcher selects which instance handles a task
  - remote_agent_api: Health endpoint feeds into dispatcher health checks
  - NATS event bus (optional): Publish allocation events for monitoring

Example:
    dispatcher = VCGDispatcher(agents=[
        VCGAgent(id='hermes-local', skills=['all'], capacity=5),
        VCGAgent(id='hermes2', skills=['vision', 'code-review'], capacity=3),
    ])

    # Register nodes with live health
    dispatcher.register_node('hermes-local', health=0.95, capacity=5)
    dispatcher.register_node('hermes2', health=0.85, capacity=3)

    # Dispatch a task
    allocation = dispatcher.dispatch_task(
        task_id='t_abc123',
        title='Review PR #42',
        required_skills=['code-review'],
        reach=8, impact=9, confidence=0.9, effort=3
    )
    # → TaskAllocationResult(agent_id='hermes2', welfare=15.2, clarke_tax=2.1)

    # Health monitoring
    health_report = dispatcher.health_check()
    # → {'nodes': {...}, 'capacity_utilization': 0.62, 'degraded_count': 0}
"""

from __future__ import annotations

import time
import logging
import threading
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Tuple
from enum import Enum
from datetime import datetime, timedelta
from pathlib import Path
import json

logger = logging.getLogger(__name__)


class HealthState(Enum):
    """Health state machine for node monitoring."""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class VCGAgent:
    """Agent capability and capacity spec for allocation."""
    id: str
    skills: List[str] = field(default_factory=list)  # ['vision', 'code-review', etc.]
    capacity: int = 5  # Max concurrent tasks
    reliability: float = 0.95  # Baseline reliability
    last_allocated_at: float = 0.0
    allocated_count: int = 0  # Running count of allocations


@dataclass
class VCGNode:
    """Runtime node state for health monitoring and dispatch."""
    agent_id: str
    last_heartbeat: float = 0.0  # Unix timestamp
    health_state: HealthState = HealthState.UNKNOWN
    health_score: float = 1.0  # 0.0-1.0 (1.0 = fully healthy)
    capacity_available: int = 5
    capacity_max: int = 5
    allocated_tasks: List[str] = field(default_factory=list)  # Current task IDs
    error_count: int = 0
    consecutive_failures: int = 0
    response_time_ms: float = 0.0

    @property
    def utilization(self) -> float:
        """Return capacity utilization as 0.0-1.0."""
        if self.capacity_max == 0:
            return 0.0
        return (self.capacity_max - self.capacity_available) / self.capacity_max

    @property
    def is_ready(self) -> bool:
        """Return True if node is ready for task dispatch."""
        return (
            self.health_state in [HealthState.HEALTHY, HealthState.DEGRADED]
            and self.capacity_available > 0
            and self.health_score > 0.3  # Below 30% health is considered unhealthy
        )

    def mark_unhealthy(self, reason: str = "") -> None:
        """Transition to UNHEALTHY state."""
        self.consecutive_failures += 1
        if self.consecutive_failures > 2:
            self.health_state = HealthState.UNHEALTHY
            logger.warning(
                f"[VCG] Node {self.agent_id} marked UNHEALTHY: {reason} "
                f"(consecutive_failures={self.consecutive_failures})"
            )


@dataclass
class TaskAllocationResult:
    """Result of a game-theoretic task allocation."""
    task_id: str
    agent_id: str
    welfare: float  # Value of allocation to agent
    clarke_tax: float  # Nash equilibrium tax payment
    skill_match: float  # 0.0-1.0 skill match score
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        """Serialize to JSON-compatible dict."""
        return {
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "welfare": self.welfare,
            "clarke_tax": self.clarke_tax,
            "skill_match": self.skill_match,
            "timestamp": self.timestamp,
        }


class VCGDispatcher:
    """Central dispatcher for game-theoretic task allocation.

    Maintains node registry, performs health checks, and allocates tasks
    using RICE scoring + agent skill matching + Clarke tax welfare.

    Health check strategy:
      - Each node reports heartbeat (health_score 0-1, capacity_available)
      - Missing heartbeat after 30s → DEGRADED
      - Missing heartbeat after 60s → UNHEALTHY
      - Consecutive failures trigger fast unhealthy transition (2+ failures)

    Allocation strategy:
      - Calculate skill match (0-1) for each node vs task required_skills
      - Calculate RICE score: (reach × impact × confidence) / effort
      - Calculate welfare = RICE × skill_match × health_score × (1 - utilization)
      - Select agent with highest welfare
      - Calculate Clarke tax (welfare loss if agent weren't available)
    """

    HEARTBEAT_DEGRADED_TIMEOUT_SEC = 30.0
    HEARTBEAT_UNHEALTHY_TIMEOUT_SEC = 60.0

    def __init__(self, agents: List[VCGAgent] = None):
        """Initialize dispatcher with optional agent specs."""
        self._agents: Dict[str, VCGAgent] = {}
        self._nodes: Dict[str, VCGNode] = {}
        self._allocations: Dict[str, TaskAllocationResult] = {}
        self._allocation_history: List[TaskAllocationResult] = []
        self._lock = threading.Lock()  # Protects _agents, _nodes, _allocations, _allocation_history

        if agents:
            for agent in agents:
                self._agents[agent.id] = agent
                self._nodes[agent.id] = VCGNode(
                    agent_id=agent.id,
                    capacity_max=agent.capacity,
                    capacity_available=agent.capacity,
                    health_state=HealthState.UNKNOWN,
                )

    def register_node(
        self,
        agent_id: str,
        health_score: float = 1.0,
        capacity_available: int = None,
        capacity_max: int = None,
        response_time_ms: float = 0.0,
    ) -> None:
        """Register or update a node with current health and capacity.

        Args:
            agent_id: Unique agent identifier
            health_score: 0.0-1.0 health score
            capacity_available: Available task slots (None = no change)
            capacity_max: Maximum capacity (None = no change)
            response_time_ms: Recent response time in ms
        """
        with self._lock:
            if agent_id not in self._nodes:
                # New node registration
                max_cap = capacity_max or (self._agents.get(agent_id, VCGAgent(agent_id)).capacity or 5)
                self._nodes[agent_id] = VCGNode(
                    agent_id=agent_id,
                    capacity_max=max_cap,
                    capacity_available=capacity_available or max_cap,
                    health_state=HealthState.HEALTHY,
                )
                logger.info(f"[VCG] Registered node: {agent_id}")

            node = self._nodes[agent_id]
            node.last_heartbeat = time.time()
            node.health_score = max(0.0, min(1.0, health_score))
            node.response_time_ms = response_time_ms
            node.consecutive_failures = 0  # Reset failure counter on successful heartbeat

            if capacity_available is not None:
                node.capacity_available = capacity_available
            if capacity_max is not None:
                node.capacity_max = capacity_max

            # Update health state based on score
            if node.health_score >= 0.8:
                node.health_state = HealthState.HEALTHY
            elif node.health_score >= 0.5:
                node.health_state = HealthState.DEGRADED
            else:
                node.health_state = HealthState.UNHEALTHY

    def get_agent_success_rate(self, agent_id: str) -> float:
        """Query cognitive audit trail for agent success rate.
        
        Reads the cognitive_audit.jsonl file to calculate what percentage of
        allocations to this agent resulted in high-confidence outcomes.
        
        Returns:
            float: Success rate (0.0-1.0). Returns 1.0 (neutral) if no data found.
        """
        try:
            audit_path = Path.home() / ".hermes" / "cognitive_audit.jsonl"
            if not audit_path.exists():
                return 1.0  # Default: assume capable if no audit file
            
            successes = 0
            total = 0
            
            for line in audit_path.read_text().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    # Match entries for this agent's allocations
                    if entry.get("agent_id", "").startswith(f"vcg:{agent_id}"):
                        total += 1
                        # High confidence (>0.7) = success
                        if entry.get("confidence", 0) > 0.7:
                            successes += 1
                except json.JSONDecodeError:
                    continue  # Skip malformed lines
            
            if total == 0:
                return 1.0  # No history = neutral weight
            
            return successes / total
        except Exception as e:
            logger.debug(f"[VCG] Failed to read agent success rate for {agent_id}: {e}")
            return 1.0  # Default to neutral weight on error

    def calculate_adaptive_weight(self, agent_id: str) -> float:
        """Calculate weight adjustment based on historical performance.
        
        Maps agent success rate to adaptive weight:
        - 0% success → 0.5 (strongly disfavor)
        - 50% success → 1.0 (neutral)
        - 100% success → 1.5 (strongly favor)
        
        Returns:
            float: Weight 0.5-1.5 to multiply into welfare calculation
        """
        success_rate = self.get_agent_success_rate(agent_id)
        # Map 0.0-1.0 to 0.5-1.5: 0.5 + (success_rate * 1.0)
        weight = 0.5 + (success_rate * 1.0)
        logger.debug(f"[VCG] {agent_id}: success_rate={success_rate:.2f}, adaptive_weight={weight:.2f}")
        return weight

    def _record_allocation_to_audit_trail(
        self,
        task_id: str,
        title: str,
        agent_id: str,
        welfare: float,
        confidence: float,
        required_skills: List[str],
    ) -> None:
        """Record task allocation outcome to cognitive audit trail.
        
        Args:
            task_id: Unique task identifier
            title: Human-readable task title
            agent_id: Agent that was allocated the task
            welfare: Calculated welfare score
            confidence: Skill match score (proxy for decision confidence)
            required_skills: Skills required for the task
        """
        try:
            from plugins.memory.cognitive import CognitiveMemoryProvider
            provider = CognitiveMemoryProvider()
            provider.record_decision(
                agent_id=f"vcg:{agent_id}",
                decision_type="task_allocation",
                reasoning=f"Allocated task {task_id} ({title}) to {agent_id} (welfare={welfare:.1f})",
                confidence=confidence,  # Use skill match as confidence
                context={
                    "task_id": task_id,
                    "title": title,
                    "welfare": welfare,
                    "required_skills": required_skills,
                    "allocation_source": "vcg_dispatcher",
                }
            )
            logger.debug(f"[VCG] Recorded allocation {task_id} → {agent_id} to audit trail")
        except ImportError:
            logger.debug("[VCG] CognitiveMemoryProvider not available; skipping audit recording")
        except Exception as e:
            logger.warning(f"[VCG] Failed to record allocation to audit trail: {e}")

    def dispatch_task(
        self,
        task_id: str,
        title: str,
        required_skills: List[str],
        reach: int = 5,
        impact: int = 5,
        confidence: float = 0.5,
        effort: float = 1.0,
    ) -> Optional[TaskAllocationResult]:
        """Allocate a task to the best agent using game-theoretic principles.

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
        # Check for stale heartbeats and update health states
        with self._lock:
            self._update_stale_nodes()

            # Calculate RICE score
            rice_score = (reach * impact * confidence) / max(effort, 0.5)

            # Score all ready agents
            scores: Dict[str, Tuple[float, float, VCGNode]] = {}  # agent_id -> (welfare, skill_match, node)
            for agent_id, node in self._nodes.items():
                if not node.is_ready:
                    logger.debug(
                        f"[VCG] Skipping {agent_id}: not ready "
                        f"(state={node.health_state.value}, util={node.utilization:.2f})"
                    )
                    continue

                # Skill match: proportion of required skills this agent has
                agent = self._agents.get(agent_id)
                if agent is None:
                    agent_skills = []
                else:
                    agent_skills = agent.skills

                skill_match = self._calculate_skill_match(required_skills, agent_skills)
                if skill_match < 0.3:  # Skip agents without minimum skill match
                    logger.debug(f"[VCG] Skipping {agent_id}: low skill match ({skill_match:.2f})")
                    continue

                # Welfare = RICE × skill_match × health_score × (1 - utilization) × adaptive_weight
                utilization = node.utilization
                welfare = rice_score * skill_match * node.health_score * (1.0 - utilization)
                
                # Apply adaptive weight based on historical performance
                adaptive_weight = self.calculate_adaptive_weight(agent_id)
                welfare = welfare * adaptive_weight
                
                scores[agent_id] = (welfare, skill_match, node)
                logger.debug(
                    f"[VCG] {agent_id}: welfare={welfare:.1f}, skill_match={skill_match:.2f}, "
                    f"util={utilization:.2f}"
                )

            if not scores:
                logger.warning(f"[VCG] No agent ready for task {task_id}: {title}")
                return None

            # Select agent with highest welfare
            best_agent_id = max(scores.keys(), key=lambda a: scores[a][0])
            best_welfare, best_skill_match, best_node = scores[best_agent_id]

            # Calculate Clarke tax (welfare loss if best agent were unavailable)
            # = best_welfare - second_best_welfare
            sorted_welfares = sorted(scores.values(), key=lambda x: x[0], reverse=True)
            second_best_welfare = sorted_welfares[1][0] if len(sorted_welfares) > 1 else 0.0
            clarke_tax = max(0.0, best_welfare - second_best_welfare)

            # Create allocation result
            result = TaskAllocationResult(
                task_id=task_id,
                agent_id=best_agent_id,
                welfare=best_welfare,
                clarke_tax=clarke_tax,
                skill_match=best_skill_match,
            )

            # Update node state
            best_node.allocated_tasks.append(task_id)
            best_node.capacity_available = max(0, best_node.capacity_available - 1)

            # Update agent allocation count
            if best_agent_id in self._agents:
                self._agents[best_agent_id].allocated_count += 1
                self._agents[best_agent_id].last_allocated_at = time.time()

            # Record allocation
            self._allocations[task_id] = result
            self._allocation_history.append(result)
            
            # Record allocation to cognitive audit trail for adaptive learning
            self._record_allocation_to_audit_trail(
                task_id=task_id,
                title=title,
                agent_id=best_agent_id,
                welfare=best_welfare,
                confidence=best_skill_match,
                required_skills=required_skills,
            )

        logger.info(
            f"[VCG] Allocated {task_id} to {best_agent_id}: "
            f"welfare={best_welfare:.1f}, clarke_tax={clarke_tax:.1f}, "
            f"skill_match={best_skill_match:.2f}"
        )

        return result

    def complete_task(self, task_id: str) -> bool:
        """Mark a task as complete and free up capacity.

        Args:
            task_id: Task to complete

        Returns:
            True if task was found and completed, False otherwise.
        """
        with self._lock:
            allocation = self._allocations.get(task_id)
            if allocation is None:
                return False

            agent_id = allocation.agent_id
            node = self._nodes.get(agent_id)
            if node is None:
                return False

            # Remove from allocated list and free capacity
            if task_id in node.allocated_tasks:
                node.allocated_tasks.remove(task_id)
            node.capacity_available = min(node.capacity_max, node.capacity_available + 1)

        logger.debug(
            f"[VCG] Task {task_id} completed on {agent_id}: "
            f"capacity_available={node.capacity_available}/{node.capacity_max}"
        )

        return True

    def health_check(self) -> dict:
        """Return a comprehensive health report for monitoring.

        Returns:
            Dict with nodes status, capacity utilization, and degradation metrics.
        """
        with self._lock:
            self._update_stale_nodes()

            nodes_status = {}
            total_capacity = 0
            allocated_capacity = 0
            healthy_count = 0
            degraded_count = 0
            unhealthy_count = 0

            for agent_id, node in self._nodes.items():
                total_capacity += node.capacity_max
                allocated_capacity += node.capacity_max - node.capacity_available
                if node.health_state == HealthState.HEALTHY:
                    healthy_count += 1
                elif node.health_state == HealthState.DEGRADED:
                    degraded_count += 1
                elif node.health_state == HealthState.UNHEALTHY:
                    unhealthy_count += 1

                nodes_status[agent_id] = {
                    "state": node.health_state.value,
                    "health_score": node.health_score,
                    "capacity_available": node.capacity_available,
                    "capacity_max": node.capacity_max,
                    "utilization": node.utilization,
                    "allocated_tasks": node.allocated_tasks,
                    "response_time_ms": node.response_time_ms,
                    "last_heartbeat_sec_ago": time.time() - node.last_heartbeat,
                    "error_count": node.error_count,
                    "consecutive_failures": node.consecutive_failures,
                }

            capacity_utilization = allocated_capacity / total_capacity if total_capacity > 0 else 0.0

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "nodes": nodes_status,
            "summary": {
                "total_nodes": len(self._nodes),
                "healthy_nodes": healthy_count,
                "degraded_nodes": degraded_count,
                "unhealthy_nodes": unhealthy_count,
                "capacity_total": total_capacity,
                "capacity_allocated": allocated_capacity,
                "capacity_utilization": capacity_utilization,
                "total_allocations": len(self._allocations),
                "total_completed": len(self._allocation_history),
            },
        }

    def resource_report(self) -> dict:
        """Return a resource allocation report for accounting.

        Returns:
            Dict with task allocation history, agent workload, and welfare stats.
        """
        with self._lock:
            agent_stats: Dict[str, dict] = {}
            total_welfare = 0.0
            total_clarke_tax = 0.0

            for agent_id in self._agents.keys():
                agent_stats[agent_id] = {
                    "allocated_count": self._agents[agent_id].allocated_count,
                    "total_welfare": 0.0,
                    "total_clarke_tax": 0.0,
                    "avg_skill_match": 0.0,
                    "tasks": [],
                }

            for allocation in self._allocation_history:
                agent_id = allocation.agent_id
                if agent_id not in agent_stats:
                    agent_stats[agent_id] = {
                        "allocated_count": 1,
                        "total_welfare": 0.0,
                        "total_clarke_tax": 0.0,
                        "avg_skill_match": 0.0,
                        "tasks": [],
                    }

                agent_stats[agent_id]["total_welfare"] += allocation.welfare
                agent_stats[agent_id]["total_clarke_tax"] += allocation.clarke_tax
                agent_stats[agent_id]["tasks"].append(allocation.to_dict())
                total_welfare += allocation.welfare
                total_clarke_tax += allocation.clarke_tax

            # Calculate averages
            for agent_id, stats in agent_stats.items():
                tasks = stats["tasks"]
                if tasks:
                    stats["avg_skill_match"] = sum(t["skill_match"] for t in tasks) / len(tasks)

        return {
            "timestamp": datetime.utcnow().isoformat(),
            "agents": agent_stats,
            "totals": {
                "total_allocations": len(self._allocation_history),
                "total_welfare": total_welfare,
                "total_clarke_tax": total_clarke_tax,
            },
        }

    # ─────────────────────────────────────────────────────────────────────
    # Private methods
    # ─────────────────────────────────────────────────────────────────────

    def _update_stale_nodes(self) -> None:
        """Check for stale heartbeats and update health states."""
        now = time.time()
        for node in self._nodes.values():
            if node.health_state == HealthState.UNKNOWN:
                continue
            time_since_heartbeat = now - node.last_heartbeat

            if time_since_heartbeat > self.HEARTBEAT_UNHEALTHY_TIMEOUT_SEC:
                # This triggers mark_unhealthy which increments consecutive_failures
                node.mark_unhealthy(f"No heartbeat for {time_since_heartbeat:.1f}s")
            elif time_since_heartbeat > self.HEARTBEAT_DEGRADED_TIMEOUT_SEC:
                if node.health_state != HealthState.DEGRADED:
                    node.health_state = HealthState.DEGRADED
                    logger.warning(
                        f"[VCG] Node {node.agent_id} marked DEGRADED: "
                        f"No heartbeat for {time_since_heartbeat:.1f}s"
                    )

    def _calculate_skill_match(self, required_skills: List[str], agent_skills: List[str]) -> float:
        """Calculate skill match as proportion of required skills the agent has.

        Args:
            required_skills: Skills required by task
            agent_skills: Skills the agent possesses

        Returns:
            0.0-1.0 match score. Special cases:
            - Empty required_skills → 1.0
            - Empty agent_skills → 0.0
            - 'all' in agent_skills → 1.0
        """
        if not required_skills:
            return 1.0
        if "all" in agent_skills:
            return 1.0
        if not agent_skills:
            return 0.0

        matched = sum(1 for skill in required_skills if skill in agent_skills)
        return matched / len(required_skills)
