#!/usr/bin/env python3
"""
Atomic OKR Creation with Goal+Plan Enforcement

Guarantees: OKR only exists if Goal+Plan exist. All-or-nothing semantics.
Rollback on any failure point.
"""

import json
import uuid
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple
from datetime import datetime
from enum import Enum


class OKRCreationError(Exception):
    """Base exception for OKR creation failures"""
    pass


class AtomicityViolation(OKRCreationError):
    """Raised when OKR+Goal+Plan atomicity is violated"""
    pass


class RollbackRequired(OKRCreationError):
    """Raised when transaction must rollback"""
    pass


@dataclass
class KeyResult:
    """Key Result value object"""
    id: str
    title: str
    description: str
    success_criteria: str
    owner: str
    
    def validate(self) -> Tuple[bool, str]:
        """Validate KR completeness"""
        if not self.title:
            return False, "KR title required"
        if not self.owner:
            return False, "KR owner required"
        if not self.success_criteria:
            return False, "Success criteria required"
        return True, ""


@dataclass
class Objective:
    """Objective value object"""
    id: str
    title: str
    description: str
    accountable_agent: str
    key_results: List[KeyResult]
    timeline_start: datetime
    timeline_end: datetime
    
    def validate(self) -> Tuple[bool, str]:
        """Validate OKR completeness"""
        if not self.title:
            return False, "OKR title required"
        if not self.accountable_agent:
            return False, "Accountable agent required"
        if not self.key_results or len(self.key_results) == 0:
            return False, "OKR must have at least 1 KR"
        if len(self.key_results) > 5:
            return False, "OKR must have 5 or fewer KRs"
        
        # Validate all KRs
        for kr in self.key_results:
            valid, msg = kr.validate()
            if not valid:
                return False, f"KR validation failed: {msg}"
        
        if self.timeline_end <= self.timeline_start:
            return False, "Timeline end must be after start"
        
        return True, ""


@dataclass
class Goal:
    """Goal value object (decomposed from OKR KR)"""
    id: str
    parent_okr_id: str
    parent_kr_id: str
    title: str
    description: str
    kpis: List[str]
    acceptance_tests: List[str]
    tier: str  # board/division/department/team/individual
    accountable_agent: str
    
    def validate(self) -> Tuple[bool, str]:
        """Validate Goal completeness"""
        if not self.title:
            return False, "Goal title required"
        if not self.parent_okr_id or not self.parent_kr_id:
            return False, "Goal must link to OKR and KR"
        if not self.kpis or len(self.kpis) == 0:
            return False, "Goal must have at least 1 KPI"
        if not self.acceptance_tests or len(self.acceptance_tests) == 0:
            return False, "Goal must have at least 1 acceptance test"
        return True, ""


@dataclass
class PlanStep:
    """Individual step in a plan"""
    id: str
    title: str
    description: str
    estimated_hours: float
    owner: str
    acceptance_criteria: str


@dataclass
class Plan:
    """Plan value object (decomposed from Goal)"""
    id: str
    parent_goal_id: str
    parent_okr_id: str
    title: str
    description: str
    steps: List[PlanStep]
    estimated_total_hours: float
    accountable_agent: str
    status: str = "proposed"  # proposed → validated → pending_signoff → approved
    
    def validate(self) -> Tuple[bool, str]:
        """Validate Plan completeness"""
        if not self.title:
            return False, "Plan title required"
        if not self.parent_goal_id or not self.parent_okr_id:
            return False, "Plan must link to Goal and OKR"
        if not self.steps or len(self.steps) == 0:
            return False, "Plan must have at least 1 step"
        if self.estimated_total_hours <= 0:
            return False, "Estimated hours must be > 0"
        
        # Validate all steps
        total = 0
        for step in self.steps:
            if not step.title:
                return False, "Step title required"
            if step.estimated_hours <= 0:
                return False, "Step hours must be > 0"
            total += step.estimated_hours
        
        if abs(total - self.estimated_total_hours) > 0.1:
            return False, f"Steps total ({total}h) doesn't match plan total ({self.estimated_total_hours}h)"
        
        return True, ""


