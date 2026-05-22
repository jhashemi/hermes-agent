"""
Phase 1.6: Integration Test - ReviewEngine with Code Review + GitHub PR

Tests ReviewEngine: Execution Results → Code Review → GitHub PR
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
from okr_executive.engines.review_engine import ReviewEngine
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
    
    async def review_code(self, **kwargs):
        """Mock code review method"""
        artifacts = kwargs.get('artifacts', [])
        okr_context = kwargs.get('okr_context', {})
        
        return {
            'review': {
                'quality_score': 0.82,
                'code_quality': 'good',
                'documentation': 'adequate',
                'test_coverage': 0.75,
                'issues': [
                    'Missing edge case handling in error scenarios',
                    'Could improve type hints in core functions',
                ],
                'strengths': [
                    'Clean architecture and separation of concerns',
                    'Good logging throughout',
                    'Well-structured error handling',
                ],
            },
            'recommendations': [
                'Add comprehensive error handling for edge cases',
                'Increase test coverage to 90%+',
                'Add more inline documentation for complex logic',
                'Consider adding integration tests',
                'Implement performance monitoring',
            ],
        }


class MockGitHubAdapter:
    """Mock GitHub adapter for testing"""
    
    async def create_pr(self, owner: str, repo: str, pr_data: dict, artifacts: list = None):
        """Mock GitHub PR creation"""
        return {
            'success': True,
            'id': 'pr-12345',
            'number': 123,
            'html_url': f'https://github.com/{owner}/{repo}/pull/123',
            'title': pr_data['title'],
            'body': pr_data['body'],
            'labels': pr_data.get('labels', []),
        }


# ==================== TESTS ====================

@pytest.mark.asyncio
async def test_review_engine_basic():
    """Test ReviewEngine basic execution"""
    
    # Setup
    event_broker = MockEventBroker()
    embodied_agent = MockEmbodiedAgent()
    github_adapter = MockGitHubAdapter()
    
    engine = ReviewEngine(
        engine_id='review-engine-test',
        event_broker=event_broker,
        dependencies={
            'embodied_agent': embodied_agent,
            'github_adapter': github_adapter,
        }
    )
    
    # Create test input
    context = ExecutionContext(
        okr_id='test-okr-1',
        correlation_id='corr-1',
        phase='phase-review',
        input_data={
            'execution_result': {
                'id': 'exec-1',
                'completion_percentage': 85.0,
                'metrics': {'test_coverage': 0.75}
            },
            'okr': {
                'id': 'okr-1',
                'objective': 'Build OKR execution system',
                'key_results': [
                    'Complete all engines',
                    'Deploy to production',
                ],
            },
            'artifacts': [
                {
                    'path': 'src/okr_executive/engines/review_engine.py',
                    'content': 'async def execute(self, context): pass',
                    'type': 'code'
                },
                {
                    'path': 'docs/INTEGRATION_ARCHITECTURE.md',
                    'content': '# Integration Architecture\n\nFull system design...',
                    'type': 'documentation'
                },
            ],
            'github_config': {
                'owner': 'hermes-research',
                'repo': 'okr-executive-system',
                'branch': 'okr-execution',
                'base_branch': 'main',
            }
        }
    )
    
    # Execute
    result = await engine.execute(context)
    
    # Assert
    assert len(result.errors) == 0, f"Errors: {result.errors}"
    assert 'review_summary' in result.output_data
    assert 'pr_url' in result.output_data
    assert 'pr_number' in result.output_data
    assert 'recommendations' in result.output_data
    
    review_summary = result.output_data['review_summary']
    assert review_summary['quality_score'] == 0.82
    
    recommendations = result.output_data['recommendations']
    assert len(recommendations) == 5
    assert 'error handling' in recommendations[0].lower()
    
    # Check event emitted
    assert len(event_broker.events) == 1
    assert event_broker.events[0]['topic'] == 'review.completed'
    
    # Check PR metadata
    assert result.output_data['pr_number'] == 123
    assert 'hermes-research' in result.output_data['pr_url']


@pytest.mark.asyncio
async def test_review_engine_artifact_validation():
    """Test artifact validation in ReviewEngine"""
    
    # Setup
    event_broker = MockEventBroker()
    embodied_agent = MockEmbodiedAgent()
    github_adapter = MockGitHubAdapter()
    
    engine = ReviewEngine(
        engine_id='review-engine-test',
        event_broker=event_broker,
        dependencies={
            'embodied_agent': embodied_agent,
            'github_adapter': github_adapter,
        }
    )
    
    # Test artifact validation
    valid_artifacts = [
        {'path': 'file1.py', 'content': 'code'},
        {'path': 'file2.py', 'content': 'code', 'type': 'code'},
    ]
    
    result = await engine._validate_artifacts(valid_artifacts)
    assert len(result) == 2
    assert result[0]['path'] == 'file1.py'
    assert result[0]['type'] == 'code'  # default type
    
    # Test invalid artifacts
    invalid_artifacts = [
        {'path': 'file1.py'},  # missing content
        'not a dict',  # not a dict
        {'content': 'code'},  # missing path
    ]
    
    result = await engine._validate_artifacts(invalid_artifacts)
    assert len(result) == 0


@pytest.mark.asyncio
async def test_review_engine_pr_metadata_generation():
    """Test GitHub PR metadata generation"""
    
    # Setup
    event_broker = MockEventBroker()
    embodied_agent = MockEmbodiedAgent()
    github_adapter = MockGitHubAdapter()
    
    engine = ReviewEngine(
        engine_id='review-engine-test',
        event_broker=event_broker,
        dependencies={
            'embodied_agent': embodied_agent,
            'github_adapter': github_adapter,
        }
    )
    
    review_summary = {
        'quality_score': 0.85,
        'issues': ['Issue 1', 'Issue 2', 'Issue 3'],
    }
    
    execution_result = {
        'completion_percentage': 90.0,
    }
    
    artifacts = [
        {'path': 'file1.py', 'content': 'code', 'type': 'code'},
        {'path': 'file2.py', 'content': 'code', 'type': 'code'},
    ]
    
    metadata = await engine._generate_pr_metadata(
        okr_id='okr-1',
        objective='Build OKR system',
        review_summary=review_summary,
        artifacts=artifacts,
        execution_result=execution_result
    )
    
    # Verify metadata
    assert 'OKR' in metadata['title']
    assert 'Build OKR system' in metadata['title']
    assert 'OKR Execution & Code Review' in metadata['description']
    assert '90.0%' in metadata['description']
    assert 'quality-good' in metadata['labels']
    assert 'okr-system' in metadata['labels']
    assert 'mostly-complete' in metadata['labels']  # 90% completion


@pytest.mark.asyncio
async def test_review_engine_labels():
    """Test GitHub PR label generation"""
    
    # Setup
    event_broker = MockEventBroker()
    embodied_agent = MockEmbodiedAgent()
    github_adapter = MockGitHubAdapter()
    
    engine = ReviewEngine(
        engine_id='review-engine-test',
        event_broker=event_broker,
        dependencies={
            'embodied_agent': embodied_agent,
            'github_adapter': github_adapter,
        }
    )
    
    # Test high quality
    review_summary = {'quality_score': 0.95, 'issues': []}
    execution_result = {'completion_percentage': 100.0}
    labels = engine._generate_pr_labels(review_summary, execution_result)
    assert 'quality-excellent' in labels
    assert 'issues-none' in labels
    assert 'complete' in labels
    
    # Test low quality
    review_summary = {'quality_score': 0.4, 'issues': ['Issue 1', 'Issue 2', 'Issue 3', 'Issue 4', 'Issue 5', 'Issue 6']}
    execution_result = {'completion_percentage': 50.0}
    labels = engine._generate_pr_labels(review_summary, execution_result)
    assert 'quality-needs-improvement' in labels
    assert 'issues-many' in labels
    assert 'in-progress' in labels


@pytest.mark.asyncio
async def test_review_engine_without_github():
    """Test ReviewEngine behavior when GitHub adapter is not available"""
    
    # Setup - no GitHub adapter
    event_broker = MockEventBroker()
    embodied_agent = MockEmbodiedAgent()
    
    engine = ReviewEngine(
        engine_id='review-engine-test',
        event_broker=event_broker,
        dependencies={
            'embodied_agent': embodied_agent,
            # No github_adapter
        }
    )
    
    # Create test input
    context = ExecutionContext(
        okr_id='test-okr-1',
        correlation_id='corr-1',
        phase='phase-review',
        input_data={
            'execution_result': {
                'id': 'exec-1',
                'completion_percentage': 85.0,
            },
            'okr': {
                'id': 'okr-1',
                'objective': 'Build OKR execution system',
                'key_results': ['Complete all engines'],
            },
            'artifacts': [
                {
                    'path': 'src/engine.py',
                    'content': 'code',
                    'type': 'code'
                },
            ],
            'github_config': {
                'owner': 'hermes-research',
                'repo': 'okr-executive-system',
            }
        }
    )
    
    # Execute
    result = await engine.execute(context)
    
    # Should still succeed but with warning about PR
    assert len(result.errors) == 1
    assert 'PR creation failed' in result.errors[0]
    assert result.output_data['pr_url'] is None
    assert result.output_data['pr_number'] is None
    # But review should still be present
    assert 'review_summary' in result.output_data
    assert 'recommendations' in result.output_data


@pytest.mark.asyncio
async def test_end_to_end_okr_with_review():
    """Test complete OKR → Parse → Research → Review flow"""
    
    print("\n=== PHASE 0: SETUP ===" )
    
    # Setup services
    event_broker = MockEventBroker()
    nexus = MockNexus()
    embodied_agent = MockEmbodiedAgent()
    github_adapter = MockGitHubAdapter()
    
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
    
    review_engine = ReviewEngine(
        engine_id='review-engine',
        event_broker=event_broker,
        dependencies={
            'embodied_agent': embodied_agent,
            'github_adapter': github_adapter,
        }
    )
    
    # Phase 1: OKR Parsing
    print("\n=== PHASE 1: OKR PARSING ===" )
    phase1_context = ExecutionContext(
        okr_id='e2e-okr-1',
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
    print(f"  - Output keys: {list(result1.output_data.keys())}")
    print(f"  - Events emitted: {len(event_broker.events)}")
    
    assert len(result1.errors) == 0
    assert 'okr' in result1.output_data
    okr_output = result1.output_data['okr']
    
    # Phase 2: Research
    print("\n=== PHASE 2: RESEARCH ===" )
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
    print(f"  - Output keys: {list(result2.output_data.keys())}")
    print(f"  - Events emitted total: {len(event_broker.events)}")
    
    assert len(result2.errors) == 0
    assert 'research_findings' in result2.output_data
    
    # Phase 3: Review with PR
    print("\n=== PHASE 3: CODE REVIEW & PR ===" )
    phase3_context = ExecutionContext(
        okr_id=okr_output['id'],
        correlation_id='e2e-test-1',
        phase='review',
        input_data={
            'execution_result': {
                'id': 'exec-1',
                'completion_percentage': 90.0,
                'metrics': {
                    'test_coverage': 0.82,
                    'code_quality': 'good',
                }
            },
            'okr': okr_output,
            'artifacts': [
                {
                    'path': 'src/okr_executive/engines/okr_engine.py',
                    'content': 'async def execute(self, context): pass',
                    'type': 'code'
                },
                {
                    'path': 'src/okr_executive/engines/research_engine.py',
                    'content': 'async def execute(self, context): pass',
                    'type': 'code'
                },
                {
                    'path': 'src/okr_executive/engines/review_engine.py',
                    'content': 'async def execute(self, context): pass',
                    'type': 'code'
                },
                {
                    'path': 'docs/INTEGRATION_ARCHITECTURE.md',
                    'content': '# OKR Executive System\n\nFull integration...',
                    'type': 'documentation'
                },
            ],
            'github_config': {
                'owner': 'hermes-research',
                'repo': 'okr-executive-system',
                'branch': 'okr-execution',
                'base_branch': 'main',
            }
        }
    )
    
    result3 = await review_engine.execute(phase3_context)
    print(f"ReviewEngine result:")
    print(f"  - Errors: {len(result3.errors)}")
    print(f"  - Output keys: {list(result3.output_data.keys())}")
    print(f"  - Events emitted total: {len(event_broker.events)}")
    print(f"  - PR URL: {result3.output_data['pr_url']}")
    print(f"  - PR Number: {result3.output_data['pr_number']}")
    
    assert len(result3.errors) == 0
    assert result3.output_data['pr_url'] is not None
    assert result3.output_data['pr_number'] == 123
    assert result3.output_data['artifacts_reviewed'] == 4
    
    # Verify complete event chain
    print(f"\n=== EVENT CHAIN ===" )
    assert len(event_broker.events) == 3
    events_by_type = [e['topic'] for e in event_broker.events]
    print(f"Events: {events_by_type}")
    assert 'okr.parsed' in events_by_type
    assert 'research.completed' in events_by_type
    assert 'review.completed' in events_by_type
    
    print("\n✓ End-to-end OKR → Research → Review flow successful!")
    
    return {
        'okr': okr_output,
        'research_findings': result2.output_data,
        'review_pr': result3.output_data,
        'events': event_broker.events,
    }


# ==================== RUN TESTS ====================

if __name__ == '__main__':
    # Run tests
    pytest.main([__file__, '-v', '-s'])
