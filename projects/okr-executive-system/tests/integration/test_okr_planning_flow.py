"""
Phase 2: Integration Test - Complete OKR Flow Through Planning

Tests complete OKR flow: Parse → Research → Plan
Validates RICE scoring, goal hierarchy, and agent assignments.
"""

import pytest
import asyncio
from datetime import datetime, timedelta
from okr_executive.engines.base import (
    ExecutionContext,
    EventType,
)
from okr_executive.engines.okr_engine import OKREngine
from okr_executive.engines.research_engine import ResearchEngine
from okr_executive.engines.planning_engine import PlanningEngine
from okr_executive.domain.models import OKR, Goal, Task


# ==================== MOCK SERVICES ====================

class MockEventBroker:
    """Mock event broker for testing"""
    def __init__(self):
        self.events = []
    
    async def emit(self, topic: str, event_data: dict):
        self.events.append({
            'topic': topic,
            'data': event_data,
            'timestamp': datetime.now()
        })


class MockNexus:
    """Mock Nexus Knowledge Base"""
    async def research(self, params: dict):
        return {
            'findings': {
                'scope': 'organization',
                'complexity': 'medium',
            }
        }
    
    async def query(self, params: dict):
        """Return mock dependency structure"""
        if params.get('type') == 'integration_surface':
            return {
                'task-1': ['task-2', 'task-3'],
                'task-2': ['task-4'],
            }
        return {}
    
    async def validate(self, params: dict):
        return {
            'valid': True,
            'issues': [],
            'validated_plan': params.get('plan', []),
            'dependencies': {}
        }


class MockEmbodiedAgent:
    """Mock ExecutiveAgentTwin"""
    id = "agent-1"
    
    async def research(self, **kwargs):
        return {
            'methods': [
                {
                    'name': 'Approach A',
                    'description': 'Test approach',
                    'effort': 'medium',
                    'timeline_weeks': 4,
                    'cost': 10000,
                    'risks': [],
                    'success_probability': 0.8,
                }
            ],
            'risks': ['Technical complexity'],
            'success_factors': ['Team experience'],
        }
    
    async def plan(self, **kwargs):
        """Plan hierarchical goals"""
        return {
            'goals': [
                {
                    'description': 'Organization-level goal: Build platform',
                    'scope': 'organization',
                    'reach': 100.0,
                    'impact': 10.0,
                    'confidence': 0.8,
                    'effort': 8.0,
                    'estimated_hours': 320.0,
                    'parent_goal': None,
                    'child_goals': ['goal-2', 'goal-3'],
                },
                {
                    'description': 'Department-level goal: Backend development',
                    'scope': 'department',
                    'reach': 50.0,
                    'impact': 8.0,
                    'confidence': 0.85,
                    'effort': 5.0,
                    'estimated_hours': 200.0,
                    'parent_goal': 'goal-1',
                    'child_goals': ['goal-4', 'goal-5'],
                },
                {
                    'description': 'Department-level goal: Frontend development',
                    'scope': 'department',
                    'reach': 50.0,
                    'impact': 8.0,
                    'confidence': 0.85,
                    'effort': 5.0,
                    'estimated_hours': 200.0,
                    'parent_goal': 'goal-1',
                    'child_goals': ['goal-6', 'goal-7'],
                },
                {
                    'description': 'Team-level goal: API design',
                    'scope': 'team',
                    'reach': 25.0,
                    'impact': 7.0,
                    'confidence': 0.9,
                    'effort': 3.0,
                    'estimated_hours': 120.0,
                    'parent_goal': 'goal-2',
                    'child_goals': [],
                },
                {
                    'description': 'Team-level goal: Database optimization',
                    'scope': 'team',
                    'reach': 25.0,
                    'impact': 7.0,
                    'confidence': 0.9,
                    'effort': 2.5,
                    'estimated_hours': 100.0,
                    'parent_goal': 'goal-2',
                    'child_goals': [],
                },
                {
                    'description': 'Team-level goal: UI components',
                    'scope': 'team',
                    'reach': 25.0,
                    'impact': 7.0,
                    'confidence': 0.85,
                    'effort': 3.5,
                    'estimated_hours': 140.0,
                    'parent_goal': 'goal-3',
                    'child_goals': [],
                },
                {
                    'description': 'Team-level goal: User experience',
                    'scope': 'team',
                    'reach': 25.0,
                    'impact': 6.0,
                    'confidence': 0.8,
                    'effort': 2.5,
                    'estimated_hours': 100.0,
                    'parent_goal': 'goal-3',
                    'child_goals': [],
                },
            ],
            'timelines': {
                'start': datetime.now().isoformat(),
                'end': (datetime.now() + timedelta(weeks=12)).isoformat(),
            },
            'dependencies': {
                'goal-1': ['goal-2', 'goal-3'],
                'goal-2': ['goal-4', 'goal-5'],
                'goal-3': ['goal-6', 'goal-7'],
            }
        }


