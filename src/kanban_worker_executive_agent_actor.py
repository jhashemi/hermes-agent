#!/usr/bin/env python3
"""
Kanban Worker - Executive Agent Actor with CQRS Event Emission

KanbanWorkerActor extends ExecutiveAgentActor to add event-driven completion.
Workers are full agents with cognitive capabilities, not just task runners.

Architecture:
1. ExecutiveAgentActor: Base agent with memory, reasoning, deliberation
2. KanbanWorkerActor extends it: Adds CQRS event emission for task completion
3. Worker executes work() with full executive capabilities
4. Worker emits TaskCompleted event to NATS (not kanban_complete call)
5. Event subscriber marks kanban task done

This ensures workers have:
✅ Cognitive architecture (clear thought, memory, reasoning)
✅ Autonomous decision-making (deliberation, consensus)
✅ Event-driven completion (CQRS, no protocol violations)
✅ Full agent lifecycle (init → deliberate → execute → emit → complete)
"""

import os
import sys
import json
from typing import Any, Dict, Optional, Tuple
from dataclasses import dataclass, asdict
from datetime import datetime
from abc import ABC, abstractmethod

# These would be real imports in production
# from executive_agents_framework.agents.executive_agent_actor import ExecutiveAgentActor
# from executive_agents_framework.events import Event
# from executive_agents_framework.memory import TemporalTrace


@dataclass
class TaskCompletedEvent:
    """Event: Task work completed successfully"""
    task_id: str
    agent_id: str  # Which agent completed it
    summary: str
    metadata: Dict[str, Any]
    reasoning: str  # Why we believe it's complete
    completed_at: str  # ISO timestamp
    
    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class TaskBlockedEvent:
    """Event: Task blocked - needs human intervention"""
    task_id: str
    agent_id: str  # Which agent is blocked
    reason: str
    blocked_at: str  # ISO timestamp
    
    def to_json(self) -> str:
        return json.dumps(asdict(self))


class ExecutiveAgentActor(ABC):
    """
    Base class for all executive agents.
    
    Provides:
    - Cognitive architecture (memory, reasoning)
    - Deliberation and consensus
    - Event emission
    - Autonomous decision-making
    """
    
    def __init__(self, agent_id: str, task_id: str = None):
        self.agent_id = agent_id
        self.task_id = task_id
        self.memory = {}  # Would be TemporalTrace in production
        self.reasoning = []  # Decision audit trail
        
    @abstractmethod
    def deliberate(self) -> Dict[str, Any]:
        """Agent deliberation - reasoning about what to do"""
        pass
    
    @abstractmethod
    def execute(self) -> Tuple[bool, str, Dict[str, Any]]:
        """Execute work - return (success, summary, metadata)"""
        pass
    
    def emit_event(self, event_type: str, event_data: Dict[str, Any]) -> int:
        """Emit event to NATS for external handling"""
        print(f"📤 EMIT: {event_type}")
        print(f"   Agent: {self.agent_id}")
        print(f"   Event: {json.dumps(event_data)}")
        return 0


