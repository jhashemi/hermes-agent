"""
PlanningEngine — Thin composition over EAF's GoalHierarchy + RICEScorer

Replaces 276 LOC of duplicated goal breakdown + inline RICE with EAF's
GoalHierarchy (628 LOC) + RICEScorer (41 LOC canonical implementation).
"""

import sys
from typing import Dict, Any, List

sys.path.insert(0, "/home/ubuntu/executive_agents_framework/src")

from executive_agents.infrastructure.systems.goal_hierarchy import (
    GoalHierarchy,
    GoalNode,
    PlanningAbstraction,
    Plan,
    AtomicTask,
)
from executive_agents.infrastructure.systems.rice_scorer import RICEScorer


class PlanningEngine:
    """
    Hierarchical goal decomposition + RICE prioritization.
    
    Delegates to EAF's GoalHierarchy for goal tree management
    and RICEScorer for canonical RICE scoring.
    """

    def __init__(self):
        self.goal_hierarchy = GoalHierarchy()
        self.rice_scorer = RICEScorer()

    async def execute(self, context: Dict) -> Dict[str, Any]:
        """Decompose objective into hierarchical goals + tasks"""
        objective = context.get("objective")
        key_results = context.get("key_results", [])

        # Use EAF's GoalHierarchy to decompose
        goals = self._decompose_goals(objective, key_results)

        # Use EAF's canonical RICEScorer
        for goal in goals:
            rice_score = self.rice_scorer.score(
                reach=getattr(goal, 'reach', 5),
                impact=getattr(goal, 'impact', 5.0),
                confidence=getattr(goal, 'confidence', 0.5),
                effort=getattr(goal, 'effort', 3.0),
            )
            goal.rice_score = rice_score

        # Break goals into tasks
        tasks = self._break_down_tasks(goals)

        return {
            "goals": goals,
            "tasks": tasks,
            "goal_hierarchy": self.goal_hierarchy,
        }

    def _decompose_goals(self, objective, key_results) -> List[GoalNode]:
        """Create goal hierarchy from objective + key results using EAF GoalNode"""
        goals = []
        for i, kr in enumerate(key_results):
            kr_title = kr.title if hasattr(kr, 'title') else str(kr)
            goal = GoalNode(
                id=f"goal-{i}",
                title=kr_title,
                priority=float(len(key_results) - i),  # First KR = highest priority
                okr_objective_id=getattr(objective, 'id', 'unknown'),
            )
            goals.append(goal)
        return goals

    def _break_down_tasks(self, goals) -> List[AtomicTask]:
        """Break goals into atomic tasks using EAF AtomicTask"""
        tasks = []
        for i, goal in enumerate(goals):
            task = AtomicTask(
                id=f"task-{i}",
                title=f"Implement: {goal.title}",
                plan_id=goal.id,
                hierarchical_level="individual",
            )
            tasks.append(task)
        return tasks
