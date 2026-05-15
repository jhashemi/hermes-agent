"""Tests for ADR 004 — Multi-Dimensional VCG Auction Scheduler."""
import math
import sqlite3
import time
import pytest

from hermes_cli.kanban_game_theory_scheduler import (
    GameTheoryScheduler,
    ProfileLoad,
    ResourceBid,
    ResourceBudget,
    TaskCandidate,
    build_dep_counts,
    build_profile_loads,
    rows_to_candidates,
    URGENCY_WEIGHT,
    WAIT_HORIZON,
    SLOT_DISCOUNT,
    CASCADE_WEIGHT,
    BUDGET_OVERFLOW_PENALTY,
    RESOURCE_WEIGHTS,
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _c(
    task_id="t_001",
    assignee="backend-eng",
    priority=1,
    failures=0,
    created_at=None,
    blocked_dependents=0,
    bid=None,
):
    return TaskCandidate(
        task_id=task_id,
        assignee=assignee,
        db_priority=priority,
        consecutive_failures=failures,
        created_at=created_at,
        blocked_dependents=blocked_dependents,
        bid=bid,
    )


def _pl(assignee, running, cap=2):
    return ProfileLoad(assignee=assignee, running=running, cap=cap)


def _infinite_budget():
    """Budget with no constraints."""
    return ResourceBudget(
        pids_remaining=1e9,
        cpu_remaining=1e9,
        ram_mb_remaining=1e9,
        tokens_per_tick=1e9,
        tool_calls_per_tick=1e9,
    )


def _tight_budget(pids=10.0):
    """Budget near exhaustion on pids."""
    return ResourceBudget(
        pids_remaining=pids,
        cpu_remaining=1e9,
        ram_mb_remaining=1e9,
        tokens_per_tick=1e9,
        tool_calls_per_tick=1e9,
    )


# ── ResourceBid tests ──────────────────────────────────────────────────────────

class TestResourceBid:
    def test_for_assignee_known(self):
        bid = ResourceBid.for_assignee("backend-eng")
        assert bid.pids > 0
        assert bid.tokens > 0

    def test_for_assignee_unknown_uses_defaults(self):
        bid = ResourceBid.for_assignee("nonexistent-profile")
        default = ResourceBid()
        assert bid.pids == default.pids
        assert bid.tokens == default.tokens

    def test_as_dict_keys(self):
        bid = ResourceBid()
        d = bid.as_dict()
        assert set(d.keys()) == {"pids", "cpu", "ram_mb", "tokens", "tool_calls"}

    def test_all_resources_positive(self):
        for assignee in ["backend-eng", "analyst", "ops", "reviewer", "writer",
                         "researcher", "frontend-eng", "pm"]:
            bid = ResourceBid.for_assignee(assignee)
            for k, v in bid.as_dict().items():
                assert v > 0, f"{assignee}.{k} must be positive"


# ── ResourceBudget tests ───────────────────────────────────────────────────────

class TestResourceBudget:
    def test_from_system_returns_positive_values(self):
        b = ResourceBudget.from_system(max_system_pids=14289)
        assert b.pids_remaining >= 0
        assert b.cpu_remaining >= 0
        assert b.ram_mb_remaining >= 0

    def test_from_system_respects_max_pids(self):
        b = ResourceBudget.from_system(max_system_pids=500)
        # pids_remaining = max(0, 500 - current_pids)
        # current_pids is ~330, so remaining should be > 0 and <= 500
        assert 0 <= b.pids_remaining <= 500

    def test_would_overflow_on_pids(self):
        b = ResourceBudget(pids_remaining=5.0, cpu_remaining=100.0,
                           ram_mb_remaining=10000.0, tokens_per_tick=1e9,
                           tool_calls_per_tick=1e9)
        bid = ResourceBid(pids=10.0, cpu=0.1, ram_mb=10.0, tokens=100.0, tool_calls=1.0)
        assert b.would_overflow(bid)

    def test_would_overflow_on_ram(self):
        b = ResourceBudget(pids_remaining=1000.0, cpu_remaining=100.0,
                           ram_mb_remaining=50.0, tokens_per_tick=1e9,
                           tool_calls_per_tick=1e9)
        bid = ResourceBid(pids=5.0, cpu=0.1, ram_mb=200.0, tokens=100.0, tool_calls=1.0)
        assert b.would_overflow(bid)

    def test_no_overflow_within_budget(self):
        b = _infinite_budget()
        bid = ResourceBid.for_assignee("backend-eng")
        assert not b.would_overflow(bid)

    def test_consume_reduces_remaining(self):
        b = ResourceBudget(pids_remaining=100.0, cpu_remaining=4.0,
                           ram_mb_remaining=2000.0, tokens_per_tick=500_000.0,
                           tool_calls_per_tick=200.0)
        bid = ResourceBid(pids=20.0, cpu=0.5, ram_mb=200.0, tokens=50_000.0, tool_calls=10.0)
        b2 = b.consume(bid)
        assert abs(b2.pids_remaining - 80.0) < 0.01
        assert abs(b2.cpu_remaining - 3.5) < 0.01
        assert abs(b2.ram_mb_remaining - 1800.0) < 0.01

    def test_consume_floors_at_zero(self):
        b = ResourceBudget(pids_remaining=5.0, cpu_remaining=0.1,
                           ram_mb_remaining=100.0, tokens_per_tick=1e6,
                           tool_calls_per_tick=100.0)
        bid = ResourceBid(pids=100.0, cpu=10.0, ram_mb=5000.0, tokens=0.0, tool_calls=0.0)
        b2 = b.consume(bid)
        assert b2.pids_remaining == 0.0
        assert b2.cpu_remaining == 0.0
        assert b2.ram_mb_remaining == 0.0


# ── Score component tests ──────────────────────────────────────────────────────

class TestScoreComponents:
    def _sched(self, budget=None):
        return GameTheoryScheduler(budget=budget or _infinite_budget(), mcts_enabled=False)

    def test_high_priority_scores_higher_than_normal(self):
        s = self._sched()
        assert s.score_one(_c("t1", priority=2)) > s.score_one(_c("t2", priority=1))

    def test_normal_scores_higher_than_low(self):
        s = self._sched()
        assert s.score_one(_c("t1", priority=1)) > s.score_one(_c("t2", priority=0))

    def test_failures_increase_score(self):
        s = self._sched()
        assert s.score_one(_c(failures=1)) > s.score_one(_c(failures=0))
        assert s.score_one(_c(failures=5)) > s.score_one(_c(failures=1))

    def test_wait_time_boosts_score(self):
        now = time.time()
        s = GameTheoryScheduler(budget=_infinite_budget(), now=now, mcts_enabled=False)
        fresh = _c("t1", created_at=now - 60)
        old = _c("t2", created_at=now - WAIT_HORIZON * 2)
        assert s.score_one(old) > s.score_one(fresh)

    def test_cascade_bonus_for_blocked_dependents(self):
        s = self._sched()
        solo = _c("t1", blocked_dependents=0)
        blocker = _c("t2", blocked_dependents=5)
        assert s.score_one(blocker, dep_count=5) > s.score_one(solo, dep_count=0)

    def test_cascade_scales_with_dep_count(self):
        s = self._sched()
        c1 = _c("t1", blocked_dependents=1)
        c5 = _c("t2", blocked_dependents=5)
        c20 = _c("t3", blocked_dependents=20)
        assert s.score_one(c20, 20) > s.score_one(c5, 5) > s.score_one(c1, 1)

    def test_overflow_reduces_score(self):
        """Task that overflows pids budget should score lower than within-budget task."""
        tight = _tight_budget(pids=5.0)   # less than any bid
        ample = _infinite_budget()
        s_tight = GameTheoryScheduler(budget=tight, mcts_enabled=False)
        s_ample = GameTheoryScheduler(budget=ample, mcts_enabled=False)
        c = _c("t1")
        assert s_ample.score_one(c) > s_tight.score_one(c)

    def test_overflow_penalty_factor(self):
        """Overflow score should be approximately BUDGET_OVERFLOW_PENALTY × base score."""
        tight = _tight_budget(pids=1.0)
        ample = _infinite_budget()
        s_tight = GameTheoryScheduler(budget=tight, mcts_enabled=False)
        s_ample = GameTheoryScheduler(budget=ample, mcts_enabled=False)
        c = _c("t1")
        ratio = s_tight.score_one(c) / s_ample.score_one(c)
        # Should be close to BUDGET_OVERFLOW_PENALTY (within 2x due to resource cost diff)
        assert ratio < BUDGET_OVERFLOW_PENALTY * 10

    def test_resource_cost_penalises_expensive_assignee(self):
        """backend-eng (heavier bids) scores lower than pm (lighter bids) at same priority."""
        s = self._sched()
        heavy = _c("t1", assignee="backend-eng", priority=1)
        light = _c("t2", assignee="pm", priority=1)
        assert s.score_one(light) > s.score_one(heavy)

    def test_saturated_profile_gets_slot_discount(self):
        loads = {"busy": _pl("busy", 2, 2), "free": _pl("free", 0, 2)}
        s = GameTheoryScheduler(profile_loads=loads, budget=_infinite_budget(), mcts_enabled=False)
        assert s.score_one(_c("t2", assignee="free")) > s.score_one(_c("t1", assignee="busy"))

    def test_partial_load_gives_partial_discount(self):
        loads_full = {"a": ProfileLoad("a", running=4, cap=4)}
        loads_3q   = {"a": ProfileLoad("a", running=3, cap=4)}
        loads_idle = {"a": ProfileLoad("a", running=0, cap=4)}
        sf = GameTheoryScheduler(profile_loads=loads_full, budget=_infinite_budget(), mcts_enabled=False)
        s3 = GameTheoryScheduler(profile_loads=loads_3q,  budget=_infinite_budget(), mcts_enabled=False)
        si = GameTheoryScheduler(profile_loads=loads_idle, budget=_infinite_budget(), mcts_enabled=False)
        c = _c("t1", assignee="a")
        assert si.score_one(c) > s3.score_one(c) > sf.score_one(c)


# ── Ranking tests ─────────────────────────────────────────────────────────────

class TestRanking:
    def _sched(self, **kw):
        kw.setdefault("budget", _infinite_budget())
        kw.setdefault("mcts_enabled", False)
        return GameTheoryScheduler(**kw)

    def test_empty_returns_empty(self):
        assert self._sched().rank([]) == []

    def test_single_returns_single(self):
        c = _c()
        assert self._sched().rank([c]) == [c]

    def test_high_priority_ranked_first(self):
        s = self._sched()
        ranked = s.rank([_c("t1", priority=0), _c("t2", priority=2), _c("t3", priority=1)])
        assert ranked[0].task_id == "t2"

    def test_retried_floats_above_fresh_same_priority(self):
        s = self._sched()
        ranked = s.rank([_c("fresh", failures=0), _c("retried", failures=3)])
        assert ranked[0].task_id == "retried"

    def test_old_task_floats_above_new(self):
        now = time.time()
        s = GameTheoryScheduler(budget=_infinite_budget(), now=now, mcts_enabled=False)
        ranked = s.rank([_c("new", created_at=now - 10), _c("old", created_at=now - WAIT_HORIZON * 3)])
        assert ranked[0].task_id == "old"

    def test_blocker_ranked_above_non_blocker(self):
        """Task blocking 5 dependents should rank above same-priority non-blocker."""
        s = self._sched()
        blocker = _c("blocker", priority=1, blocked_dependents=5)
        normal  = _c("normal",  priority=1, blocked_dependents=0)
        ranked = s.rank([normal, blocker])
        assert ranked[0].task_id == "blocker"

    def test_overflow_tasks_ranked_last(self):
        """Tasks that exceed pids budget are penalised and ranked below within-budget tasks."""
        tight = _tight_budget(pids=5.0)
        s = GameTheoryScheduler(budget=tight, mcts_enabled=False)
        within = _c("within", assignee="pm",          bid=ResourceBid(pids=3.0, cpu=0.1, ram_mb=50.0, tokens=1000.0, tool_calls=1.0))
        exceed = _c("exceed", assignee="backend-eng", bid=ResourceBid(pids=50.0, cpu=0.5, ram_mb=200.0, tokens=50000.0, tool_calls=20.0))
        ranked = s.rank([exceed, within])
        assert ranked[0].task_id == "within"

    def test_saturated_profile_pushed_back(self):
        loads = {"busy": _pl("busy", 2, 2), "free": _pl("free", 0, 2)}
        s = GameTheoryScheduler(profile_loads=loads, budget=_infinite_budget(), mcts_enabled=False)
        busy_ts = [_c(f"b{i}", assignee="busy", priority=1) for i in range(3)]
        free_ts = [_c(f"f{i}", assignee="free", priority=1) for i in range(3)]
        ranked = s.rank(busy_ts + free_ts)
        free_ids = {c.task_id for c in free_ts}
        first_busy = next(i for i, c in enumerate(ranked) if c.task_id not in free_ids)
        last_free  = max(i for i, c in enumerate(ranked) if c.task_id in free_ids)
        assert last_free < first_busy

    def test_order_deterministic_without_mcts(self):
        s = self._sched()
        candidates = [_c(f"t{i}", priority=i % 3, failures=i % 4) for i in range(10)]
        r1 = s.rank(list(candidates))
        r2 = s.rank(list(candidates))
        assert [c.task_id for c in r1] == [c.task_id for c in r2]

    def test_dep_counts_passed_via_kwarg(self):
        s = self._sched()
        blocker = _c("blocker", priority=1)
        normal  = _c("normal",  priority=1)
        ranked = s.rank([normal, blocker], dep_counts={"blocker": 8})
        assert ranked[0].task_id == "blocker"


# ── MCTS tests ────────────────────────────────────────────────────────────────

class TestMCTS:
    def test_mcts_returns_correct_count(self):
        s = GameTheoryScheduler(budget=_infinite_budget(), mcts_enabled=True)
        ranked = s.rank([_c(f"t{i}") for i in range(6)])
        assert len(ranked) == 6

    def test_mcts_no_duplicates(self):
        s = GameTheoryScheduler(budget=_infinite_budget(), mcts_enabled=True)
        ranked = s.rank([_c(f"t{i}") for i in range(8)])
        assert len({c.task_id for c in ranked}) == 8

    def test_mcts_with_tight_budget_no_crash(self):
        tight = _tight_budget(pids=30.0)
        s = GameTheoryScheduler(budget=tight, mcts_enabled=True)
        candidates = [
            _c("e1", assignee="backend-eng", priority=1, failures=2),
            _c("e2", assignee="backend-eng", priority=1),
            _c("o1", assignee="ops", priority=2),
            _c("o2", assignee="ops", priority=1, blocked_dependents=3),
        ]
        ranked = s.rank(candidates)
        assert len(ranked) == 4
        assert {c.task_id for c in ranked} == {"e1", "e2", "o1", "o2"}

    def test_mcts_score_layer_prefers_high_priority(self):
        """Pure-score (no MCTS) must rank HIGH priority first."""
        loads = {"eng": _pl("eng", 1, 2), "ops": _pl("ops", 0, 2)}
        s = GameTheoryScheduler(profile_loads=loads, budget=_infinite_budget(), mcts_enabled=False)
        candidates = [
            _c("e1", assignee="eng", priority=1, failures=2),
            _c("e2", assignee="eng", priority=1),
            _c("o1", assignee="ops", priority=2),   # HIGH
            _c("o2", assignee="ops", priority=1),
        ]
        ranked = s.rank(candidates)
        assert ranked[0].task_id == "o1"


# ── build_dep_counts tests ────────────────────────────────────────────────────

class TestBuildDepCounts:
    def _conn(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("""CREATE TABLE tasks (
            id TEXT PRIMARY KEY, status TEXT, assignee TEXT)""")
        conn.execute("""CREATE TABLE task_links (
            source_id TEXT, target_id TEXT, link_type TEXT)""")
        return conn

    def test_counts_blocked_dependents(self):
        conn = self._conn()
        # t1 and t2 both depend on t3
        conn.execute("INSERT INTO tasks VALUES ('t1','ready','eng')")
        conn.execute("INSERT INTO tasks VALUES ('t2','blocked','eng')")
        conn.execute("INSERT INTO tasks VALUES ('t3','running','eng')")
        conn.execute("INSERT INTO task_links VALUES ('t1','t3','depends_on')")
        conn.execute("INSERT INTO task_links VALUES ('t2','t3','depends_on')")
        dc = build_dep_counts(conn)
        assert dc.get("t3", 0) == 2

    def test_ignores_done_dependents(self):
        conn = self._conn()
        conn.execute("INSERT INTO tasks VALUES ('t1','done','eng')")
        conn.execute("INSERT INTO tasks VALUES ('t2','running','eng')")
        conn.execute("INSERT INTO task_links VALUES ('t1','t2','depends_on')")
        dc = build_dep_counts(conn)
        assert dc.get("t2", 0) == 0  # done tasks don't count as blocked

    def test_empty_links_returns_empty(self):
        conn = self._conn()
        dc = build_dep_counts(conn)
        assert dc == {}

    def test_missing_table_returns_empty(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        # No task_links table at all
        dc = build_dep_counts(conn)
        assert dc == {}


# ── rows_to_candidates tests ──────────────────────────────────────────────────

class TestRowsToCandidates:
    def test_basic_conversion(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id TEXT, assignee TEXT, consecutive_failures INT, priority INT)")
        conn.execute("INSERT INTO tasks VALUES ('t1','eng',2,1)")
        rows = conn.execute("SELECT id, assignee, consecutive_failures, priority FROM tasks").fetchall()
        cands = rows_to_candidates(rows, created_ats={"t1": 1000.0}, dep_counts={"t1": 3})
        c = cands[0]
        assert c.task_id == "t1"
        assert c.assignee == "eng"
        assert c.consecutive_failures == 2
        assert c.db_priority == 1
        assert c.created_at == 1000.0
        assert c.blocked_dependents == 3

    def test_bid_auto_derived(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id TEXT, assignee TEXT, consecutive_failures INT)")
        conn.execute("INSERT INTO tasks VALUES ('t1','backend-eng',0)")
        rows = conn.execute("SELECT id, assignee, consecutive_failures FROM tasks").fetchall()
        cands = rows_to_candidates(rows)
        assert cands[0].bid is not None
        assert cands[0].bid.pids > 0

    def test_missing_priority_defaults_to_normal(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id TEXT, assignee TEXT, consecutive_failures INT)")
        conn.execute("INSERT INTO tasks VALUES ('t1','eng',0)")
        rows = conn.execute("SELECT id, assignee, consecutive_failures FROM tasks").fetchall()
        assert rows_to_candidates(rows)[0].db_priority == 1


# ── build_profile_loads tests ─────────────────────────────────────────────────

class TestBuildProfileLoads:
    def test_counts_running_per_assignee(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, assignee TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('t1','running','eng')")
        conn.execute("INSERT INTO tasks VALUES ('t2','running','eng')")
        conn.execute("INSERT INTO tasks VALUES ('t3','running','ops')")
        loads = build_profile_loads(conn, max_concurrent_per_profile=3)
        assert loads["eng"].running == 2 and loads["eng"].cap == 3
        assert loads["ops"].running == 1

    def test_ignores_non_running(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE tasks (id TEXT, status TEXT, assignee TEXT)")
        conn.execute("INSERT INTO tasks VALUES ('t1','ready','eng')")
        conn.execute("INSERT INTO tasks VALUES ('t2','done','eng')")
        loads = build_profile_loads(conn)
        assert "eng" not in loads


# ── Nash stability property ───────────────────────────────────────────────────

class TestNashStability:
    def test_no_profile_improves_unilaterally(self):
        """
        ops has HIGH priority task + idle slots: must rank first.
        eng tasks must all be present in output.
        No profile can improve by reordering.
        """
        loads = {"eng": _pl("eng", 1, 2), "ops": _pl("ops", 0, 2)}
        s = GameTheoryScheduler(
            profile_loads=loads,
            budget=_infinite_budget(),
            mcts_enabled=False,
        )
        candidates = [
            _c("e1", assignee="eng", priority=1, failures=0),
            _c("e2", assignee="eng", priority=1, failures=2),
            _c("o1", assignee="ops", priority=1, failures=0),
            _c("o2", assignee="ops", priority=2, failures=0),  # HIGH
        ]
        ranked = s.rank(candidates)
        assert ranked[0].task_id == "o2"
        ids = [c.task_id for c in ranked]
        assert "e1" in ids and "e2" in ids

    def test_cascade_overcomes_priority_tie(self):
        """A NORMAL task blocking 10 others should outrank a NORMAL task blocking none."""
        s = GameTheoryScheduler(budget=_infinite_budget(), mcts_enabled=False)
        blocker = _c("blocker", priority=1, blocked_dependents=10)
        leaf    = _c("leaf",    priority=1, blocked_dependents=0)
        ranked = s.rank([leaf, blocker])
        assert ranked[0].task_id == "blocker"
