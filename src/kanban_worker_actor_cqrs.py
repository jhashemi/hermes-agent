#!/usr/bin/env python3
"""
Kanban Worker - Actor Model with CQRS Event Emission

Workers emit TaskCompleted events instead of calling kanban_complete() directly.
This prevents protocol violations and decouples worker logic from kanban completion.

Pattern:
1. Worker aggregate: ExecutiveAgentAggregate (keyed by task_id)
2. Worker executes work() method
3. Worker emits TaskCompleted event to NATS: exec.agents.kanban.{task_id}.completed
4. Event subscriber (KanbanEventHandler) subscribes to * and marks task done
5. No protocol violation possible - event-driven

This is the proper Actor-ES-CQRS pattern for distributed systems.
"""

import os
import sys
import json
from typing import Any, Dict, Optional
from dataclasses import dataclass, asdict
from datetime import datetime

# NATS would normally be imported here
# from nats_connector import nats_client


@dataclass
class TaskCompletedEvent:
    """Event: Task work completed successfully"""
    task_id: str
    summary: str
    metadata: Dict[str, Any]
    completed_at: str  # ISO timestamp
    
    def to_json(self) -> str:
        return json.dumps(asdict(self))


@dataclass
class TaskBlockedEvent:
    """Event: Task blocked - needs human intervention"""
    task_id: str
    reason: str
    blocked_at: str  # ISO timestamp
    
    def to_json(self) -> str:
        return json.dumps(asdict(self))


class KanbanWorkerActor:
    """
    Actor-based kanban worker using CQRS event model.
    
    Instead of calling kanban_complete(), workers emit events.
    Event subscribers handle kanban state transitions.
    
    This prevents protocol violations and keeps worker logic pure.
    """
    
    def __init__(self, task_id: str, nats_connection=None):
        self.task_id = task_id
        self.nats = nats_connection  # Would be real NATS in production
        
    def emit_completed(self, summary: str, metadata: Dict[str, Any] = None) -> int:
        """Emit TaskCompleted event - triggers kanban completion via subscriber"""
        
        event = TaskCompletedEvent(
            task_id=self.task_id,
            summary=summary,
            metadata=metadata or {},
            completed_at=datetime.utcnow().isoformat()
        )
        
        # In production: NATS publish
        # subject = f"exec.agents.kanban.{self.task_id}.completed"
        # self.nats.publish(subject, event.to_json())
        
        # For now: simulate with print
        print(f"📤 EMIT: TaskCompleted")
        print(f"   Task: {self.task_id}")
        print(f"   Summary: {summary}")
        print(f"   Event: {event.to_json()}")
        
        return 0  # Success
    
    def emit_blocked(self, reason: str) -> int:
        """Emit TaskBlocked event - triggers kanban blocking via subscriber"""
        
        event = TaskBlockedEvent(
            task_id=self.task_id,
            reason=reason,
            blocked_at=datetime.utcnow().isoformat()
        )
        
        # In production: NATS publish
        # subject = f"exec.agents.kanban.{self.task_id}.blocked"
        # self.nats.publish(subject, event.to_json())
        
        # For now: simulate with print
        print(f"🚫 EMIT: TaskBlocked")
        print(f"   Task: {self.task_id}")
        print(f"   Reason: {reason}")
        print(f"   Event: {event.to_json()}")
        
        return 1  # Failure
    
    def run(self, work_fn) -> int:
        """
        Execute work function and emit appropriate event.
        
        Work function should:
        - Return (success: bool, summary: str, metadata: dict)
        - Raise exception on fatal error
        """
        try:
            success, summary, metadata = work_fn()
            
            if success:
                return self.emit_completed(summary, metadata)
            else:
                return self.emit_blocked(summary)
                
        except Exception as e:
            return self.emit_blocked(f"Exception: {type(e).__name__}: {str(e)}")


# Event Subscriber (would run on hermes gateway or separate service)
class KanbanEventHandler:
    """
    Subscribes to task events and updates kanban board.
    
    Subject tree:
    - exec.agents.kanban.{task_id}.completed → mark task done
    - exec.agents.kanban.{task_id}.blocked → mark task blocked
    - exec.agents.kanban.* → catch-all for monitoring
    """
    
    def __init__(self, nats_connection=None):
        self.nats = nats_connection
    
    def handle_task_completed(self, subject: str, event_json: str):
        """Handle TaskCompleted event - mark kanban task done"""
        event = TaskCompletedEvent(**json.loads(event_json))
        
        # In production: hermes kanban complete <task_id> <summary>
        print(f"✅ HANDLE: TaskCompleted")
        print(f"   Subject: {subject}")
        print(f"   Task: {event.task_id}")
        print(f"   Summary: {event.summary}")
        print(f"   (Would call: hermes kanban complete {event.task_id})")
    
    def handle_task_blocked(self, subject: str, event_json: str):
        """Handle TaskBlocked event - mark kanban task blocked"""
        event = TaskBlockedEvent(**json.loads(event_json))
        
        # In production: hermes kanban block <task_id> <reason>
        print(f"🚫 HANDLE: TaskBlocked")
        print(f"   Subject: {subject}")
        print(f"   Task: {event.task_id}")
        print(f"   Reason: {event.reason}")
        print(f"   (Would call: hermes kanban block {event.task_id})")
    
    def subscribe(self):
        """Subscribe to all task completion/blocking events"""
        # In production:
        # self.nats.subscribe("exec.agents.kanban.*.completed", self.handle_task_completed)
        # self.nats.subscribe("exec.agents.kanban.*.blocked", self.handle_task_blocked)
        print("📡 SUBSCRIBED to kanban events")


# Example worker implementation
def example_task_worker(task_id: str):
    """Example: proper worker using Actor+CQRS model"""
    
    actor = KanbanWorkerActor(task_id)
    
    def work():
        """Do the actual work"""
        print(f"Executing task {task_id}...")
        
        # Simulate work
        result = "Task completed successfully"
        
        # Return: (success, summary, metadata)
        return (
            True,
            "API Documentation generated - 12 hours work",
            {
                "files_created": 5,
                "lines_written": 2400,
                "coverage": "95%"
            }
        )
    
    # Run work and emit event (no kanban_complete() call needed)
    exit_code = actor.run(work)
    return exit_code


if __name__ == "__main__":
    task_id = os.getenv("HERMES_TASK_ID", "t_test_12345")
    
    print("════════════════════════════════════════════════════════════════════════════")
    print("Actor-CQRS Kanban Worker - Event-Driven Completion")
    print("════════════════════════════════════════════════════════════════════════════")
    print()
    
    # Run example
    exit_code = example_task_worker(task_id)
    
    print()
    print("════════════════════════════════════════════════════════════════════════════")
    print(f"Exit Code: {exit_code}")
    print("════════════════════════════════════════════════════════════════════════════")
    
    sys.exit(exit_code)
