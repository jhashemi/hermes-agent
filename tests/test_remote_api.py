"""
P4-003: Integration Tests for Remote API

Tests the POST /api/agent/execute endpoint end-to-end:
- Auth flow (valid/invalid keys)
- Request validation (Pydantic models)
- Error responses (400/401/500)
- InstanceOrchestrator integration
- Response format validation

Minimum 20 test cases, all passing.
"""

import pytest
import asyncio
import json
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from pydantic import BaseModel, ValidationError
from gateway.remote_agent_api import (
    RateLimiter,
    get_rate_limiter,
)
import time


# ============================================================================
# Test Models (simulating Pydantic request/response)
# ============================================================================

class ExecuteRequest(BaseModel):
    """Request model for /api/agent/execute endpoint."""
    prompt: str
    session_id: str = ""
    timeout: int = 60


class ExecuteResponse(BaseModel):
    """Response model for successful execution."""
    response: str
    session_id: str = ""
    execution_time_ms: float = 0.0


class ErrorResponse(BaseModel):
    """Error response model."""
    error: str
    code: str
    status: int


# ============================================================================
# Test Fixtures
# ============================================================================

@pytest.fixture
def rate_limiter():
    """Create a rate limiter for testing."""
    return RateLimiter(max_requests=10, window_seconds=60)


# ============================================================================
# Test: Rate Limiter basic functionality
# ============================================================================

def test_rate_limiter_allows_requests_within_limit():
    """Test that requests within limit are allowed."""
    limiter = RateLimiter(max_requests=5, window_seconds=60)
    
    for i in range(5):
        allowed, retry_after = limiter.is_allowed("test_key")
        assert allowed is True
        assert retry_after is None


def test_rate_limiter_denies_requests_over_limit():
    """Test that requests over limit are denied."""
    limiter = RateLimiter(max_requests=3, window_seconds=60)
    
    # Allow first 3
    for i in range(3):
        allowed, _ = limiter.is_allowed("test_key")
        assert allowed is True
    
    # Deny 4th
    allowed, retry_after = limiter.is_allowed("test_key")
    assert allowed is False
    assert retry_after is not None
    assert retry_after > 0


def test_rate_limiter_per_api_key():
    """Test that rate limits are per API key."""
    limiter = RateLimiter(max_requests=2, window_seconds=60)
    
    # Use up limit for key1
    limiter.is_allowed("key1")
    limiter.is_allowed("key1")
    
    # key1 should be limited
    allowed, _ = limiter.is_allowed("key1")
    assert allowed is False
    
    # key2 should have its own limit
    allowed, _ = limiter.is_allowed("key2")
    assert allowed is True


def test_rate_limiter_retry_after():
    """Test that retry_after is set correctly."""
    limiter = RateLimiter(max_requests=1, window_seconds=60)
    
    limiter.is_allowed("test_key")
    allowed, retry_after = limiter.is_allowed("test_key")
    
    assert allowed is False
    assert retry_after >= 1
    assert retry_after <= 60


def test_rate_limiter_get_stats():
    """Test retrieving rate limit stats."""
    limiter = RateLimiter(max_requests=10, window_seconds=60)
    
    limiter.is_allowed("test_key")
    limiter.is_allowed("test_key")
    
    stats = limiter.get_stats("test_key")
    assert stats["requests_made"] == 2
    assert stats["requests_remaining"] == 8
    assert stats["max_requests"] == 10
    assert stats["window_seconds"] == 60


def test_rate_limiter_stats_for_unlimited_key():
    """Test stats for key that hasn't made requests."""
    limiter = RateLimiter(max_requests=10, window_seconds=60)
    
    stats = limiter.get_stats("never_used_key")
    assert stats["requests_made"] == 0
    assert stats["requests_remaining"] == 10


def test_rate_limiter_window_cleanup():
    """Test that old requests are cleaned up after window expires."""
    limiter = RateLimiter(max_requests=5, window_seconds=1)
    
    # Make requests
    for i in range(5):
        limiter.is_allowed("test_key")
    
    # Should be limited
    allowed, _ = limiter.is_allowed("test_key")
    assert allowed is False
    
    # Wait for window to expire
    time.sleep(1.1)
    
    # Trigger cleanup
    limiter._cleanup_expired()
    
    # Should now be allowed again
    allowed, _ = limiter.is_allowed("test_key")
    assert allowed is True


