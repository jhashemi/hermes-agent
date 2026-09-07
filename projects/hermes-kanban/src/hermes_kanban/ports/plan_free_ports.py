"""Hexagonal ports: PlanPort and FreePort.

Factorization of kanban dispatcher's Plan/Free logic into distinct
hexagonal adapter shells (Gap G-S5-H3-1).

Architecture
------------
The dispatcher unfolds candidate tasks in two stages:

  Stage 1 — FreePort (tree unfold):
    Input:  list of ready task ids + sqlite connection
    Output: SubtreeInfo per ready task (descendant count, max depth,
            priority sum of descendants)

    The FreePort knows nothing about ranking or value functions.
    It only traverses the task DAG and annotates each root with
    structural metadata.

  Stage 2 — PlanPort (value iteration / learned G):
    Input:  list of CandidateTask (ready rows enriched with SubtreeInfo)
    Output: ranked list of CandidateTask (sorted by learned G score)

    The PlanPort knows nothing about the database or the graph.
    It only operates on the candidate list in memory.

  Integration (PlanFreeDispatchAdapter):
    Calls FreePort, converts results to CandidateTask, calls PlanPort,
    returns the ranked candidates to the caller (dispatch_once_free).

Isolation guarantees
--------------------
* FreePort accepts a read-only DB connection and returns a pure
  mapping — it never writes to the DB.
* PlanPort receives only the CandidateTask list — it has NO reference
  to a DB connection, connection factory, or any mutable state.
* The two adapters are instantiated independently and wired by
  PlanFreeDispatchAdapter. Callers that want only one port may import
  and use BFSFreeAdapter / RICEPlanAdapter directly.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import List, Protocol, Sequence, runtime_checkable


# ---------------------------------------------------------------------------
# DTOs — immutable data transfer objects shared by both ports
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SubtreeInfo:
    """Structural metadata about a ready task's descendant subtree.

    Returned by ``FreePort.unfold()``; consumed by ``PlanPort.rank()``.
    Contains NO database connection or mutable state.
    """

    task_id: str
    """The ready task whose subtree is described."""

    descendant_count: int = 0
    """Number of pending descendants reachable from this task."""

    descendant_priority_sum: float = 0.0
    """Sum of priority values of all reachable descendants."""

    max_descendant_depth: int = 0
    """Depth of the deepest reachable descendant (0 = leaf / no children)."""


@dataclass(frozen=True)
class CandidateTask:
    """A ready task enriched with its SubtreeInfo, ready for ranking.

    Created by PlanFreeDispatchAdapter after FreePort.unfold() returns.
    Passed to PlanPort.rank() as input. Contains NO DB connection.
    """

    task_id: str
    assignee: str
    priority: float = 0.0
    created_at: float = 0.0

    subtree: SubtreeInfo = field(default_factory=lambda: SubtreeInfo(task_id=""))
    """Structural metadata from the Free unfold stage."""

    g_score: float = 0.0
    """Learned G value assigned by PlanPort.rank(). Defaults to 0.0
    (not yet ranked). After PlanPort.rank() this is the decisive sort key."""


# ---------------------------------------------------------------------------
# Port Protocols — the hexagonal contracts
# ---------------------------------------------------------------------------

@runtime_checkable
class FreePort(Protocol):
    """Free-monad port: unfolds the task DAG from ready roots.

    Implementations MUST:
    * Accept a read-only ``sqlite3.Connection``.
    * Return one ``SubtreeInfo`` per ready root id.
    * NOT write to the DB.
    * NOT share state with any ``PlanPort`` instance.

    The only dependency is the DB connection passed per-call.
    No instance-level mutable state is required or permitted.
    """

    def unfold(
        self,
        conn: sqlite3.Connection,
        ready_ids: Sequence[str],
        *,
        max_depth: int = 10,
    ) -> List[SubtreeInfo]:
        """Traverse the task DAG and return subtree metadata for each ready id.

        Parameters
        ----------
        conn:
            Read-only sqlite3 connection to the kanban DB.
        ready_ids:
            Task ids with ``status='ready'``. These are the roots of the
            BFS/DFS traversal.
        max_depth:
            Maximum traversal depth (default 10).

        Returns
        -------
        list[SubtreeInfo]
            One entry per id in ``ready_ids``, in the same order.
        """
        ...


@runtime_checkable
class PlanPort(Protocol):
    """Plan port: value iteration on a learned scoring function G.

    Implementations MUST:
    * Accept a list of ``CandidateTask`` (no DB reference).
    * Return the same list re-sorted by ``g_score`` (descending).
    * NOT accept or reference a DB connection.
    * NOT share state with any ``FreePort`` instance.

    The scoring function G may be learned (e.g. value iteration, RL,
    RICE scoring) or deterministic. The default implementation uses
    RICE-inspired scoring.
    """

    def rank(
        self,
        candidates: Sequence[CandidateTask],
    ) -> List[CandidateTask]:
        """Score and rank candidates by the learned G function.

        Parameters
        ----------
        candidates:
            Ready tasks enriched with SubtreeInfo from FreePort.

        Returns
        -------
        list[CandidateTask]
            Re-ranked candidates, highest ``g_score`` first.
            Each returned CandidateTask has its ``g_score`` field set.
        """
        ...


# ---------------------------------------------------------------------------
# Concrete adapters
# ---------------------------------------------------------------------------

class BFSFreeAdapter:
    """Concrete FreePort: breadth-first DAG traversal.

    Implements the ``FreePort`` protocol. Traverses task_links BFS-style
    from the ready roots, annotating each root with descendant_count,
    descendant_priority_sum, and max_descendant_depth.

    State: NONE (stateless, every call is independent).
    """

    def unfold(
        self,
        conn: sqlite3.Connection,
        ready_ids: Sequence[str],
        *,
        max_depth: int = 10,
    ) -> List[SubtreeInfo]:
        """BFS from each ready root through task_links.

        Returns one SubtreeInfo per ready root.  Nodes that appear in
        multiple subtrees are counted per-root independently.
        """
        results: List[SubtreeInfo] = []

        for root_id in ready_ids:
            # Per-root BFS — independent traversal, no shared state
            root_info = _bfs_subtree(conn, root_id, max_depth=max_depth)
            results.append(root_info)

        return results


def _bfs_subtree(
    conn: sqlite3.Connection,
    root_id: str,
    max_depth: int,
) -> SubtreeInfo:
    """BFS from ``root_id`` and return its SubtreeInfo.

    Pure helper: no side effects, no state mutation outside local vars.
    """
    descendant_count = 0
    descendant_priority_sum = 0.0
    max_depth_seen = 0

    frontier: List[tuple[str, int]] = [(root_id, 0)]
    visited: set[str] = {root_id}

    while frontier:
        next_frontier: List[tuple[str, int]] = []
        for parent_id, depth in frontier:
            if depth >= max_depth:
                continue
            rows = conn.execute(
                "SELECT t.id, COALESCE(t.priority, 0.0) "
                "FROM tasks t "
                "JOIN task_links l ON l.child_id = t.id "
                "WHERE l.parent_id = ?",
                (parent_id,),
            ).fetchall()
            for child_id, child_priority in rows:
                child_depth = depth + 1
                if child_id not in visited:
                    visited.add(child_id)
                    descendant_count += 1
                    descendant_priority_sum += child_priority
                    if child_depth > max_depth_seen:
                        max_depth_seen = child_depth
                    next_frontier.append((child_id, child_depth))
        frontier = next_frontier

    return SubtreeInfo(
        task_id=root_id,
        descendant_count=descendant_count,
        descendant_priority_sum=descendant_priority_sum,
        max_descendant_depth=max_depth_seen,
    )


class RICEPlanAdapter:
    """Concrete PlanPort: RICE-inspired value iteration.

    Implements the ``PlanPort`` protocol using a deterministic G function
    modelled on RICE scoring:

        G(task) = alpha * descendant_count
                + beta  * descendant_priority_sum
                + gamma * task.priority
                - delta * task.created_at          (older = lower score)

    All weights are configurable at instantiation time.

    State: weights only (immutable after construction). No DB reference.
    """

    def __init__(
        self,
        *,
        alpha: float = 1.0,
        beta: float = 0.1,
        gamma: float = 0.5,
        delta: float = 1e-9,
    ) -> None:
        """Initialise RICE weight vector.

        Parameters
        ----------
        alpha:  weight on descendant_count    (reach)
        beta:   weight on descendant_priority_sum (impact)
        gamma:  weight on task's own priority (confidence)
        delta:  penalty on age (older tasks get a tiny score reduction)
        """
        self._alpha = alpha
        self._beta = beta
        self._gamma = gamma
        self._delta = delta

    def rank(self, candidates: Sequence[CandidateTask]) -> List[CandidateTask]:
        """Score and re-rank ``candidates`` by G.

        Returns a new list (the input sequence is not mutated).
        Each returned CandidateTask has ``g_score`` set.
        """
        scored: List[CandidateTask] = []
        for c in candidates:
            g = (
                self._alpha * c.subtree.descendant_count
                + self._beta  * c.subtree.descendant_priority_sum
                + self._gamma * c.priority
                - self._delta * c.created_at
            )
            # dataclass is frozen — create a new instance with g_score set
            scored.append(
                CandidateTask(
                    task_id=c.task_id,
                    assignee=c.assignee,
                    priority=c.priority,
                    created_at=c.created_at,
                    subtree=c.subtree,
                    g_score=g,
                )
            )
        scored.sort(key=lambda x: x.g_score, reverse=True)
        return scored


# ---------------------------------------------------------------------------
# Integration adapter (documented)
# ---------------------------------------------------------------------------

class PlanFreeDispatchAdapter:
    """Integration adapter: wires FreePort → PlanPort → ranked candidates.

    This adapter documents the contract between the two ports:

      1. Call ``free_port.unfold(conn, ready_ids)`` → list[SubtreeInfo].
      2. Zip with the raw ready rows to build list[CandidateTask].
      3. Call ``plan_port.rank(candidates)`` → sorted list[CandidateTask].
      4. Return the ranked list to the caller.

    State: only the two port references (set at construction time,
    never mutated). No DB connection stored at instance level.

    No state is shared between ``free_port`` and ``plan_port``.
    """

    def __init__(
        self,
        *,
        free_port: FreePort,
        plan_port: PlanPort,
    ) -> None:
        self._free = free_port
        self._plan = plan_port

    # type verification at construction time
    def __post_init__(self) -> None:
        assert isinstance(self._free, FreePort), (
            "free_port must implement FreePort protocol"
        )
        assert isinstance(self._plan, PlanPort), (
            "plan_port must implement PlanPort protocol"
        )

    def rank_ready_tasks(
        self,
        conn: sqlite3.Connection,
        ready_rows: Sequence[tuple],
        *,
        max_depth: int = 10,
    ) -> List[CandidateTask]:
        """Unfold + rank the ready tasks.

        Parameters
        ----------
        conn:
            Read-only sqlite3 connection.  Passed to FreePort only.
        ready_rows:
            Rows from ``SELECT id, assignee, priority, created_at FROM tasks``
            where ``status='ready'``.
        max_depth:
            Max BFS depth forwarded to FreePort.

        Returns
        -------
        list[CandidateTask]
            Ready tasks ranked by G score (highest first).
        """
        if not ready_rows:
            return []

        # Stage 1: Free unfold (pure DB read, no write)
        ready_ids = [row[0] for row in ready_rows]
        subtrees = self._free.unfold(conn, ready_ids, max_depth=max_depth)
        subtree_by_id = {s.task_id: s for s in subtrees}

        # Stage 2: Build CandidateTask DTOs (no DB access)
        candidates: List[CandidateTask] = []
        for row in ready_rows:
            task_id = row[0]
            assignee = row[1] or ""
            priority = float(row[2] or 0.0)
            created_at = float(row[3] or 0.0)
            subtree = subtree_by_id.get(
                task_id,
                SubtreeInfo(task_id=task_id),
            )
            candidates.append(
                CandidateTask(
                    task_id=task_id,
                    assignee=assignee,
                    priority=priority,
                    created_at=created_at,
                    subtree=subtree,
                )
            )

        # Stage 3: Plan rank (pure in-memory, no DB access)
        return self._plan.rank(candidates)


def make_default_plan_free_adapter() -> PlanFreeDispatchAdapter:
    """Factory: create a PlanFreeDispatchAdapter with the default adapters.

    Returns a ``PlanFreeDispatchAdapter`` wired to:
    * ``BFSFreeAdapter``   — breadth-first DAG traversal
    * ``RICEPlanAdapter``  — RICE-inspired G scoring

    This is the adapter used by ``dispatch_once_free`` when no explicit
    adapter is passed.
    """
    return PlanFreeDispatchAdapter(
        free_port=BFSFreeAdapter(),
        plan_port=RICEPlanAdapter(),
    )
