"""
E2E Test: OKR pipeline with EAF-backed engines

Tests: OKR Input → 5 engines → Production output
Uses EAF imports (VCGTaskScheduler, GoalHierarchy, OKRAccountabilitySystem, etc.)
"""

import pytest
import asyncio
import sys

sys.path.insert(0, "/home/ubuntu/executive_agents_framework/src")

from okr_executive.engines.okr_engine import OKREngine
from okr_executive.engines.planning_engine import PlanningEngine
from okr_executive.engines.execution_engine import ExecutionEngine
from okr_executive.engines.review_engine import ReviewEngine
from okr_executive.engines.metrics_engine import MetricsEngine
from okr_executive.engines.scheduling import OKRScheduler, MakespanMinimizer, MachineType, Machine
from okr_executive.domain.models import OKRInput, GitHubIssueRef, Objective, KeyResult


# ==================== FIXTURES ====================

SAMPLE_OKR = """Objective: Build production OKR platform
KR1: Ship MVP with all 6 engines
KR2: Achieve 80% test coverage
KR3: Deploy to 2 machines
KR4: Process real GitHub issues
"""

SAMPLE_GITHUB_ISSUE = GitHubIssueRef(
    number=72,
    title="Cross-machine event ordering: vector clocks + NATS stream sequencing",
    body="Implement vector clock-based event ordering across hermes1 and hermes2.",
    dependencies=[71, 67],
    url="https://github.com/jhashemi/executive-agents-framework/issues/72",
)

CLUSTER_MACHINES = [
    Machine(MachineType.LOCAL, available_slots=2, latency_ms=5.0, capable_engines={"okr", "research", "planning"}),
    Machine(MachineType.REMOTE_COMPUTE, available_slots=3, latency_ms=50.0, capable_engines={"execution", "planning"}),
    Machine(MachineType.REMOTE_QA, available_slots=2, latency_ms=80.0, capable_engines={"review", "metrics"}),
]


# ==================== ENGINE TESTS ====================

@pytest.mark.asyncio
async def test_okr_engine_parses_input():
    """Phase 1: OKR Engine parses raw text into Objective + KeyResult via EAF"""
    engine = OKREngine()
    result = await engine.execute({"okr_input": SAMPLE_OKR})

    assert "objective" in result
    assert "key_results" in result
    assert len(result["key_results"]) >= 1
    assert isinstance(result["objective"], Objective)
    assert isinstance(result["key_results"][0], KeyResult)


@pytest.mark.asyncio
async def test_planning_engine_decomposes_goals():
    """Phase 2: Planning Engine decomposes into GoalNode + AtomicTask via EAF"""
    engine = PlanningEngine()
    objective = Objective(
        id="test-obj-1",
        team_id="exec-agents",
        owner_id="test_runner",
        title="Build platform",
    )
    key_results = [
        KeyResult(id="kr-1", objective_id="test-obj-1", title="Ship MVP", target_value=1.0, current_value=0.0),
        KeyResult(id="kr-2", objective_id="test-obj-1", title="Get users", target_value=1.0, current_value=0.0),
    ]
    result = await engine.execute({"objective": objective, "key_results": key_results})

    assert "goals" in result
    assert "tasks" in result
    assert len(result["goals"]) >= 1


@pytest.mark.asyncio
async def test_execution_engine_assigns_tasks():
    """Phase 3: Execution Engine assigns tasks via VCG + dispatches via EAF"""
    engine = ExecutionEngine()
    result = await engine.execute({"tasks": []})

    assert "scheduler" in result


@pytest.mark.asyncio
async def test_review_engine_deliberates():
    """Phase 4: Review Engine uses ConsensusVotingFramework via EAF"""
    engine = ReviewEngine()
    result = await engine.execute({"tasks": [], "implementations": [{"id": "impl-1", "code": "print('hello')"}]})

    assert "reviews" in result
    assert len(result["reviews"]) == 1
    assert result["reviews"][0]["deliberation_protocol"] == "Position→Critique→Vote→Synthesis"


@pytest.mark.asyncio
async def test_metrics_engine_tracks_kpis():
    """Phase 5: Metrics Engine tracks KPIs via EAF KPITracker + DecisionAuditTrail"""
    engine = MetricsEngine()
    result = await engine.execute({
        "objective": Objective(id="test-obj-1", team_id="exec-agents", owner_id="test", title="Build platform"),
        "tasks": [],
        "reviews": [],
    })

    assert "kpi_dashboard" in result
    assert "audit_id" in result


