"""Integration tests for the remote agent execution API (t_b9fdf6bd).

Task: [TEST] Integration Tests for Remote API
File: tests/test_remote_api.py
Coverage required: 75%+ of gateway/remote_agent_api.py endpoint code.

These tests boot a real FastAPI app, register the remote-agent blueprint via
``create_remote_api_blueprint`` and drive it with ``TestClient`` so every
request path exercised here is the exact path a remote instance would hit
in production (auth check -> rate limiter -> Pydantic validator ->
orchestrator dispatch -> response envelope).

Endpoints under test:
  * POST /api/agent/execute
  * GET  /health
  * GET  /api/agent/status  (auth-shared with /health)

The 8 canonical tests required by the ticket:
  1. test_execute_agent_prompt_success               happy path
  2. test_execute_agent_prompt_auth_missing          missing X-Hermes-Key header
  3. test_execute_agent_prompt_auth_invalid          wrong X-Hermes-Key
  4. test_execute_agent_prompt_prompt_missing        missing 'prompt' field
  5. test_execute_agent_prompt_prompt_too_long       max length enforced
  6. test_execute_agent_prompt_session_validation    session_id format check
  7. test_health_endpoint                            200 OK on authenticated /health
  8. test_error_handling                             500-class exception path
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Ensure the repo root is importable when pytest is run from anywhere.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from gateway import remote_agent_api  # noqa: E402
from gateway.remote_agent_api import (  # noqa: E402
    create_remote_api_blueprint,
    get_expected_key,
    get_rate_limiter,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# All requests in this file use the same test-only API key. The value is
# injected into the process env BEFORE the FastAPI blueprint is registered
# and BEFORE ``get_expected_key`` (LRU-cached) is called.
_TEST_API_KEY = "test-api-key-t_b9fdf6bd"


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Reset LRU cache + rate limiter for every test so each test is isolated.

    ``get_expected_key`` is ``@lru_cache(maxsize=1)``-decorated, so once it
    reads the env var it never re-reads. We must invalidate it whenever we
    change the env, and we do the same for the process-wide rate limiter
    singleton so per-test 429 accounting doesn't leak.
    """
    monkeypatch.setenv("HERMES_REMOTE_API_KEY", _TEST_API_KEY)
    get_expected_key.cache_clear()

    # Reset the rate-limiter singleton so previous tests' hits don't affect us.
    previous = getattr(remote_agent_api, "_rate_limiter_instance", None)
    remote_agent_api._rate_limiter_instance = None
    try:
        yield
    finally:
        # Best-effort shutdown of anything the test spawned, then restore.
        current = getattr(remote_agent_api, "_rate_limiter_instance", None)
        if current is not None and current is not previous:
            try:
                current.shutdown()
            except Exception:
                pass
        remote_agent_api._rate_limiter_instance = previous
        get_expected_key.cache_clear()


def _build_gateway_runner(chat_return: str = "hello from local agent") -> MagicMock:
    """Return a MagicMock GatewayRunner with an orchestrator + local agent."""
    orchestrator = MagicMock()
    # Default: orchestrator returns None => remote_api falls through to local agent.
    orchestrator.execute_on_instance = AsyncMock(return_value=None)

    gateway = MagicMock()
    gateway.instance_orchestrator = orchestrator
    gateway.agent = MagicMock()
    gateway.agent.chat = MagicMock(return_value=chat_return)
    return gateway


def _build_app(gateway_runner: MagicMock) -> FastAPI:
    """Register the remote-agent blueprint on a fresh FastAPI app."""
    app = FastAPI()
    # ``create_remote_api_blueprint`` is async because it awaits nothing but is
    # declared that way; we run it synchronously here to attach routes.
    asyncio.get_event_loop() if False else None  # noqa: unused-lint hint
    asyncio.run(create_remote_api_blueprint(app, gateway_runner))
    return app


@pytest.fixture
def gateway_runner() -> MagicMock:
    return _build_gateway_runner()


@pytest.fixture
def client(gateway_runner) -> TestClient:
    app = _build_app(gateway_runner)
    return TestClient(app)


def _auth_headers(user: str = "integration-tester") -> Dict[str, str]:
    return {"X-Hermes-Key": _TEST_API_KEY, "X-Hermes-User": user}