# ==================== TESTS ====================

@pytest.mark.asyncio
async def test_planning_engine_basic():
    """Test basic PlanningEngine execution"""
    
    # Setup
    event_broker = MockEventBroker()
    nexus = MockNexus()
    embodied_agent = MockEmbodiedAgent()
    
    engine = PlanningEngine(
        engine_id='planning-engine-test',
        event_broker=event_broker,
        dependencies={
            'nexus': nexus,
            'embodied_agent': embodied_agent,
        }
    )
    
    # Create test input (output from ResearchEngine)
    context = ExecutionContext(
        okr_id='test-okr-1',
        correlation_id='corr-1',
        phase='phase-3',
        input_data={
            'okr': {
                'id': 'okr-1',
                'objective': 'Build AI-powered OKR execution platform',
                'key_results': [
                    'Complete all 6 engines with full cognitive integration',
                    'Deploy to production with < 50ms latency',
                    'Achieve 95% accuracy on OKR predictions',
                ]
            },
            'research_findings': {
                'agent_research': {
                    'methods': ['Agile', 'Modular architecture'],
                    'risks': ['Integration complexity'],
                },
                'nexus_insights': {
                    'insights': ['Use microservices', 'Event-driven architecture'],
                },
            },
            'recommended_approaches': [
                {
                    'id': 'approach-1',
                    'name': 'Microservices architecture',
                    'effort': 'medium',
                    'timeline_weeks': 8,
                    'resource_cost': 50000,
                }
            ],
            'org_structure': {
                'org_id': 'org-1',
                'org_name': 'Hermes Research',
                'level': 'org',
                'alignment_level': 0.9,
            }
        }
    )
    
    # Execute
    result = await engine.execute(context)
    
    # Assert
    assert len(result.errors) == 0, f"Errors: {result.errors}"
    assert 'goals' in result.output_data
    assert 'goal_hierarchy' in result.output_data
    assert 'tasks' in result.output_data
    assert 'accountability_map' in result.output_data
    assert 'priority_scores' in result.output_data
    
    goals = result.output_data['goals']
    assert len(goals) > 0, "No goals generated"
    
    # Check goal hierarchy
    hierarchy = result.output_data['goal_hierarchy']
    assert 'level_1_org' in hierarchy
    assert 'level_2_dept' in hierarchy
    assert 'level_3_team' in hierarchy
    
    # Check priorities are calculated
    priorities = result.output_data['priority_scores']
    assert len(priorities) > 0
    
    # Check tasks created
    tasks = result.output_data['tasks']
    assert len(tasks) == len(goals)
    
    # Check event emitted
    assert len(event_broker.events) == 1
    assert event_broker.events[0]['topic'] == 'plan.created'
    
    print(f"✓ Test passed: {len(goals)} goals, {len(tasks)} tasks created")


@pytest.mark.asyncio
async def test_rice_scoring():
    """Test RICE scoring calculation"""
    
    event_broker = MockEventBroker()
    nexus = MockNexus()
    embodied_agent = MockEmbodiedAgent()
    
    engine = PlanningEngine(
        engine_id='planning-engine-rice-test',
        event_broker=event_broker,
        dependencies={
            'nexus': nexus,
            'embodied_agent': embodied_agent,
        }
    )
    
    context = ExecutionContext(
        okr_id='rice-test-okr',
        correlation_id='rice-corr-1',
        phase='phase-3',
        input_data={
            'okr': {
                'id': 'okr-rice-test',
                'objective': 'Test RICE scoring',
                'key_results': ['Test KR'],
            },
            'research_findings': {},
            'recommended_approaches': [],
            'org_structure': {'alignment_level': 0.8},
        }
    )
    
    result = await engine.execute(context)
    
    # Verify RICE scores
    priorities = result.output_data['priority_scores']
    assert len(priorities) > 0
    
    # All scores should be positive
    for goal_id, score in priorities.items():
        assert score > 0, f"Invalid RICE score for {goal_id}: {score}"
    
    print(f"✓ RICE scoring test passed: {len(priorities)} goals scored")