class AtomicOKRCreationTransaction:
    """
    Atomic transaction for OKR → Goal → Plan creation.
    
    All-or-nothing semantics:
    - Success: OKR, Goals, Plans all created
    - Failure: Everything rolled back, no partial state
    """
    
    def __init__(self, storage_backend=None):
        """Initialize transaction"""
        self.storage = storage_backend or InMemoryStorage()
        self.transaction_id = str(uuid.uuid4())
        self.created_artifacts = {
            "okr": None,
            "goals": [],
            "plans": []
        }
        self.state = "init"  # init → validating → creating → committing → committed
        self.errors = []
    
    def create_objective_atomic(self, okr: Objective) -> Tuple[str, List[str], List[str]]:
        """
        Atomically create OKR with Goals and Plans.
        
        Returns: (okr_id, goal_ids, plan_ids)
        Raises: OKRCreationError on any failure (with automatic rollback)
        """
        try:
            self.state = "validating"
            
            # Phase 1: Validate OKR
            valid, msg = okr.validate()
            if not valid:
                raise OKRCreationError(f"OKR validation failed: {msg}")
            
            self.state = "creating"
            
            # Phase 2: Create OKR
            okr_id = self._create_okr(okr)
            self.created_artifacts["okr"] = okr_id
            
            # Phase 3: Create Goals (one per KR)
            goal_ids = []
            for kr in okr.key_results:
                goal = self._goal_from_kr(okr, kr)
                valid, msg = goal.validate()
                if not valid:
                    raise AtomicityViolation(f"Goal creation failed: {msg}")
                
                goal_id = self._create_goal(goal)
                goal_ids.append(goal_id)
            
            self.created_artifacts["goals"] = goal_ids
            
            # Phase 4: Create Plans (one per Goal)
            plan_ids = []
            for i, goal_id in enumerate(goal_ids):
                goal = self.storage.get_goal(goal_id)
                plan = self._plan_from_goal(okr, goal)
                valid, msg = plan.validate()
                if not valid:
                    raise AtomicityViolation(f"Plan creation failed: {msg}")
                
                plan_id = self._create_plan(plan)
                plan_ids.append(plan_id)
            
            self.created_artifacts["plans"] = plan_ids
            
            # Phase 5: Commit transaction
            self.state = "committing"
            self._commit()
            self.state = "committed"
            
            return okr_id, goal_ids, plan_ids
        
        except Exception as e:
            # Automatic rollback on any failure
            self._rollback()
            self.errors.append(str(e))
            raise OKRCreationError(f"OKR creation failed (rolled back): {e}")
    
    def _create_okr(self, okr: Objective) -> str:
        """Create OKR artifact"""
        okr_id = f"okr_{uuid.uuid4().hex[:8]}"
        okr.id = okr_id
        self.storage.save_okr(okr)
        return okr_id
    
    def _goal_from_kr(self, okr: Objective, kr: KeyResult) -> Goal:
        """Generate Goal from KR"""
        goal_id = f"g_{uuid.uuid4().hex[:8]}"
        return Goal(
            id=goal_id,
            parent_okr_id=okr.id,
            parent_kr_id=kr.id,
            title=f"Goal: {kr.title}",
            description=kr.description,
            kpis=[kr.success_criteria],
            acceptance_tests=[f"Verify: {kr.success_criteria}"],
            tier="team",
            accountable_agent=kr.owner
        )
    
    def _create_goal(self, goal: Goal) -> str:
        """Create Goal artifact"""
        self.storage.save_goal(goal)
        return goal.id
    
    def _plan_from_goal(self, okr: Objective, goal: Goal) -> Plan:
        """Generate Plan from Goal"""
        plan_id = f"plan_{uuid.uuid4().hex[:8]}"
        
        # Generate placeholder steps (in real system, these come from detailed planning)
        steps = [
            PlanStep(
                id=f"step_{uuid.uuid4().hex[:8]}",
                title=f"Implement {goal.title}",
                description=goal.description,
                estimated_hours=40.0,
                owner=goal.accountable_agent,
                acceptance_criteria=goal.acceptance_tests[0] if goal.acceptance_tests else ""
            )
        ]
        
        return Plan(
            id=plan_id,
            parent_goal_id=goal.id,
            parent_okr_id=goal.parent_okr_id,
            title=f"Plan: {goal.title}",
            description=f"Implementation plan for {goal.title}",
            steps=steps,
            estimated_total_hours=40.0,
            accountable_agent=goal.accountable_agent,
            status="proposed"
        )
    
    def _create_plan(self, plan: Plan) -> str:
        """Create Plan artifact"""
        self.storage.save_plan(plan)
        return plan.id
    
    def _commit(self):
        """Commit transaction (all artifacts persisted)"""
        self.storage.commit(self.transaction_id)
    
    def _rollback(self):
        """Rollback transaction (delete all created artifacts)"""
        if self.created_artifacts["okr"]:
            self.storage.delete_okr(self.created_artifacts["okr"])
        for goal_id in self.created_artifacts["goals"]:
            self.storage.delete_goal(goal_id)
        for plan_id in self.created_artifacts["plans"]:
            self.storage.delete_plan(plan_id)
        self.storage.rollback(self.transaction_id)