# ==================== SCHEDULING TESTS ====================

def test_makespan_minimizer_critical_path():
    """MakespanMinimizer computes critical-path schedule (novel, no EAF equivalent)"""
    from okr_executive.engines.scheduling import MakespanMinimizer, MachineType, Machine

    issues = {
        1: type('Issue', (), {'dependencies': [], 'estimated_duration': 2.0, 'required_engines': set()})(),
        2: type('Issue', (), {'dependencies': [1], 'estimated_duration': 3.0, 'required_engines': set()})(),
        3: type('Issue', (), {'dependencies': [1], 'estimated_duration': 1.0, 'required_engines': set()})(),
    }

    machines = CLUSTER_MACHINES
    minimizer = MakespanMinimizer(issues, machines)
    schedule = minimizer.compute_schedule()

    assert len(schedule) == 3
    # Issue 1 should start first (no deps)
    assert schedule[0].issue_number == 1
    # Issue 2 and 3 should start after issue 1
    issue_1_end = schedule[0].start_time + schedule[0].duration
    for s in schedule[1:]:
        assert s.start_time >= issue_1_end - 0.1  # Allow for latency


def test_okr_scheduler_composes_eaf():
    """OKRScheduler composes EAF's VCGTaskScheduler + VCGMCTSIntegration + VCGDispatcher"""
    scheduler = OKRScheduler()

    assert scheduler.vcg_scheduler is not None
    assert scheduler.mcts_integration is not None
    assert scheduler.dispatcher is not None


def test_github_issue_ref():
    """GitHubIssueRef (novel type) stores issue data with dependencies"""
    issue = SAMPLE_GITHUB_ISSUE

    assert issue.number == 72
    assert issue.dependencies == [71, 67]
    assert "vector clock" in issue.body


# ==================== EAF INTEGRATION VERIFICATION ====================

def test_eaf_types_importable():
    """Verify all EAF types import correctly from domain.models"""
    from okr_executive.domain.models import (
        GoalNode, AtomicTask, TaskAllocation,
        Objective, KeyResult, PostMortemEntry,
        OKRAccountabilitySystem, RICEScorer, KPITracker,
        DecisionAuditTrail, DuckDBKanbanBoard,
    )

    # Verify they're real EAF classes, not local redefinitions
    assert GoalNode.__module__.startswith("executive_agents")
    assert AtomicTask.__module__.startswith("executive_agents")
    assert Objective.__module__.startswith("executive_agents")


def test_eaf_rice_scorer_works():
    """EAF RICEScorer computes canonical scores"""
    from okr_executive.domain.models import RICEScorer

    scorer = RICEScorer()
    score = scorer.score(reach=10, impact=8, confidence=0.7, effort=3)

    assert score > 0
    assert isinstance(score, float)


# ==================== FULL PIPELINE TEST ====================

@pytest.mark.asyncio
async def test_full_okr_pipeline():
    """Full pipeline: OKR → Planning → Execution → Review → Metrics"""
    # Phase 1: Parse OKR
    okr_engine = OKREngine()
    okr_result = await okr_engine.execute({"okr_input": SAMPLE_OKR})
    assert isinstance(okr_result["objective"], Objective)

    # Phase 2: Plan goals
    plan_engine = PlanningEngine()
    plan_result = await plan_engine.execute({
        "objective": okr_result["objective"],
        "key_results": okr_result["key_results"],
    })
    assert len(plan_result["goals"]) >= 1

    # Phase 3: Execute tasks
    exec_engine = ExecutionEngine()
    exec_result = await exec_engine.execute({"tasks": plan_result.get("tasks", [])})
    assert "scheduler" in exec_result

    # Phase 4: Review
    rev_engine = ReviewEngine()
    rev_result = await rev_engine.execute({
        "tasks": plan_result.get("tasks", []),
        "implementations": [{"id": "impl-1", "code": "pass"}],
    })
    assert len(rev_result["reviews"]) >= 1

    # Phase 5: Metrics
    met_engine = MetricsEngine()
    met_result = await met_engine.execute({
        "objective": okr_result["objective"],
        "tasks": plan_result.get("tasks", []),
        "reviews": rev_result["reviews"],
    })
    assert "kpi_dashboard" in met_result
    assert "audit_id" in met_result


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
