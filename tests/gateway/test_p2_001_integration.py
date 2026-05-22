"""
P2-001b: Integration Tests for Request Validation with Pydantic BaseModel.

Updated to include HMAC-SHA256 request signature verification.

This test suite verifies end-to-end integration of:
1. HMAC-SHA256 request signature (X-Hermes-Signature) — P1-HMAC
2. Pydantic BaseModel validation with 422 responses — P2-VALIDATION
3. Authentication with 401 Unauthorized responses — P1-001
4. Response serialization and format compliance
5. Error handling and edge cases
6. SessionManager lifecycle — P3-SESSION-LEAK

Tests use FastAPI TestClient for real HTTP testing against the actual endpoint.
"""

import asyncio
import hashlib
import hmac
import json
import pytest
import os
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Import the blueprint creator
from gateway.remote_agent_api import (
    create_remote_api_blueprint,
    verify_api_key,
    get_expected_key,
    compute_hmac_signature,
    MAX_PROMPT_LENGTH,
)

TEST_API_KEY = "test-secret-key"


def _sign_body(body: bytes, secret: str = TEST_API_KEY) -> str:
    """Helper: compute HMAC-SHA256 of body bytes."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


@pytest.fixture
def fastapi_app_with_remote_api():
    """Create a FastAPI app with remote API endpoints registered."""
    app = FastAPI()

    mock_orchestrator = AsyncMock()
    mock_orchestrator.execute_on_instance = AsyncMock(
        return_value="Mocked response from orchestrator"
    )

    mock_gateway_runner = MagicMock()
    mock_gateway_runner.instance_orchestrator = mock_orchestrator
    mock_gateway_runner.agent = MagicMock()
    mock_gateway_runner.agent.chat = MagicMock(return_value="Mocked local response")

    with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": TEST_API_KEY}):
        get_expected_key.cache_clear()
        asyncio.run(create_remote_api_blueprint(app, mock_gateway_runner))

    return app, mock_gateway_runner, mock_orchestrator


@pytest.fixture
def client_with_auth(fastapi_app_with_remote_api):
    """Create a TestClient with authentication set up."""
    app, mock_gateway, mock_orchestrator = fastapi_app_with_remote_api
    with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": TEST_API_KEY}):
        get_expected_key.cache_clear()
        client = TestClient(app)
        yield client, mock_gateway, mock_orchestrator
        get_expected_key.cache_clear()


def _post_with_auth(client, body_dict, api_key=TEST_API_KEY):
    """Helper: POST with both HMAC signature and API key."""
    body_bytes = json.dumps(body_dict).encode("utf-8")
    sig = _sign_body(body_bytes, api_key)
    return client.post(
        "/api/agent/execute",
        content=body_bytes,
        headers={
            "Content-Type": "application/json",
            "X-Hermes-Key": api_key,
            "X-Hermes-Signature": sig,
            "X-Hermes-User": "testuser",
        },
    )


def _get_status_with_auth(client, api_key=TEST_API_KEY):
    """Helper: GET status with HMAC signature and API key."""
    sig = _sign_body(b"", api_key)
    return client.get(
        "/api/agent/status",
        headers={
            "X-Hermes-Key": api_key,
            "X-Hermes-Signature": sig,
        },
    )


# ============================================================================
# P2-001: PYDANTIC MODEL VALIDATION TESTS
# ============================================================================

class TestPydanticModelIntegration:
    """Test Pydantic BaseModel integration with FastAPI endpoint."""

    def test_valid_request_accepted(self, client_with_auth):
        """Valid request with all required fields should return 200."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "What is AI?",
            "session_id": "user123",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "output" in data
        assert data["session_id"] == "user123"
        assert "timestamp" in data

    def test_valid_request_without_session_id(self, client_with_auth):
        """Valid request without optional session_id should work."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "What is AI?",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"

    def test_missing_agent_id_returns_422(self, client_with_auth):
        """Request without agent_id should return 422."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "prompt": "What is AI?",
            "session_id": "user123",
        })
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    def test_empty_agent_id_returns_422(self, client_with_auth):
        """Request with empty agent_id should return 422."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "",
            "prompt": "What is AI?",
        })
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    def test_whitespace_only_agent_id_returns_422(self, client_with_auth):
        """Request with whitespace-only agent_id should fail."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "   ",
            "prompt": "What is AI?",
        })
        assert response.status_code == 422

    def test_missing_prompt_returns_422(self, client_with_auth):
        """Request without prompt should return 422."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "default",
            "session_id": "user123",
        })
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    def test_empty_prompt_returns_422(self, client_with_auth):
        """Request with empty prompt should return 422."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "",
        })
        assert response.status_code == 422

    def test_whitespace_only_prompt_returns_422(self, client_with_auth):
        """Request with whitespace-only prompt should be rejected."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "   \n  \t  ",
        })
        assert response.status_code == 422

    def test_prompt_exceeds_max_returns_422(self, client_with_auth):
        """Request with prompt > 10000 chars should return 422."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "x" * (MAX_PROMPT_LENGTH + 1),
        })
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    def test_prompt_exactly_max_accepted(self, client_with_auth):
        """Request with prompt exactly at max length should be accepted."""
        client, _, _ = client_with_auth
        exact_prompt = "x" * MAX_PROMPT_LENGTH
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": exact_prompt,
        })
        assert response.status_code == 200

    def test_agent_id_max_length_255(self, client_with_auth):
        """agent_id with exactly 255 chars should be accepted."""
        client, _, _ = client_with_auth
        agent_id = "a" * 255
        response = _post_with_auth(client, {
            "agent_id": agent_id,
            "prompt": "What is AI?",
        })
        assert response.status_code == 200

    def test_agent_id_exceeds_255_returns_422(self, client_with_auth):
        """agent_id exceeding 255 chars should be rejected."""
        client, _, _ = client_with_auth
        agent_id = "a" * 256
        response = _post_with_auth(client, {
            "agent_id": agent_id,
            "prompt": "What is AI?",
        })
        assert response.status_code == 422

    def test_session_id_max_length_255(self, client_with_auth):
        """session_id with exactly 255 chars should be accepted."""
        client, _, _ = client_with_auth
        session_id = "s" * 255
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "What is AI?",
            "session_id": session_id,
        })
        assert response.status_code == 200

    def test_session_id_exceeds_255_returns_422(self, client_with_auth):
        """session_id exceeding 255 chars should be rejected."""
        client, _, _ = client_with_auth
        session_id = "s" * 256
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "What is AI?",
            "session_id": session_id,
        })
        assert response.status_code == 422

    def test_empty_session_id_string_stripped(self, client_with_auth):
        """Empty session_id should be converted to None by validator."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "What is AI?",
            "session_id": "   ",
        })
        # Should be accepted and converted to None → defaults to "remote-exec"
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] is not None


# ============================================================================
# AUTHENTICATION TESTS (P1-001 + P1-HMAC)
# ============================================================================

class TestAuthenticationIntegration:
    """Test authentication with API key header and HMAC signature."""

    def test_missing_api_key_returns_401(self, client_with_auth):
        """Request without X-Hermes-Key header should return 401."""
        client, _, _ = client_with_auth
        body_dict = {"agent_id": "default", "prompt": "What is AI?"}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        sig = _sign_body(body_bytes)
        response = client.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 401
        data = response.json()
        assert "Unauthorized" in data.get("detail", "")

    def test_wrong_api_key_returns_401(self, client_with_auth):
        """Request with wrong API key should return 401."""
        client, _, _ = client_with_auth
        body_dict = {"agent_id": "default", "prompt": "What is AI?"}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        # Sign with correct key but send wrong key in header
        sig = _sign_body(body_bytes)
        response = client.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": "wrong-key",
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 401

    def test_correct_api_key_allows_request(self, client_with_auth):
        """Request with correct API key + signature should be accepted."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "What is AI?",
        })
        assert response.status_code == 200

    def test_empty_api_key_returns_401(self, client_with_auth):
        """Request with empty API key should return 401."""
        client, _, _ = client_with_auth
        body_dict = {"agent_id": "default", "prompt": "What is AI?"}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        sig = _sign_body(body_bytes)
        response = client.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": "",
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 401

    def test_missing_signature_returns_401(self, client_with_auth):
        """Request without X-Hermes-Signature should return 401."""
        client, _, _ = client_with_auth
        response = client.post(
            "/api/agent/execute",
            json={"agent_id": "default", "prompt": "What is AI?"},
            headers={"X-Hermes-Key": TEST_API_KEY},
        )
        assert response.status_code == 401

    def test_invalid_signature_returns_401(self, client_with_auth):
        """Request with invalid HMAC signature should return 401."""
        client, _, _ = client_with_auth
        response = client.post(
            "/api/agent/execute",
            json={"agent_id": "default", "prompt": "What is AI?"},
            headers={
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": "invalid-signature",
            },
        )
        assert response.status_code == 401


