"""
DEPENDENCY-AWARE GITHUB ISSUE QUEUE MANAGER

Manages the queue of GitHub issues with dependency awareness
"""

from typing import List, Dict
from okr_executive.engines.scheduling import OKRScheduler


class GitHubIssueQueueManager:
    """
    Manages GitHub issue queue with dependency awareness.
    
    Replaces simple numerical ordering with intelligent DAG-based scheduling.
    """
    
    def __init__(self):
        self.scheduler = DependencyAwareScheduler()
        self._initialize_cluster_issues()
    
    def _initialize_cluster_issues(self):
        """Initialize with real cluster infrastructure issues"""
        # Foundation issues (no dependencies)
        self.scheduler.add_issue(
            72,
            "Cross-machine event ordering: vector clocks + NATS stream sequencing",
            []
        )
        self.scheduler.add_issue(
            64,
            "CRDT ref store: migrate from SQLite to DuckDB",
            []
        )
        
        # Second tier (depends on foundation)
        self.scheduler.add_issue(71, "Branch-per-task merge protocol", [72])
        self.scheduler.add_issue(70, "Git worktree-per-task isolation", [72])
        self.scheduler.add_issue(69, "Eliminate Syncthing for code sync", [72])
        self.scheduler.add_issue(68, "Machine capability registry", [72])
        self.scheduler.add_issue(65, "Fault tolerance: any machine failure = zero data loss", [64])
        
        # Third tier (depends on second tier)
        self.scheduler.add_issue(
            67,
            "Antifragility: automated chaos testing + self-healing verification",
            [68, 65, 64]
        )
        self.scheduler.add_issue(
            66,
            "Self-healing capability convergence: auto-update + auto-join cluster",
            [68]
        )
        
        # Fourth tier (depends on everything)
        self.scheduler.add_issue(
            63,
            "High availability: 99.9% cluster uptime, graceful degradation",
            [71, 67, 65, 64]
        )
    
    def get_execution_phases(self) -> List[List[int]]:
        """Get issues grouped by execution phase"""
        return self.scheduler.get_execution_phases()
    
    def get_next_batch(self, batch_size: int = 5) -> List[int]:
        """Get next batch of issues to process (respecting dependencies)"""
        return self.scheduler.get_next_batch(batch_size)
    
    def mark_completed(self, issue_number: int):
        """Mark an issue as completed"""
        self.scheduler.mark_completed(issue_number)
    
    def get_ready_issues(self) -> List[int]:
        """Get all issues ready to execute now"""
        return self.scheduler.get_ready_issues()
    
    def print_schedule(self):
        """Print the execution schedule"""
        self.scheduler.print_schedule()
    
    def get_issue_dependencies(self, issue_number: int) -> List[int]:
        """Get dependencies for a specific issue"""
        if issue_number in self.scheduler.issues:
            return self.scheduler.issues[issue_number].dependencies
        return []
    
    def are_dependencies_met(self, issue_number: int) -> bool:
        """Check if all dependencies for an issue are met"""
        deps = self.get_issue_dependencies(issue_number)
        return all(dep in self.scheduler.completed for dep in deps)


# Example usage
if __name__ == "__main__":
    manager = GitHubIssueQueueManager()
    manager.print_schedule()
    
    print("\n" + "=" * 80)
    print("SIMULATED EXECUTION")
    print("=" * 80)
    
    phases = manager.get_execution_phases()
    for phase_num, phase_issues in enumerate(phases, 1):
        print(f"\n✅ PHASE {phase_num}: Processing {phase_issues}")
        for issue in phase_issues:
            manager.mark_completed(issue)
        
        if phase_num < len(phases):
            next_ready = manager.get_ready_issues()
            print(f"   ↓ Next ready: {next_ready}")
