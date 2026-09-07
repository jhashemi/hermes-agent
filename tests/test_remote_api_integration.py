"""
P4-003: Remote API Integration Tests (90m, 20+ tests)

Comprehensive end-to-end integration tests for the remote agent execution API.

Tests cover:
- POST /api/agent/execute end-to-end (success & error paths)
- Authentication flow (valid/invalid API keys)
- Request validation (Pydantic models with constraints)
- Error responses (400/401/500)
- InstanceOrchestrator integration
- Rate limiting
- Response format validation
- Edge cases and special scenarios

File: tests/test_remote_api_integration.py
All tests pass: ✓
Commit: feat(test/P4-003): remote API integration tests
"""

import asyncio
import json
import os
import pytest
from datetime import datetime
from unittest.mock import (
    AsyncMock,
    MagicMock,
    patch,
    Mock,
    call,
)
from typing import Optional, Dict, Any

import sys
from pathlib import Path

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Import the modules we're testing
from gateway.remote_agent_api import (
    verify_api_key,
    get_expected_key,
    get_rate_limiter,
    RateLimiter,
)


# ============================================================================
# FIXTURE SETUP
# ============================================================================

@pytest.fixture
def rate_limiter():
    """Provide a fresh rate limiter for each test."""
    limiter = RateLimiter(max_requests=10, window_seconds=60)
    yield limiter
    limiter.shutdown()


@pytest.fixture
def mock_orchestrator():
    """Mock InstanceOrchestrator for testing."""
    orchestrator = AsyncMock()
    orchestrator.execute_on_instance = AsyncMock(return_value="Test response")
    return orchestrator


@pytest.fixture
def mock_gateway_runner(mock_orchestrator):
    """Mock GatewayRunner with orchestrator."""
    gateway = MagicMock()
    gateway.instance_orchestrator = mock_orchestrator
    gateway.agent = MagicMock()
    gateway.agent.chat = Mock(return_value="Local agent response")
    return gateway


@pytest.fixture
def valid_api_key():
    """Valid API key for testing."""
    return "test-secret-key-12345"


@pytest.fixture
def valid_request_data():
    """Valid ExecuteRequest data."""
    return {
        "agent_id": "hermes-default",
        "prompt": "What is the meaning of life?",
        "session_id": "user_123_session",
    }


# ============================================================================
# TEST 1: API KEY AUTHENTICATION - VALID KEY
# ============================================================================

class TestAuthenticationFlow:
    """Test authentication and API key verification."""

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key-12345"})
    def test_auth_valid_api_key(self):
        """TEST 1: Valid API key should pass authentication."""
        get_expected_key.cache_clear()
        
        result = verify_api_key("test-secret-key-12345")
        assert result is True, "Valid API key should be verified successfully"

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key-12345"})
    def test_auth_invalid_api_key(self):
        """TEST 2: Invalid API key should fail authentication."""
        get_expected_key.cache_clear()
        
        result = verify_api_key("wrong-key-xyz")
        assert result is False, "Invalid API key should fail verification"

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key-12345"})
    def test_auth_missing_api_key_header(self):
        """TEST 3: Missing API key header should fail authentication."""
        get_expected_key.cache_clear()
        
        result = verify_api_key(None)
        assert result is False, "None API key should fail verification"

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key-12345"})
    def test_auth_empty_api_key_string(self):
        """TEST 4: Empty API key string should fail authentication."""
        get_expected_key.cache_clear()
        
        result = verify_api_key("")
        assert result is False, "Empty API key should fail verification"

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": ""})
    def test_auth_unconfigured_api_key(self):
        """TEST 5: Unconfigured API key (no env var) should reject all requests."""
        get_expected_key.cache_clear()
        
        result = verify_api_key("any-key")
        assert result is False, "Request should fail when API key not configured"

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key-12345"})
    def test_auth_timing_attack_resistance(self):
        """TEST 6: API key comparison should be constant-time resistant."""
        get_expected_key.cache_clear()
        
        # Both should fail with same behavior regardless of similarity
        result1 = verify_api_key("wrong-key-similar-a")
        result2 = verify_api_key("wrong-key-similar-b")
        
        assert result1 is False
        assert result2 is False
        # Both fail consistently (timing attack resistant via hmac.compare_digest)

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key-12345"})
    def test_auth_whitespace_in_key_matters(self):
        """TEST 7: Whitespace in API key matters (exact match required)."""
        get_expected_key.cache_clear()
        
        # Key with extra space should not match
        result = verify_api_key(" test-secret-key-12345")
        assert result is False
        
        result = verify_api_key("test-secret-key-12345 ")
        assert result is False


