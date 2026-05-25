#!/usr/bin/env python3
"""
Kanban Worker Template - Proper Protocol Implementation

All dispatch tasks MUST:
1. Initialize with task metadata
2. Execute work
3. Call kanban_complete() with summary
OR kanban_block() with reason

Failure to do so results in "protocol violation" crash marking.
"""

import sys
import os
from pathlib import Path

# CRITICAL: Add hermes tools to path
sys.path.insert(0, str(Path.home() / ".hermes" / "lib"))

try:
    from hermes_tools import kanban_complete, kanban_block
except ImportError:
    # Fallback if tools not available
    def kanban_complete(task_id, summary, metadata=None):
        """Stub for testing."""
        print(f"✅ COMPLETE: {task_id}")
        print(f"Summary: {summary}")
        if metadata:
            print(f"Metadata: {metadata}")
    
    def kanban_block(task_id, reason):
        """Stub for testing."""
        print(f"🚫 BLOCKED: {task_id}")
        print(f"Reason: {reason}")


class KanbanWorker:
    """Base class for all dispatch workers."""
    
    def __init__(self, task_id: str):
        """Initialize worker with task ID."""
        self.task_id = task_id
        self.completed = False
        self.blocked = False
    
    def work(self):
        """Override this method with actual work."""
        raise NotImplementedError("Subclasses must implement work()")
    
    def run(self):
        """Execute work and handle kanban protocol."""
        try:
            print(f"🚀 Starting work on {self.task_id}...")
            
            # Do the work
            result = self.work()
            
            # CRITICAL: Call kanban_complete() before exit
            summary = result.get("summary", "Work complete") if isinstance(result, dict) else "Work complete"
            metadata = result.get("metadata", {}) if isinstance(result, dict) else {}
            
            kanban_complete(
                task_id=self.task_id,
                summary=summary,
                metadata=metadata
            )
            
            self.completed = True
            print(f"✅ {self.task_id} completed successfully")
            return 0
        
        except Exception as e:
            # If work fails, block the task with reason
            reason = f"Work failed: {str(e)}"
            kanban_block(
                task_id=self.task_id,
                reason=reason
            )
            
            self.blocked = True
            print(f"🚫 {self.task_id} blocked: {reason}")
            return 1
    
    def __enter__(self):
        """Context manager entry."""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensures kanban call."""
        if not self.completed and not self.blocked:
            if exc_type:
                kanban_block(
                    task_id=self.task_id,
                    reason=f"Exception: {exc_type.__name__}: {str(exc_val)}"
                )
            else:
                # No exception but work not completed
                kanban_complete(
                    task_id=self.task_id,
                    summary="Completed via context manager"
                )


# Example usage:
if __name__ == "__main__":
    # Get task ID from environment or command line
    task_id = os.getenv("HERMES_TASK_ID", "t_example")
    
    class ExampleWorker(KanbanWorker):
        def work(self):
            """Example work."""
            print(f"  Executing work for {self.task_id}...")
            
            # Simulated work
            for i in range(5):
                print(f"    Step {i+1}/5...")
            
            return {
                "summary": f"Completed 5 steps for {self.task_id}",
                "metadata": {
                    "steps_completed": 5,
                    "duration_seconds": 10
                }
            }
    
    # Run with proper protocol handling
    worker = ExampleWorker(task_id)
    exit_code = worker.run()
    
    # CRITICAL: Always exit with the code from worker.run()
    sys.exit(exit_code)
