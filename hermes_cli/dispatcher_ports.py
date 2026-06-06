"""
dispatcher_ports.py — Hexagonal port interfaces for the Kanban dispatcher.

Gap G-S5-H3-1 remediation: Plan and Free factorization.
The FUNCTOR_GAP_MANIFEST.md identifies that Plan and Free are entangled in
dispatcher logic.  This module introduces two hexagonal ports that cleanly
separate the two concerns:

    FreePort   — domain boundary for task-tree unfolding (F₁: Mem → Tree(S))
    PlanPort   — domain boundary for policy/scoring (F₃: Mem → Policy)

Architecture (hexagonal):

    ┌──────────────────────────────────────────────────────────────────┐
    │  dispatch_once_free  (integration layer)                         │
    │  ─ calls FreePort.unfold_tree(conn, ready_ids)                   │
    │  ─ calls PlanPort.score_tasks(conn, ready_rows, tree_info)       │
    │  ─ NO direct SQL / scoring logic; only wires the two ports       │
    └──────────────────────────────────────────────────────────────────┘
            │                          │
            ▼                          ▼
    ┌────────────────┐      ┌───────────────────────┐
    │  FreePort      │      │  PlanPort             │
    │  (ABC)         │      │  (ABC)                │
    │                │      │                       │
    │  unfold_tree() │      │  score_tasks()        │
    └───────┬────────┘      └──────────┬────────────┘
            │                          │
            ▼                          ▼
    ┌────────────────┐      ┌───────────────────────┐
    │BFSFreeAdapter  │      │RICEPlanAdapter        │
    │                │      │                       │
    │  BFS traversal │      │  Learned G scoring:   │
    │  task_links    │      │  - descendant_count   │
    │  parent→child  │      │  - priority_sum       │
    │  edges         │      │  - task priority      │
    │                │      │  - created_at         │
    └────────────────┘      └───────────────────────┘

No shared state: FreePort reads only task_links; PlanPort reads only task
metadata fields (priority, created_at) plus the tree_info dict produced by
FreePort.  Neither port mutates the database.

References:
- FUNCTOR_GAP_MANIFEST.md §G-S5-H3-1
- FUNCTOR_GAP_MANIFEST.md §F₃ Plan: Mem → Policy
- FUNCTOR_GAP_MANIFEST.md §F₁ Free: Mem → Tree(S)
"""

from __future__ import annotations

import sqlite3
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple


# ---------------------------------------------------------------------------
# Shared value objects — no mutable state, fully hashable
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class TaskRef:
    """Immutable handle for a task row as seen by the ports.

    Both FreePort and PlanPort operate on TaskRef sequences so they have no
    dependency on raw sqlite3.Row internals.
    """
    task_id: str
    assignee: Optional[str]
    priority: float
    created_at: int


@dataclass(frozen=True)
class TreeNode:
    """Describes a single task's position inside the unfolded subtree.

    Produced by FreePort; consumed (read-only) by PlanPort.
    No shared mutable references — both sides see the same frozen snapshot.
    """
    task_id: str
    depth: int                       # 0 = ready root, >0 = descendant
    descendant_count: int            # number of reachable children
    descendant_priority_sum: float   # sum of child task priorities


# ---------------------------------------------------------------------------
# Port interfaces (hexagonal boundaries)
# ---------------------------------------------------------------------------

class FreePort(ABC):
    """Port for the Free functor: task-tree unfolding.

    F₁ — Free: Mem → Tree(S)
    Input:  connection (read-only) + list of ready task IDs (roots)
    Output: dict mapping task_id → TreeNode (the unfolded subtree)

    Invariants:
    - MUST NOT mutate the database.
    - MUST NOT share mutable state with PlanPort.
    - MUST return a dict covering at minimum the supplied ready_ids
      (depth-0 roots), even when no children exist.
    """

    @abstractmethod
    def unfold_tree(
        self,
        conn: sqlite3.Connection,
        ready_ids: Sequence[str],
        *,
        max_depth: int = 10,
    ) -> Dict[str, TreeNode]:
        """Unfold the task subtree rooted at ``ready_ids``.

        Returns a mapping task_id → TreeNode for every reachable task
        (including the ready roots at depth 0).
        """


class PlanPort(ABC):
    """Port for the Plan functor: value-iteration scoring.

    F₃ — Plan: Mem → Policy
    Input:  list of ready task rows + tree_info (output of FreePort)
    Output: ordered list of TaskRef (best to worst)

    Invariants:
    - MUST NOT query the database (no conn parameter).
    - MUST NOT share mutable state with FreePort.
    - MUST be a pure function of (tasks, tree_info): same inputs ⟹ same order.
    """

    @abstractmethod
    def score_tasks(
        self,
        tasks: Sequence[TaskRef],
        tree_info: Dict[str, TreeNode],
    ) -> List[TaskRef]:
        """Score and rank ``tasks`` using ``tree_info``.

        Returns tasks ordered from best candidate (index 0) to worst.
        All input tasks MUST appear in the output exactly once.
        """


# ---------------------------------------------------------------------------
# Concrete adapters
# ---------------------------------------------------------------------------

