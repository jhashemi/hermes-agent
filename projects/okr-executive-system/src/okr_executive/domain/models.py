"""
Phase 1.2: Domain Models & Events

Core domain models for OKR executive system.
All models use Pydantic for validation.
"""

from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional
from datetime import datetime
from enum import Enum
import uuid


# ==================== OKR DOMAIN ====================

class OKR(BaseModel):
    """Objective & Key Results"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    objective: str
    key_results: List[str]
    org_context: Optional[Dict[str, Any]] = None
    assigned_team: Optional[str] = None
    assigned_agent: Optional[str] = None
    status: str = "pending"  # pending, in_progress, completed
    created_at: datetime = Field(default_factory=datetime.now)
    due_date: Optional[datetime] = None
    
    class Config:
        arbitrary_types_allowed = True


# ==================== GOAL HIERARCHY ====================

class Goal(BaseModel):
    """Multi-level goal in org hierarchy"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    okr_id: str
    level: int  # 1=Org, 2=Dept, 3=Team, 4=Individual
    description: str
    parent_goal: Optional[str] = None
    child_goals: List[str] = Field(default_factory=list)
    
    # Prioritization
    rice_reach: float = 0.0
    rice_impact: float = 0.0
    rice_confidence: float = 0.0
    rice_effort: float = 0.0
    priority_score: float = 0.0
    
    # Accountability
    assigned_agent: Optional[str] = None
    assigned_team: Optional[str] = None
    
    # Timeline
    start_date: Optional[datetime] = None
    due_date: Optional[datetime] = None
    estimated_hours: float = 0.0
    
    # Status
    status: str = "planned"  # planned, in_progress, at_risk, completed
    completion_percentage: float = 0.0
    
    class Config:
        arbitrary_types_allowed = True


# ==================== TASK EXECUTION ====================

class Task(BaseModel):
    """Executable task"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    okr_id: str
    goal_id: str
    description: str
    
    # Assignment
    assigned_agent: str  # ExecutiveAgentTwin ID
    assigned_team: Optional[str] = None
    
    # Requirements
    required_skills: List[str] = Field(default_factory=list)
    cognitive_complexity: str = "medium"  # simple, medium, complex, expert
    
    # Effort
    estimated_hours: float
    timeline_start: datetime
    timeline_end: datetime
    
    # Status
    status: str = "pending"  # pending, assigned, in_progress, blocked, completed
    actual_hours: Optional[float] = None
    completion_percentage: float = 0.0
    
    # Tracking
    created_at: datetime = Field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    
    class Config:
        arbitrary_types_allowed = True


# ==================== EVENT MODELS ====================

class OKRParsedEvent(BaseModel):
    """Emitted when OKREngine parses OKR"""
    okr_id: str
    objective: str
    key_results: List[str]
    org_context: Dict[str, Any]
    parsed_at: datetime = Field(default_factory=datetime.now)


class ResearchCompletedEvent(BaseModel):
    """Emitted when ResearchEngine completes"""
    okr_id: str
    research_findings: Dict[str, Any]
    recommended_approaches: List[Dict[str, Any]]
    confidence_scores: Dict[str, float]
    completed_at: datetime = Field(default_factory=datetime.now)


class PlanCreatedEvent(BaseModel):
    """Emitted when PlanningEngine creates plan"""
    okr_id: str
    goals: List[Goal]
    timeline_start: datetime
    timeline_end: datetime
    accountability_map: Dict[str, List[str]]  # agent_id → task_ids
    dependencies: Dict[str, List[str]]  # task_id → dependent_task_ids
    created_at: datetime = Field(default_factory=datetime.now)


class ExecutionStartedEvent(BaseModel):
    """Emitted when ExecutionEngine starts tasks"""
    okr_id: str
    tasks: List[Task]
    agent_assignments: Dict[str, List[str]]  # agent_id → task_ids
    started_at: datetime = Field(default_factory=datetime.now)


class ExecutionCompletedEvent(BaseModel):
    """Emitted when ExecutionEngine completes tasks"""
    okr_id: str
    completion_percentage: float
    artifacts: List[Dict[str, Any]]  # Code, docs, etc.
    metrics: Dict[str, Any]
    completed_at: datetime = Field(default_factory=datetime.now)


class ReviewCompletedEvent(BaseModel):
    """Emitted when ReviewEngine completes review"""
    okr_id: str
    pr_url: str
    pr_number: int
    review_summary: str
    recommendations: List[str]
    reviewed_at: datetime = Field(default_factory=datetime.now)


class MetricsCalculatedEvent(BaseModel):
    """Emitted when MetricsEngine calculates metrics"""
    okr_id: str
    completion_percentage: float
    quality_score: float
    timeline_adherence: float
    cost_incurred: float
    agent_performance: Dict[str, float]
    post_mortem: Optional[Dict[str, Any]] = None
    calculated_at: datetime = Field(default_factory=datetime.now)


# ==================== METRIC MODELS ====================

class MetricSnapshot(BaseModel):
    """Metrics snapshot for OKR"""
    okr_id: str
    completion_percentage: float
    quality_score: float
    timeline_adherence: float
    cost_incurred: float
    agent_ratings: Dict[str, float]
    timestamp: datetime = Field(default_factory=datetime.now)


class PostMortem(BaseModel):
    """Post-mortem analysis"""
    okr_id: str
    assigned_agent: str
    completion_percentage: float
    quality_score: float
    
    successes: List[str]
    failures: List[str]
    root_causes: List[str]
    
    improvements: List[str]
    lessons_learned: List[str]
    
    agent_rating_update: float
    team_feedback: str
    
    created_at: datetime = Field(default_factory=datetime.now)


# ==================== ACCOUNTABILITY ====================

class AccountabilityRecord(BaseModel):
    """Accountability record for OKR"""
    okr_id: str
    assigned_agent: str
    assigned_team: Optional[str] = None
    
    start_date: datetime
    due_date: datetime
    completed_date: Optional[datetime] = None
    
    completion_percentage: float
    quality_score: float
    timeline_adherence: float
    cost_incurred: float
    
    status: str = "in_progress"  # in_progress, at_risk, completed, failed
    post_mortem: Optional[PostMortem] = None
    
    created_at: datetime = Field(default_factory=datetime.now)
    
    class Config:
        arbitrary_types_allowed = True


# ==================== EXPORTS ====================

__all__ = [
    'OKR',
    'Goal',
    'Task',
    'OKRParsedEvent',
    'ResearchCompletedEvent',
    'PlanCreatedEvent',
    'ExecutionStartedEvent',
    'ExecutionCompletedEvent',
    'ReviewCompletedEvent',
    'MetricsCalculatedEvent',
    'MetricSnapshot',
    'PostMortem',
    'AccountabilityRecord',
]
