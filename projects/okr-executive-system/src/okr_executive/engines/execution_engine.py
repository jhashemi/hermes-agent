"""
ExecutionEngine — Thin composition over EAF's VCGTaskScheduler + VCGDispatcher

Replaces 257 LOC of duplicated VCG allocation with EAF's production-grade
VCGTaskScheduler (87 LOC, DuckDB-backed) + VCGDispatcher (701 LOC, health checks, NATS).
"""

import sys
from typing import Dict, Any, List

sys.path.insert(0, "/home/ubuntu/executive_agents_framework/src")

from executive_agents.infrastructure.systems.vcg_scheduler import VCGTaskScheduler
from executive_agents.infrastructure.systems.vcg_dispatcher import (
    VCGDispatcher,
    NodeRegistry,
)
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

        # Use EAF's VCGTaskScheduler for allocation
        allocations = self.scheduler.vcg_scheduler.compute_allocation(
            [{"id": t.id, "description": t.description} for t in tasks]
        ) if tasks else []

        # Use EAF's VCGDispatcher for node dispatch
        dispatches = []
        for task in tasks:
            dispatch = self.scheduler.dispatcher.dispatch_task(
                {"id": task.id, "description": task.description}
            )
            dispatches.append(dispatch)

        return {
            "allocations": allocations,
            "dispatches": dispatches,
            "scheduler": self.scheduler,
        }
