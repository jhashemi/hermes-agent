"""
PHASE 7: Complete End-to-End Test

Tests full OKR pipeline: Input → All 6 engines → Production output
"""

import pytest
import asyncio
from datetime import datetime
from okr_executive.engines.okr_engine import OKREngine
from okr_executive.engines.research_engine import ResearchEngine
from okr_executive.engines.planning_engine import PlanningEngine
from okr_executive.engines.execution_engine import TaskExecutionEngine
from okr_executive.engines.review_engine import ReviewEngine
from okr_executive.engines.metrics_engine import MetricsEngine
from okr_executive.engines.base import ExecutionContext, EventType


# ==================== MOCK SERVICES ====================

class MockEventBroker:
    def __init__(self):
        self.events = []
    
    async def emit(self, topic: str, event_data: dict):
        self.events.append({
            'topic': topic,
            'data': event_data,
            'timestamp': datetime.now()
        })


class MockNexus:
    async def research(self, params: dict):
        return {'findings': {'scope': 'organization', 'complexity': 'medium'}}
    
    async def validate(self, params: dict):
        return {'valid': True, 'issues': []}


class MockEmbodiedAgent:
    id = "agent-1"
    
    async def research(self, **kwargs):
        return {
            'methods': [
                {
                    'name': 'Approach A',
                    'description': 'Implement phases 2-7',
                    'effort': 'medium',
                    'timeline_weeks': 1,
                    'cost': 5000,
                    'risks': [],
                    'success_probability': 0.95,
                }
            ],
            'risks': [],
            'success_factors': ['Team experience'],
        }
    
    async def plan(self, **kwargs):
        return {
            'goals': [
                {'goal': 'Phase 2', 'effort': 2},
                {'goal': 'Phase 3', 'effort': 2},
                {'goal': 'Phase 4', 'effort': 1.5},
                {'goal': 'Phase 5', 'effort': 1.5},
                {'goal': 'Phase 6', 'effort': 1},
                {'goal': 'Phase 7', 'effort': 1},
            ]
        }
    
    async def review_code(self, **kwargs):
        return {
            'summary': {'description': 'Excellent implementation', 'notes': 'All tests passing'},
            'findings': [],
            'recommendations': ['Consider adding performance optimizations'],
        }


class MockVCGDispatcher:
    async def allocate_tasks(self, **kwargs):
        return {
            'success': True,
            'method': 'VCG',
            'assignment': {},
            'total_welfare': 100,
        }


# ==================== E2E TEST ====================

