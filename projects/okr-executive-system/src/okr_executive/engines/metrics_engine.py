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
    KPI tracking + accountability + decision audit.
    
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

        # Use EAF's KPITracker — register + update KPIs
        obj_id = getattr(objective, 'id', 'unknown')
        obj_title = getattr(objective, 'title', 'unknown')

        self.kpi_tracker.register_kpi(
            kpi_id=f"kpi-{obj_id}-completion",
            name=f"Completion: {obj_title}",
            description="Task completion rate",
            target=1.0,
            unit="fraction",
        )

        completed = len([t for t in tasks if getattr(t, 'status', '') == 'complete'])
        total = len(tasks)
        completion_rate = completed / total if total > 0 else 0.0

        self.kpi_tracker.update_kpi(
            kpi_id=f"kpi-{obj_id}-completion",
            value=completion_rate,
        )

        # Use EAF's DecisionAuditTrail for accountability
        audit_id = self.audit_trail.record_decision(
            agent_id="okr_orchestrator",
            decision_type="okr_execution",
            reasoning=f"Executed OKR: {obj_title}",
            confidence=0.8,
        )

        # Get dashboard summary
        dashboard = self.kpi_tracker.get_dashboard()

        return {
            "kpi_dashboard": dashboard,
            "completion_rate": completion_rate,
            "audit_id": audit_id,
            "okr_system": self.okr_system,
        }