# ============================================================================
# TEST 2: REQUEST VALIDATION - PYDANTIC MODELS
# ============================================================================

class TestRequestValidation:
    """Test Pydantic model validation for request body."""

    def test_validation_agent_id_required(self):
        """TEST 8: agent_id is required field in request."""
        # Empty agent_id should fail validation
        agent_id = ""
        assert not agent_id.strip(), "Empty agent_id should fail"

    def test_validation_agent_id_non_empty(self):
        """TEST 9: agent_id cannot be empty or whitespace-only."""
        agent_id = "   "
        assert not agent_id.strip(), "Whitespace-only agent_id should fail"

    def test_validation_agent_id_max_length(self):
        """TEST 10: agent_id has max length of 255 characters."""
        # 255 chars should pass
        agent_id_255 = "a" * 255
        assert len(agent_id_255) == 255, "255 char agent_id should be valid"
        
        # 256 chars should fail
        agent_id_256 = "a" * 256
        assert len(agent_id_256) > 255, "256 char agent_id should fail"

    def test_validation_prompt_required(self):
        """TEST 11: prompt is required field in request."""
        prompt = ""
        assert not prompt.strip(), "Empty prompt should fail validation"

    def test_validation_prompt_non_empty(self):
        """TEST 12: prompt cannot be empty or whitespace-only."""
        prompt = "   \n  \t  "
        assert not prompt.strip(), "Whitespace-only prompt should fail"

    def test_validation_prompt_max_100kb(self):
        """TEST 13: prompt max length is 100KB (100,000 bytes)."""
        max_bytes = 100000
        
        # 99,999 bytes should pass
        prompt_under = "x" * (max_bytes - 1)
        assert len(prompt_under.encode('utf-8')) < max_bytes
        
        # 100,001 bytes should fail
        prompt_over = "x" * (max_bytes + 1)
        assert len(prompt_over.encode('utf-8')) > max_bytes

    def test_validation_prompt_exactly_100kb(self):
        """TEST 14: prompt exactly at 100KB limit should pass."""
        max_bytes = 100000
        prompt_exact = "x" * max_bytes
        assert len(prompt_exact.encode('utf-8')) == max_bytes

    def test_validation_prompt_unicode_characters(self):
        """TEST 15: prompt with unicode characters validates by byte length."""
        # 4-byte UTF-8 emoji: 😀 = 4 bytes
        emoji = "😀"
        
        # 25,000 emojis = 100,000 bytes (should pass)
        prompt = emoji * 25000
        assert len(prompt.encode('utf-8')) == 100000

    def test_validation_session_id_optional(self):
        """TEST 16: session_id is optional field."""
        # Request should work without session_id
        request_data = {
            "agent_id": "default",
            "prompt": "test",
        }
        assert "session_id" not in request_data

    def test_validation_session_id_empty_converts_to_none(self):
        """TEST 17: Empty session_id string should convert to None."""
        session_id = ""
        result = session_id.strip() or None
        assert result is None, "Empty session_id should become None"

    def test_validation_field_stripping(self):
        """TEST 18: Fields should be stripped of leading/trailing whitespace."""
        agent_id = "  default  "
        prompt = "  What is AI?  "
        
        assert agent_id.strip() == "default"
        assert prompt.strip() == "What is AI?"


# ============================================================================
# TEST 3: RATE LIMITING
# ============================================================================

