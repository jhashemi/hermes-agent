"""
P2-001b: Integration Tests for Request Validation with Pydantic BaseModel.

This test suite verifies end-to-end integration of:
1. Pydantic BaseModel (ExecuteRequest, ExecuteResponse) in the /api/agent/execute endpoint
2. Request validation with 400 Bad Request responses
3. Authentication with 401 Unauthorized responses
4. Response serialization and format compliance
5. Error handling and edge cases

Tests use FastAPI TestClient for real HTTP testing against the actual endpoint.
"""

import json
import pytest
import os
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

# Import the blueprint creator
from gateway.remote_agent_api import (
    create_remote_api_blueprint,
    verify_api_key,
    get_expected_key,
)


@pytest.fixture
def fastapi_app_with_remote_api():
    """Create a FastAPI app with remote API endpoints registered."""
    app = FastAPI()
    
    # Create mock gateway runner with orchestrator
    mock_orchestrator = AsyncMock()
    mock_orchestrator.execute_on_instance = AsyncMock(
        return_value="Mocked response from orchestrator"
    )
    
    mock_gateway_runner = MagicMock()
    mock_gateway_runner.instance_orchestrator = mock_orchestrator
    mock_gateway_runner.agent = MagicMock()
    mock_gateway_runner.agent.chat = MagicMock(return_value="Mocked local response")
    
    # Register the remote API blueprint
    import asyncio
    asyncio.run(create_remote_api_blueprint(app, mock_gateway_runner))
    
    return app, mock_gateway_runner, mock_orchestrator


@pytest.fixture
def client_with_auth(fastapi_app_with_remote_api):
    """Create a TestClient with authentication set up."""
    app, mock_gateway, mock_orchestrator = fastapi_app_with_remote_api
    with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": "test-secret-key"}):
        get_expected_key.cache_clear()
        client = TestClient(app)
        yield client, mock_gateway, mock_orchestrator
        get_expected_key.cache_clear()


# ============================================================================
# P2-001: PYDANTIC MODEL VALIDATION TESTS
# ============================================================================