@pytest.mark.asyncio
async def test_goal_hierarchy_levels():
    """Test hierarchical goal level assignment"""
    
    event_broker = MockEventBroker()
    nexus = MockNexus()
    embodied_agent = MockEmbodiedAgent()
    
    engine = PlanningEngine(
        engine_id='planning-engine-hierarchy-test',
        event_broker=event_broker,
        dependencies={
            'nexus': nexus,
            'embodied_agent': embodied_agent,
        }
    )
    
    context = ExecutionContext(
        okr_id='hierarchy-test-okr',
        correlation_id='hierarchy-corr-1',
        phase='phase-3',
        input_data={
            'okr': {
                'id': 'okr-hierarchy-test',
                'objective': 'Test goal hierarchy levels',
                'key_results': ['Test KR'],
            },
            'research_findings': {},
            'recommended_approaches': [],
            'org_structure': {'alignment_level': 0.8},
        }
    )
    
    result = await engine.execute(context)
    
    # Verify hierarchy
    hierarchy = result.output_data['goal_hierarchy']
    goals = result.output_data['goals']
    
    # Count goals at each level
    level_1_count = len([g for g in goals if g.level == 1])
    level_2_count = len([g for g in goals if g.level == 2])
    level_3_count = len([g for g in goals if g.level == 3])
    level_4_count = len([g for g in goals if g.level == 4])
    
    print(f"Goal levels: L1={level_1_count}, L2={level_2_count}, L3={level_3_count}, L4={level_4_count}")
    
    # Should have goals at multiple levels
    total_goals = level_1_count + level_2_count + level_3_count + level_4_count
    assert total_goals == len(goals)
    
    print(f"✓ Hierarchy test passed: {total_goals} goals across 4 levels")


@pytest.mark.asyncio
async def test_agent_assignments():
    """Test agent assignments and accountability map"""
    
    event_broker = MockEventBroker()
    nexus = MockNexus()
    embodied_agent = MockEmbodiedAgent()
    
    engine = PlanningEngine(
        engine_id='planning-engine-assignments-test',
        event_broker=event_broker,
        dependencies={
            'nexus': nexus,
            'embodied_agent': embodied_agent,
        }
    )
    
    context = ExecutionContext(
        okr_id='assignments-test-okr',
        correlation_id='assignments-corr-1',
        phase='phase-3',
        input_data={
            'okr': {
                'id': 'okr-assignments-test',
                'objective': 'Test agent assignments',
                'key_results': ['Test KR'],
            },
            'research_findings': {},
            'recommended_approaches': [],
            'org_structure': {'alignment_level': 0.8},
        }
    )
    
    result = await engine.execute(context)
    
    # Verify agent assignments
    accountability_map = result.output_data['accountability_map']
    tasks = result.output_data['tasks']
    
    assert len(accountability_map) > 0
    
    # All tasks should be assigned
    total_assigned = sum(len(v) for v in accountability_map.values())
    assert total_assigned == len(tasks)
    
    # Each agent should have tasks
    for agent_id, task_ids in accountability_map.items():
        assert len(task_ids) > 0, f"Agent {agent_id} has no tasks"
    
    print(f"✓ Agent assignment test passed: {len(accountability_map)} agents, {total_assigned} tasks assigned")