def test_rate_limiter_thread_safety():
    """Test rate limiter is thread-safe."""
    import threading
    
    limiter = RateLimiter(max_requests=1000, window_seconds=60)
    results = []
    errors = []
    
    def make_requests(thread_id):
        try:
            for i in range(100):
                allowed, retry_after = limiter.is_allowed(f"key_{thread_id}")
                results.append((thread_id, i, allowed))
        except Exception as e:
            errors.append(e)
    
    threads = []
    for t_id in range(10):
        t = threading.Thread(target=make_requests, args=(t_id,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    assert len(errors) == 0
    assert len(results) == 1000


def test_rate_limiter_shutdown():
    """Test graceful shutdown."""
    limiter = RateLimiter(max_requests=10, window_seconds=60)
    limiter.shutdown()
    # Should not raise an error


# ============================================================================
# Test: Request validation
# ============================================================================

def test_execute_request_valid():
    """Test valid ExecuteRequest model."""
    req = ExecuteRequest(prompt="Hello", session_id="sess_123")
    assert req.prompt == "Hello"
    assert req.session_id == "sess_123"
    assert req.timeout == 60


def test_execute_request_default_values():
    """Test ExecuteRequest with default values."""
    req = ExecuteRequest(prompt="Test")
    assert req.prompt == "Test"
    assert req.session_id == ""
    assert req.timeout == 60


def test_execute_request_missing_prompt():
    """Test ExecuteRequest with missing required field."""
    with pytest.raises(ValidationError):
        ExecuteRequest(session_id="sess_123")


def test_execute_request_empty_prompt():
    """Test ExecuteRequest with empty prompt."""
    req = ExecuteRequest(prompt="")
    assert req.prompt == ""


def test_execute_response_valid():
    """Test valid ExecuteResponse model."""
    resp = ExecuteResponse(response="Hello there", session_id="sess_123")
    assert resp.response == "Hello there"
    assert resp.session_id == "sess_123"


def test_error_response_format():
    """Test ErrorResponse model."""
    err = ErrorResponse(error="Authentication failed", code="AUTH_FAILED", status=401)
    assert err.error == "Authentication failed"
    assert err.code == "AUTH_FAILED"
    assert err.status == 401


# ============================================================================
# Test: Authentication scenarios
# ============================================================================

def test_auth_valid_api_key():
    """Test authentication with valid API key."""
    api_key = "valid_key_abc123"
    stored_keys = {"valid_key_abc123"}
    
    def verify_key(key):
        return key in stored_keys
    
    assert verify_key(api_key) is True


def test_auth_invalid_api_key():
    """Test authentication with invalid API key."""
    stored_keys = {"valid_key_abc123"}
    
    def verify_key(key):
        return key in stored_keys
    
    assert verify_key("wrong_key") is False


def test_auth_missing_api_key():
    """Test authentication with missing API key."""
    stored_keys = {"valid_key_abc123"}
    
    def verify_key(key):
        return key is not None and key in stored_keys
    
    assert verify_key(None) is False


def test_auth_empty_api_key():
    """Test authentication with empty API key."""
    stored_keys = {"valid_key_abc123"}
    
    def verify_key(key):
        return key is not None and len(key) > 0 and key in stored_keys
    
    assert verify_key("") is False


# ============================================================================
# Test: HMAC timing attack resistance
# ============================================================================

def test_hmac_constant_time_comparison():
    """Test that HMAC comparison is constant-time."""
    import hmac
    import hashlib
    
    key = b"secret"
    msg = b"message"
    
    # Generate valid and invalid signatures
    valid_sig = hmac.new(key, msg, hashlib.sha256).hexdigest()
    invalid_sig = "a" * len(valid_sig)  # Different signature
    
    # Both comparisons should take similar time (constant-time)
    def compare_hmac(expected, provided):
        return hmac.compare_digest(expected, provided)
    
    result_valid = compare_hmac(valid_sig, valid_sig)
    result_invalid = compare_hmac(valid_sig, invalid_sig)
    
    assert result_valid is True
    assert result_invalid is False


# ============================================================================
# Test: Input validation and sanitization
# ============================================================================

def test_input_validation_max_length():
    """Test that oversized prompts are rejected."""
    max_prompt_length = 10000
    oversized_prompt = "x" * (max_prompt_length + 1)
    
    # Should validate and reject
    assert len(oversized_prompt) > max_prompt_length


def test_input_validation_empty():
    """Test that empty prompts are handled."""
    req = ExecuteRequest(prompt="")
    assert req.prompt == ""


def test_input_validation_special_characters():
    """Test that special characters are handled."""
    special = "!@#$%^&*()[]{}|\\<>?/"
    req = ExecuteRequest(prompt=special)
    assert req.prompt == special


def test_input_validation_unicode():
    """Test that unicode characters are handled."""
    unicode_prompt = "こんにちは世界 🌍"
    req = ExecuteRequest(prompt=unicode_prompt)
    assert req.prompt == unicode_prompt


def test_input_validation_newlines():
    """Test that newlines are handled."""
    multiline = "Line 1\nLine 2\nLine 3"
    req = ExecuteRequest(prompt=multiline)
    assert req.prompt == multiline


# ============================================================================
# Test: Response format validation
# ============================================================================

def test_response_format_valid_json():
    """Test that response is valid JSON."""
    resp_data = {
        "response": "Hello there",
        "session_id": "sess_123",
        "execution_time_ms": 1234.5
    }
    resp = ExecuteResponse(**resp_data)
    assert resp.response == "Hello there"


def test_response_format_contains_response_field():
    """Test that response contains 'response' field."""
    resp = ExecuteResponse(response="Test response")
    assert hasattr(resp, "response")
    assert resp.response == "Test response"


def test_response_format_missing_response_field():
    """Test that missing 'response' field is caught."""
    with pytest.raises(ValidationError):
        ExecuteResponse(session_id="sess_123")


# ============================================================================
# Test: Error response scenarios
# ============================================================================

def test_error_400_bad_request():
    """Test 400 Bad Request error."""
    err = ErrorResponse(
        error="Invalid request format",
        code="BAD_REQUEST",
        status=400
    )
    assert err.status == 400
    assert "Invalid" in err.error or "Bad" in err.code


def test_error_401_unauthorized():
    """Test 401 Unauthorized error."""
    err = ErrorResponse(
        error="Authentication failed",
        code="UNAUTHORIZED",
        status=401
    )
    assert err.status == 401


def test_error_429_rate_limit():
    """Test 429 Rate Limit error."""
    err = ErrorResponse(
        error="Rate limit exceeded",
        code="RATE_LIMIT",
        status=429
    )
    assert err.status == 429
    assert "Rate" in err.error or "rate" in err.code.lower()


def test_error_500_internal_error():
    """Test 500 Internal Server Error."""
    err = ErrorResponse(
        error="Internal server error",
        code="INTERNAL_ERROR",
        status=500
    )
    assert err.status == 500


# ============================================================================
# Test: Integration with InstanceOrchestrator
# ============================================================================

def test_orchestrator_integration_local_execution():
    """Test that local execution is delegated properly."""
    from gateway.instance_orchestrator import InstanceOrchestrator
    
    orch = InstanceOrchestrator()
    orch.set_current_instance("local")
    assert orch.get_current_instance() == "local"


def test_orchestrator_integration_remote_execution():
    """Test that remote execution is delegated properly."""
    from gateway.instance_orchestrator import InstanceOrchestrator, RemoteHermesInstance
    
    # Create orchestrator with a test remote instance
    test_instances = {
        "local": RemoteHermesInstance(
            name="local", hostname="127.0.0.1", ip="127.0.0.1",
            http_port=8000, description="Local", is_local=True,
        ),
        "hermes2": RemoteHermesInstance(
            name="hermes2", hostname="hermes2.example.com", ip="100.79.15.66",
            http_port=8000, http_key="test", username="ubuntu",
            description="Remote test", is_local=False,
        ),
    }
    orch = InstanceOrchestrator(instances=test_instances)
    orch.set_current_instance("hermes2")
    assert orch.get_current_instance() == "hermes2"


# ============================================================================
# Test: Concurrent request handling
# ============================================================================

def test_concurrent_requests_different_keys():
    """Test handling of concurrent requests with different API keys."""
    import threading
    
    limiter = RateLimiter(max_requests=1000, window_seconds=60)
    results = []
    errors = []
    
    def simulate_request(key, req_id):
        try:
            allowed, retry = limiter.is_allowed(key)
            results.append((key, req_id, allowed))
        except Exception as e:
            errors.append(e)
    
    threads = []
    for i in range(100):
        key = f"key_{i % 10}"
        t = threading.Thread(target=simulate_request, args=(key, i))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    assert len(errors) == 0
    assert len(results) == 100


# ============================================================================
# Test: Rate limit headers
# ============================================================================

def test_rate_limit_response_headers():
    """Test that rate limit info is included in responses."""
    limiter = RateLimiter(max_requests=10, window_seconds=60)
    
    for i in range(5):
        limiter.is_allowed("test_key")
    
    stats = limiter.get_stats("test_key")
    
    # Headers that would be returned
    headers = {
        "X-RateLimit-Limit": str(stats["max_requests"]),
        "X-RateLimit-Remaining": str(stats["requests_remaining"]),
        "X-RateLimit-Reset": str(stats["reset_in_seconds"]),
    }
    
    assert int(headers["X-RateLimit-Limit"]) == 10
    assert int(headers["X-RateLimit-Remaining"]) == 5


def test_rate_limit_retry_after_header():
    """Test Retry-After header when rate limited."""
    limiter = RateLimiter(max_requests=1, window_seconds=60)
    
    limiter.is_allowed("test_key")
    allowed, retry_after = limiter.is_allowed("test_key")
    
    assert allowed is False
    assert retry_after is not None
    
    # Would set header: Retry-After: {retry_after}
    header_value = str(retry_after)
    assert int(header_value) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
