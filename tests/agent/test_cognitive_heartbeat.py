"""Tests for cognitive heartbeat control loop.

Tests the predict-then-declare gate, substrate-scoped surprise,
and prediction budget tracking.
"""

import pytest
import json
from agent.cognitive_heartbeat import (
    _detect_claim_keywords,
    _extract_cognitive_predict_calls,
    perform_cognitive_heartbeat_inspection,
    inject_heartbeat_warning_into_response,
)


class TestClaimKeywordDetection:
    """Test claim keyword detection in responses."""
    
    def test_detects_done(self):
        assert _detect_claim_keywords("The work is DONE")
        assert _detect_claim_keywords("I am done with the task")
        
    def test_detects_complete(self):
        assert _detect_claim_keywords("COMPLETE: all tests pass")
        assert _detect_claim_keywords("Implementation is complete")
        
    def test_detects_verified(self):
        assert _detect_claim_keywords("VERIFIED on production")
        assert _detect_claim_keywords("The fix has been verified")
        
    def test_detects_working(self):
        assert _detect_claim_keywords("Code is WORKING now")
        
    def test_detects_fixed(self):
        assert _detect_claim_keywords("Bug FIXED")
        assert _detect_claim_keywords("Issue has been fixed")
        
    def test_detects_shipped(self):
        assert _detect_claim_keywords("SHIPPED to production")
        
    def test_case_insensitive(self):
        assert _detect_claim_keywords("done")
        assert _detect_claim_keywords("Done")
        assert _detect_claim_keywords("DONE")
        
    def test_no_false_positives(self):
        assert not _detect_claim_keywords("The meeting is scheduled")
        assert not _detect_claim_keywords("I did the task")
        assert not _detect_claim_keywords("This is a normal message")


class TestCognitivePredictExtraction:
    """Test extraction of cognitive_predict calls from messages."""
    
    def test_extract_single_prediction(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "cognitive_predict",
                            "arguments": json.dumps({
                                "prediction": "NERVE chain is COMPLETE",
                                "falsifier": "Live regression detected on prod",
                                "substrate": "production-service",
                                "budget": 3,
                            })
                        }
                    }
                ]
            }
        ]
        
        predictions = _extract_cognitive_predict_calls(messages)
        assert len(predictions) == 1
        assert predictions[0]["prediction"] == "NERVE chain is COMPLETE"
        assert predictions[0]["falsifier"] == "Live regression detected on prod"
        assert predictions[0]["substrate"] == "production-service"
        assert predictions[0]["budget"] == 3
        
    def test_extract_multiple_predictions(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "cognitive_predict",
                            "arguments": json.dumps({
                                "prediction": "Claim 1",
                                "falsifier": "F1",
                                "substrate": "bare-repo-main",
                            })
                        }
                    },
                    {
                        "function": {
                            "name": "cognitive_predict",
                            "arguments": json.dumps({
                                "prediction": "Claim 2",
                                "falsifier": "F2",
                                "substrate": "production-service",
                            })
                        }
                    }
                ]
            }
        ]
        
        predictions = _extract_cognitive_predict_calls(messages)
        assert len(predictions) == 2
        
    def test_extract_skips_other_tools(self):
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "terminal",
                            "arguments": "{}"
                        }
                    },
                    {
                        "function": {
                            "name": "cognitive_predict",
                            "arguments": json.dumps({
                                "prediction": "Claim",
                                "falsifier": "F",
                                "substrate": "bare-repo-main",
                            })
                        }
                    },
                    {
                        "function": {
                            "name": "read_file",
                            "arguments": "{}"
                        }
                    }
                ]
            }
        ]
        
        predictions = _extract_cognitive_predict_calls(messages)
        assert len(predictions) == 1
        
    def test_extract_empty_messages(self):
        predictions = _extract_cognitive_predict_calls([])
        assert predictions == []
        
    def test_extract_skips_user_messages(self):
        messages = [
            {
                "role": "user",
                "content": "Run some tests"
            }
        ]
        
        predictions = _extract_cognitive_predict_calls(messages)
        assert predictions == []