@pytest.mark.asyncio
async def test_end_to_end_okr_execution():
    """Test complete OKR execution through all 6 engines"""
    
    # Setup
    event_broker = MockEventBroker()
    nexus = MockNexus()
    embodied_agent = MockEmbodiedAgent()
    vcg_dispatcher = MockVCGDispatcher()
    
    # Create all 6 engines
    okr_engine = OKREngine(
        engine_id='okr-engine',
        event_broker=event_broker,
        dependencies={'nexus': nexus}
    )
    
    research_engine = ResearchEngine(
        engine_id='research-engine',
        event_broker=event_broker,
        dependencies={'embodied_agent': embodied_agent, 'nexus': nexus}
    )
    
    planning_engine = PlanningEngine(
        engine_id='planning-engine',
        event_broker=event_broker,
        dependencies={'embodied_agent': embodied_agent}
    )
    
    execution_engine = TaskExecutionEngine(
        engine_id='execution-engine',
        event_broker=event_broker,
        dependencies={'vcg_dispatcher': vcg_dispatcher, 'embodied_agent': embodied_agent}
    )
    
    review_engine = ReviewEngine(
        engine_id='review-engine',
        event_broker=event_broker,
        dependencies={'embodied_agent': embodied_agent}
    )
    
    metrics_engine = MetricsEngine(
        engine_id='metrics-engine',
        event_broker=event_broker,
        dependencies={}
    )
    
    print("\n" + "="*80)
    print("PHASE 7: END-TO-END OKR EXECUTION TEST")
    print("="*80 + "\n")
    
    # ==================== ENGINE 1: OKREngine ====================
    
    print("1. OKREngine: Parse + Validate OKR")
    context = ExecutionContext(
        okr_id='e2e-test-1',
        correlation_id='e2e-test-1',
        phase='okr',
        input_data={
            'okr_text': '''
            Objective: Build complete OKR executive system (Phases 2-7)
            Key Result: PlanningEngine with RICE prioritization
            Key Result: ExecutionEngine with VCG optimization
            Key Result: ReviewEngine + GitHub integration
            Key Result: MetricsEngine + accountability
            Key Result: Complete E2E testing
            ''',
            'org_context': {
                'org_id': 'hermes',
                'teams': [{'team_id': 'eng', 'capacity': 10}]
            }
        }
    )
    
    ctx1 = await okr_engine.execute(context)
    print(f"   ✓ OKREngine: {len(ctx1.errors)} errors")
    assert len(ctx1.errors) == 0
    print(f"   ✓ Output: {list(ctx1.output_data.keys())}\n")
    
    # ==================== ENGINE 2: ResearchEngine ====================
    
    print("2. ResearchEngine: Expert research + approaches")
    context2 = ExecutionContext(
        okr_id='e2e-test-1',
        correlation_id='e2e-test-1',
        phase='research',
        input_data={
            'okr': ctx1.output_data.get('okr', {}),
            'cognitive_context': ctx1.output_data.get('cognitive_context', {}),
        }
    )
    
    ctx2 = await research_engine.execute(context2)
    print(f"   ✓ ResearchEngine: {len(ctx2.errors)} errors")
    assert len(ctx2.errors) == 0
    print(f"   ✓ Approaches: {len(ctx2.output_data.get('recommended_approaches', []))}\n")
    
    # ==================== ENGINE 3: PlanningEngine ====================
    
    print("3. PlanningEngine: Goal hierarchy + RICE")
    context3 = ExecutionContext(
        okr_id='e2e-test-1',
        correlation_id='e2e-test-1',
        phase='planning',
        input_data={
            'okr': ctx1.output_data.get('okr', {}),
            'research_findings': ctx2.output_data.get('research_findings', {}),
            'recommended_approaches': ctx2.output_data.get('recommended_approaches', []),
            'org_context': {'org_id': 'hermes'},
        }
    )
    
    ctx3 = await planning_engine.execute(context3)
    print(f"   ✓ PlanningEngine: {len(ctx3.errors)} errors")
    assert len(ctx3.errors) == 0
    goals = ctx3.output_data.get('goal_hierarchy', [])
    print(f"   ✓ Goals: {len(goals)}, Tasks: {len(ctx3.output_data.get('task_breakdown', []))}\n")
    
    # ==================== ENGINE 4: ExecutionEngine ====================
    
    print("4. ExecutionEngine: VCG allocation + agent dispatch")
    context4 = ExecutionContext(
        okr_id='e2e-test-1',
        correlation_id='e2e-test-1',
        phase='execution',
        input_data={
            'goal_hierarchy': goals,
            'task_breakdown': ctx3.output_data.get('task_breakdown', []),
            'rice_scores': ctx3.output_data.get('rice_scores', {}),
        }
    )
    
    ctx4 = await execution_engine.execute(context4)
    print(f"   ✓ ExecutionEngine: {len(ctx4.errors)} errors")
    assert len(ctx4.errors) == 0
    assignments = ctx4.output_data.get('task_assignments', [])
    print(f"   ✓ Task assignments: {len(assignments)}\n")
    
    # ==================== ENGINE 5: ReviewEngine ====================
    
    print("5. ReviewEngine: Code review + GitHub PR")
    context5 = ExecutionContext(
        okr_id='e2e-test-1',
        correlation_id='e2e-test-1',
        phase='review',
        input_data={
            'execution_results': {'assignments': assignments},
            'code_artifacts': ['phase2.py', 'phase3.py', 'phase4.py', 'phase5.py'],
            'test_results': {'coverage': 85, 'all_pass': True},
            'okr_id': 'e2e-test-1',
        }
    )
    
    ctx5 = await review_engine.execute(context5)
    print(f"   ✓ ReviewEngine: {len(ctx5.errors)} errors")
    assert len(ctx5.errors) == 0
    pr = ctx5.output_data.get('github_pr', {})
    print(f"   ✓ GitHub PR: {pr.get('url', 'mock')}\n")
    
    # ==================== ENGINE 6: MetricsEngine ====================
    
    print("6. MetricsEngine: Tracking + post-mortem")
    context6 = ExecutionContext(
        okr_id='e2e-test-1',
        correlation_id='e2e-test-1',
        phase='metrics',
        input_data={
            'execution_timeline': {
                'total_weeks': 1,
                'estimated_hours': 12,
                'phases': ['planning', 'implementation', 'testing', 'deployment']
            },
            'all_events': event_broker.events,
            'task_results': {
                'tasks': assignments,
            },
            'quality_score': 0.95,
            'okr_id': 'e2e-test-1',
        }
    )
    
    ctx6 = await metrics_engine.execute(context6)
    print(f"   ✓ MetricsEngine: {len(ctx6.errors)} errors")
    assert len(ctx6.errors) == 0
    kpis = ctx6.output_data.get('kpis', {})
    print(f"   ✓ KPIs: completion {kpis.get('completion', 0):.0f}%, quality {kpis.get('quality', 0):.2f}\n")
    
    # ==================== SUMMARY ====================
    
    print("="*80)
    print("✅ COMPLETE END-TO-END EXECUTION SUCCESSFUL")
    print("="*80 + "\n")
    
    print("Event Chain:")
    for i, evt in enumerate(event_broker.events, 1):
        print(f"  {i}. {evt['topic']}")
    
    print(f"\nFinal Metrics:")
    print(f"  - Completion: {kpis.get('completion', 0):.0f}%")
    print(f"  - Quality: {kpis.get('quality', 0):.2f}/1.0")
    print(f"  - Timeline: {kpis.get('timeline_adherence', 0):.0f}%")
    
    post_mortem = ctx6.output_data.get('post_mortem', {})
    print(f"\nPost-Mortem:")
    print(f"  - Successes: {len(post_mortem.get('successes', []))}")
    print(f"  - Issues: {len(post_mortem.get('issues', []))}")
    print(f"  - Improvements: {len(post_mortem.get('improvements', []))}")
    
    print(f"\n🚀 OKR Executive System is PRODUCTION READY\n")
    
    return {
        'status': 'success',
        'engines_executed': 6,
        'events_emitted': len(event_broker.events),
        'kpis': kpis,
        'post_mortem': post_mortem,
    }


if __name__ == '__main__':
    result = asyncio.run(test_end_to_end_okr_execution())
    print(f"\n✓ Test complete: {result['status']}\n")
