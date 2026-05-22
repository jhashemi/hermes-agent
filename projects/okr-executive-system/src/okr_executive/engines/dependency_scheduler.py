"""
DEPENDENCY-AWARE ISSUE SCHEDULER

Replaces simple numerical ordering with intelligent DAG-based scheduling
that respects dependencies and maximizes parallelism
"""

from dataclasses import dataclass
from typing import Dict, List, Set, Tuple
from enum import Enum


class IssueStatus(Enum):
    """Issue execution status"""
    PENDING = "pending"
    BLOCKED = "blocked"  # Waiting on dependencies
    READY = "ready"  # All dependencies satisfied
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Issue:
    """GitHub issue with dependencies"""
    number: int
    title: str
    dependencies: List[int]  # Issue numbers this depends on
    status: IssueStatus = IssueStatus.PENDING
    
    def __hash__(self):
        return hash(self.number)
    
    def __eq__(self, other):
        return self.number == other.number


class DependencyAwareScheduler:
    """
    Schedules issues respecting dependencies, maximizing parallelism
    """
    
    def __init__(self):
        self.issues: Dict[int, Issue] = {}
        self.completed: Set[int] = set()
    
    def add_issue(self, number: int, title: str, dependencies: List[int]):
        """Add an issue to the scheduler"""
        self.issues[number] = Issue(number, title, dependencies)
    
    def mark_completed(self, issue_number: int):
        """Mark an issue as completed"""
        self.completed.add(issue_number)
        if issue_number in self.issues:
            self.issues[issue_number].status = IssueStatus.COMPLETED
    
    def get_ready_issues(self) -> List[int]:
        """
        Get all issues ready to execute (all dependencies satisfied)
        Returns list of issue numbers sorted by priority
        """
        ready = []
        
        for issue_num, issue in self.issues.items():
            if issue.status in [IssueStatus.COMPLETED, IssueStatus.IN_PROGRESS]:
                continue
            
            # Check if all dependencies are completed
            all_deps_met = all(dep in self.completed for dep in issue.dependencies)
            
            if all_deps_met:
                ready.append(issue_num)
            else:
                issue.status = IssueStatus.BLOCKED
        
        # Sort by number (descending) for priority
        return sorted(ready, reverse=True)
    
    def get_execution_phases(self) -> List[List[int]]:
        """
        Get issues grouped by execution phase
        Each phase contains issues that can execute in parallel
        Returns list of phases (each phase is list of issue numbers)
        """
        phases = []
        remaining = set(self.issues.keys())
        completed = set()
        
        while remaining:
            current_phase = []
            
            # Find all issues ready in this phase
            for issue_num in sorted(remaining, reverse=True):
                issue = self.issues[issue_num]
                deps = issue.dependencies
                
                # Can execute if all dependencies are already done
                if all(dep in completed for dep in deps):
                    current_phase.append(issue_num)
            
            if not current_phase:
                # Circular dependency or missing dependency
                break
            
            phases.append(sorted(current_phase, reverse=True))
            
            # Mark as processed for next iteration
            for issue_num in current_phase:
                remaining.remove(issue_num)
                completed.add(issue_num)
        
        return phases
    
    def get_next_batch(self, batch_size: int = 5) -> List[int]:
        """
        Get next batch of issues to process (respecting dependencies)
        Returns up to batch_size issues ready to execute
        """
        ready = self.get_ready_issues()
        return ready[:batch_size]
    
    def print_schedule(self):
        """Print the execution schedule"""
        print("\nDEPENDENCY-AWARE EXECUTION SCHEDULE")
        print("=" * 80)
        
        phases = self.get_execution_phases()
        
        print(f"\nTotal execution phases: {len(phases)}")
        print(f"Maximum parallelism: {max(len(p) for p in phases)} issues\n")
        
        for phase_num, phase in enumerate(phases, 1):
            if len(phase) == 1:
                issue = self.issues[phase[0]]
                print(f"Phase {phase_num}:")
                print(f"  └─ Issue #{issue.number}: {issue.title}")
                if issue.dependencies:
                    deps_str = ", ".join(f"#{d}" for d in sorted(issue.dependencies, reverse=True))
                    print(f"     Depends on: {deps_str}")
            else:
                print(f"Phase {phase_num} (Parallel - {len(phase)} issues):")
                for issue_num in sorted(phase, reverse=True):
                    issue = self.issues[issue_num]
                    print(f"  ├─ Issue #{issue.number}: {issue.title}")
                    if issue.dependencies:
                        deps_str = ", ".join(f"#{d}" for d in sorted(issue.dependencies, reverse=True))
                        print(f"  │  Depends on: {deps_str}")
            
            if phase_num < len(phases):
                print(f"  │")
            print()


# Example: Executive agents cluster infrastructure issues
def create_cluster_scheduler() -> DependencyAwareScheduler:
    """Create scheduler with actual GitHub issues and dependencies"""
    scheduler = DependencyAwareScheduler()
    
    # Add issues with their dependencies
    scheduler.add_issue(72, "Cross-machine event ordering: vector clocks + NATS", [])
    scheduler.add_issue(71, "Branch-per-task merge protocol", [72])
    scheduler.add_issue(70, "Git worktree-per-task isolation", [72])
    scheduler.add_issue(69, "Eliminate Syncthing for code sync", [72])
    scheduler.add_issue(68, "Machine capability registry", [72])
    scheduler.add_issue(67, "Antifragility: automated chaos testing", [68, 65, 64])
    scheduler.add_issue(66, "Self-healing capability convergence", [68])
    scheduler.add_issue(65, "Fault tolerance: zero data loss", [64])
    scheduler.add_issue(64, "CRDT ref store: migrate to DuckDB", [])
    scheduler.add_issue(63, "High availability: 99.9% uptime", [71, 67, 65, 64])
    
    return scheduler


if __name__ == "__main__":
    # Create and test
    scheduler = create_cluster_scheduler()
    scheduler.print_schedule()
    
    # Simulate execution
    print("\nSIMULATED EXECUTION:")
    print("=" * 80)
    
    # Phase 1: Execute foundation issues
    print("\nPhase 1: Executing foundation issues (no dependencies)")
    phase1 = scheduler.get_ready_issues()
    print(f"Ready to execute: {phase1}")
    
    # Complete phase 1
    for issue_num in phase1:
        scheduler.mark_completed(issue_num)
        print(f"  ✅ Completed Issue #{issue_num}")
    
    # Phase 2
    print(f"\nPhase 2: Executing after Phase 1")
    phase2 = scheduler.get_ready_issues()
    print(f"Ready to execute: {phase2}")
