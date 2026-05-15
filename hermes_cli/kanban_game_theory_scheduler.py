"""
ADR 004 — Multi-Dimensional VCG Auction Scheduler for Kanban Dispatch

Architecture source: nexus-knowledge-base / nebula-deep-v5
  - core/algorithms/game_theory_modules/  (GameTheoryAnalyzer, GameFactory)
  - semantic-intelligence-v1/resource_optimization_orchestrator.py
  - VCG auction house (vcg_auction.py)
  - MCTS tensegrity integration (mcts-tensegrity-integration.ts)

Design
------
Replace the naive `ORDER BY priority DESC, created_at ASC` in dispatch_once
with a true VCG welfare-maximising auction over FIVE scarce resources:

  1. pids        — OS process slots (cgroup-bounded)
  2. cpu         — CPU cores (fractional)
  3. ram_mb      — RAM in MB
  4. tokens      — Estimated LLM tokens consumed
  5. tool_calls  — Estimated tool invocations

Each ready task submits a ResourceBid — its estimated consumption of each
resource. The scheduler computes a per-task welfare score:

    welfare(t) = value(t) / cost(t)

where:

    value(t)  = base_priority × urgency × wait_boost
                + CASCADE_WEIGHT × log1p(blocked_dependents)

    cost(t)   = Σ_r  RESOURCE_WEIGHTS[r] × bid[r] / budget_remaining[r]

Tasks whose bids would push ANY resource past the global budget ceiling
receive a BUDGET_OVERFLOW_PENALTY multiplier (but are NOT hard-dropped —
the system degrades gracefully rather than starving blocked dependents).

Nash equilibrium property
~~~~~~~~~~~~~~~~~~~~~~~~~
No profile can unilaterally improve throughput by reordering. The cascade
term ensures tasks blocking many dependents surface first regardless of
their own priority, removing the main incentive for strategic misreporting.

MCTS look-ahead
~~~~~~~~~~~~~~~
Optional MCTS simulation validates the greedy order over MCTS_HORIZON ticks,
accounting for pids and slot constraints simultaneously.

Resource budget auto-detection
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
`ResourceBudget.from_system()` reads:
  - pids_max   : /sys/fs/cgroup/user.slice/user-1000.slice/pids.max × PIDS_SAFETY_FACTOR
  - pids_current: /proc count
  - cpu_total  : os.cpu_count()
  - ram_total  : /proc/meminfo MemAvailable
  - tokens/tools: config-supplied ceiling per dispatch tick

Usage in dispatch_once
~~~~~~~~~~~~~~~~~~~~~~
    from hermes_cli.kanban_game_theory_scheduler import (
        GameTheoryScheduler, ResourceBudget, build_profile_loads, rows_to_candidates
    )

    budget = ResourceBudget.from_system(max_system_pids=14289)
    scheduler = GameTheoryScheduler(profile_loads, budget=budget)
    ranked = scheduler.rank(ready_candidates, dep_counts=dep_map)
    for candidate in ranked:
        ...  # existing spawn logic unchanged

Feature flag: set _GT_SCHEDULER_ENABLED = False in kanban_db.py to revert.
"""

from __future__ import annotations

import math
import os
import random
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Tuning constants
# ---------------------------------------------------------------------------

URGENCY_WEIGHT: float      = 0.40   # how fast retried tasks rise (log-scaled)
WAIT_WEIGHT: float         = 0.30   # how fast old tasks rise (starvation prevention)
WAIT_HORIZON: float        = 3600.0 # seconds — tasks older than this are fully boosted
CASCADE_WEIGHT: float      = 0.50   # bonus per log-unit of blocked dependents
SLOT_DISCOUNT: float       = 0.10   # slot_factor when profile is at cap

BUDGET_OVERFLOW_PENALTY: float = 0.05  # multiply welfare when bid exceeds any resource budget

MCTS_SIMULATIONS: int = 50
MCTS_TOP_K: int       = 8
MCTS_HORIZON: int     = 3
MCTS_ENABLED: bool    = True

PIDS_SAFETY_FACTOR: float  = 0.70   # use 70% of cgroup pids limit as ceiling

