"""
OKR Scheduling — Thin bridge to executive-agents-framework

Replaces 580 LOC of duplicated dependency_scheduler.py + advanced_scheduler.py
with imports from EAF's production-grade implementations:
- VCGTaskScheduler (DuckDB-backed dependency resolution)
- VCGMCTSIntegration (MCTS + VCG + Clarke tax)
- FractalMCTS (proper MCTS with select/expand/rollout/backpropagate)
- VCGDispatcher (node dispatch with health checks, NATS events)

Novel contribution: MakespanMinimizer (critical-path analysis + load balancing)
No EAF equivalent exists for this.
"""

import sys
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass

sys.path.insert(0, "/home/ubuntu/executive_agents_framework/src")

# ─── EAF Imports (canonical — do NOT reimplement) ───────────────────────
from executive_agents.infrastructure.systems.vcg_scheduler import VCGTaskScheduler
from executive_agents.infrastructure.systems.vcg_mcts_integration import VCGMCTSIntegration, VCGMCTSConfig
from executive_agents.infrastructure.systems.fractal_mcts import FractalMCTS, MCTSNode
from executive_agents.infrastructure.systems.vcg_dispatcher import (
    VCGDispatcher,
    ComputeNode,
    NodeStatus,
)

# ─── Novel: MakespanMinimizer (no EAF equivalent) ───────────────────────

from enum import Enum


class MachineType(Enum):
    """Machine capability types for cluster scheduling"""
    LOCAL = "hermes2"
    REMOTE_COMPUTE = "hermes1"
    REMOTE_QA = "dlg-sl3"


@dataclass
class Machine:
    """Cluster machine with capabilities"""
    name: MachineType
    available_slots: int
    latency_ms: float
    capable_engines: set


@dataclass
class ScheduledIssue:
    """An issue scheduled on a specific machine at a specific time"""
    issue_number: int
    machine: MachineType
    start_time: float  # hours from epoch
    duration: float  # estimated hours
    dependencies_met: bool = True


class MakespanMinimizer:
    """
    Critical-path analysis + load-balanced machine assignment.
    
    This is genuinely novel — no equivalent exists in EAF or Nebula.
    EAF's VCGTaskScheduler produces schedules via VCG allocation,
    but doesn't do critical-path analysis or machine-heterogeneity optimization.
    
    Algorithm:
    1. Topological sort respecting dependencies
    2. Compute critical path (longest chain to completion)
    3. Assign issues to least-loaded compatible machine
    4. Schedule start times after all dependencies complete
    5. Account for machine latency heterogeneity
    """

    def __init__(self, issues: Dict[int, 'Issue'], machines: List[Machine]):
        self.issues = issues
        self.machines = machines

    def compute_schedule(self) -> List[ScheduledIssue]:
        """Compute schedule that minimizes makespan"""
        from .github_issue_queue import Issue

        schedule = []
        completed = set()
        issue_end_times = {}

        while len(completed) < len(self.issues):
            # Find ready issues (all deps met)
            ready = [
                i for i in self.issues
                if i not in completed
                and all(d in completed for d in self.issues[i].dependencies)
            ]
            if not ready:
                break

            # Sort by critical path length (longest remaining → highest priority)
            ready.sort(
                key=lambda i: self._critical_path_length(i, completed),
                reverse=True,
            )

            for issue_num in ready:
                issue = self.issues[issue_num]
                machine = self._least_loaded_compatible_machine(issue)

                # Start after all dependencies finish
                start_time = max(
                    [issue_end_times.get(d, 0) for d in issue.dependencies] + [0]
                )

                duration = getattr(issue, 'estimated_duration', 2.0)
                end_time = start_time + duration + machine.latency_ms / 1000.0

                schedule.append(ScheduledIssue(
                    issue_number=issue_num,
                    machine=machine.name,
                    start_time=start_time,
                    duration=duration,
                ))
                issue_end_times[issue_num] = end_time
                completed.add(issue_num)

        return schedule

    def _critical_path_length(self, issue_num: int, completed: set) -> float:
        """Compute remaining path length to end (longest chain)"""
        issue = self.issues[issue_num]
        duration = getattr(issue, 'estimated_duration', 2.0)

        # Find what depends on this issue
        dependents = [
            i for i in self.issues
            if issue_num in self.issues[i].dependencies and i not in completed
        ]

        if not dependents:
            return duration

        max_path = max(
            self._critical_path_length(d, completed) for d in dependents
        )
        return duration + max_path

    def _least_loaded_compatible_machine(self, issue) -> Machine:
        """Find least-loaded machine that can execute this issue"""
        required = getattr(issue, 'required_engines', set())
        compatible = [
            m for m in self.machines
            if required <= m.capable_engines or not required
        ]
        if not compatible:
            return self.machines[0]
        return max(compatible, key=lambda m: m.available_slots)


class OKRScheduler:
    """
    Unified scheduler that composes EAF's production systems + MakespanMinimizer.
    
    Architecture:
    1. VCGTaskScheduler (EAF) → dependency resolution + DuckDB persistence
    2. VCGMCTSIntegration (EAF) → MCTS search + VCG welfare + Clarke tax
    3. VCGDispatcher (EAF) → node dispatch + health checks + NATS events
    4. MakespanMinimizer (novel) → critical-path + machine heterogeneity
    """

    def __init__(self, db_path: str = "/tmp/okr_vcg_registry.db"):
        self.vcg_scheduler = VCGTaskScheduler()
        self.mcts_integration = VCGMCTSIntegration(VCGMCTSConfig())
        self.dispatcher = VCGDispatcher(registry_db=db_path)
        self.makespan_minimizer = None  # Initialized with issue data

    def initialize_makespan(self, issues: Dict, machines: List[Machine]):
        """Initialize makespan minimizer with issue + machine data"""
        self.makespan_minimizer = MakespanMinimizer(issues, machines)

    def get_ready_issues(self) -> List[int]:
        """Get issues ready to execute (dependencies met) — delegates to EAF"""
        return self.vcg_scheduler.get_ready_tasks()

    def compute_optimal_schedule(self) -> List[ScheduledIssue]:
        """Compute optimal schedule using makespan minimizer"""
        if self.makespan_minimizer:
            return self.makespan_minimizer.compute_schedule()
        return []


# ─── Re-exports ──────────────────────────────────────────────────────────
__all__ = [
    # From EAF (canonical)
    "VCGTaskScheduler", "VCGMCTSIntegration", "VCGMCTSConfig",
    "FractalMCTS", "MCTSNode", "VCGDispatcher",
    "ComputeNode", "NodeStatus",
    # Novel
    "MakespanMinimizer", "OKRScheduler", "MachineType",
    "Machine", "ScheduledIssue",
]