def _valid_body(**overrides: Any) -> Dict[str, Any]:
    body = {
        "agent_id": "default",
        "prompt": "What is the meaning of life?",
        "session_id": "user_abc.123",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_execute_agent_prompt_success(client, gateway_runner):
    """POST /api/agent/execute with a valid key + body returns 200 + success."""
    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(prompt="ping"),
        headers=_auth_headers(),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "success"
    # The stubbed local agent returns our canned string.
    assert body["output"] == "hello from local agent"
    # session_id round-trips.
    assert body["session_id"] == "user_abc.123"
    assert body["timestamp"].endswith("Z")
    # Orchestrator was consulted first, then local agent invoked on None.
    gateway_runner.instance_orchestrator.execute_on_instance.assert_awaited_once()
    gateway_runner.agent.chat.assert_called_once_with("ping")


# ---------------------------------------------------------------------------
# 2. Missing auth header
# ---------------------------------------------------------------------------


def test_execute_agent_prompt_auth_missing(client):
    """No X-Hermes-Key header -> 401 Unauthorized."""
    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(),
        headers={"X-Hermes-User": "no-key-user"},  # deliberately no key
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Unauthorized"


# ---------------------------------------------------------------------------
# 3. Invalid auth header
# ---------------------------------------------------------------------------


def test_execute_agent_prompt_auth_invalid(client):
    """Wrong X-Hermes-Key -> 401 Unauthorized."""
    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(),
        headers={"X-Hermes-Key": "not-the-real-key", "X-Hermes-User": "attacker"},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Unauthorized"


# ---------------------------------------------------------------------------
# 4. Missing required field
# ---------------------------------------------------------------------------


def test_execute_agent_prompt_prompt_missing(client, gateway_runner):
    """Body without 'prompt' -> 422 (Pydantic validation)."""
    resp = client.post(
        "/api/agent/execute",
        json={"agent_id": "default"},  # no prompt
        headers=_auth_headers(),
    )
    assert resp.status_code == 422, resp.text
    payload = resp.json()
    # FastAPI/Pydantic v2 returns {"detail": [{...}]}
    assert "detail" in payload
    # At least one error mentions the 'prompt' field.
    loc_tokens = [str(err.get("loc")) for err in payload["detail"]]
    assert any("prompt" in tok for tok in loc_tokens), payload
    # And we never reached the orchestrator.
    gateway_runner.instance_orchestrator.execute_on_instance.assert_not_called()


# ---------------------------------------------------------------------------
# 5. Prompt too long
# ---------------------------------------------------------------------------


def test_execute_agent_prompt_prompt_too_long(client, gateway_runner):
    """Prompt over the 100_000-char cap -> 422 validation error."""
    oversized = "x" * 100_001  # one byte past the byte-length cap
    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(prompt=oversized),
        headers=_auth_headers(),
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    # Pydantic surfaces the max-length constraint in the detail payload.
    text_blob = str(body).lower()
    assert "prompt" in text_blob
    assert "100000" in text_blob or "max" in text_blob or "length" in text_blob
    gateway_runner.instance_orchestrator.execute_on_instance.assert_not_called()


# ---------------------------------------------------------------------------
# 6. session_id format check
# ---------------------------------------------------------------------------


def test_execute_agent_prompt_session_validation(client, gateway_runner):
    """session_id (carries chat_id) must pass INPUT-INVARIANT-01.

    A newline inside session_id is a hard-fail control character. The
    Pydantic validator raises ValueError, which FastAPI reports as 422.
    """
    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(session_id="bad\nsession"),
        headers=_auth_headers(),
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    text_blob = str(body).lower()
    assert "session_id" in text_blob or "chat_id" in text_blob
    gateway_runner.instance_orchestrator.execute_on_instance.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Health endpoint
# ---------------------------------------------------------------------------


def test_health_endpoint(client):
    """GET /health with a valid key returns 200 + status ok."""
    resp = client.get("/health", headers={"X-Hermes-Key": _TEST_API_KEY})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "ok"
    assert body["timestamp"].endswith("Z")

    # And an unauthenticated /health returns 401 (fail-closed by design).
    resp_bad = client.get("/health")
    assert resp_bad.status_code == 401


# ---------------------------------------------------------------------------
# 8. Error handling (orchestrator raises -> API returns 200 with error envelope)
# ---------------------------------------------------------------------------


def test_error_handling():
    """When the local agent raises, the API catches it and returns an error envelope.

    The remote API is designed to never leak stack traces to callers: any
    exception raised by the orchestrator or local agent is caught and
    wrapped in an ``ExecuteResponse`` with ``status='error'`` and a
    truncated error message. This test drives that path by making the
    local agent throw.
    """
    boom = _build_gateway_runner()
    boom.agent.chat = MagicMock(side_effect=RuntimeError("kaboom: orchestrator down"))
    # Orchestrator still returns None so we drop into the local-agent branch.
    boom.instance_orchestrator.execute_on_instance = AsyncMock(return_value=None)

    app = _build_app(boom)
    with TestClient(app) as tc:
        resp = tc.post(
            "/api/agent/execute",
            json=_valid_body(prompt="trigger-the-error"),
            headers=_auth_headers(),
        )

    # The endpoint catches the exception and returns a well-formed envelope.
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "error"
    assert body["output"] is None
    assert "kaboom" in body["error"]
    # Truncated to <= 500 chars per implementation.
    assert len(body["error"]) <= 500
    assert body["session_id"] == "user_abc.123"
    assert body["timestamp"].endswith("Z")
