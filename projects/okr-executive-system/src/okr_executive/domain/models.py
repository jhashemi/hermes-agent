"""
OKR Executive Domain — Thin bridge to executive-agents-framework

All domain types are imported from EAF's canonical modules.
Only OKR-specific types with NO EAF equivalent are defined here.

RULE: If EAF has it, import it. Never redefine.
"""

import sys
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any
from datetime import datetime

# ─── EAF sys.path ────────────────────────────────────────────────────────
sys.path.insert(0, "/home/ubuntu/executive_agents_framework/src")

# ─── EAF Imports (canonical types — do NOT redefine) ────────────────────

# shared_types.py
from executive_agents.infrastructure.systems.shared_types import (
    GoalNode,
    KanbanCard,
    ErrorReport,
    VoteResult,
    AccountableAgent,
    SeverityLevel,
)

# goal_hierarchy.py (AtomicTask lives here, not in shared_types)
from executive_agents.infrastructure.systems.goal_hierarchy import (
    AtomicTask,
    GoalHierarchy,
    PlanningAbstraction,
    Plan,
    ReasoningStep,
)

# okr_accountability.py (Objective, KeyResult, PostMortem live here)
from executive_agents.infrastructure.systems.okr_accountability import (
    Objective,
    KeyResult,
    PostMortemEntry,
    PostMortem,
    OKRAccountabilitySystem,
    Organization,
    Team,
    KPI,
)

# kanban_board.py (TaskAllocation lives here)
from executive_agents.infrastructure.systems.kanban_board import (
    TaskAllocation,
    DuckDBKanbanBoard,
    AgentSpec,
)

# rice_scorer.py
from executive_agents.infrastructure.systems.rice_scorer import RICEScorer

# kpi_tracker.py
from executive_agents.infrastructure.systems.kpi_tracker import KPITracker, KPIMetric

# decision_audit.py
from executive_agents.infrastructure.systems.decision_audit import DecisionAuditTrail


# ─── OKR-Specific Extensions (genuinely novel, no EAF equivalent) ───────

@dataclass
class OKRInput:
    """Raw OKR text input — EAF uses Objective/KeyResult objects directly"""
    raw_text: str
    objective: str = ""
    key_results: List[str] = field(default_factory=list)
    parsed_at: datetime = field(default_factory=datetime.now)


@dataclass
class GitHubIssueRef:
    """GitHub issue reference — EAF uses internal KanbanCard, not GitHub Issues"""
    number: int
    title: str
    body: str = ""
    dependencies: List[int] = field(default_factory=list)
    url: str = ""


# ─── Re-exports ──────────────────────────────────────────────────────────
__all__ = [
    # From EAF (canonical — never redefine)
    "GoalNode", "KanbanCard", "ErrorReport", "VoteResult",
    "AccountableAgent", "SeverityLevel",
    "AtomicTask", "GoalHierarchy", "PlanningAbstraction", "Plan", "ReasoningStep",
    "Objective", "KeyResult", "PostMortemEntry", "PostMortem",
    "OKRAccountabilitySystem", "Organization", "Team", "KPI",
    "TaskAllocation", "DuckDBKanbanBoard", "AgentSpec",
    "RICEScorer", "KPITracker", "KPIMetric", "DecisionAuditTrail",
    # OKR-specific (novel)
    "OKRInput", "GitHubIssueRef",
]