class TestHeartbeatInspection:
    """Test the full heartbeat inspection flow."""
    
    def test_no_claim_no_inspection(self):
        """Response without claims should not trigger inspection."""
        result = perform_cognitive_heartbeat_inspection(
            final_response="I finished implementing the requested feature step by step.",
            messages=[],
        )
        assert result is None
        
    def test_claim_without_prediction(self):
        """Response with claim but no cognitive_predict should downgrade."""
        result = perform_cognitive_heartbeat_inspection(
            final_response="The NERVE chain is now COMPLETE.",
            messages=[],
        )
        assert result is not None
        assert result["has_claim"] is True
        assert result["predictions_found"] == 0
        assert result["confidence_downgrade"] == 0.5
        assert "cognitive_predict" in result["soft_warning"].lower()
        
    def test_claim_with_complete_prediction(self):
        """Response with claim and well-formed prediction should pass."""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "cognitive_predict",
                            "arguments": json.dumps({
                                "prediction": "NERVE chain is COMPLETE",
                                "falsifier": "Live regression detected",
                                "substrate": "production-service",
                                "budget": 3,
                            })
                        }
                    }
                ]
            }
        ]
        
        result = perform_cognitive_heartbeat_inspection(
            final_response="The NERVE chain is now COMPLETE.",
            messages=messages,
        )
        # Should pass — all fields populated, budget > 0
        assert result is None
        
    def test_claim_with_missing_falsifier(self):
        """Prediction with empty falsifier should be caught."""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "cognitive_predict",
                            "arguments": json.dumps({
                                "prediction": "NERVE chain is COMPLETE",
                                "falsifier": "",  # MISSING
                                "substrate": "production-service",
                                "budget": 3,
                            })
                        }
                    }
                ]
            }
        ]
        
        result = perform_cognitive_heartbeat_inspection(
            final_response="The NERVE chain is now COMPLETE.",
            messages=messages,
        )
        assert result is not None
        assert "empty falsifier" in result["prediction_details"]
        assert result["confidence_downgrade"] == 0.3
        
    def test_claim_with_missing_substrate(self):
        """Prediction with empty substrate should be caught."""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "cognitive_predict",
                            "arguments": json.dumps({
                                "prediction": "NERVE chain is COMPLETE",
                                "falsifier": "Live regression",
                                "substrate": "",  # MISSING
                                "budget": 3,
                            })
                        }
                    }
                ]
            }
        ]
        
        result = perform_cognitive_heartbeat_inspection(
            final_response="The NERVE chain is now COMPLETE.",
            messages=messages,
        )
        assert result is not None
        assert "empty substrate" in result["prediction_details"]
        
    def test_claim_with_expired_budget(self):
        """Prediction with budget <= 0 should be caught."""
        messages = [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "function": {
                            "name": "cognitive_predict",
                            "arguments": json.dumps({
                                "prediction": "NERVE chain is COMPLETE",
                                "falsifier": "Live regression",
                                "substrate": "production-service",
                                "budget": 0,  # EXPIRED
                            })
                        }
                    }
                ]
            }
        ]
        
        result = perform_cognitive_heartbeat_inspection(
            final_response="The NERVE chain is now COMPLETE.",
            messages=messages,
        )
        assert result is not None
        assert "budget expired" in result["prediction_details"]


class TestHeartbeatWarningInjection:
    """Test warning injection into responses."""
    
    def test_no_warning_when_no_heartbeat_result(self):
        response = "The NERVE chain is now COMPLETE."
        result = inject_heartbeat_warning_into_response(response, None)
        assert result == response
        
    def test_no_warning_when_heartbeat_empty_dict(self):
        response = "The NERVE chain is now COMPLETE."
        result = inject_heartbeat_warning_into_response(response, {})
        assert result == response
        
    def test_injects_warning_text(self):
        response = "The work is DONE."
        heartbeat_result = {
            "soft_warning": "⚠️  Missing falsifier",
            "has_claim": True,
        }
        
        result = inject_heartbeat_warning_into_response(response, heartbeat_result)
        assert "⚠️  Missing falsifier" in result
        assert "The work is DONE." in result
        assert result.startswith("⚠️")
        
    def test_preserves_original_response(self):
        response = "This is a long response\nwith multiple lines\nof content."
        heartbeat_result = {
            "soft_warning": "⚠️  Warning",
        }
        
        result = inject_heartbeat_warning_into_response(response, heartbeat_result)
        assert response in result