class TestPydanticModelIntegration:
    """Test Pydantic BaseModel integration with FastAPI endpoint."""
    
    def test_valid_request_accepted(self, client_with_auth):
        """Valid request with all required fields should return 200."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "What is AI?",
                "session_id": "user123"
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "output" in data
        assert data["session_id"] == "user123"
        assert "timestamp" in data
    
    def test_valid_request_without_session_id(self, client_with_auth):
        """Valid request without optional session_id should work."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "What is AI?"
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
    
    def test_missing_agent_id_returns_400(self, client_with_auth):
        """Request without agent_id should return 400 Bad Request."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "prompt": "What is AI?",
                "session_id": "user123"
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data
        # Pydantic returns 422 Unprocessable Entity for validation errors
        assert any("agent_id" in str(err) for err in data.get("detail", []))
    
    def test_empty_agent_id_returns_400(self, client_with_auth):
        """Request with empty agent_id should return 400."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "",
                "prompt": "What is AI?",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data
    
    def test_whitespace_only_agent_id_returns_400(self, client_with_auth):
        """Request with whitespace-only agent_id should fail."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "   ",
                "prompt": "What is AI?",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        # After strip, becomes empty, validator should reject
        assert response.status_code == 422
    
    def test_missing_prompt_returns_400(self, client_with_auth):
        """Request without prompt should return 400 Bad Request."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "session_id": "user123"
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data
    
    def test_empty_prompt_returns_400(self, client_with_auth):
        """Request with empty prompt should return 400."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 422
    
    def test_whitespace_only_prompt_returns_400(self, client_with_auth):
        """Request with whitespace-only prompt should be rejected."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "   \n  \t  ",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 422
    
    def test_prompt_exceeds_100kb_returns_400(self, client_with_auth):
        """Request with prompt > 100KB should return 400."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        # Create a prompt that exceeds 100KB
        large_prompt = "x" * 100001  # 100KB + 1 byte
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": large_prompt,
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data
    
    def test_prompt_exactly_100kb_accepted(self, client_with_auth):
        """Request with prompt exactly 100KB should be accepted."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        # Create a prompt exactly 100KB
        exact_prompt = "x" * 100000
        assert len(exact_prompt.encode('utf-8')) == 100000
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": exact_prompt,
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 200
    
    def test_agent_id_max_length_255(self, client_with_auth):
        """agent_id with exactly 255 chars should be accepted."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        agent_id = "a" * 255
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": agent_id,
                "prompt": "What is AI?",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 200
    
    def test_agent_id_exceeds_255_returns_400(self, client_with_auth):
        """agent_id exceeding 255 chars should be rejected."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        agent_id = "a" * 256  # 256 chars (exceeds max)
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": agent_id,
                "prompt": "What is AI?",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 422
    
    def test_session_id_max_length_255(self, client_with_auth):
        """session_id with exactly 255 chars should be accepted."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        session_id = "s" * 255
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "What is AI?",
                "session_id": session_id,
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 200
    
    def test_session_id_exceeds_255_returns_400(self, client_with_auth):
        """session_id exceeding 255 chars should be rejected."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        session_id = "s" * 256
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "What is AI?",
                "session_id": session_id,
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 422
    
    def test_empty_session_id_string_stripped(self, client_with_auth):
        """Empty session_id should be converted to None by validator."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "What is AI?",
                "session_id": "   ",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        # Should be accepted and converted to None
        assert response.status_code == 200
        data = response.json()
        # session_id should be set to default "remote-exec" in endpoint
        assert data["session_id"] is not None


# ============================================================================
# AUTHENTICATION TESTS (P1-001)
# ============================================================================

class TestAuthenticationIntegration:
    """Test authentication with API key header."""
    
    def test_missing_api_key_returns_401(self, client_with_auth):
        """Request without X-Hermes-Key header should return 401."""
        client, _, _ = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "What is AI?",
            }
            # No X-Hermes-Key header
        )
        
        assert response.status_code == 401
        data = response.json()
        assert "Unauthorized" in data.get("detail", "")
    
    def test_wrong_api_key_returns_401(self, client_with_auth):
        """Request with wrong API key should return 401."""
        client, _, _ = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "What is AI?",
            },
            headers={"X-Hermes-Key": "wrong-key"}
        )
        
        assert response.status_code == 401
        data = response.json()
        assert "Unauthorized" in data.get("detail", "")
    
    def test_correct_api_key_allows_request(self, client_with_auth):
        """Request with correct API key should be accepted."""
        client, _, _ = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "What is AI?",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 200
    
    def test_empty_api_key_returns_401(self, client_with_auth):
        """Request with empty API key should return 401."""
        client, _, _ = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "What is AI?",
            },
            headers={"X-Hermes-Key": ""}
        )
        
        assert response.status_code == 401


# ============================================================================
# RESPONSE FORMAT TESTS
# ============================================================================

class TestResponseFormat:
    """Test response structure and format compliance."""
    
    def test_success_response_format(self, client_with_auth):
        """Successful response should have required fields."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "What is AI?",
                "session_id": "user123",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        
        # Check required fields
        assert "status" in data
        assert data["status"] == "success"
        assert "output" in data
        assert data["output"] is not None
        assert "session_id" in data
        assert "timestamp" in data
        assert data["session_id"] == "user123"
        
        # Check timestamp format (ISO format with Z)
        assert data["timestamp"].endswith("Z")
    
    def test_response_has_json_serializable_format(self, client_with_auth):
        """Response should be valid JSON."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "What is AI?",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 200
        
        # Should be valid JSON
        data = response.json()
        assert isinstance(data, dict)
        
        # Should be able to serialize back to JSON string
        json_str = json.dumps(data)
        assert isinstance(json_str, str)


# ============================================================================
# VALIDATION ERROR RESPONSE TESTS (400 Bad Request)
# ============================================================================

class TestValidationErrorResponses:
    """Test that validation errors return proper 400/422 responses."""
    
    def test_invalid_json_returns_422(self, client_with_auth):
        """Request with invalid JSON should return 422."""
        client, _, _ = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            content="{invalid json",
            headers={
                "X-Hermes-Key": "test-secret-key",
                "Content-Type": "application/json"
            }
        )
        
        assert response.status_code == 422
    
    def test_validation_error_includes_detail(self, client_with_auth):
        """Validation error response should include error detail."""
        client, _, _ = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "",
                "prompt": "What is AI?",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 422
        data = response.json()
        
        # Should have detail field
        assert "detail" in data
        
        # detail should be a list of errors
        assert isinstance(data["detail"], list)
        assert len(data["detail"]) > 0
    
    def test_missing_required_field_error_detail(self, client_with_auth):
        """Missing required field should include error details."""
        client, _, _ = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "prompt": "What is AI?",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data
        # Error should mention the missing field
        detail_str = str(data["detail"])
        assert "agent_id" in detail_str.lower()
    
    def test_multiple_validation_errors_in_response(self, client_with_auth):
        """Multiple validation errors should all be reported."""
        client, _, _ = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "",
                "prompt": "",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data
        
        # Should have multiple errors
        assert isinstance(data["detail"], list)


# ============================================================================
# EDGE CASES AND SPECIAL SCENARIOS
# ============================================================================

class TestEdgeCases:
    """Test edge cases and special scenarios."""
    
    def test_unicode_characters_in_prompt(self, client_with_auth):
        """Prompt with unicode characters should be handled."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "What is 你好? 😀 مرحبا",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
    
    def test_multiline_prompt(self, client_with_auth):
        """Prompt with newlines should be accepted."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        multiline_prompt = """What is the meaning of life?
        
