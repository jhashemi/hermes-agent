"""Extended tests for cognitive memory plugin with prediction support."""

import pytest
import json
import tempfile
from pathlib import Path
from plugins.memory.cognitive import (
    _StandaloneAuditTrail,
    CognitiveMemoryProvider,
    PREDICT_SCHEMA,
    DECIDE_SCHEMA,
    ALL_TOOL_SCHEMAS,
)


class TestPredictSchema:
    """Test cognitive_predict tool schema."""
    
    def test_schema_registered(self):
        assert "cognitive_predict" in [s["name"] for s in ALL_TOOL_SCHEMAS]
        
    def test_schema_structure(self):
        assert PREDICT_SCHEMA["name"] == "cognitive_predict"
        assert "prediction" in PREDICT_SCHEMA["parameters"]["properties"]
        assert "falsifier" in PREDICT_SCHEMA["parameters"]["properties"]
        assert "substrate" in PREDICT_SCHEMA["parameters"]["properties"]
        assert "budget" in PREDICT_SCHEMA["parameters"]["properties"]
        
    def test_schema_required_fields(self):
        required = PREDICT_SCHEMA["parameters"]["required"]
        assert "prediction" in required
        assert "falsifier" in required
        assert "substrate" in required
        assert "budget" not in required  # Has default
        
    def test_schema_substrate_enum(self):
        substrate_enum = PREDICT_SCHEMA["parameters"]["properties"]["substrate"]["enum"]
        assert "python-repl-fresh-import" in substrate_enum
        assert "running-gateway" in substrate_enum
        assert "production-service" in substrate_enum


class TestStandaloneAuditTrailPredictions:
    """Test prediction management in audit trail."""
    
    def test_record_prediction(self):
        trail = _StandaloneAuditTrail(":memory:")
        
        pid = trail.record_prediction(
            agent_id="test-agent",
            prediction="Code is COMPLETE",
            falsifier="Tests fail on main",
            substrate="bare-repo-main",
            budget=3,
        )
        
        assert pid.startswith("P-")
        assert "test-a" in pid
        
    def test_get_open_predictions(self):
        trail = _StandaloneAuditTrail(":memory:")
        
        pid1 = trail.record_prediction("agent1", "Pred1", "F1", "bare-repo-main", 3)
        pid2 = trail.record_prediction("agent1", "Pred2", "F2", "production-service", 2)
        trail.record_prediction("agent2", "Pred3", "F3", "bare-repo-main", 1)
        
        agent1_preds = trail.get_open_predictions(agent_id="agent1")
        assert len(agent1_preds) == 2
        assert all(p["status"] == "open" for p in agent1_preds)
        
    def test_ratify_prediction(self):
        trail = _StandaloneAuditTrail(":memory:")
        pid = trail.record_prediction("agent1", "Pred", "F", "bare-repo-main", 3)
        
        # Initially open
        open_preds = trail.get_open_predictions("agent1")
        assert len(open_preds) == 1
        
        # Ratify it
        success = trail.ratify_prediction(pid)
        assert success is True
        
        # No longer in open list
        open_preds = trail.get_open_predictions("agent1")
        assert len(open_preds) == 0
        
    def test_decrement_prediction_budget(self):
        trail = _StandaloneAuditTrail(":memory:")
        pid = trail.record_prediction("agent1", "Pred", "F", "bare-repo-main", 2)
        
        # Decrement to 1
        still_open = trail.decrement_prediction_budget(pid)
        assert still_open is True
        
        # Check budget
        preds = trail._predictions
        assert preds[pid]["budget"] == 1
        
        # Decrement to 0 (stale)
        still_open = trail.decrement_prediction_budget(pid)
        assert still_open is False  # Budget is now 0, prediction stale
        
        # Should not be in open list anymore
        open_preds = trail.get_open_predictions("agent1")
        assert len(open_preds) == 0


class TestCognitiveProviderPredictTool:
    """Test cognitive_predict tool in CognitiveMemoryProvider."""
    
    def test_handle_cognitive_predict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = CognitiveMemoryProvider()
            provider.initialize(
                session_id="test-session",
                hermes_home=tmpdir,
                agent_identity="test-agent",
            )
            
            result = provider.handle_tool_call(
                tool_name="cognitive_predict",
                args={
                    "prediction": "NERVE chain is COMPLETE",
                    "falsifier": "Live regression on prod",
                    "substrate": "production-service",
                    "budget": 3,
                },
            )
            
            result_dict = json.loads(result)
            assert result_dict["prediction_id"].startswith("P-")
            assert result_dict["open_predictions"] == 1
            assert result_dict["remaining_budget"] == 3
            
    def test_handle_cognitive_predict_missing_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = CognitiveMemoryProvider()
            provider.initialize(
                session_id="test-session",
                hermes_home=tmpdir,
                agent_identity="test-agent",
            )
            
            result = provider.handle_tool_call(
                tool_name="cognitive_predict",
                args={
                    "prediction": "NERVE chain is COMPLETE",
                    "falsifier": "",  # MISSING
                    "substrate": "production-service",
                },
            )
            
            assert "error" in result.lower() or "required" in result.lower()
            
    def test_multiple_predictions_tracked(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            provider = CognitiveMemoryProvider()
            provider.initialize(
                session_id="test-session",
                hermes_home=tmpdir,
                agent_identity="test-agent",
            )
            
            # Record first prediction
            result1 = provider.handle_tool_call(
                tool_name="cognitive_predict",
                args={
                    "prediction": "Prediction 1",
                    "falsifier": "F1",
                    "substrate": "bare-repo-main",
                    "budget": 2,
                },
            )
            r1_dict = json.loads(result1)
            assert r1_dict["open_predictions"] == 1
            
            # Record second prediction
            result2 = provider.handle_tool_call(
                tool_name="cognitive_predict",
                args={
                    "prediction": "Prediction 2",
                    "falsifier": "F2",
                    "substrate": "production-service",
                    "budget": 1,
                },
            )
            r2_dict = json.loads(result2)
            assert r2_dict["open_predictions"] == 2


class TestToolSchemaConsistency:
    """Ensure all cognitive tools are registered properly."""
    
    def test_all_schemas_have_names(self):
        for schema in ALL_TOOL_SCHEMAS:
            assert "name" in schema
            assert schema["name"]  # non-empty
            
    def test_all_schemas_have_descriptions(self):
        for schema in ALL_TOOL_SCHEMAS:
            assert "description" in schema
            assert schema["description"]  # non-empty
            
    def test_all_schemas_have_parameters(self):
        for schema in ALL_TOOL_SCHEMAS:
            assert "parameters" in schema
            assert "properties" in schema["parameters"]
            assert "required" in schema["parameters"]
            
    def test_schema_names_unique(self):
        names = [s["name"] for s in ALL_TOOL_SCHEMAS]
        assert len(names) == len(set(names))  # all unique
