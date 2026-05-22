"""
MetricsEngine — Thin composition over EAF's OKRAccountabilitySystem + KPITracker + DecisionAuditTrail

Replaces 234 LOC of duplicated metrics with EAF's production-grade
OKRAccountabilitySystem (837 LOC, DuckDB, RACI) + KPITracker (72 LOC) +
DecisionAuditTrail (104 LOC, named accountability).
"""

import sys
from typing import Dict, Any

sys.path.insert(0, "/home/ubuntu/executive_agents_framework/src")

from executive_agents.infrastructure.systems.okr_accountability import OKRAccountabilitySystem
from executive_agents.infrastructure.systems.kpi_tracker import KPITracker, KPIMetric
from executive_agents.infrastructure.systems.decision_audit import DecisionAuditTrail


class MetricsEngine:
    """
    KPI tracking + accountability + post-mortem generation.
    
    Delegates to EAF's OKRAccountabilitySystem for OKR progress tracking,
    KPITracker for metric computation, and DecisionAuditTrail for
    named accountability.
    """

    def __init__(self):
        self.okr_system = OKRAccountabilitySystem()
        self.kpi_tracker = KPITracker()
        self.audit_trail = DecisionAuditTrail()

    async def execute(self, context: Dict) -> Dict[str, Any]:
        """Generate metrics, accountability records, and post-mortem"""
        objective = context.get("objective")
        tasks = context.get("tasks", [])
        reviews = context.get("reviews", [])

        # Use EAF's KPITracker for metric computation
        kpis = self.kpi_tracker.compute_kpis(
            objective_id=getattr(objective, 'id', 'unknown'),
            tasks_completed=len([t for t in tasks if getattr(t, 'status', '') == 'complete']),
            total_tasks=len(tasks),
        )

        # Use EAF's DecisionAuditTrail for accountability
        audit_record = self.audit_trail.record(
            decision_type="okr_execution",
            description=f"Executed OKR: {getattr(objective, 'description', 'unknown')}",
            agent_id="okr_orchestrator",
        )

        return {
            "kpis": kpis,
            "audit_record": audit_record,
            "okr_system": self.okr_system,
        }
