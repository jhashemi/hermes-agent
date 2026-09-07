"""Tests for kanban rule distiller (VFE-NERVE-05).

Regression: replay t_c43af288 (motivating incident) and verify the distiller
generates a sensible cascade rule.
"""

import asyncio
import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock

import sys
from pathlib import Path

# Add plugin root to path for imports
_plugin_root = Path(__file__).parent.parent
if str(_plugin_root) not in sys.path:
    sys.path.insert(0, str(_plugin_root))

from src.distiller import (
    RuleDistiller,
    ECARule,
    RuleProposal,
)


@pytest.fixture
def distiller(tmp_path):
    """Create a distiller instance with temporary paths."""
    db_path = tmp_path / "kanban.db"
    rules_path = tmp_path / "eca_rules.yaml"
    
    return RuleDistiller(
        kanban_db_path=db_path,
        eca_rules_path=rules_path,
        poll_interval=0.1,  # Fast for tests
    )


@pytest.mark.asyncio
async def test_distiller_initialization(distiller):
    """Test distiller initializes with correct paths."""
    # Parent directories should exist (created by __init__)
    assert distiller.kanban_db_path.parent.exists()
    assert distiller.eca_rules_path.parent.exists()
    assert distiller.model == "haiku-4-5"
    assert distiller.poll_interval == 0.1


def test_is_operator_override_completion(distiller):
    """Test operator-override detection."""
    
    # Test: metadata.operator_override = true
    task = {"id": "t_123"}
    run = {
        "metadata": json.dumps({"operator_override": True}),
        "summary": "Fixed the blocker",
    }
    assert distiller._is_operator_override_completion(task, run)
    
    # Test: summary contains "operator authority"
    task = {"id": "t_123"}
    run = {
        "metadata": "{}",
        "summary": "Operator authority override — forced closure due to triage false-positive",
    }
    assert distiller._is_operator_override_completion(task, run)
    
    # Test: no override flag
    task = {"id": "t_123"}
    run = {
        "metadata": "{}",
        "summary": "Normal completion",
    }
    assert not distiller._is_operator_override_completion(task, run)


def test_load_save_eca_rules(distiller):
    """Test loading and saving ECA rules."""
    
    # Initially empty
    rules = distiller._load_eca_rules()
    assert rules["version"] == 1
    assert rules["rules"] == []
    
    # Add a rule
    rule_data = {
        "version": 1,
        "rules": [
            {
                "id": "test_rule",
                "event": "kanban_block_loop_detected",
                "condition": "x > 0",
                "action": "auto_unblock",
                "audit_note": "Test",
                "provenance": {"source_incident": "t_123"},
            }
        ]
    }
    
    distiller._save_eca_rules(rule_data)
    
    # Load it back
    loaded = distiller._load_eca_rules()
    assert loaded["rules"][0]["id"] == "test_rule"


def test_eca_rule_dataclass():
    """Test ECARule serialization."""
    rule = ECARule(
        id="cascade_review",
        event="block_loop_detected",
        condition="reason contains 'review'",
        action="auto_unblock",
        audit_note="Test cascade rule",
        provenance={
            "source_incident": "t_c43af288",
            "distilled_at": "2026-08-16T22:32:00Z",
            "distiller_model": "haiku-4-5",
            "ratified_by": "jeff_dean",
            "ratified_at": "2026-08-16T22:35:00Z",
        }
    )
    
    rule_dict = rule.to_dict()
    assert rule_dict["id"] == "cascade_review"
    assert rule_dict["provenance"]["source_incident"] == "t_c43af288"


@pytest.mark.asyncio
async def test_poll_for_operator_overrides_no_db(distiller):
    """Test poll with nonexistent DB (graceful degradation)."""
    # DB path doesn't exist
    assert not distiller.kanban_db_path.exists()
    
    # Should return 0 proposals gracefully
    new_proposals = await distiller.poll_for_operator_overrides()
    assert new_proposals == 0


@pytest.mark.asyncio
async def test_rule_proposal_dataclass():
    """Test RuleProposal creation and tracking."""
    rule = ECARule(
        id="test",
        event="block",
        condition="x",
        action="y",
        audit_note="z",
        provenance={},
    )
    
    proposal = RuleProposal(
        source_incident="t_123",
        rule=rule,
    )
    
    assert proposal.source_incident == "t_123"
    assert proposal.rule.id == "test"
    assert proposal.ratified is False
    assert proposal.proposal_ticket is None


# TODO: Integration test for full distillation pipeline once NERVE-01 schema lands
# @pytest.mark.asyncio
# async def test_regression_t_c43af288():
#     """Regression: verify distiller generates sensible cascade rule from t_c43af288."""
#     # Replay the motivating incident and verify rule proposal
#     pass