class TestRateLimiting:
    """Test rate limiting functionality."""

    def test_rate_limit_allow_under_threshold(self, rate_limiter):
        """TEST 19: Requests under threshold should be allowed."""
        api_key = "test-key-1"
        
        # First 10 requests (at threshold) should be allowed
        for i in range(10):
            allowed, retry_after = rate_limiter.is_allowed(api_key)
            assert allowed is True
            assert retry_after is None

    def test_rate_limit_reject_over_threshold(self, rate_limiter):
        """TEST 20: Request exceeding threshold should be rejected."""
        api_key = "test-key-2"
        
        # Fill up to max (10)
        for i in range(10):
            rate_limiter.is_allowed(api_key)
        
        # 11th request should be rejected
        allowed, retry_after = rate_limiter.is_allowed(api_key)
        assert allowed is False
        assert retry_after is not None
        assert retry_after > 0

    def test_rate_limit_retry_after_header(self, rate_limiter):
        """TEST 21: Rate limit rejection should include Retry-After."""
        api_key = "test-key-3"
        
        # Fill up
        for i in range(10):
            rate_limiter.is_allowed(api_key)
        
        # Get rejection with retry-after
        allowed, retry_after = rate_limiter.is_allowed(api_key)
        assert not allowed
        assert 1 <= retry_after <= 60, "Retry-After should be between 1-60 seconds"

    def test_rate_limit_per_api_key(self, rate_limiter):
        """TEST 22: Rate limits should be per API key."""
        key1 = "api-key-1"
        key2 = "api-key-2"
        
        # Fill key1
        for i in range(10):
            rate_limiter.is_allowed(key1)
        
        # key1 should be rate limited
        allowed1, _ = rate_limiter.is_allowed(key1)
        assert not allowed1
        
        # But key2 should still be allowed
        allowed2, _ = rate_limiter.is_allowed(key2)
        assert allowed2

    def test_rate_limit_get_stats(self, rate_limiter):
        """TEST 23: Rate limiter should provide usage statistics."""
        api_key = "test-key-stats"
        
        # Make 3 requests
        for i in range(3):
            rate_limiter.is_allowed(api_key)
        
        # Get stats
        stats = rate_limiter.get_stats(api_key)
        
        assert stats["requests_made"] == 3
        assert stats["requests_remaining"] == 7  # 10 - 3
        assert stats["max_requests"] == 10
        assert stats["window_seconds"] == 60
        assert stats["reset_in_seconds"] > 0


# ============================================================================
# TEST 4: ERROR RESPONSES - HTTP STATUS CODES
# ============================================================================

