"""
CRITICAL Security Fix Tests for remote_agent_api.py

Tests cover:
1. HMAC-SHA256 Request Signature Verification (X-Hermes-Signature)
2. Pydantic Input Validation (agent_id format, prompt max 10000, session_id format)
3. HTTP Session Leak Fix (SessionManager singleton, lifecycle, unclosed detection)

Uses FastAPI TestClient for real HTTP testing against the actual endpoint.
"""

import asyncio
import hashlib
import hmac
import json
import os
import pytest
import warnings
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway.remote_agent_api import (
    verify_api_key,
    get_expected_key,
    verify_request_signature,
    compute_hmac_signature,
    validate_execute_request,
    get_session_manager,
    close_session_manager,
    SessionManager,
    AGENT_ID_PATTERN,
    SESSION_ID_PATTERN,
    MAX_PROMPT_LENGTH,
    create_remote_api_blueprint,
)


# ============================================================================
# Fixtures
# ============================================================================

TEST_API_KEY = "test-secret-key-for-hmac-2024"


def _sign_body(body: bytes, secret: str = TEST_API_KEY) -> str:
    """Helper: compute HMAC-SHA256 of body bytes with secret."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _make_auth_headers(secret: str = TEST_API_KEY, body: bytes = b"") -> dict:
    """Helper: build auth headers with both API key and HMAC signature."""
    return {
        "X-Hermes-Key": secret,
        "X-Hermes-Signature": _sign_body(body, secret),
    }


@pytest.fixture
def fastapi_app():
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
        yield app, mock_gateway_runner, mock_orchestrator
        get_expected_key.cache_clear()


@pytest.fixture
def client(fastapi_app):
    """Create a TestClient with auth configured."""
    app, mock_gateway, mock_orchestrator = fastapi_app
    with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": TEST_API_KEY}):
        get_expected_key.cache_clear()
        client = TestClient(app)
        yield client, mock_gateway, mock_orchestrator
        get_expected_key.cache_clear()


# ============================================================================
# FIX 1: HMAC-SHA256 Request Signature Verification
# ============================================================================

class TestHMACSignatureVerification:
    """Test HMAC-SHA256 request signature verification."""

    # --- Unit tests for compute_hmac_signature and verify_request_signature ---

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": TEST_API_KEY})
    def test_compute_hmac_signature_deterministic(self):
        """Same body + same key → same signature."""
        get_expected_key.cache_clear()
        body = b'{"agent_id":"default","prompt":"hello"}'
        sig1 = compute_hmac_signature(body, TEST_API_KEY)
        sig2 = compute_hmac_signature(body, TEST_API_KEY)
        assert sig1 == sig2
        assert len(sig1) == 64  # SHA-256 hex digest is 64 chars

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": TEST_API_KEY})
    def test_compute_hmac_signature_different_body(self):
        """Different body → different signature."""
        get_expected_key.cache_clear()
        sig1 = compute_hmac_signature(b'body1', TEST_API_KEY)
        sig2 = compute_hmac_signature(b'body2', TEST_API_KEY)
        assert sig1 != sig2

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": TEST_API_KEY})
    def test_verify_request_signature_valid(self):
        """Correct HMAC signature should verify."""
        get_expected_key.cache_clear()
        body = b'{"agent_id":"default","prompt":"hello"}'
        sig = compute_hmac_signature(body, TEST_API_KEY)
        assert verify_request_signature(body, sig) is True

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": TEST_API_KEY})
    def test_verify_request_signature_invalid(self):
        """Wrong HMAC signature should fail."""
        get_expected_key.cache_clear()
        body = b'{"agent_id":"default","prompt":"hello"}'
        assert verify_request_signature(body, "bad-signature") is False

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": TEST_API_KEY})
    def test_verify_request_signature_missing(self):
        """Missing signature should fail."""
        get_expected_key.cache_clear()
        body = b'some-body'
        assert verify_request_signature(body, None) is False
        assert verify_request_signature(body, "") is False

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": ""})
    def test_verify_request_signature_no_key_configured(self):
        """No API key configured → signature verification fails."""
        get_expected_key.cache_clear()
        body = b'some-body'
        sig = compute_hmac_signature(body, "anything")
        assert verify_request_signature(body, sig) is False

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": TEST_API_KEY})
    def test_verify_request_signature_wrong_key(self):
        """Signature computed with wrong key should fail."""
        get_expected_key.cache_clear()
        body = b'{"agent_id":"default"}'
        sig = compute_hmac_signature(body, "wrong-secret")
        assert verify_request_signature(body, sig) is False

    # --- Integration tests with the endpoint ---

    def test_execute_without_signature_returns_401(self, client):
        """Request without X-Hermes-Signature should return 401."""
        c, _, _ = client
        body_dict = {"agent_id": "default", "prompt": "Hello"}
        response = c.post(
            "/api/agent/execute",
            json=body_dict,
            headers={"X-Hermes-Key": TEST_API_KEY},
            # No X-Hermes-Signature
        )
        assert response.status_code == 401
        assert "signature" in response.json()["detail"].lower()

    def test_execute_with_invalid_signature_returns_401(self, client):
        """Request with wrong HMAC signature should return 401."""
        c, _, _ = client
        body_dict = {"agent_id": "default", "prompt": "Hello"}
        response = c.post(
            "/api/agent/execute",
            json=body_dict,
            headers={
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": "invalid-signature-here",
            },
        )
        assert response.status_code == 401

    def test_execute_with_correct_signature_succeeds(self, client):
        """Request with correct HMAC signature + API key should succeed."""
        c, _, _ = client
        body_dict = {"agent_id": "default", "prompt": "Hello"}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        sig = _sign_body(body_bytes)

        response = c.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 200

    def test_execute_signature_rejects_body_tampering(self, client):
        """Signature computed for one body should not validate for another."""
        c, _, _ = client
        # Compute sig for original body
        original_body = json.dumps({"agent_id": "default", "prompt": "Hello"}).encode()
        sig = _sign_body(original_body)

        # Send different body with old sig
        tampered_body = json.dumps({"agent_id": "default", "prompt": "MALICIOUS"}).encode()
        response = c.post(
            "/api/agent/execute",
            content=tampered_body,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 401

    def test_status_endpoint_requires_signature(self, client):
        """GET /api/agent/status also requires HMAC signature."""
        c, _, _ = client
        response = c.get(
            "/api/agent/status",
            headers={"X-Hermes-Key": TEST_API_KEY},
        )
        assert response.status_code == 401

    def test_status_endpoint_with_correct_signature(self, client):
        """GET /api/agent/status with correct signature should succeed."""
        c, _, _ = client
        # GET requests have empty body
        body = b""
        sig = _sign_body(body, TEST_API_KEY)
        response = c.get(
            "/api/agent/status",
            headers={
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 200

    def test_signature_is_constant_time(self):
        """verify_request_signature uses hmac.compare_digest (constant-time)."""
        # We can't easily test timing, but we verify both short and long
        # wrong signatures are rejected consistently
        body = b'test-body'
        with patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": TEST_API_KEY}):
            get_expected_key.cache_clear()
            sig_short = "a"
            sig_long = "a" * 64
            assert verify_request_signature(body, sig_short) is False
            assert verify_request_signature(body, sig_long) is False


# ============================================================================
# FIX 2: Pydantic Input Validation
# ============================================================================

class TestInputValidation:
    """Test Pydantic input validation for all request schemas."""

    # --- agent_id format validation (alphanumeric + dashes) ---

    def test_agent_id_alphanumeric(self):
        """Simple alphanumeric agent_id should be accepted."""
        req = validate_execute_request({"agent_id": "agent01", "prompt": "hello"})
        assert req.agent_id == "agent01"

    def test_agent_id_with_dashes(self):
        """agent_id with dashes should be accepted."""
        req = validate_execute_request({"agent_id": "my-agent-01", "prompt": "hello"})
        assert req.agent_id == "my-agent-01"

    def test_agent_id_alphanumeric_start(self):
        """agent_id must start with alphanumeric character."""
        req = validate_execute_request({"agent_id": "a-default", "prompt": "hello"})
        assert req.agent_id == "a-default"

    def test_agent_id_starting_with_dash_rejected(self):
        """agent_id starting with dash should be rejected."""
        with pytest.raises(Exception):
            validate_execute_request({"agent_id": "-agent", "prompt": "hello"})

    def test_agent_id_with_spaces_rejected(self):
        """agent_id with spaces should be rejected."""
        with pytest.raises(Exception):
            validate_execute_request({"agent_id": "my agent", "prompt": "hello"})

    def test_agent_id_with_special_chars_rejected(self):
        """agent_id with special characters should be rejected."""
        for bad_id in ["agent@1", "agent.io", "agent_1", "agent#1", "agent$1"]:
            with pytest.raises(Exception, match="agent_id"):
                validate_execute_request({"agent_id": bad_id, "prompt": "hello"})

    def test_agent_id_empty_rejected(self):
        """Empty agent_id should be rejected."""
        with pytest.raises(Exception):
            validate_execute_request({"agent_id": "", "prompt": "hello"})

    def test_agent_id_whitespace_only_rejected(self):
        """Whitespace-only agent_id should be rejected."""
        with pytest.raises(Exception):
            validate_execute_request({"agent_id": "   ", "prompt": "hello"})

    def test_agent_id_too_long_rejected(self):
        """agent_id exceeding 255 chars should be rejected."""
        with pytest.raises(Exception):
            validate_execute_request({"agent_id": "a" * 256, "prompt": "hello"})

    def test_agent_id_exactly_255_accepted(self):
        """agent_id with exactly 255 chars should be accepted."""
        req = validate_execute_request({"agent_id": "a" * 255, "prompt": "hello"})
        assert len(req.agent_id) == 255

    def test_agent_id_missing_rejected(self):
        """Missing agent_id should be rejected."""
        with pytest.raises(Exception):
            validate_execute_request({"prompt": "hello"})

    # --- prompt validation (max 10000 chars) ---

    def test_prompt_within_limit(self):
        """Prompt within 10000 chars should be accepted."""
        req = validate_execute_request({"agent_id": "default", "prompt": "Hello world"})
        assert req.prompt == "Hello world"

    def test_prompt_exactly_max_length(self):
        """Prompt with exactly 10000 chars should be accepted."""
        prompt = "x" * MAX_PROMPT_LENGTH
        req = validate_execute_request({"agent_id": "default", "prompt": prompt})
        assert len(req.prompt) == MAX_PROMPT_LENGTH

    def test_prompt_exceeds_max_length(self):
        """Prompt exceeding 10000 chars should be rejected."""
        prompt = "x" * (MAX_PROMPT_LENGTH + 1)
        with pytest.raises(Exception, match="10000"):
            validate_execute_request({"agent_id": "default", "prompt": prompt})

    def test_prompt_empty_rejected(self):
        """Empty prompt should be rejected."""
        with pytest.raises(Exception):
            validate_execute_request({"agent_id": "default", "prompt": ""})

    def test_prompt_whitespace_only_rejected(self):
        """Whitespace-only prompt should be rejected."""
        with pytest.raises(Exception):
            validate_execute_request({"agent_id": "default", "prompt": "   \n\t  "})

    def test_prompt_missing_rejected(self):
        """Missing prompt should be rejected."""
        with pytest.raises(Exception):
            validate_execute_request({"agent_id": "default"})

    def test_prompt_with_newlines_accepted(self):
        """Prompt with newlines should be accepted."""
        prompt = "Line 1\nLine 2\nLine 3"
        req = validate_execute_request({"agent_id": "default", "prompt": prompt})
        assert req.prompt == prompt

    def test_prompt_unicode_accepted(self):
        """Prompt with unicode characters should be accepted."""
        prompt = "你好世界 🌍 Héllo"
        req = validate_execute_request({"agent_id": "default", "prompt": prompt})
        assert req.prompt == prompt

    # --- session_id format validation ---

    def test_session_id_alphanumeric(self):
        """Simple alphanumeric session_id should be accepted."""
        req = validate_execute_request({
            "agent_id": "default",
            "prompt": "hello",
            "session_id": "user123",
        })
        assert req.session_id == "user123"

    def test_session_id_with_dashes_and_underscores(self):
        """session_id with dashes and underscores should be accepted."""
        req = validate_execute_request({
            "agent_id": "default",
            "prompt": "hello",
            "session_id": "telegram_user-123",
        })
        assert req.session_id == "telegram_user-123"

    def test_session_id_optional(self):
        """session_id is optional."""
        req = validate_execute_request({
            "agent_id": "default",
            "prompt": "hello",
        })
        assert req.session_id is None

    def test_session_id_empty_converts_to_none(self):
        """Empty session_id string should convert to None."""
        req = validate_execute_request({
            "agent_id": "default",
            "prompt": "hello",
            "session_id": "",
        })
        assert req.session_id is None

    def test_session_id_whitespace_converts_to_none(self):
        """Whitespace-only session_id should convert to None."""
        req = validate_execute_request({
            "agent_id": "default",
            "prompt": "hello",
            "session_id": "   ",
        })
        assert req.session_id is None

    def test_session_id_with_spaces_rejected(self):
        """session_id with spaces should be rejected."""
        with pytest.raises(Exception, match="session_id"):
            validate_execute_request({
                "agent_id": "default",
                "prompt": "hello",
                "session_id": "user 123",
            })

    def test_session_id_with_special_chars_rejected(self):
        """session_id with special characters should be rejected."""
        for bad_session in ["user@123", "user.io", "user#1", "user$1", "user!"]:
            with pytest.raises(Exception, match="session_id"):
                validate_execute_request({
                    "agent_id": "default",
                    "prompt": "hello",
                    "session_id": bad_session,
                })

    def test_session_id_too_long_rejected(self):
        """session_id exceeding 255 chars should be rejected."""
        with pytest.raises(Exception):
            validate_execute_request({
                "agent_id": "default",
                "prompt": "hello",
                "session_id": "s" * 256,
            })

    # --- Integration: endpoint returns 422 for validation failures ---

    def test_endpoint_rejects_invalid_agent_id_format(self, client):
        """Endpoint should return 422 for invalid agent_id format."""
        c, _, _ = client
        body_dict = {"agent_id": "agent@invalid", "prompt": "hello"}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        sig = _sign_body(body_bytes)

        response = c.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 422

    def test_endpoint_rejects_prompt_over_10000(self, client):
        """Endpoint should return 422 for prompt exceeding 10000 chars."""
        c, _, _ = client
        body_dict = {"agent_id": "default", "prompt": "x" * (MAX_PROMPT_LENGTH + 1)}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        sig = _sign_body(body_bytes)

        response = c.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 422

    def test_endpoint_rejects_invalid_session_id(self, client):
        """Endpoint should return 422 for invalid session_id format."""
        c, _, _ = client
        body_dict = {"agent_id": "default", "prompt": "hello", "session_id": "bad session!"}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        sig = _sign_body(body_bytes)

        response = c.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 422

    def test_endpoint_accepts_valid_request(self, client):
        """Endpoint should accept a fully valid request."""
        c, _, _ = client
        body_dict = {"agent_id": "default", "prompt": "Hello", "session_id": "user-123"}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        sig = _sign_body(body_bytes)

        response = c.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 200

    def test_endpoint_rejects_invalid_json(self, client):
        """Endpoint should return 422 for invalid JSON."""
        c, _, _ = client
        body_bytes = b'{not valid json}'
        sig = _sign_body(body_bytes)

        response = c.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 422

    def test_endpoint_rejects_empty_prompt(self, client):
        """Endpoint should return 422 for empty prompt."""
        c, _, _ = client
        body_dict = {"agent_id": "default", "prompt": ""}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        sig = _sign_body(body_bytes)

        response = c.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 422


# ============================================================================
# FIX 3: HTTP Session Leak — SessionManager
# ============================================================================

class TestSessionManager:
    """Test shared aiohttp ClientSession lifecycle management."""

    def test_session_manager_singleton(self):
        """get_session_manager() returns a singleton."""
        # Reset global
        import gateway.remote_agent_api as mod
        mod._session_manager = None

        sm1 = get_session_manager()
        sm2 = get_session_manager()
        assert sm1 is sm2

        # Cleanup
        mod._session_manager = None

    def test_session_manager_not_initialized_initially(self):
        """New SessionManager should not have a session."""
        sm = SessionManager()
        assert sm.is_initialized is False

    @pytest.mark.asyncio
    async def test_session_manager_creates_session(self):
        """get_session() lazily creates an aiohttp ClientSession."""
        sm = SessionManager()
        session = await sm.get_session()
        assert session is not None
        assert not session.closed
        assert sm.is_initialized is True

        # Cleanup
        await sm.close_session()

    @pytest.mark.asyncio
    async def test_session_manager_reuses_session(self):
        """get_session() returns the same session on subsequent calls."""
        sm = SessionManager()
        session1 = await sm.get_session()
        session2 = await sm.get_session()
        assert session1 is session2

        # Cleanup
        await sm.close_session()

    @pytest.mark.asyncio
    async def test_session_manager_close_session(self):
        """close_session() properly closes the session."""
        sm = SessionManager()
        session = await sm.get_session()
        assert not session.closed

        await sm.close_session()
        assert session.closed is True
        assert sm.is_initialized is False

    @pytest.mark.asyncio
    async def test_session_manager_close_idempotent(self):
        """close_session() can be called multiple times safely."""
        sm = SessionManager()
        await sm.get_session()
        await sm.close_session()
        await sm.close_session()  # Should not raise

    def test_check_unclosed_detects_leak(self):
        """check_unclosed() returns True and warns when session is unclosed."""
        sm = SessionManager()
        # Simulate an unclosed session by creating a mock
        sm._session = MagicMock()
        sm._session.closed = False

        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            result = sm.check_unclosed()
            assert result is True
            assert len(w) == 1
            assert issubclass(w[0].category, ResourceWarning)
            assert "Unclosed" in str(w[0].message)

    def test_check_unclosed_clean_when_closed(self):
        """check_unclosed() returns False when no session or session is closed."""
        sm = SessionManager()
        # No session created
        assert sm.check_unclosed() is False

        # Session created but closed
        sm._session = MagicMock()
        sm._session.closed = True
        assert sm.check_unclosed() is False

    @pytest.mark.asyncio
    async def test_close_session_manager_global(self):
        """close_session_manager() closes the global session manager."""
        import gateway.remote_agent_api as mod
        mod._session_manager = None

        sm = get_session_manager()
        session = await sm.get_session()
        assert not session.closed

        await close_session_manager()
        assert session.closed is True
        assert mod._session_manager is None

    @pytest.mark.asyncio
    async def test_session_manager_recreates_after_close(self):
        """After close_session(), get_session() creates a new session."""
        sm = SessionManager()
        session1 = await sm.get_session()
        await sm.close_session()

        session2 = await sm.get_session()
        assert session2 is not session1  # New session created
        assert not session2.closed

        # Cleanup
        await sm.close_session()


# ============================================================================
# Combined Auth + Validation Integration Tests
# ============================================================================

class TestCombinedSecurityIntegration:
    """Test that all three security fixes work together."""

    def test_full_valid_request(self, client):
        """Full valid request with API key + HMAC signature + valid body."""
        c, mock_gateway, mock_orch = client
        body_dict = {"agent_id": "default", "prompt": "What is AI?", "session_id": "user-123"}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        sig = _sign_body(body_bytes)

        response = c.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"

    def test_auth_checked_before_validation(self, client):
        """Auth (signature + key) is checked before request validation."""
        c, _, _ = client
        # Invalid signature → 401, even with valid body
        body_dict = {"agent_id": "default", "prompt": "Hello"}
        body_bytes = json.dumps(body_dict).encode("utf-8")

        response = c.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": "wrong-sig",
            },
        )
        assert response.status_code == 401

    def test_wrong_key_with_valid_signature_rejected(self, client):
        """Correct HMAC but wrong API key → 401."""
        c, _, _ = client
        body_dict = {"agent_id": "default", "prompt": "Hello"}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        sig = _sign_body(body_bytes)

        response = c.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": "wrong-key",
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 401

    def test_valid_auth_but_invalid_body_returns_422(self, client):
        """Valid auth but invalid body → 422."""
        c, _, _ = client
        body_dict = {"agent_id": "bad@id", "prompt": "hello"}
        body_bytes = json.dumps(body_dict).encode("utf-8")
        sig = _sign_body(body_bytes)

        response = c.post(
            "/api/agent/execute",
            content=body_bytes,
            headers={
                "Content-Type": "application/json",
                "X-Hermes-Key": TEST_API_KEY,
                "X-Hermes-Signature": sig,
            },
        )
        assert response.status_code == 422


# ============================================================================
# Regression: existing tests should still work
# ============================================================================

class TestAPIKeyAuthenticationRegression:
    """Ensure original API key auth still works (regression tests)."""

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key"})
    def test_verify_api_key_correct(self):
        get_expected_key.cache_clear()
        assert verify_api_key("test-secret-key") is True

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key"})
    def test_verify_api_key_wrong(self):
        get_expected_key.cache_clear()
        assert verify_api_key("wrong-key") is False

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key"})
    def test_verify_api_key_none(self):
        get_expected_key.cache_clear()
        assert verify_api_key(None) is False

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": ""})
    def test_verify_api_key_unconfigured(self):
        get_expected_key.cache_clear()
        assert verify_api_key("any-key") is False
