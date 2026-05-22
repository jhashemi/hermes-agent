"""
Phase 3: Integration Test - ExecutionEngine with VCG Allocation

Tests complete OKR flow: Parse → Research → Plan → Execution

Integration points tested:
- Goal to Task conversion
- VCG dispatcher allocation
- VoiceTwin session creation
- LiveKit WebRTC setup
- Multi-agent coordination
- Accountability record generation
- ExecutionStartedEvent emission
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
from okr_executive.engines.execution_engine import ExecutionEngine, TaskMetrics
from okr_executive.domain.models import Goal, Task


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


class MockVCGDispatcher:
    """Mock VCG Dispatcher"""
    def __init__(self):
        self.allocations = []
    
    def dispatch_task(self,
                      task_id: str,
                      title: str,
                      required_skills: list,
                      reach: int = 5,
                      impact: int = 5,
                      confidence: float = 0.5,
                      effort: float = 1.0):
        """Mock VCG allocation"""
        
        # Simple round-robin to different agents
        agents = ['voice-twin-1', 'voice-twin-2', 'voice-twin-3']
        agent_idx = len(self.allocations) % len(agents)
        agent_id = agents[agent_idx]
        
        # Mock allocation result
        from dataclasses import dataclass
        @dataclass
        class MockAllocationResult:
            agent_id: str
            welfare: float
            clarke_tax: float
            skill_match: float
        
        result = MockAllocationResult(
            agent_id=agent_id,
            welfare=(reach * impact * confidence) / max(effort, 0.5),
            clarke_tax=2.0,
            skill_match=0.8 + (0.1 * (agent_idx % 2)),
        )
        
        self.allocations.append((task_id, result))
        return result


class MockVoiceTwinsBridge:
    """Mock VoiceTwins bridge"""
    async def load(self, params: dict):
        """Mock loading a VoiceTwin session"""
        return {
            'session_id': f"voice-session-{params.get('agent_id')}-{params.get('task_id')[:8]}",
            'agent_id': params.get('agent_id'),
            'status': 'ready',
        }


class MockLiveKitAdapter:
    """Mock LiveKit adapter"""
    async def create_session(self, params: dict):
        """Mock creating a LiveKit session"""
        return {
            'session_id': f"livekit-{params.get('task_id')[:8]}",
            'room_name': f"room-{params.get('okr_id')}",
            'token': f"token-{params.get('agent_id')}",
        }


class MockRepository:
    """Mock repository for storing data"""
    def __init__(self):
        self.stored_data = {
            'tasks': [],
            'accountability_records': [],
        }
    
    async def store(self, collection: str, data: dict):
        """Store data in collection"""
        if collection not in self.stored_data:
            self.stored_data[collection] = []
        self.stored_data[collection].append(data)


# ==================== FIXTURE: MOCK PLANNING ENGINE OUTPUT ====================

def create_sample_goals() -> list:
    """Create sample goals as if from PlanningEngine"""
    return [
        {
            'id': 'goal-1',
            'level': 3,
            'description': 'Implement core VCG allocation algorithm',
            'estimated_hours': 16.0,
            'cognitive_complexity': 'complex',
            'required_skills': ['algorithm-design', 'game-theory', 'optimization'],
            'priority_score': 0.9,
        },
        {
            'id': 'goal-2',
            'level': 3,
            'description': 'Create VoiceTwin session management',
            'estimated_hours': 12.0,
            'cognitive_complexity': 'medium',
            'required_skills': ['voice-processing', 'session-management'],
            'priority_score': 0.8,
        },
        {
            'id': 'goal-3',
            'level': 3,
            'description': 'Set up LiveKit WebRTC coordination',
            'estimated_hours': 10.0,
            'cognitive_complexity': 'medium',
            'required_skills': ['webrtc', 'network-streaming'],
            'priority_score': 0.75,
        },
        {
            'id': 'goal-4',
            'level': 3,
            'description': 'Create task accountability records',
            'estimated_hours': 8.0,
            'cognitive_complexity': 'simple',
            'required_skills': ['data-modeling', 'audit-trails'],
            'priority_score': 0.7,
        },
    ]


# ==================== TESTS ====================

@pytest.mark.asyncio
async def test_execution_engine_goal_to_task_conversion():
    """Test converting goals to task metrics"""
    
    # Setup
    event_broker = MockEventBroker()
    engine = ExecutionEngine(
        engine_id='execution-engine-test',
        event_broker=event_broker,
        dependencies={}
    )
    
    # Create sample goals
    goals = create_sample_goals()
    
    # Execute conversion
    task_metrics = await engine._convert_goals_to_task_metrics(goals, 'okr-1')
    
    # Assert
    assert len(task_metrics) == len(goals)
    
    for i, metrics in enumerate(task_metrics):
        assert metrics.goal_id == goals[i]['id']
        assert metrics.description == goals[i]['description']
        assert metrics.estimated_hours == goals[i]['estimated_hours']
        assert metrics.rice_score is not None
        assert metrics.rice_score > 0
    
    print(f"✓ Converted {len(task_metrics)} goals to task metrics")
    for m in task_metrics:
        print(f"  - {m.description[:50]}... (RICE={m.rice_score:.1f})")


@pytest.mark.asyncio
async def test_execution_engine_vcg_allocation():
    """Test VCG allocation of tasks"""
    
    # Setup
    event_broker = MockEventBroker()
    vcg_dispatcher = MockVCGDispatcher()
    
    engine = ExecutionEngine(
        engine_id='execution-engine-test',
        event_broker=event_broker,
        dependencies={'vcg_dispatcher': vcg_dispatcher}
    )
    
    # Create task metrics
    goals = create_sample_goals()
    task_metrics = await engine._convert_goals_to_task_metrics(goals, 'okr-1')
    
    # Execute VCG allocation
    allocations = await engine._allocate_tasks_via_vcg(task_metrics)
    
    # Assert
    assert len(allocations) == len(task_metrics)
    
    for allocation in allocations:
        assert allocation.agent_id in ['voice-twin-1', 'voice-twin-2', 'voice-twin-3']
        assert allocation.welfare > 0
        assert 0.5 <= allocation.skill_match <= 1.0
    
    # Check multi-agent distribution
    agents = set(a.agent_id for a in allocations)
    assert len(agents) >= 1  # At least one agent
    
    print(f"✓ Allocated {len(allocations)} tasks to {len(agents)} agents")
    for agent in agents:
        tasks_for_agent = [a for a in allocations if a.agent_id == agent]
        print(f"  - {agent}: {len(tasks_for_agent)} tasks")


@pytest.mark.asyncio
async def test_execution_engine_session_creation():
    """Test creating VoiceTwin and LiveKit sessions"""
    
    # Setup
    event_broker = MockEventBroker()
    voice_twins = MockVoiceTwinsBridge()
    livekit = MockLiveKitAdapter()
    vcg_dispatcher = MockVCGDispatcher()
    
    engine = ExecutionEngine(
        engine_id='execution-engine-test',
        event_broker=event_broker,
        dependencies={
            'vcg_dispatcher': vcg_dispatcher,
            'voice_twins': voice_twins,
            'livekit': livekit,
        }
    )
    
    # Create allocations
    from okr_executive.engines.execution_engine import AgentAllocation
    allocations = [
        AgentAllocation(
            task_id='task-1',
            agent_id='voice-twin-1',
            welfare=50.0,
            clarke_tax=2.0,
            skill_match=0.8,
            estimated_completion_time=datetime.now() + timedelta(hours=16),
        ),
        AgentAllocation(
            task_id='task-2',
            agent_id='voice-twin-2',
            welfare=45.0,
            clarke_tax=1.5,
            skill_match=0.75,
            estimated_completion_time=datetime.now() + timedelta(hours=12),
        ),
    ]
    
    # Create sessions
    sessions = await engine._create_agent_sessions(allocations, 'okr-1')
    
    # Assert
    assert len(sessions) == len(allocations)
    
    for session in sessions:
        assert session['status'] == 'ready'
        assert 'voice_twin_session_id' in session
        assert 'livekit_session_id' in session
        assert 'livekit_room' in session
        assert 'livekit_token' in session
    
    print(f"✓ Created {len(sessions)} sessions")
    for s in sessions:
        print(f"  - Task {s['task_id']}: voice={s['voice_twin_session_id'][:20]}..., livekit={s['livekit_room']}")


@pytest.mark.asyncio
async def test_execution_engine_task_object_creation():
    """Test creating Task objects"""
    
    # Setup
    event_broker = MockEventBroker()
    engine = ExecutionEngine(
        engine_id='execution-engine-test',
        event_broker=event_broker,
        dependencies={}
    )
    
    # Create task metrics and allocations
    goals = create_sample_goals()
    task_metrics = await engine._convert_goals_to_task_metrics(goals, 'okr-1')
    
    from okr_executive.engines.execution_engine import AgentAllocation
    allocations = [
        AgentAllocation(
            task_id=tm.task_id,
            agent_id=f'voice-twin-{(i % 3) + 1}',
            welfare=tm.rice_score or 50.0,
            clarke_tax=2.0,
            skill_match=0.8,
            estimated_completion_time=datetime.now() + timedelta(hours=tm.estimated_hours),
        )
        for i, tm in enumerate(task_metrics)
    ]
    
    sessions = [
        {
            'task_id': a.task_id,
            'agent_id': a.agent_id,
            'status': 'ready',
        }
        for a in allocations
    ]
    
    # Create Task objects
    tasks = await engine._create_task_objects(task_metrics, allocations, sessions)
    
    # Assert
    assert len(tasks) == len(task_metrics)
    
    for task in tasks:
        assert task.description
        assert task.assigned_agent.startswith('voice-twin-')
        assert task.estimated_hours > 0
        assert task.status == 'pending'
        assert task.cognitive_complexity in ['simple', 'medium', 'complex', 'expert']
    
    print(f"✓ Created {len(tasks)} Task objects")
    for t in tasks:
        print(f"  - {t.description[:40]}... → {t.assigned_agent} ({t.estimated_hours}h)")


@pytest.mark.asyncio
async def test_execution_engine_accountability_records():
    """Test creating accountability records"""
    
    # Setup
    event_broker = MockEventBroker()
    engine = ExecutionEngine(
        engine_id='execution-engine-test',
        event_broker=event_broker,
        dependencies={}
    )
    
    # Create test tasks
    tasks = [
        Task(
            okr_id='okr-1',
            goal_id='goal-1',
            description='Test task 1',
            assigned_agent='voice-twin-1',
            required_skills=['skill-1'],
            cognitive_complexity='medium',
            estimated_hours=8.0,
            timeline_start=datetime.now(),
            timeline_end=datetime.now() + timedelta(hours=8),
        ),
        Task(
            okr_id='okr-1',
            goal_id='goal-2',
            description='Test task 2',
            assigned_agent='voice-twin-2',
            required_skills=['skill-2'],
            cognitive_complexity='simple',
            estimated_hours=4.0,
            timeline_start=datetime.now(),
            timeline_end=datetime.now() + timedelta(hours=4),
        ),
    ]
    
    from okr_executive.engines.execution_engine import AgentAllocation
    allocations = [
        AgentAllocation(
            task_id=t.id,
            agent_id=t.assigned_agent,
            welfare=50.0,
            clarke_tax=2.0,
            skill_match=0.8,
            estimated_completion_time=t.timeline_end,
        )
        for t in tasks
    ]
    
    # Create accountability records
    records = await engine._create_accountability_records(tasks, allocations, 'okr-1')
    
    # Assert
    assert len(records) == len(tasks)
    
    for i, record in enumerate(records):
        assert record.okr_id == 'okr-1'
        assert record.assigned_agent == tasks[i].assigned_agent
        assert record.start_date == tasks[i].timeline_start
        assert record.due_date == tasks[i].timeline_end
        assert record.status == 'in_progress'
    
    print(f"✓ Created {len(records)} accountability records")


@pytest.mark.asyncio
async def test_execution_engine_full_flow():
    """Test complete execution flow: Goals → Tasks → Allocations → Sessions"""
    
    # Setup
    event_broker = MockEventBroker()
    vcg_dispatcher = MockVCGDispatcher()
    voice_twins = MockVoiceTwinsBridge()
    livekit = MockLiveKitAdapter()
    repository = MockRepository()
    
    engine = ExecutionEngine(
        engine_id='execution-engine',
        event_broker=event_broker,
        dependencies={
            'vcg_dispatcher': vcg_dispatcher,
            'voice_twins': voice_twins,
            'livekit': livekit,
            'repository': repository,
        }
    )
    
    # Create sample goals (as from PlanningEngine)
    goals = create_sample_goals()
    
    # Create execution context
    context = ExecutionContext(
        okr_id='okr-1',
        correlation_id='e2e-test-execution',
        phase='execution',
        input_data={
            'goals': goals,
            'okr_id': 'okr-1',
            'org_context': {'org_id': 'org-1'},
        }
    )
    
    # Execute
    print("\n=== FULL EXECUTION FLOW ===")
    result = await engine.execute(context)
    
    # Assert no errors
    assert len(result.errors) == 0, f"Errors: {result.errors}"
    
    # Assert output data
    assert 'tasks' in result.output_data
    assert 'allocations' in result.output_data
    assert 'accountability_records' in result.output_data
    assert 'livekit_sessions' in result.output_data
    
    tasks = result.output_data['tasks']
    allocations = result.output_data['allocations']
    records = result.output_data['accountability_records']
    sessions = result.output_data['livekit_sessions']
    
    # Verify counts
    assert len(tasks) == len(goals)
    assert len(allocations) == len(goals)
    assert len(records) == len(goals)
    assert len(sessions) == len(goals)
    
    # Verify multi-agent distribution
    agents = set(a['agent_id'] for a in allocations)
    assert len(agents) >= 1
    
    # Verify event emitted
    assert len(event_broker.events) >= 1
    assert any(e['topic'] == 'execution.started' for e in event_broker.events)
    
    # Verify storage
    assert len(repository.stored_data['tasks']) == len(goals)
    assert len(repository.stored_data['accountability_records']) == len(goals)
    
    # Print results
    print(f"\n✓ Complete execution flow successful!")
    print(f"  - Tasks created: {len(tasks)}")
    print(f"  - Agents allocated: {len(agents)}")
    print(f"  - Sessions created: {len(sessions)}")
    print(f"  - Accountability records: {len(records)}")
    print(f"  - Events emitted: {len(event_broker.events)}")
    
    print(f"\nAgent assignments:")
    for agent in sorted(agents):
        agent_tasks = [a for a in allocations if a['agent_id'] == agent]
        total_hours = sum(a['estimated_completion_time'] for a in agent_tasks if isinstance(a, dict))
        print(f"  - {agent}: {len(agent_tasks)} tasks")
    
    return result


@pytest.mark.asyncio
async def test_multi_agent_coordination():
    """Test multi-agent coordination tracking"""
    
    # Setup
    event_broker = MockEventBroker()
    vcg_dispatcher = MockVCGDispatcher()
    
    engine = ExecutionEngine(
        engine_id='execution-engine',
        event_broker=event_broker,
        dependencies={'vcg_dispatcher': vcg_dispatcher}
    )
    
    # Create multiple goals
    goals = create_sample_goals()
    
    # Convert to task metrics
    task_metrics = await engine._convert_goals_to_task_metrics(goals, 'okr-1')
    
    # Allocate via VCG
    allocations = await engine._allocate_tasks_via_vcg(task_metrics)
    
    # Build agent assignment map
    agent_map = engine._build_agent_assignment_map(
        await engine._create_task_objects(
            task_metrics,
            allocations,
            [{'task_id': a.task_id, 'agent_id': a.agent_id} for a in allocations]
        )
    )
    
    # Assert multi-agent coordination
    assert len(agent_map) >= 1
    
    total_tasks = sum(len(tasks) for tasks in agent_map.values())
    assert total_tasks == len(goals)
    
    print(f"✓ Multi-agent coordination verified")
    print(f"  - Agents: {len(agent_map)}")
    print(f"  - Total tasks: {total_tasks}")
    for agent, tasks in agent_map.items():
        print(f"    - {agent}: {len(tasks)} tasks")


# ==================== RUN TESTS ====================

if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])