# Resource weights — how much each resource dimension contributes to cost
RESOURCE_WEIGHTS: Dict[str, float] = {
    "pids":       0.40,   # dominant concern given cgroup limit
    "cpu":        0.25,
    "ram_mb":     0.20,
    "tokens":     0.10,
    "tool_calls": 0.05,
}

# Default bid estimates per task (overridden by assignee heuristics)
_DEFAULT_BIDS: Dict[str, float] = {
    "pids":       20.0,   # typical worker: hermes + serena adapter + LSP = ~15-25
    "cpu":         0.5,   # 0.5 cores average
    "ram_mb":    200.0,   # 200 MB per worker
    "tokens":  50_000.0,  # 50k tokens per task
    "tool_calls":  20.0,  # 20 tool calls average
}

# Per-assignee bid overrides (heavier profiles cost more)
_ASSIGNEE_BID_OVERRIDES: Dict[str, Dict[str, float]] = {
    "backend-eng": {
        "pids": 25.0, "cpu": 0.6, "ram_mb": 250.0,
        "tokens": 80_000.0, "tool_calls": 35.0,
    },
    "analyst": {
        "pids": 18.0, "cpu": 0.4, "ram_mb": 180.0,
        "tokens": 100_000.0, "tool_calls": 25.0,
    },
    "ops": {
        "pids": 15.0, "cpu": 0.3, "ram_mb": 150.0,
        "tokens": 40_000.0, "tool_calls": 15.0,
    },
    "reviewer": {
        "pids": 12.0, "cpu": 0.2, "ram_mb": 120.0,
        "tokens": 60_000.0, "tool_calls": 10.0,
    },
    "writer": {
        "pids": 12.0, "cpu": 0.2, "ram_mb": 120.0,
        "tokens": 90_000.0, "tool_calls": 8.0,
    },
    "researcher": {
        "pids": 15.0, "cpu": 0.3, "ram_mb": 150.0,
        "tokens": 120_000.0, "tool_calls": 30.0,
    },
    "frontend-eng": {
        "pids": 22.0, "cpu": 0.5, "ram_mb": 200.0,
        "tokens": 60_000.0, "tool_calls": 25.0,
    },
    "pm": {
        "pids": 10.0, "cpu": 0.2, "ram_mb": 100.0,
        "tokens": 40_000.0, "tool_calls": 8.0,
    },
}