@pytest.mark.asyncio
async def test_end_to_end_planning_flow():
    """Test complete OKR → Parse → Research → Plan flow"""
    
    # Setup services
    event_broker = MockEventBroker()
    nexus = MockNexus()
    embodied_agent = MockEmbodiedAgent()
    
    # Create engines
    okr_engine = OKREngine(
        engine_id='okr-engine',
        event_broker=event_broker,
        dependencies={'nexus': nexus}
    )
    
    research_engine = ResearchEngine(
        engine_id='research-engine',
        event_broker=event_broker,
        dependencies={
            'embodied_agent': embodied_agent,
            'nexus': nexus,
        }
    )
    
    planning_engine = PlanningEngine(
        engine_id='planning-engine',
        event_broker=event_broker,
        dependencies={
            'embodied_agent': embodied_agent,
            'nexus': nexus,
        }
    )
    
    # Phase 1: OKR Parsing
    print("\n=== PHASE 1: OKR PARSING ===")
    phase1_context = ExecutionContext(
        okr_id='e2e-test-okr-1',
        correlation_id='e2e-test-1',
        phase='okr-parsing',
        input_data={
            'okr_text': '''\
            Objective: Launch AI-powered OKR execution platform
            
            Key Result: Complete all 6 engines with full cognitive integration
            Key Result: Deploy to production with < 50ms latency
            Key Result: Achieve 95% accuracy on OKR predictions
            ''',
            'org_context': {
                'org_id': 'org-1',
                'org_name': 'Hermes Research',
                'level': 'org'
            }
        }
    )
    
    result1 = await okr_engine.execute(phase1_context)
    print(f"OKREngine result:")
    print(f"  - Errors: {len(result1.errors)}")
    print(f"  - Output: {list(result1.output_data.keys())}")
    print(f"  - Events emitted: {len(event_broker.events)}")
    
    assert len(result1.errors) == 0
    assert 'okr' in result1.output_data
    okr_output = result1.output_data['okr']
    
    # Phase 2: Research
    print("\n=== PHASE 2: RESEARCH ===")
    phase2_context = ExecutionContext(
        okr_id=okr_output['id'],
        correlation_id='e2e-test-1',
        phase='research',
        input_data={
            'okr': okr_output,
            'cognitive_context': result1.output_data.get('cognitive_context', {}),
        }
    )
    
    result2 = await research_engine.execute(phase2_context)
    print(f"ResearchEngine result:")
    print(f"  - Errors: {len(result2.errors)}")
    print(f"  - Output: {list(result2.output_data.keys())}")
    print(f"  - Events emitted: {len(event_broker.events)}")
    
    assert len(result2.errors) == 0
    assert 'research_findings' in result2.output_data
    
    # Phase 3: Planning
    print("\n=== PHASE 3: PLANNING ===")
    phase3_context = ExecutionContext(
        okr_id=okr_output['id'],
        correlation_id='e2e-test-1',
        phase='planning',
        input_data={
            'okr': okr_output,
            'research_findings': result2.output_data.get('research_findings', {}),
            'recommended_approaches': result2.output_data.get('recommended_approaches', []),
            'org_structure': {
                'org_id': 'org-1',
                'org_name': 'Hermes Research',
                'level': 'org',
                'alignment_level': 0.9,
            }
        }
    )
    
    result3 = await planning_engine.execute(phase3_context)
    print(f"PlanningEngine result:")
    print(f"  - Errors: {len(result3.errors)}")
    print(f"  - Output: {list(result3.output_data.keys())}")
    print(f"  - Events emitted: {len(event_broker.events)}")
    
    assert len(result3.errors) == 0
    assert 'goals' in result3.output_data
    assert 'tasks' in result3.output_data
    assert 'accountability_map' in result3.output_data
    
    goals = result3.output_data['goals']
    tasks = result3.output_data['tasks']
    accountability_map = result3.output_data['accountability_map']
    
    print(f"\nPlan Summary:")
    print(f"  - Goals: {len(goals)}")
    print(f"  - Tasks: {len(tasks)}")
    print(f"  - Agents: {len(accountability_map)}")
    
    # Verify event chain
    print(f"\n=== EVENT CHAIN ===")
    assert len(event_broker.events) >= 3
    for i, event in enumerate(event_broker.events):
        print(f"{i+1}. {event['topic']} at {event['timestamp']}")
    
    # Verify goal hierarchy
    hierarchy = result3.output_data['goal_hierarchy']
    level_1 = hierarchy.get('level_1_org', [])
    level_2 = hierarchy.get('level_2_dept', [])
    level_3 = hierarchy.get('level_3_team', [])
    
    print(f"\n=== GOAL HIERARCHY ===")
    print(f"  - Level 1 (Org): {len(level_1)}")
    print(f"  - Level 2 (Dept): {len(level_2)}")
    print(f"  - Level 3 (Team): {len(level_3)}")
    
    # Verify priorities
    priorities = result3.output_data['priority_scores']
    print(f"\n=== PRIORITIES ===")
    top_3 = sorted(priorities.items(), key=lambda x: x[1], reverse=True)[:3]
    for goal_id, score in top_3:
        print(f"  - {goal_id}: {score:.2f}")
    
    print("\n✓ End-to-end planning flow successful!")
    
    return {
        'okr': okr_output,
        'research': result2.output_data,
        'plan': result3.output_data,
        'events': event_broker.events,
    }


# ==================== RUN TESTS ====================

if __name__ == '__main__':
    # Run tests
    pytest.main([__file__, '-v', '-s'])
