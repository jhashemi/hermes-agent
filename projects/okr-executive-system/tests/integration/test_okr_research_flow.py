"""
Phase 1.5: Integration Test - OKR through Engines

Tests complete OKR flow: Parse → Research
"""

import pytest
import asyncio
from datetime import datetime
from okr_executive.engines.base import (
    ExecutionContext,
    EventType,
)
from okr_executive.engines.okr_engine import OKREngine
from okr_executive.engines.research_engine import ResearchEngine
from okr_executive.domain.models import OKR


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
    
    async def validate(self, params: dict):
        return {
            'valid': True,
            'issues': []
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


# ==================== TESTS ====================

@pytest.mark.asyncio
async def test_okr_engine_parse():
    """Test OKREngine parsing"""
    
    # Setup
    event_broker = MockEventBroker()
    nexus = MockNexus()
    
    engine = OKREngine(
        engine_id='okr-engine-test',
        event_broker=event_broker,
        dependencies={'nexus': nexus}
    )
    
    # Create test input
    context = ExecutionContext(
        okr_id='test-okr-1',
        correlation_id='corr-1',
        phase='phase-1',
        input_data={
            'okr_text': '''
            Objective: Build AI-powered OKR execution system
            Key Result: Complete Phase 1 in 4 hours
            Key Result: All 6 engines wired by EOW
            ''',
            'org_context': {'org_id': 'org-1'}
        }
    )
    
    # Execute
    result = await engine.execute(context)
    
    # Assert
    assert len(result.errors) == 0, f"Errors: {result.errors}"
    assert 'okr' in result.output_data
    
    okr = result.output_data['okr']
    assert 'Build AI-powered' in okr['objective']
    assert len(okr['key_results']) >= 2
    
    # Check event emitted
    assert len(event_broker.events) == 1
    assert event_broker.events[0]['topic'] == 'okr.parsed'


@pytest.mark.asyncio
async def test_research_engine_research():
    """Test ResearchEngine execution"""
    
    # Setup
    event_broker = MockEventBroker()
    embodied_agent = MockEmbodiedAgent()
    nexus = MockNexus()
    
    engine = ResearchEngine(
        engine_id='research-engine-test',
        event_broker=event_broker,
        dependencies={
            'embodied_agent': embodied_agent,
            'nexus': nexus,
        }
    )
    
    # Create test input (output from OKREngine)
    context = ExecutionContext(
        okr_id='test-okr-1',
        correlation_id='corr-1',
        phase='phase-2',
        input_data={
            'okr': {
                'id': 'okr-1',
                'objective': 'Build OKR execution system',
                'key_results': [
                    'Complete Phase 1 in 4 hours',
                    'All 6 engines working',
                ]
            },
            'cognitive_context': {
                'source': 'nexus',
                'scope': 'organization'
            }
        }
    )
    
    # Execute
    result = await engine.execute(context)
    
    # Assert
    assert len(result.errors) == 0, f"Errors: {result.errors}"
    assert 'research_findings' in result.output_data
    assert 'recommended_approaches' in result.output_data
    assert 'confidence_scores' in result.output_data
    
    approaches = result.output_data['recommended_approaches']
    assert len(approaches) > 0
    assert approaches[0]['name'] == 'Approach A'
    
    # Check confidence scores
    confidence = result.output_data['confidence_scores']
    assert 'overall' in confidence
    assert confidence['overall'] > 0.5


@pytest.mark.asyncio
async def test_end_to_end_okr_flow():
    """Test complete OKR → Parse → Research flow"""
    
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
    
    # Phase 1: OKR Parsing
    print("\n=== PHASE 1: OKR PARSING ===")
    phase1_context = ExecutionContext(
        okr_id='test-okr-1',
        correlation_id='e2e-test-1',
        phase='okr-parsing',
        input_data={
            'okr_text': '''
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
    assert 'recommended_approaches' in result2.output_data
    
    approaches = result2.output_data['recommended_approaches']
    print(f"  - Approaches: {len(approaches)}")
    for approach in approaches[:3]:
        print(f"    - {approach['name']}: {approach['effort']}, {approach['timeline_weeks']}w")
    
    # Verify event chain
    print(f"\n=== EVENT CHAIN ===")
    assert len(event_broker.events) >= 2
    for i, event in enumerate(event_broker.events):
        print(f"{i+1}. {event['topic']} at {event['timestamp']}")
    
    print("\n✓ End-to-end OKR flow successful!")
    return {
        'okr': okr_output,
        'research_findings': result2.output_data,
        'events': event_broker.events,
    }


# ==================== RUN TESTS ====================

if __name__ == '__main__':
    # Run tests
    pytest.main([__file__, '-v', '-s'])
