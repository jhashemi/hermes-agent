"""
Phase 1.1: ExecutionEngine Base Class & Core Abstractions

This module provides the foundation for all execution engines.
All engines inherit from ExecutionEngine and implement async execute().
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, TypeVar, Generic
from enum import Enum
from datetime import datetime
import json
from pydantic import BaseModel, Field


# ==================== EVENT MODELS ====================

class EventType(Enum):
    """All event types in the OKR system"""
    OKR_PARSED = "okr.parsed"
    RESEARCH_COMPLETED = "research.completed"
    PLAN_CREATED = "plan.created"
    EXECUTION_STARTED = "execution.started"
    EXECUTION_COMPLETED = "execution.completed"
    REVIEW_COMPLETED = "review.completed"
    METRICS_CALCULATED = "metrics.calculated"


class Event(BaseModel):
    """Base event model - all events inherit from this"""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: EventType
    timestamp: datetime = Field(default_factory=datetime.now)
    source_engine: str
    payload: Dict[str, Any]
    correlation_id: str  # Links events in same OKR lifecycle
    
    class Config:
        arbitrary_types_allowed = True


# ==================== DOMAIN MODELS ====================

class OKRModel(BaseModel):
    """Objective & Key Results model"""
    id: str
    objective: str
    key_results: List[str]
    org_context: Optional[Dict[str, Any]] = None
    timelines: Optional[Dict[str, Any]] = None
    created_at: datetime = Field(default_factory=datetime.now)
    
    class Config:
        arbitrary_types_allowed = True


class GoalModel(BaseModel):
    """Multi-level goal in hierarchy"""
    id: str
    level: int  # 1=Org, 2=Dept, 3=Team, 4=Individual
    objective: str
    parent_goal: Optional[str] = None
    child_goals: List[str] = Field(default_factory=list)
    priority_score: float = 0.0
    assigned_agent: Optional[str] = None
    status: str = "planned"  # planned, in_progress, completed
    
    class Config:
        arbitrary_types_allowed = True


class TaskModel(BaseModel):
    """Executable task with accountability"""
    id: str
    okr_id: str
    goal_id: str
    description: str
    assigned_agent: str
    required_skills: List[str] = Field(default_factory=list)
    estimated_hours: float
    status: str = "pending"
    created_at: datetime = Field(default_factory=datetime.now)
    
    class Config:
        arbitrary_types_allowed = True


# ==================== EXECUTION ENGINE BASE ====================

class ExecutionContext(BaseModel):
    """Context passed through execution pipeline"""
    okr_id: str
    correlation_id: str
    phase: str
    input_data: Dict[str, Any]
    output_data: Dict[str, Any] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)
    
    class Config:
        arbitrary_types_allowed = True


class ExecutionEngine(ABC):
    """
    Base class for all execution engines.
    
    CRITICAL PATTERN:
    - All engines inherit from this
    - All implement async execute(context)
    - All emit events via event_broker
    - All follow dependency injection pattern
    """
    
    def __init__(self, 
                 engine_id: str,
                 event_broker: 'EventBroker',
                 dependencies: Optional[Dict[str, Any]] = None):
        """
        Initialize execution engine.
        
        Args:
            engine_id: Unique identifier for this engine
            event_broker: For publishing events
            dependencies: Other services (Nexus, Voice, etc.)
        """
        self.engine_id = engine_id
        self.event_broker = event_broker
        self.dependencies = dependencies or {}
        self.logger = self._setup_logger()
    
    @abstractmethod
    async def execute(self, context: ExecutionContext) -> ExecutionContext:
        """
        Execute this engine's work.
        
        MUST BE IMPLEMENTED by all subclasses.
        
        Args:
            context: Execution context with input data
            
        Returns:
            Updated context with output data and/or errors
        """
        pass
    
    async def emit_event(self, 
                        event_type: EventType,
                        payload: Dict[str, Any],
                        correlation_id: str) -> None:
        """Emit event through event broker"""
        event = Event(
            type=event_type,
            source_engine=self.engine_id,
            payload=payload,
            correlation_id=correlation_id
        )
        await self.event_broker.emit(event.type.value, event.dict())
    
    def _setup_logger(self):
        """Setup logger for this engine"""
        import logging
        return logging.getLogger(self.engine_id)
    
    async def call_dependency(self, 
                             service_name: str,
                             method_name: str,
                             **kwargs) -> Any:
        """
        Call a dependency service safely.
        
        Pattern for calling Nexus, Voice Twins, etc.
        """
        if service_name not in self.dependencies:
            raise ValueError(f"Dependency {service_name} not found")
        
        service = self.dependencies[service_name]
        method = getattr(service, method_name)
        return await method(**kwargs)


# ==================== EVENT BROKER INTERFACE ====================

class EventBroker(ABC):
    """Abstract event broker - implemented by NATS adapter"""
    
    @abstractmethod
    async def emit(self, topic: str, event_data: Dict[str, Any]) -> None:
        """Publish event to topic"""
        pass
    
    @abstractmethod
    async def subscribe(self, topic: str, handler: callable) -> None:
        """Subscribe to topic with event handler"""
        pass


# ==================== REPOSITORY INTERFACE ====================

class Repository(ABC):
    """Abstract repository - implemented by DuckDB adapter"""
    
    @abstractmethod
    async def store(self, collection: str, data: Dict[str, Any]) -> str:
        """Store data and return ID"""
        pass
    
    @abstractmethod
    async def query(self, collection: str, **filters) -> List[Dict[str, Any]]:
        """Query data from collection"""
        pass
    
    @abstractmethod
    async def update(self, collection: str, id: str, data: Dict[str, Any]) -> None:
        """Update existing data"""
        pass


# ==================== EXECUTION RESULT ====================

class ExecutionResult(BaseModel):
    """Result of engine execution"""
    success: bool
    engine_id: str
    correlation_id: str
    output_data: Dict[str, Any] = Field(default_factory=dict)
    errors: List[str] = Field(default_factory=list)
    events_emitted: List[str] = Field(default_factory=list)
    duration_seconds: float = 0.0
    
    class Config:
        arbitrary_types_allowed = True


# ==================== EXPORTS ====================

__all__ = [
    'ExecutionEngine',
    'ExecutionContext',
    'ExecutionResult',
    'Event',
    'EventType',
    'EventBroker',
    'Repository',
    'OKRModel',
    'GoalModel',
    'TaskModel',
]


import uuid
