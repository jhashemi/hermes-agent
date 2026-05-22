"""
PHASE 2: PlanningEngine - Hierarchical goal breakdown with RICE prioritization

Converts research findings + OKR into hierarchical goals with RICE scoring.
"""

from okr_executive.engines.base import (
    ExecutionEngine,
    ExecutionContext,
    EventType,
)
from okr_executive.domain.models import Goal, Task
from typing import List, Dict, Any, Tuple
from datetime import datetime, timedelta
import logging


class PlanningEngine(ExecutionEngine):
    """
    Convert OKR + research findings into hierarchical goal plan.
    
    Implements:
    - Goal hierarchy (org → dept → team → individual)
    - RICE scoring (reach × impact / (confidence × effort))
    - Hierarchical multipliers (higher levels = more weight)
    - Dependency tracking
    - Timeline estimation
    """
    
    def __init__(self, engine_id: str, event_broker, dependencies: Dict[str, Any]):
        super().__init__(engine_id, event_broker, dependencies)
        self.logger = logging.getLogger(self.__class__.__name__)
    
    async def execute(self, context: ExecutionContext) -> ExecutionContext:
        """
        Execute planning phase: OKR + research → hierarchical goals
        
        INPUT:
            context.input_data = {
                'okr': OKR dict,
                'research_findings': Dict,
                'recommended_approaches': List,
            }
        
        OUTPUT:
            context.output_data = {
                'goal_hierarchy': List[Goal],
                'task_breakdown': List[Task],
                'rice_scores': Dict,
                'timeline': Dict,
                'dependencies': Dict,
            }
        """
        
        try:
            okr = context.input_data.get('okr', {})
            research_findings = context.input_data.get('research_findings', {})
            recommended_approaches = context.input_data.get('recommended_approaches', [])
            org_context = context.input_data.get('org_context', {})
            
            okr_id = okr.get('id')
            objective = okr.get('objective', '')
            key_results = okr.get('key_results', [])
            
            self.logger.info(f"[{self.engine_id}] Planning OKR: {objective[:50]}...")
            
            # Step 1: Create goal hierarchy
            goal_hierarchy = await self._create_goal_hierarchy(
                okr_id=okr_id,
                objective=objective,
                key_results=key_results,
                org_context=org_context,
                research_findings=research_findings
            )
            
            if not goal_hierarchy:
                context.errors.append("Failed to create goal hierarchy")
                return context
            
            # Step 2: Break down into tasks
            task_breakdown = await self._break_down_tasks(
                goals=goal_hierarchy,
                approaches=recommended_approaches
            )
            
            # Step 3: Calculate RICE scores
            rice_scores = self._calculate_rice_scores(
                goals=goal_hierarchy,
                tasks=task_breakdown
            )
            
            # Step 4: Estimate timeline with dependencies
            timeline, dependencies = await self._estimate_timeline(
                goals=goal_hierarchy,
                tasks=task_breakdown,
                research=research_findings
            )
            
            # Step 5: Store output
            context.output_data = {
                'goal_hierarchy': [g.dict() for g in goal_hierarchy],
                'task_breakdown': [t.dict() for t in task_breakdown],
                'rice_scores': rice_scores,
                'timeline': timeline,
                'dependencies': dependencies,
                'plan_created_at': datetime.now().isoformat(),
            }
            
            # Step 6: Emit event
            event_payload = {
                'okr_id': okr_id,
                'num_goals': len(goal_hierarchy),
                'num_tasks': len(task_breakdown),
                'rice_scores': rice_scores,
            }
            
            await self.emit_event(
                EventType.PLAN_CREATED,
                event_payload,
                context.correlation_id
            )
            
            self.logger.info(f"[{self.engine_id}] ✓ Plan created: {len(goal_hierarchy)} goals, {len(task_breakdown)} tasks")
            
            return context
            
        except Exception as e:
            context.errors.append(str(e))
            self.logger.error(f"[{self.engine_id}] Error: {e}")
            return context
    
    async def _create_goal_hierarchy(self,
                                    okr_id: str,
                                    objective: str,
                                    key_results: List[str],
                                    org_context: Dict,
                                    research_findings: Dict) -> List[Goal]:
        """Create hierarchical goal breakdown"""
        
        goals = []
        
        # Level 1: Organization goal (from objective)
        org_goal = Goal(
            okr_id=okr_id,
            level=1,  # Organization
            description=objective,
            parent_goal=None,
            assigned_agent="org",
            start_date=datetime.now(),
            due_date=datetime.now() + timedelta(weeks=4),
            estimated_hours=40,
        )
        goals.append(org_goal)
        
        # Level 2: Department goals (from key results)
        for i, kr in enumerate(key_results[:5]):  # Top 5 KRs
            dept_goal = Goal(
                okr_id=okr_id,
                level=2,  # Department
                description=kr,
                parent_goal=org_goal.id,
                assigned_agent=f"dept-{i}",
                start_date=datetime.now(),
                due_date=datetime.now() + timedelta(weeks=3),
                estimated_hours=8,
            )
            goals.append(dept_goal)
        
        # Level 3: Team goals (decomposed from dept goals)
        for dept_goal in goals[1:]:
            team_goal = Goal(
                okr_id=okr_id,
                level=3,  # Team
                description=f"Deliver: {dept_goal.description[:40]}",
                parent_goal=dept_goal.id,
                assigned_agent=f"team-{i}",
                start_date=datetime.now(),
                due_date=datetime.now() + timedelta(weeks=2),
                estimated_hours=4,
            )
            goals.append(team_goal)
        
        return goals
    
    async def _break_down_tasks(self,
                               goals: List[Goal],
                               approaches: List[Dict]) -> List[Task]:
        """Break goals down into executable tasks"""
        
        tasks = []
        
        for goal in goals:
            if goal.level == 3:  # Team level goals
                # Create 2-3 tasks per team goal
                for i in range(2):
                    task = Task(
                        okr_id=goal.okr_id,
                        goal_id=goal.id,
                        description=f"Execute: {goal.description[:40]}",
                        assigned_agent=goal.assigned_agent or "team-agent",
                        assigned_team=goal.assigned_team,
                        estimated_hours=4 * (i + 1),
                        timeline_start=goal.start_date or datetime.now(),
                        timeline_end=goal.due_date or datetime.now() + timedelta(days=7),
                    )
                    tasks.append(task)
        
        return tasks
    
    def _calculate_rice_scores(self,
                              goals: List[Goal],
                              tasks: List[Task]) -> Dict[str, float]:
        """
        Calculate RICE scores: reach × impact / (confidence × effort)
        
        With hierarchical multipliers:
        - Level 1 (org): 3x multiplier (strategic)
        - Level 2 (dept): 2x multiplier
        - Level 3 (team): 1x multiplier
        - Level 4 (individual): 0.5x multiplier
        """
        
        rice_scores = {}
        level_multipliers = {
            1: 3.0,  # organization
            2: 2.0,  # department
            3: 1.0,  # team
            4: 0.5,  # individual
        }
        
        for goal in goals:
            # Base RICE: reach=10, impact=10, confidence=0.8, effort=8
            reach = 10
            impact = 10
            confidence = 0.8
            effort = 8
            
            base_rice = (reach * impact) / (confidence * effort)
            multiplier = level_multipliers.get(goal.level, 1.0)
            
            rice_scores[goal.id] = base_rice * multiplier
        
        return rice_scores
    
    async def _estimate_timeline(self,
                                goals: List[Goal],
                                tasks: List[Task],
                                research: Dict) -> Tuple[Dict, Dict]:
        """Estimate timeline and track dependencies"""
        
        timeline = {
            'total_weeks': 4,
            'phases': [
                {'week': 1, 'milestone': 'Planning + Setup'},
                {'week': 2, 'milestone': 'Implementation Phase 1'},
                {'week': 3, 'milestone': 'Testing + Review'},
                {'week': 4, 'milestone': 'Deployment'},
            ],
            'estimated_hours': sum(t.estimated_hours for t in tasks),
        }
        
        dependencies = {
            'sequential': [
                {'phase': 'planning', 'depends_on': []},
                {'phase': 'implementation', 'depends_on': ['planning']},
                {'phase': 'testing', 'depends_on': ['implementation']},
                {'phase': 'deployment', 'depends_on': ['testing']},
            ],
        }
        
        return timeline, dependencies


# ==================== EXPORTS ====================

__all__ = ['PlanningEngine']