# ============================================================================
# RESPONSE FORMAT TESTS
# ============================================================================

class TestResponseFormat:
    """Test response structure and format compliance."""

    def test_success_response_format(self, client_with_auth):
        """Successful response should have required fields."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "What is AI?",
            "session_id": "user123",
        })
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
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "What is AI?",
        })
        assert response.status_code == 200

        data = response.json()
        assert isinstance(data, dict)

        # Should be able to serialize back to JSON string
        json_str = json.dumps(data)
        assert isinstance(json_str, str)


# ============================================================================
# VALIDATION ERROR RESPONSE TESTS
# ============================================================================

class TestValidationErrorResponses:
    """Test that validation errors return proper 422 responses."""

    def test_invalid_json_returns_422(self, client_with_auth):
        """Request with invalid JSON should return 422."""
        client, _, _ = client_with_auth
        body_bytes = b"{invalid json"
        sig = _sign_body(body_bytes)
        response = client.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 422

    def test_validation_error_includes_detail(self, client_with_auth):
        """Validation error response should include error detail."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "",
            "prompt": "What is AI?",
        })
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data

    def test_missing_required_field_error_detail(self, client_with_auth):
        """Missing required field should include error details."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "prompt": "What is AI?",
        })
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data
        detail_str = str(data["detail"]).lower()
        assert "agent_id" in detail_str

    def test_multiple_validation_errors_in_response(self, client_with_auth):
        """Multiple validation errors should all be reported."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "",
            "prompt": "",
        })
        assert response.status_code == 422
        data = response.json()
        assert "detail" in data


