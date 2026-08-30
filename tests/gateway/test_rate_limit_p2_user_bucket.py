"""
DoD tests for task t_b292b559 — Phase-2 rate limiting.

Requirements verified here:
1. Rate limiter enforces 10 requests/minute per user (11th request throttled).
2. Bucket key is derived from the X-Hermes-User header (users are independent).
3. Rate-limited responses return HTTP 429 with a Retry-After header.

These tests intentionally reset the global rate-limiter singleton around each
scenario so they don't spill counter state between cases or clash with tests
that pre-warm the limiter with a different config.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from gateway import remote_agent_api as rm
from gateway.remote_agent_api import (
    RateLimiter,
    _rate_limit_key,
    get_rate_limiter,
)


# ---------------------------------------------------------------------------
# Fixture: reset the singleton so each test starts with a clean bucket state
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Ensure each test starts with a fresh 10-per-minute singleton."""
    old = rm._rate_limiter
    if old is not None:
        try:
            old.shutdown()
        except Exception:
            pass
    rm._rate_limiter = None
    yield
    current = rm._rate_limiter
    if current is not None:
        try:
            current.shutdown()
        except Exception:
            pass
    rm._rate_limiter = None


# ---------------------------------------------------------------------------
# get_rate_limiter() defaults — the singleton is 10/min per DoD
# ---------------------------------------------------------------------------
class TestSingletonPolicy:
    def test_singleton_defaults_are_10_per_minute(self):
        limiter = get_rate_limiter()
        assert limiter.max_requests == 10
        assert limiter.window_seconds == 60


# ---------------------------------------------------------------------------
# _rate_limit_key() — X-Hermes-User is the primary bucket key
# ---------------------------------------------------------------------------
class TestRateLimitKey:
    def test_user_header_takes_precedence(self):
        key = _rate_limit_key(x_hermes_user="alice", x_hermes_key="k-abc")
        assert key == "user:alice"

    def test_user_header_whitespace_stripped(self):
        assert _rate_limit_key("  bob  ", "k-abc") == "user:bob"

    def test_falls_back_to_api_key_when_user_missing(self):
        assert _rate_limit_key(None, "k-xyz") == "key:k-xyz"

    def test_falls_back_to_api_key_when_user_blank(self):
        assert _rate_limit_key("   ", "k-xyz") == "key:k-xyz"

    def test_anonymous_bucket_when_both_missing(self):
        assert _rate_limit_key(None, None) == "anonymous:"

    def test_user_and_key_buckets_are_disjoint(self):
        """A user literally named "k-secret" must not share the same bucket
        as the API key "k-secret"."""
        assert _rate_limit_key("k-secret", None) != _rate_limit_key(None, "k-secret")


# ---------------------------------------------------------------------------
# 10 requests per minute per user is enforced (DoD: "11th request is throttled")
# ---------------------------------------------------------------------------
class TestPerUserThrottling:
    def test_eleventh_request_in_one_minute_is_throttled(self):
        limiter = get_rate_limiter()
        assert limiter.max_requests == 10
        # First 10 must be allowed.
        for i in range(10):
            allowed, retry_after = limiter.is_allowed("user:alice")
            assert allowed is True, f"request {i + 1}/10 unexpectedly rate-limited"
            assert retry_after is None
        # 11th must be rate-limited with a non-null Retry-After hint.
        allowed, retry_after = limiter.is_allowed("user:alice")
        assert allowed is False
        assert isinstance(retry_after, int)
        assert 1 <= retry_after <= 60

    def test_different_users_have_independent_buckets(self):
        limiter = get_rate_limiter()
        # Exhaust Alice.
        for _ in range(10):
            allowed, _ = limiter.is_allowed("user:alice")
            assert allowed is True
        allowed, _ = limiter.is_allowed("user:alice")
        assert allowed is False, "Alice should be throttled"
        # Bob is untouched.
        allowed, retry_after = limiter.is_allowed("user:bob")
        assert allowed is True
        assert retry_after is None