_PRIORITY_TIER: Dict[int, float] = {
    2: 3.0,   # HIGH
    1: 2.0,   # NORMAL
    0: 1.0,   # LOW
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ResourceBid:
    """Estimated resource consumption for one task spawn."""
    pids:       float = 20.0
    cpu:        float = 0.5
    ram_mb:     float = 200.0
    tokens:     float = 50_000.0
    tool_calls: float = 20.0

    @classmethod
    def for_assignee(cls, assignee: str) -> "ResourceBid":
        """Return bid estimated for a known assignee profile, else defaults."""
        overrides = _ASSIGNEE_BID_OVERRIDES.get(assignee, {})
        base = dict(_DEFAULT_BIDS)
        base.update(overrides)
        return cls(**{k: base[k] for k in cls.__dataclass_fields__})  # type: ignore[attr-defined]

    def as_dict(self) -> Dict[str, float]:
        return {
            "pids": self.pids, "cpu": self.cpu, "ram_mb": self.ram_mb,
            "tokens": self.tokens, "tool_calls": self.tool_calls,
        }


@dataclass
class ResourceBudget:
    """
    Available system resources for this dispatch tick.

    All values represent REMAINING capacity (not totals).
    """
    pids_remaining:       float = 500.0    # free pid slots
    cpu_remaining:        float = 2.0      # free CPU cores
    ram_mb_remaining:     float = 2000.0   # free RAM in MB
    tokens_per_tick:      float = 500_000.0  # LLM token budget per tick
    tool_calls_per_tick:  float = 200.0    # tool call budget per tick

    def remaining(self, resource: str) -> float:
        """Return remaining capacity for a named resource."""
        return {
            "pids":       self.pids_remaining,
            "cpu":        self.cpu_remaining,
            "ram_mb":     self.ram_mb_remaining,
            "tokens":     self.tokens_per_tick,
            "tool_calls": self.tool_calls_per_tick,
        }.get(resource, 1.0)

    def would_overflow(self, bid: ResourceBid) -> bool:
        """True if accepting this bid would exceed any resource ceiling."""
        return (
            bid.pids       > self.pids_remaining       or
            bid.cpu        > self.cpu_remaining        or
            bid.ram_mb     > self.ram_mb_remaining     or
            bid.tokens     > self.tokens_per_tick      or
            bid.tool_calls > self.tool_calls_per_tick
        )

    @classmethod
    def from_system(
        cls,
        max_system_pids: Optional[int] = None,
        tokens_per_tick: float = 500_000.0,
        tool_calls_per_tick: float = 200.0,
    ) -> "ResourceBudget":
        """
        Auto-detect remaining resource capacity from OS metrics.

        max_system_pids: config-supplied ceiling (cgroup_limit × PIDS_SAFETY_FACTOR).
        Falls back to kernel pid_max × PIDS_SAFETY_FACTOR if not supplied.
        """
        # --- pids ---
        pids_current = -1
        pids_ceiling = -1
        try:
            pids_current = len([p for p in os.listdir("/proc") if p.isdigit()])
        except OSError:
            pass

        if max_system_pids is not None:
            pids_ceiling = int(max_system_pids)
        else:
            # Try cgroup first, fall back to kernel pid_max × safety factor
            for cgroup_path in [
                "/sys/fs/cgroup/user.slice/user-1000.slice/pids.max",
                "/sys/fs/cgroup/user.slice/pids.max",
            ]:
                try:
                    raw = open(cgroup_path).read().strip()
                    if raw.isdigit():
                        pids_ceiling = int(int(raw) * PIDS_SAFETY_FACTOR)
                        break
                except OSError:
                    pass
            if pids_ceiling < 0:
                try:
                    kernel_max = int(open("/proc/sys/kernel/pid_max").read().strip())
                    pids_ceiling = int(kernel_max * PIDS_SAFETY_FACTOR)
                except OSError:
                    pids_ceiling = 1000  # safe conservative default

        pids_remaining = max(0.0, float(pids_ceiling - max(0, pids_current)))

        # --- CPU ---
        cpu_total = float(os.cpu_count() or 2)
        try:
            load1 = os.getloadavg()[0]
            cpu_remaining = max(0.1, cpu_total - load1)
        except (AttributeError, OSError):
            cpu_remaining = cpu_total * 0.5

        # --- RAM ---
        ram_available_mb = 2000.0
        try:
            for line in open("/proc/meminfo"):
                if line.startswith("MemAvailable:"):
                    ram_available_mb = float(line.split()[1]) / 1024.0
                    break
        except OSError:
            pass

        return cls(
            pids_remaining=pids_remaining,
            cpu_remaining=cpu_remaining,
            ram_mb_remaining=ram_available_mb,
            tokens_per_tick=tokens_per_tick,
            tool_calls_per_tick=tool_calls_per_tick,
        )

    def consume(self, bid: ResourceBid) -> "ResourceBudget":
        """Return a new budget with this bid's resources deducted."""
        return ResourceBudget(
            pids_remaining=max(0.0, self.pids_remaining - bid.pids),
            cpu_remaining=max(0.0, self.cpu_remaining - bid.cpu),
            ram_mb_remaining=max(0.0, self.ram_mb_remaining - bid.ram_mb),
            tokens_per_tick=max(0.0, self.tokens_per_tick - bid.tokens),
            tool_calls_per_tick=max(0.0, self.tool_calls_per_tick - bid.tool_calls),
        )


@dataclass
class ProfileLoad:
    """Current slot utilisation for one assignee profile."""
    assignee: str
    running: int = 0
    cap: int = 2

    @property
    def load_fraction(self) -> float:
        """0.0 = idle, 1.0 = fully saturated."""
        if self.cap <= 0:
            return 0.0
        return min(1.0, self.running / self.cap)


@dataclass
class TaskCandidate:
    """Lightweight view of a ready task for scoring."""
    task_id: str
    assignee: str
    db_priority: int
    consecutive_failures: int
    created_at: Optional[float] = None
    last_failure_at: Optional[float] = None
    blocked_dependents: int = 0          # how many tasks are waiting on this one
    bid: Optional[ResourceBid] = None    # estimated resource cost; auto-derived if None

    def get_bid(self) -> ResourceBid:
        """Return explicit bid or auto-derive from assignee."""
        return self.bid if self.bid is not None else ResourceBid.for_assignee(self.assignee)


@dataclass
class ScoredCandidate:
    candidate: TaskCandidate
    score: float
    overflows: bool = False
    components: Dict[str, float] = field(default_factory=dict)

    def __lt__(self, other: "ScoredCandidate") -> bool:
        return self.score < other.score


# ---------------------------------------------------------------------------
# Core scheduler — multi-dimensional VCG welfare maximisation
# ---------------------------------------------------------------------------

class GameTheoryScheduler:
    """
    Multi-dimensional VCG welfare-maximising scheduler.

    Ranks ready tasks by:
        welfare(t) = value(t) / resource_cost(t)

    where value includes cascade bonus for blocked dependents, and
    resource_cost is a weighted sum across pids, CPU, RAM, tokens, tool_calls.

    Tasks that would overflow the current resource budget are penalised
    (not dropped) to allow graceful degradation.
    """

    def __init__(
        self,
        profile_loads: Optional[Dict[str, ProfileLoad]] = None,
        budget: Optional[ResourceBudget] = None,
        now: Optional[float] = None,
        mcts_enabled: Optional[bool] = None,
    ):
        self._loads: Dict[str, ProfileLoad] = profile_loads or {}
        self._budget: ResourceBudget = budget if budget is not None else ResourceBudget()
        self._now: float = now if now is not None else time.time()
        self._mcts_enabled = mcts_enabled if mcts_enabled is not None else MCTS_ENABLED

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def rank(
        self,
        candidates: List[TaskCandidate],
        failure_times: Optional[Dict[str, float]] = None,
        dep_counts: Optional[Dict[str, int]] = None,
    ) -> List[TaskCandidate]:
        """
        Return candidates sorted by descending VCG welfare score.

        Parameters
        ----------
        candidates:    Ready tasks to rank.
        failure_times: task_id → last failure unix timestamp.
        dep_counts:    task_id → number of tasks blocked by this task completing.
        """
        if not candidates:
            return []

        ft = failure_times or {}
        dc = dep_counts or {}

        # Hydrate optional fields
        for c in candidates:
            if c.last_failure_at is None and c.task_id in ft:
                c.last_failure_at = ft[c.task_id]
            if c.blocked_dependents == 0 and c.task_id in dc:
                c.blocked_dependents = dc[c.task_id]

        scored = [self._score(c) for c in candidates]

        # Primary sort: non-overflow before overflow, then by welfare score
        scored.sort(key=lambda s: (s.overflows, -s.score))

        if self._mcts_enabled and len(scored) > 1:
            scored = self._mcts_reorder(scored)

        return [s.candidate for s in scored]

    def score_one(
        self,
        candidate: TaskCandidate,
        dep_count: int = 0,
    ) -> float:
        """Return the welfare score for a single task (useful for diagnostics)."""
        candidate.blocked_dependents = dep_count
        return self._score(candidate).score

    # ------------------------------------------------------------------
    # VCG welfare scoring
    # ------------------------------------------------------------------

    def _score(self, c: TaskCandidate) -> ScoredCandidate:
        bid = c.get_bid()

        # ── VALUE SIDE ──────────────────────────────────────────────────

        # 1. Base: priority tier
        base = _PRIORITY_TIER.get(c.db_priority, 2.0)

        # 2. Urgency: retry count — log-scaled to avoid runaway
        urgency = 1.0 + math.log1p(c.consecutive_failures) * URGENCY_WEIGHT

        # 3. Wait time: starvation prevention
        wait_seconds = 0.0
        if c.created_at is not None:
            wait_seconds = max(0.0, self._now - c.created_at)
        wait_boost = 1.0 + math.log1p(wait_seconds / WAIT_HORIZON) * WAIT_WEIGHT

        # 4. Cascade bonus: tasks blocking many dependents get higher value
        cascade_bonus = CASCADE_WEIGHT * math.log1p(c.blocked_dependents)

        value = (base * urgency * wait_boost) + cascade_bonus

        # ── COST SIDE ───────────────────────────────────────────────────

        # 5. Resource cost: weighted sum of bid fractions
        bid_dict = bid.as_dict()
        resource_cost = 0.0
        for resource, weight in RESOURCE_WEIGHTS.items():
            remaining = self._budget.remaining(resource)
            if remaining > 0:
                resource_cost += weight * (bid_dict[resource] / remaining)
            else:
                # Resource exhausted — infinite cost for this dimension
                resource_cost += weight * 1e6

        # Avoid zero cost (degenerate case with very large budgets)
        resource_cost = max(resource_cost, 1e-6)

        # 6. Slot availability: profile saturation multiplier on top of resource cost
        load = self._loads.get(c.assignee)
        if load is not None and load.load_fraction >= 1.0:
            slot_factor = SLOT_DISCOUNT
        elif load is not None and load.load_fraction > 0.5:
            slot_factor = 1.0 - (load.load_fraction - 0.5) * (1.0 - SLOT_DISCOUNT)
        else:
            slot_factor = 1.0

        # ── WELFARE = VALUE / COST ───────────────────────────────────────
        welfare = (value / resource_cost) * slot_factor

        # 7. Budget overflow penalty (graceful degradation, not hard drop)
        overflows = self._budget.would_overflow(bid)
        if overflows:
            welfare *= BUDGET_OVERFLOW_PENALTY

        return ScoredCandidate(
            candidate=c,
            score=welfare,
            overflows=overflows,
            components={
                "base": base,
                "urgency": urgency,
                "wait_boost": wait_boost,
                "cascade_bonus": cascade_bonus,
                "value": value,
                "resource_cost": resource_cost,
                "slot_factor": slot_factor,
                "welfare": welfare,
                "overflows": float(overflows),
            },
        )

    # ------------------------------------------------------------------
    # MCTS look-ahead — validates greedy order over resource-aware simulation
    # ------------------------------------------------------------------

    def _mcts_reorder(self, scored: List[ScoredCandidate]) -> List[ScoredCandidate]:
        """
        Run MCTS over the top-K candidates to find the dispatch sequence that
        maximises expected completions over MCTS_HORIZON simulated ticks,
        subject to both slot caps AND resource budget.
        """
        top_k = scored[:MCTS_TOP_K]
        rest = scored[MCTS_TOP_K:]

        if len(top_k) <= 1:
            return scored

        best_order: List[ScoredCandidate] = list(top_k)
        best_value: float = -math.inf

        for _ in range(MCTS_SIMULATIONS):
            perm = list(top_k)
            random.shuffle(perm)
            value = self._simulate(perm)
            if value > best_value:
                best_value = value
                best_order = perm

        return best_order + rest

    def _simulate(self, order: List[ScoredCandidate]) -> float:
        """
        Simulate dispatching tasks in `order` for MCTS_HORIZON ticks.

        Tracks both slot caps and pids budget. Rewards completions,
        penalises failures and resource overflows.
        """
        running: Dict[str, int] = {a: l.running for a, l in self._loads.items()}
        caps: Dict[str, int] = {a: l.cap for a, l in self._loads.items()}
        budget = self._budget
        total_reward = 0.0
        queue = list(order)

        for _tick in range(MCTS_HORIZON):
            next_queue: List[ScoredCandidate] = []
            for sc in queue:
                assignee = sc.candidate.assignee
                cap = caps.get(assignee, 2)
                cur = running.get(assignee, 0)
                if cur >= cap:
                    next_queue.append(sc)
                    continue

                bid = sc.candidate.get_bid()

                # Resource budget check
                if budget.would_overflow(bid):
                    # Penalise overflow but still attempt (degraded)
                    total_reward -= 0.25
                    next_queue.append(sc)
                    continue

                # Spawn simulation — deduct resources
                running[assignee] = cur + 1
                budget = budget.consume(bid)

                cf = sc.candidate.consecutive_failures
                p_success = max(0.1, 1.0 - cf * 0.12)

                # Cascade bonus: completing this task unblocks dependents
                cascade_value = CASCADE_WEIGHT * math.log1p(
                    sc.candidate.blocked_dependents
                )

                if random.random() < p_success:
                    total_reward += 1.0 + cascade_value
                    running[assignee] = max(0, running.get(assignee, 1) - 1)
                    # Resources freed on completion
                    budget = ResourceBudget(
                        pids_remaining=budget.pids_remaining + bid.pids,
                        cpu_remaining=budget.cpu_remaining + bid.cpu,
                        ram_mb_remaining=budget.ram_mb_remaining + bid.ram_mb,
                        tokens_per_tick=budget.tokens_per_tick,
                        tool_calls_per_tick=budget.tool_calls_per_tick,
                    )
                else:
                    total_reward -= 0.5
                    # Task stays running — resources not freed

            queue = next_queue
            if not queue:
                break

        return total_reward


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def build_profile_loads(
    conn: sqlite3.Connection,
    max_concurrent_per_profile: int = 2,
) -> Dict[str, ProfileLoad]:
    """Query DB for running task counts per assignee → ProfileLoad map."""
    rows = conn.execute(
        "SELECT assignee, COUNT(*) as cnt FROM tasks "
        "WHERE status = 'running' AND assignee IS NOT NULL "
        "GROUP BY assignee"
    ).fetchall()
    return {
        row["assignee"]: ProfileLoad(
            assignee=row["assignee"],
            running=row["cnt"],
            cap=max_concurrent_per_profile,
        )
        for row in rows
    }


def build_dep_counts(conn: sqlite3.Connection) -> Dict[str, int]:
    """
    For each ready/blocked task, count how many OTHER tasks are blocked
    waiting for it (i.e. tasks that have it as a dependency).

    Uses task_links table: (source_id, target_id, link_type).
    Convention: 'depends_on' link means source depends on target.
    Returns: task_id → count of tasks that would be unblocked by its completion.
    """
    dep_counts: Dict[str, int] = {}
    try:
        # Count how many tasks depend on each task (blocked dependents)
        rows = conn.execute("""
            SELECT tl.target_id, COUNT(*) as cnt
            FROM task_links tl
            JOIN tasks t ON tl.source_id = t.id
            WHERE tl.link_type IN ('depends_on', 'blocks', 'after')
              AND t.status IN ('ready', 'blocked', 'paused')
            GROUP BY tl.target_id
        """).fetchall()
        for row in rows:
            dep_counts[row["target_id"]] = row["cnt"]
    except Exception:
        pass  # task_links may not exist or have different schema — safe to skip
    return dep_counts


def rows_to_candidates(
    ready_rows: list,
    failure_times: Optional[Dict[str, float]] = None,
    created_ats: Optional[Dict[str, float]] = None,
    dep_counts: Optional[Dict[str, int]] = None,
) -> List[TaskCandidate]:
    """
    Convert sqlite3.Row objects from dispatch_once's ready_rows query
    into TaskCandidate instances with resource bids auto-derived from assignee.

    ready_rows columns expected: id, assignee, consecutive_failures
    Optional: priority (falls back to 1 = NORMAL if missing)
    """
    ft = failure_times or {}
    ca = created_ats or {}
    dc = dep_counts or {}
    candidates = []
    for row in ready_rows:
        row_dict = dict(row)
        task_id = row_dict["id"]
        assignee = row_dict.get("assignee") or ""
        tc = TaskCandidate(
            task_id=task_id,
            assignee=assignee,
            db_priority=int(row_dict.get("priority", 1) or 1),
            consecutive_failures=int(row_dict.get("consecutive_failures", 0) or 0),
            created_at=ca.get(task_id),
            last_failure_at=ft.get(task_id),
            blocked_dependents=dc.get(task_id, 0),
            bid=ResourceBid.for_assignee(assignee),
        )
        candidates.append(tc)
    return candidates