class InMemoryStorage:
    """In-memory storage backend (for testing)"""
    
    def __init__(self):
        self.okrs = {}
        self.goals = {}
        self.plans = {}
        self.committed_transactions = []
    
    def save_okr(self, okr: Objective):
        self.okrs[okr.id] = okr
    
    def save_goal(self, goal: Goal):
        self.goals[goal.id] = goal
    
    def save_plan(self, plan: Plan):
        self.plans[plan.id] = plan
    
    def get_goal(self, goal_id: str) -> Goal:
        return self.goals.get(goal_id)
    
    def delete_okr(self, okr_id: str):
        if okr_id in self.okrs:
            del self.okrs[okr_id]
    
    def delete_goal(self, goal_id: str):
        if goal_id in self.goals:
            del self.goals[goal_id]
    
    def delete_plan(self, plan_id: str):
        if plan_id in self.plans:
            del self.plans[plan_id]
    
    def commit(self, transaction_id: str):
        self.committed_transactions.append(transaction_id)
    
    def rollback(self, transaction_id: str):
        pass  # No-op for in-memory


# CLI Interface
def create_objective_cli(okr_spec: dict) -> dict:
    """
    CLI entry point for creating OKR atomically.
    
    Args:
        okr_spec: {
            "title": str,
            "description": str,
            "accountable_agent": str,
            "key_results": [
                {
                    "title": str,
                    "description": str,
                    "success_criteria": str,
                    "owner": str
                }
            ],
            "timeline_start": ISO8601,
            "timeline_end": ISO8601
        }
    
    Returns:
        {
            "status": "success" | "failed",
            "okr_id": str,
            "goal_ids": [str],
            "plan_ids": [str],
            "errors": [str]
        }
    """
    try:
        # Parse input
        krs = [
            KeyResult(
                id=f"kr_{uuid.uuid4().hex[:8]}",
                title=kr["title"],
                description=kr["description"],
                success_criteria=kr["success_criteria"],
                owner=kr["owner"]
            )
            for kr in okr_spec["key_results"]
        ]
        
        okr = Objective(
            id="",  # Will be assigned
            title=okr_spec["title"],
            description=okr_spec["description"],
            accountable_agent=okr_spec["accountable_agent"],
            key_results=krs,
            timeline_start=datetime.fromisoformat(okr_spec["timeline_start"]),
            timeline_end=datetime.fromisoformat(okr_spec["timeline_end"])
        )
        
        # Execute atomic creation
        transaction = AtomicOKRCreationTransaction()
        okr_id, goal_ids, plan_ids = transaction.create_objective_atomic(okr)
        
        return {
            "status": "success",
            "okr_id": okr_id,
            "goal_ids": goal_ids,
            "plan_ids": plan_ids,
            "errors": []
        }
    
    except OKRCreationError as e:
        return {
            "status": "failed",
            "okr_id": None,
            "goal_ids": [],
            "plan_ids": [],
            "errors": [str(e)]
        }


if __name__ == "__main__":
    # Example usage
    okr_spec = {
        "title": "Cognitive Worker Architecture",
        "description": "Implement executive agent cognitive architecture for all dispatch workers",
        "accountable_agent": "demis-hassabis",
        "key_results": [
            {
                "title": "Architecture Integration",
                "description": "Full lifecycle: deliberate → execute → emit",
                "success_criteria": "Zero protocol violations",
                "owner": "demis-hassabis"
            },
            {
                "title": "Protocol Elimination",
                "description": "Event-driven completion via CQRS",
                "success_criteria": "100% CQRS adoption",
                "owner": "demis-hassabis"
            }
        ],
        "timeline_start": "2026-05-26T00:00:00",
        "timeline_end": "2026-05-31T23:59:59"
    }
    
    result = create_objective_cli(okr_spec)
    print(json.dumps(result, indent=2, default=str))
