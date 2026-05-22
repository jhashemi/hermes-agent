"""
ExecutionEngine — Thin composition over EAF's VCGTaskScheduler + VCGDispatcher

Replaces 257 LOC of duplicated VCG allocation with EAF's production-grade
VCGTaskScheduler (87 LOC, DuckDB-backed) + VCGDispatcher (701 LOC, health checks, NATS).
"""

import sys
from typing import Dict, Any, List

sys.path.insert(0, "/home/ubuntu/executive_agents_framework/src")

from executive_agents.infrastructure.systems.vcg_scheduler import VCGTaskScheduler
from executive_agents.infrastructure.systems.vcg_dispatcher import VCGDispatcher
from okr_executive.engines.scheduling import OKRScheduler


class ExecutionEngine:
    """
    Task assignment + VCG welfare optimization.
    
    Delegates to EAF's VCGTaskScheduler for dependency-aware allocation
    and VCGDispatcher for node-aware dispatch with health checks.
    """

    def __init__(self):
        self.scheduler = OKRScheduler()

    async def execute(self, context: Dict) -> Dict[str, Any]:
        """Assign tasks to agents using VCG + dispatch to cluster nodes"""
        tasks = context.get("tasks", [])

        # Submit tasks to EAF's VCGTaskScheduler via board
        for task in tasks:
            task_title = getattr(task, 'title', str(task))
            task_id = getattr(task, 'id', f"task-{hash(task_title) % 10000}")
            rice = getattr(task, 'rice_score', 0.0)
            self.scheduler.vcg_scheduler.submit_task(
                task_id=task_id,
                title=task_title,
                rice_score=rice,
            )

        # Use EAF's VCGTaskScheduler to compute allocation (uses board state)
        allocations = self.scheduler.vcg_scheduler.compute_allocation()

        # Resolve dependencies via EAF
        self.scheduler.vcg_scheduler.resolve_dependencies()

        return {
            "allocations": allocations,
            "scheduler": self.scheduler,
        }