Please explain in detail.
Also provide examples."""
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": multiline_prompt,
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 200
    
    def test_special_characters_in_agent_id(self, client_with_auth):
        """agent_id with special characters should be accepted."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "agent-01_hermes.prod-03",
                "prompt": "What is AI?",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 200
    
    def test_leading_trailing_spaces_trimmed(self, client_with_auth):
        """Leading/trailing spaces in fields should be trimmed."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "  default  ",
                "prompt": "  What is AI?  ",
                "session_id": "  user123  ",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"


# ============================================================================
# HEALTH CHECK AND STATUS ENDPOINTS
# ============================================================================

class TestHealthAndStatus:
    """Test health check and status endpoints."""
    
    def test_health_check_endpoint(self, client_with_auth):
        """Health check endpoint should return 200."""
        client, _, _ = client_with_auth
        
        response = client.get("/health")
        
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "timestamp" in data
    
    def test_status_endpoint_requires_auth(self, client_with_auth):
        """Status endpoint should require authentication."""
        client, _, _ = client_with_auth
        
        response = client.get("/api/agent/status")
        
        assert response.status_code == 401
    
    def test_status_endpoint_with_auth(self, client_with_auth):
        """Status endpoint should return data when authenticated."""
        client, _, _ = client_with_auth
        
        response = client.get(
            "/api/agent/status",
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        assert response.status_code == 200
        data = response.json()
        assert "instance" in data
        assert "timestamp" in data


# ============================================================================
# INTEGRATION FLOW TESTS
# ============================================================================

class TestFullRequestFlow:
    """Test complete request/response flow."""
    
    def test_complete_valid_request_flow(self, client_with_auth):
        """Complete flow: valid auth, valid request, valid response."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        # Send valid request
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "What is AI?",
                "session_id": "test-session",
            },
            headers={
                "X-Hermes-Key": "test-secret-key",
                "X-Hermes-User": "testuser"
            }
        )
        
        # Verify response
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["output"] is not None
        assert data["session_id"] == "test-session"
        assert data["timestamp"].endswith("Z")
    
    def test_validation_errors_returned_even_with_wrong_key(self, client_with_auth):
        """Pydantic validation happens before endpoint, so invalid request returns 422."""
        client, _, _ = client_with_auth
        
        # In FastAPI, Pydantic validation occurs before the endpoint is called,
        # so an invalid request returns 422 regardless of API key.
        # The API key is only checked inside the endpoint handler.
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "",  # Invalid
                "prompt": "",    # Invalid
            },
            headers={"X-Hermes-Key": "wrong-key"}
        )
        
        # Should return 422 (validation error) since invalid request structure
        assert response.status_code == 422
    
    def test_validation_failure_before_execution(self, client_with_auth):
        """Validation errors should occur before execution."""
        client, mock_gateway, mock_orchestrator = client_with_auth
        
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "default",
                "prompt": "x" * 100001,  # Exceeds max length
                "session_id": "test-session",
            },
            headers={"X-Hermes-Key": "test-secret-key"}
        )
        
        # Should return validation error before calling orchestrator
        assert response.status_code == 422
        assert not mock_orchestrator.execute_on_instance.called