# ---------------------------------------------------------------------------
# HTTP surface: 429 + Retry-After header (via FastAPI TestClient)
# ---------------------------------------------------------------------------
class TestHttpSurface:
    """Wire up the FastAPI blueprint and drive it with TestClient."""

    def _build_app(self, monkeypatch):
        fastapi = pytest.importorskip("fastapi")
        pytest.importorskip("httpx")
        from fastapi import FastAPI

        # A valid API key must be present, otherwise verify_api_key() returns
        # False and every request 401s before hitting the rate limiter.
        monkeypatch.setenv("HERMES_REMOTE_API_KEY", "test-key-t_b292b559")
        # get_expected_key is @lru_cache'd — clear it so the new env is picked up.
        rm.get_expected_key.cache_clear()

        # Stub orchestrator so happy-path requests short-circuit before touching
        # real agent code. We only need to reach the rate-limit gate.
        class _StubOrchestrator:
            async def execute_on_instance(self, **kwargs):
                return {
                    "status": "success",
                    "output": "stub",
                    "session_id": kwargs.get("session_id"),
                    "timestamp": "2026-05-22T00:00:00Z",
                }

        class _StubRunner:
            instance_orchestrator = _StubOrchestrator()

        app = FastAPI()
        asyncio.get_event_loop().run_until_complete(
            rm.create_remote_api_blueprint(app, _StubRunner())
        )
        return app

    def test_11th_request_returns_429_with_retry_after(self, monkeypatch):
        from fastapi.testclient import TestClient

        app = self._build_app(monkeypatch)
        client = TestClient(app)

        headers = {
            "X-Hermes-Key": "test-key-t_b292b559",
            "X-Hermes-User": "alice",
        }
        payload = {
            "agent_id": "default",
            "prompt": "hi",
            "session_id": "s-alice-1",
        }

        # 10 requests must all be allowed (they may 200 or 500 depending on
        # orchestrator wiring, but MUST NOT be 429).
        for i in range(10):
            resp = client.post("/api/agent/execute", json=payload, headers=headers)
            assert resp.status_code != 429, (
                f"request {i + 1}/10 was throttled prematurely "
                f"(status={resp.status_code}, body={resp.text[:200]})"
            )

        # 11th must be 429 with Retry-After.
        resp = client.post("/api/agent/execute", json=payload, headers=headers)
        assert resp.status_code == 429, (
            f"11th request should be 429, got {resp.status_code}: {resp.text[:200]}"
        )
        assert "retry-after" in {k.lower() for k in resp.headers}, (
            f"Retry-After header missing from response headers: {dict(resp.headers)}"
        )
        retry_after = resp.headers.get("Retry-After") or resp.headers.get("retry-after")
        assert retry_after is not None
        assert int(retry_after) >= 1
        body = resp.json()
        assert body.get("status") == "error"
        assert "Rate limit exceeded" in body.get("error", "")

    def test_bucket_is_keyed_by_x_hermes_user_not_api_key(self, monkeypatch):
        """Alice and Bob share the same API key but must not share a bucket."""
        from fastapi.testclient import TestClient

        app = self._build_app(monkeypatch)
        client = TestClient(app)
        payload = {"agent_id": "default", "prompt": "hi", "session_id": "s"}

        # Exhaust Alice.
        alice = {
            "X-Hermes-Key": "test-key-t_b292b559",
            "X-Hermes-User": "alice",
        }
        for _ in range(10):
            r = client.post("/api/agent/execute", json=payload, headers=alice)
            assert r.status_code != 429
        r = client.post("/api/agent/execute", json=payload, headers=alice)
        assert r.status_code == 429, "Alice should be throttled after 10 requests"

        # Bob (same API key, different user) must still be allowed.
        bob = {
            "X-Hermes-Key": "test-key-t_b292b559",
            "X-Hermes-User": "bob",
        }
        r = client.post("/api/agent/execute", json=payload, headers=bob)
        assert r.status_code != 429, (
            f"Bob should not be throttled — separate user bucket "
            f"(status={r.status_code}, body={r.text[:200]})"
        )