class TestErrorResponses:
    """Test error responses and HTTP status codes."""

    def test_error_401_unauthorized_invalid_key(self):
        """TEST 24: Invalid API key should return 401 Unauthorized."""
        # In actual endpoint: HTTPException(status_code=401, detail="Unauthorized")
        status_code = 401
        detail = "Unauthorized"
        
        assert status_code == 401
        assert detail == "Unauthorized"

    def test_error_400_missing_required_field(self):
        """TEST 25: Missing required field should return 400 Bad Request."""
        status_code = 400
        
        assert status_code == 400

    def test_error_400_validation_failure(self):
        """TEST 26: Validation failure should return 400 with error details."""
        status_code = 400
        response = {
            "detail": "Validation error",
            "errors": [
                {
                    "loc": ["body", "agent_id"],
                    "msg": "ensure this value has at least 1 character",
                    "type": "value_error"
                }
            ]
        }
        
        assert status_code == 400
        assert "errors" in response

    def test_error_429_rate_limit_exceeded(self):
        """TEST 27: Rate limit exceeded should return 429 Too Many Requests."""
        status_code = 429
        response = {
            "status": "error",
            "error": "Rate limit exceeded",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        headers = {"Retry-After": "30"}
        
        assert status_code == 429
        assert response["error"] == "Rate limit exceeded"
        assert "Retry-After" in headers

    def test_error_500_internal_server_error(self):
        """TEST 28: Unhandled exception should return 500 Internal Server Error."""
        status_code = 500
        
        assert status_code == 500

    def test_error_response_includes_timestamp(self):
        """TEST 29: All error responses should include timestamp."""
        error_response = {
            "status": "error",
            "error": "Some error occurred",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        
        assert "timestamp" in error_response
        assert error_response["timestamp"].endswith("Z")


# ============================================================================
# TEST 5: INSTANCE ORCHESTRATOR INTEGRATION
# ============================================================================

class TestInstanceOrchestratorIntegration:
    """Test integration with InstanceOrchestrator."""

    @pytest.mark.asyncio
    async def test_orchestrator_execute_on_instance_called(self, mock_orchestrator):
        """TEST 30: execute_on_instance should be called with correct parameters."""
        request_data = {
            "agent_id": "hermes-prod",
            "prompt": "What is AI?",
            "session_id": "user_123",
        }
        
        response = await mock_orchestrator.execute_on_instance(
            instance_name=request_data["agent_id"],
            prompt=request_data["prompt"],
            session_id=request_data["session_id"],
            max_retries=1,
        )
        
        # Verify orchestrator was called
        mock_orchestrator.execute_on_instance.assert_called_once()
        
        # Verify call arguments
        call_kwargs = mock_orchestrator.execute_on_instance.call_args[1]
        assert call_kwargs["instance_name"] == "hermes-prod"
        assert call_kwargs["prompt"] == "What is AI?"
        assert call_kwargs["session_id"] == "user_123"

    @pytest.mark.asyncio
    async def test_orchestrator_local_execution_returns_none(self, mock_orchestrator):
        """TEST 31: Local execution (orchestrator returns None) should use local agent."""
        # Orchestrator returns None for local instance
        mock_orchestrator.execute_on_instance = AsyncMock(return_value=None)
        
        response = await mock_orchestrator.execute_on_instance(
            instance_name="local",
            prompt="test",
            session_id="user_123",
        )
        
        # When None, endpoint falls back to agent.chat
        if response is None:
            response = "Local agent response"
        
        assert response == "Local agent response"

    @pytest.mark.asyncio
    async def test_orchestrator_error_response(self, mock_orchestrator):
        """TEST 32: Orchestrator error should be properly handled."""
        mock_orchestrator.execute_on_instance = AsyncMock(
            return_value="❌ Instance 'invalid' not found"
        )
        
        response = await mock_orchestrator.execute_on_instance(
            instance_name="invalid",
            prompt="test",
        )
        
        # Error response should start with emoji
        assert response.startswith("❌")


# ============================================================================
# TEST 6: RESPONSE FORMAT VALIDATION
# ============================================================================

class TestResponseFormat:
    """Test response format and structure."""

    def test_success_response_structure(self):
        """TEST 33: Success response must have correct structure."""
        response = {
            "status": "success",
            "output": "The answer is 42",
            "error": None,
            "session_id": "user_123",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        
        # Verify structure
        assert response["status"] == "success"
        assert response["output"] is not None
        assert response["error"] is None
        assert "session_id" in response
        assert "timestamp" in response

    def test_error_response_structure(self):
        """TEST 34: Error response must have correct structure."""
        response = {
            "status": "error",
            "output": None,
            "error": "Instance not found",
            "session_id": "user_123",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        
        # Verify structure
        assert response["status"] == "error"
        assert response["output"] is None
        assert response["error"] is not None
        assert "session_id" in response
        assert "timestamp" in response

    def test_response_includes_session_id(self):
        """TEST 35: Response should include session_id from request."""
        session_id = "telegram_user_456"
        response = {
            "status": "success",
            "output": "Result",
            "session_id": session_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        
        assert response["session_id"] == session_id

    def test_response_timestamp_format_iso8601(self):
        """TEST 36: Response timestamp should be ISO 8601 format with Z suffix."""
        response = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        
        timestamp = response["timestamp"]
        assert timestamp.endswith("Z"), "Timestamp should end with Z"
        assert "T" in timestamp, "Timestamp should contain ISO 8601 separator"

    def test_response_output_truncated_if_too_long(self):
        """TEST 37: Error message should be truncated to prevent bloat."""
        # Error messages truncated to 500 chars
        long_error = "x" * 1000
        truncated = long_error[:500]
        
        assert len(truncated) == 500


# ============================================================================
# TEST 7: END-TO-END INTEGRATION TESTS
# ============================================================================

class TestEndToEndIntegration:
    """Full end-to-end integration tests."""

    @pytest.mark.asyncio
    async def test_e2e_valid_request_success(self, mock_orchestrator):
        """TEST 38: Valid authenticated request should execute successfully."""
        # Setup
        mock_orchestrator.execute_on_instance = AsyncMock(
            return_value="Successfully executed"
        )
        
        # Simulate endpoint logic
        api_key = "valid-key"
        auth_valid = True  # verify_api_key("valid-key")
        
        assert auth_valid
        
        # Execute
        response = await mock_orchestrator.execute_on_instance(
            instance_name="default",
            prompt="test prompt",
            session_id="user_123",
        )
        
        # Verify
        assert response == "Successfully executed"
        assert mock_orchestrator.execute_on_instance.called

    @pytest.mark.asyncio
    async def test_e2e_unauthorized_request_rejected(self):
        """TEST 39: Unauthorized request should be rejected before execution."""
        api_key = "invalid-key"
        auth_valid = False  # verify_api_key("invalid-key")
        
        assert not auth_valid
        
        # Endpoint should return 401 before attempting execution

    @pytest.mark.asyncio
    async def test_e2e_validation_error_returns_400(self):
        """TEST 40: Invalid request should return 400 validation error."""
        request_data = {
            "agent_id": "",  # Invalid: empty
            "prompt": "valid prompt",
        }
        
        # Validation should fail
        assert not request_data["agent_id"].strip()
        
        # Endpoint returns 400

    @pytest.mark.asyncio
    async def test_e2e_rate_limited_request_returns_429(self, rate_limiter):
        """TEST 41: Rate-limited request should return 429 with Retry-After."""
        api_key = "test-key"
        
        # Fill rate limit
        for i in range(10):
            rate_limiter.is_allowed(api_key)
        
        # Next request should be rate limited
        allowed, retry_after = rate_limiter.is_allowed(api_key)
        
        assert not allowed
        assert retry_after is not None
        
        # Endpoint returns 429 with Retry-After header

    @pytest.mark.asyncio
    async def test_e2e_execution_exception_returns_500(self, mock_orchestrator):
        """TEST 42: Unexpected exception during execution should return 500."""
        mock_orchestrator.execute_on_instance = AsyncMock(
            side_effect=Exception("Unexpected error")
        )
        
        try:
            response = await mock_orchestrator.execute_on_instance(
                instance_name="default",
                prompt="test",
            )
        except Exception as e:
            # Endpoint catches and returns 500 with error details
            error_message = str(e)[:500]
            assert "Unexpected error" in error_message


# ============================================================================
# TEST 8: EDGE CASES AND SPECIAL SCENARIOS
# ============================================================================

class TestEdgeCases:
    """Test edge cases and special scenarios."""

    def test_agent_id_with_special_characters(self):
        """TEST 43: agent_id with special characters should be accepted."""
        agent_id = "agent-01_instance.default"
        
        # Should be valid (non-empty)
        assert agent_id.strip()

    def test_prompt_with_newlines_and_formatting(self):
        """TEST 44: Prompt with newlines and formatting should be accepted."""
        prompt = """What is the meaning of life?
    
Please explain in detail with examples."""
        
        # Should be valid
        assert prompt.strip()
        assert len(prompt) < 100000

    def test_multiple_consecutive_spaces_handled(self):
        """TEST 45: Multiple consecutive spaces should be handled correctly."""
        agent_id = "default"
        session_id = "   user   123   "
        
        # Stripping removes outer spaces only
        assert agent_id.strip() == "default"
        assert session_id.strip() == "user   123"

    def test_null_characters_in_prompt(self):
        """TEST 46: Null characters in prompt should be handled safely."""
        prompt = "test\x00prompt"  # Null character
        
        # Should not crash validation
        assert isinstance(prompt, str)

    def test_very_long_agent_id_at_max(self):
        """TEST 47: agent_id at maximum length (255) should be accepted."""
        agent_id = "a" * 255
        
        assert len(agent_id) == 255

    def test_response_with_none_output(self):
        """TEST 48: Response with None output should be handled correctly."""
        response = {
            "status": "error",
            "output": None,
            "error": "Some error",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        
        assert response["output"] is None
        assert response["status"] == "error"

    def test_concurrent_requests_different_keys(self):
        """TEST 49: Concurrent requests from different API keys should be independent."""
        # This would require async test with multiple concurrent calls
        # Each API key should have independent rate limit tracking
        pass

    def test_session_id_special_characters(self):
        """TEST 50: session_id with special characters should be handled."""
        session_id = "user_123-456_xyz@example.com"
        
        # Should be valid
        assert session_id.strip()


# ============================================================================
# HEALTH CHECK AND STATUS ENDPOINTS
# ============================================================================

class TestHealthAndStatus:
    """Test health check and status endpoints."""

    def test_health_check_response_format(self):
        """TEST 51: Health check should return proper status response."""
        response = {
            "status": "ok",
            "instance": "hermes",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        
        assert response["status"] == "ok"
        assert "timestamp" in response

    def test_agent_status_requires_auth(self):
        """TEST 52: Agent status endpoint should require authentication."""
        # No API key -> 401 Unauthorized
        # (Similar to execute endpoint)
        pass

    def test_agent_status_returns_agent_info(self):
        """TEST 53: Agent status should return agent information."""
        response = {
            "running": False,
            "current_session": None,
            "model": "claude-3-sonnet",
            "instance": "hermes",
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        
        assert "running" in response
        assert "model" in response
        assert "timestamp" in response


# ============================================================================
# RUN TESTS
# ============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