class BFSFreeAdapter(FreePort):
    """Concrete Free adapter: BFS task-tree unfolding via ``task_links``.

    Follows parent→child edges (task_links.parent_id → task_links.child_id)
    so unfolding reveals which future tasks would become unblocked by
    completing a given ready root.

    Complexity: O(V + E) per BFS; V/E bounded by board size.
    """

    def unfold_tree(
        self,
        conn: sqlite3.Connection,
        ready_ids: Sequence[str],
        *,
        max_depth: int = 10,
    ) -> Dict[str, TreeNode]:
        """BFS from ready_ids through task_links.

        Returns dict task_id → TreeNode for every reachable task
        (including the depth-0 roots).
        """
        ready_list: List[str] = list(ready_ids)

        # Mutable scratch only — never written to conn.
        _mutable: Dict[str, Dict[str, Any]] = {}
        visited: set = set(ready_list)

        for tid in ready_list:
            _mutable[tid] = {
                "depth": 0,
                "descendant_count": 0,
                "descendant_priority_sum": 0.0,
            }

        frontier: List[Tuple[str, int]] = [(tid, 0) for tid in ready_list]

        while frontier:
            next_frontier: List[Tuple[str, int]] = []
            for parent_id, depth in frontier:
                if depth >= max_depth:
                    continue
                rows = conn.execute(
                    "SELECT t.id, t.priority FROM tasks t "
                    "JOIN task_links l ON l.child_id = t.id "
                    "WHERE l.parent_id = ?",
                    (parent_id,),
                ).fetchall()
                for row in rows:
                    child_id = row[0]
                    child_priority = row[1] or 0.0
                    child_depth = depth + 1
                    if child_id not in visited:
                        visited.add(child_id)
                        _mutable[child_id] = {
                            "depth": child_depth,
                            "descendant_count": 0,
                            "descendant_priority_sum": 0.0,
                        }
                        next_frontier.append((child_id, child_depth))
                    # Propagate stats upward to the direct parent root.
                    if parent_id in _mutable:
                        _mutable[parent_id]["descendant_count"] += 1
                        _mutable[parent_id]["descendant_priority_sum"] += child_priority
            frontier = next_frontier

        # Freeze into TreeNode value objects — no mutable state escapes.
        return {
            tid: TreeNode(
                task_id=tid,
                depth=info["depth"],
                descendant_count=info["descendant_count"],
                descendant_priority_sum=info["descendant_priority_sum"],
            )
            for tid, info in _mutable.items()
        }


class RICEPlanAdapter(PlanPort):
    """Concrete Plan adapter: RICE-style descendant scoring (learned G).

    Implements F₃ Plan: Mem → Policy.

    Score tuple (higher is better):
        (descendant_count, descendant_priority_sum, priority, -created_at)

    descendant_count + descendant_priority_sum approximate the value function
    V*(s) learned from the empirical Markov kernel G = Cofree⁻¹(M).  The
    remaining fields break ties deterministally (β₈ fix: stable ordering
    independent of dict insertion order).

    This is a pure function: no I/O, no shared mutable state.
    """

    def score_tasks(
        self,
        tasks: Sequence[TaskRef],
        tree_info: Dict[str, TreeNode],
    ) -> List[TaskRef]:
        """Score and rank tasks by descendant value, then priority, then age.

        Returns a new list (input is not mutated).  Same inputs always
        produce the same order (required by PlanPort invariant).
        """

        def _score(ref: TaskRef) -> Tuple[int, float, float, int, str]:
            node = tree_info.get(ref.task_id)
            if node is not None:
                dc = node.descendant_count
                dps = node.descendant_priority_sum
            else:
                dc = 0
                dps = 0.0
            # -created_at: earlier tasks win on tie (ASC ordering)
            # ref.task_id: stable lexicographic tiebreaker (β₈ determinism)
            return (dc, dps, ref.priority, -ref.created_at, ref.task_id)

        return sorted(tasks, key=_score, reverse=True)


# ---------------------------------------------------------------------------
# Adapter registry — dependency-inversion for integration
# ---------------------------------------------------------------------------

@dataclass
class DispatcherPorts:
    """Wire the two port implementations together.

    The integration layer (dispatch_once_free) accepts a DispatcherPorts
    instance and calls through ports only — no direct SQL or scoring logic.

    Default adapters implement the canonical behaviour; tests inject stubs.
    """
    free_port: FreePort = field(default_factory=BFSFreeAdapter)
    plan_port: PlanPort = field(default_factory=RICEPlanAdapter)


# Module-level default singleton (zero-config usage)
_DEFAULT_PORTS: Optional[DispatcherPorts] = None


def get_default_ports() -> DispatcherPorts:
    """Return the module-level default DispatcherPorts (lazy-init singleton).

    Tests should NOT use this — pass an explicit DispatcherPorts instead
    to keep test isolation intact.
    """
    global _DEFAULT_PORTS
    if _DEFAULT_PORTS is None:
        _DEFAULT_PORTS = DispatcherPorts()
    return _DEFAULT_PORTS