# ============================================================================
# EDGE CASES AND SPECIAL SCENARIOS
# ============================================================================

class TestEdgeCases:
    """Test edge cases and special scenarios."""

    def test_unicode_characters_in_prompt(self, client_with_auth):
        """Prompt with unicode characters should be handled."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "What is \u4f60\u597d? \U0001f600 \u0645\u0631\u062d\u0628\u0627",
        })
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"

    def test_multiline_prompt(self, client_with_auth):
        """Prompt with newlines should be accepted."""
        client, _, _ = client_with_auth
        multiline_prompt = """What is the meaning of life?

Please explain in detail.
Also provide examples."""
        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": multiline_prompt,
        })
        assert response.status_code == 200

    def test_dashed_agent_id_accepted(self, client_with_auth):
        """agent_id with dashes should be accepted (alphanumeric+dashes)."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "agent-01-hermes",
            "prompt": "What is AI?",
        })
        assert response.status_code == 200

    def test_special_chars_in_agent_id_rejected(self, client_with_auth):
        """agent_id with dots/underscores/special chars should be rejected."""
        client, _, _ = client_with_auth
        # agent_id with underscores → rejected by new format validation
        response = _post_with_auth(client, {
            "agent_id": "agent_01",
            "prompt": "What is AI?",
        })
        assert response.status_code == 422

    def test_leading_trailing_spaces_trimmed(self, client_with_auth):
        """Leading/trailing spaces in fields should be trimmed."""
        client, _, _ = client_with_auth
        response = _post_with_auth(client, {
            "agent_id": "  default  ",
            "prompt": "  What is AI?  ",
            "session_id": "  user123  ",
        })
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
        response = _get_status_with_auth(client)
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

        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "What is AI?",
            "session_id": "test-session",
        })

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["output"] is not None
        assert data["session_id"] == "test-session"
        assert data["timestamp"].endswith("Z")

    def test_validation_errors_returned_even_with_wrong_key(self, client_with_auth):
        """Auth is checked first; without valid signature → 401."""
        client, _, _ = client_with_auth

        # Without valid signature, auth fails first
        response = client.post(
            "/api/agent/execute",
            json={
                "agent_id": "",
                "prompt": "",
            },
            headers={"X-Hermes-Key": "wrong-key"},
        )
        # Signature missing → 401
        assert response.status_code == 401

    def test_validation_failure_before_execution(self, client_with_auth):
        """Validation errors should occur before execution."""
        client, mock_gateway, mock_orchestrator = client_with_auth

        response = _post_with_auth(client, {
            "agent_id": "default",
            "prompt": "x" * (MAX_PROMPT_LENGTH + 1),
            "session_id": "test-session",
        })

        # Should return validation error before calling orchestrator
        assert response.status_code == 422
        assert not mock_orchestrator.execute_on_instance.called