class KanbanWorkerActor(ExecutiveAgentActor):
    """
    Kanban worker that is also an executive agent.
    
    Combines:
    - Executive agent capabilities (deliberation, memory, reasoning)
    - Task execution (work_fn)
    - CQRS event emission (no kanban_complete calls)
    
    Workflow:
    1. Deliberate: Reason about how to approach the task
    2. Execute: Do the actual work
    3. Emit: Send TaskCompleted event (not kanban_complete call)
    4. Exit: Return appropriate exit code
    """
    
    def __init__(self, task_id: str, agent_id: str = None, nats_connection=None):
        super().__init__(agent_id or "kanban-worker", task_id)
        self.nats = nats_connection
        self.work_fn = None
        
    def set_work(self, work_fn):
        """Set the work function to execute"""
        self.work_fn = work_fn
        return self
    
    def deliberate(self) -> Dict[str, Any]:
        """
        Agent reasoning: Think through task approach
        
        In production, this would:
        - Load task specification
        - Consult prior memory
        - Think through risks/constraints
        - Generate execution plan
        """
        reasoning = {
            "task_id": self.task_id,
            "approach": "Execute work_fn and emit completion event",
            "risks": ["work_fn may throw exception"],
            "plan": [
                "1. Execute work_fn()",
                "2. Handle result (success/failure)",
                "3. Emit appropriate event",
                "4. Return exit code"
            ]
        }
        self.reasoning.append(reasoning)
        return reasoning
    
    def execute(self) -> Tuple[bool, str, Dict[str, Any]]:
        """
        Execute the work function.
        
        Returns: (success, summary, metadata)
        """
        if not self.work_fn:
            return (False, "No work function provided", {})
        
        try:
            success, summary, metadata = self.work_fn()
            return (success, summary, metadata)
        except Exception as e:
            return (False, f"Exception: {type(e).__name__}: {str(e)}", {})
    
    def emit_completed(self, summary: str, metadata: Dict[str, Any] = None) -> int:
        """Emit TaskCompleted event"""
        event = TaskCompletedEvent(
            task_id=self.task_id,
            agent_id=self.agent_id,
            summary=summary,
            metadata=metadata or {},
            reasoning=json.dumps(self.reasoning[-1] if self.reasoning else {}),
            completed_at=datetime.utcnow().isoformat()
        )
        
        print(f"✅ TaskCompleted Event:")
        print(f"   Task: {self.task_id}")
        print(f"   Agent: {self.agent_id}")
        print(f"   Summary: {summary}")
        print(f"   Event JSON: {event.to_json()}")
        
        # In production: NATS publish
        # subject = f"exec.agents.kanban.{self.task_id}.completed"
        # self.nats.publish(subject, event.to_json())
        
        return 0  # Success
    
    def emit_blocked(self, reason: str) -> int:
        """Emit TaskBlocked event"""
        event = TaskBlockedEvent(
            task_id=self.task_id,
            agent_id=self.agent_id,
            reason=reason,
            blocked_at=datetime.utcnow().isoformat()
        )
        
        print(f"🚫 TaskBlocked Event:")
        print(f"   Task: {self.task_id}")
        print(f"   Agent: {self.agent_id}")
        print(f"   Reason: {reason}")
        print(f"   Event JSON: {event.to_json()}")
        
        # In production: NATS publish
        # subject = f"exec.agents.kanban.{self.task_id}.blocked"
        # self.nats.publish(subject, event.to_json())
        
        return 1  # Failure
    
    def run(self) -> int:
        """
        Full agent lifecycle:
        1. Deliberate (reason about task)
        2. Execute (do work)
        3. Emit (send event)
        4. Return exit code
        """
        print(f"\n{'='*80}")
        print(f"KanbanWorkerActor Lifecycle: {self.task_id}")
        print(f"{'='*80}\n")
        
        # Phase 1: Deliberation
        print("📋 PHASE 1: DELIBERATION")
        deliberation = self.deliberate()
        print(f"  Reasoning: {json.dumps(deliberation, indent=2)}\n")
        
        # Phase 2: Execution
        print("⚙️  PHASE 2: EXECUTION")
        success, summary, metadata = self.execute()
        print(f"  Success: {success}")
        print(f"  Summary: {summary}\n")
        
        # Phase 3: Emission
        print("📤 PHASE 3: EMISSION")
        if success:
            exit_code = self.emit_completed(summary, metadata)
        else:
            exit_code = self.emit_blocked(summary)
        
        print(f"\n{'='*80}")
        print(f"Exit Code: {exit_code}")
        print(f"{'='*80}\n")
        
        return exit_code


# Event Handler (would run on gateway)
class KanbanEventHandler:
    """
    Subscribes to kanban task events and updates board.
    
    Receives TaskCompleted/TaskBlocked events from workers and marks tasks done/blocked.
    """
    
    def handle_task_completed(self, subject: str, event_json: str):
        """Handle TaskCompleted event - mark task done"""
        event = TaskCompletedEvent(**json.loads(event_json))
        print(f"✅ EventHandler: TaskCompleted")
        print(f"   Task: {event.task_id}")
        print(f"   Agent: {event.agent_id}")
        print(f"   Summary: {event.summary}")
        print(f"   (Would call: hermes kanban complete {event.task_id})")
    
    def handle_task_blocked(self, subject: str, event_json: str):
        """Handle TaskBlocked event - mark task blocked"""
        event = TaskBlockedEvent(**json.loads(event_json))
        print(f"🚫 EventHandler: TaskBlocked")
        print(f"   Task: {event.task_id}")
        print(f"   Agent: {event.agent_id}")
        print(f"   Reason: {event.reason}")
        print(f"   (Would call: hermes kanban block {event.task_id})")


# Example: Full agent worker
def example_executive_agent_worker(task_id: str):
    """Example: Executive agent as kanban worker"""
    
    # Create agent
    agent = KanbanWorkerActor(
        task_id=task_id,
        agent_id="margaret_hamilton"
    )
    
    # Define work
    def work():
        """Do the actual work (with access to agent's reasoning/memory)"""
        print("    📝 Generating API documentation...")
        
        # In real implementation, agent would:
        # - Access its memory (prior patterns, standards)
        # - Reason about documentation structure
        # - Generate with full cognitive context
        
        return (
            True,
            "API Documentation complete - 12 hours of work",
            {
                "files_created": 5,
                "lines_written": 2400,
                "standards_applied": ["OpenAPI 3.0", "Google Style"],
                "coverage": "95%"
            }
        )
    
    # Set work and run full lifecycle
    agent.set_work(work)
    exit_code = agent.run()
    
    return exit_code


if __name__ == "__main__":
    task_id = os.getenv("HERMES_TASK_ID", "t_doc_api_12345")
    agent_id = os.getenv("HERMES_AGENT_ID", "margaret_hamilton")
    
    print("\n" + "="*80)
    print("KanbanWorkerActor as ExecutiveAgentActor")
    print("Full agent lifecycle with event-driven completion")
    print("="*80 + "\n")
    
    # Run example
    exit_code = example_executive_agent_worker(task_id)
    
    sys.exit(exit_code)
