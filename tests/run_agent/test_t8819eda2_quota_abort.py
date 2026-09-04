"""Regression tests for t_8819eda2 (ERR-DRIVE-01 follow-up):
Retry ladder must abort immediately on period-based quota exhaustion
(weekly/monthly usage limit) instead of burning 5 backoff retries.

FAILURE CHAIN that produced the bug (2026-09-04 03:18 window):
  zai/glm-5.3-flash transient RPM 429
  → fallback to ollama-cloud/glm-5.3-flash (weekly quota dead)
  → ollama 429: "you (jhashemi) have reached your weekly usage limit"
  → classifier: rate_limit, retryable=False — CORRECT
  → pool rotation: "no available entries (all exhausted)" — exhausted
  → fallback chain: also exhausted (already on fallback provider)
  → is_client_error gate: excluded rate_limit unconditionally → FALSE
  → backoff loop burns all 5 retries (2s + 5s + 8s + 23s + ... = ~60s)
  → hard drop: "API call failed after 5 retries"

Same chain for kimi/kimi-k3 weekly-quota 403:
  403 body: "You've reached your weekly (7-day) usage limit."
  → old classifier: auth (403 status path no quota check) → auth refresh
  → after refresh fails: still burns retries

Fix (t_8819eda2):
  1. conversation_loop.py: is_client_error now only excludes rate_limit
     from the abort set when classified.retryable=True (transient 429s).
     Quota-exhaustion 429s (retryable=False) fall through to abort/fallback.
  2. error_classifier.py: 403 handler checks _QUOTA_EXHAUSTED_PATTERNS
     so kimi/moonshot weekly-quota 403 is classified as billing (not auth).
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from agent.error_classifier import (
    FailoverReason,
    classify_api_error,
)


# ─────────────────────────────────────────────────────────────────────
# Error-shape helpers
# ─────────────────────────────────────────────────────────────────────


class _RLError(Exception):
    """Mimics openai.RateLimitError with status_code + body dict."""

    def __init__(self, status_code: int, message: str, body: dict | None = None):
        super().__init__(f"Error code: {status_code} - {message}")
        self.status_code = status_code
        self.body = body or {"error": {"message": message}}
        self.response = SimpleNamespace(headers={}, status_code=status_code)


class _PermissionError(Exception):
    """Mimics a 403 PermissionDeniedError (kimi-coding style)."""

    def __init__(self, message: str, body: dict | None = None):
        super().__init__(f"Error code: 403 - {message}")
        self.status_code = 403
        self.body = body or {"error": {"message": message}}
        self.response = SimpleNamespace(headers={}, status_code=403)


# ─────────────────────────────────────────────────────────────────────
# is_client_error predicate mirror (keep in lock-step with
# agent/conversation_loop.py — see test_31273_402_not_retried.py)
# ─────────────────────────────────────────────────────────────────────


def _is_client_error(classified, *, is_local_validation_error=False, is_context_length_error=False):
    """Mirror of conversation_loop.py's is_client_error gate (t_8819eda2 shape)."""
    return (
        is_local_validation_error
        or (
            not classified.retryable
            and not classified.should_compress
            and classified.reason not in {
                FailoverReason.rate_limit if classified.retryable else None,
                FailoverReason.overloaded,
                FailoverReason.context_overflow,
                FailoverReason.payload_too_large,
                FailoverReason.long_context_tier,
                FailoverReason.thinking_signature,
            }
        )
    ) and not is_context_length_error


# ─────────────────────────────────────────────────────────────────────
# S1: classifier output for the real error bodies from the incident
# ─────────────────────────────────────────────────────────────────────


class TestQuotaExhaustionClassifier:
    """Classifier must produce retryable=False for all quota-wall bodies."""

    def test_ollama_weekly_limit_429_is_non_retryable(self):
        """Exact ollama-cloud body from 2026-09-04 03:18 window."""
        err = _RLError(
            429,
            "you (jhashemi) have reached your weekly usage limit, "
            "add extra usage: https://ollama.com/settings (ref: abc123)",
        )
        c = classify_api_error(err, provider="ollama-cloud", model="glm-5.3-flash")
        assert c.reason == FailoverReason.rate_limit
        assert not c.retryable, "Weekly-quota 429 must not be retryable"
        assert c.should_fallback

    def test_kimi_weekly_limit_403_is_billing(self):
        """Kimi/Moonshot weekly-quota 403 (exact body from agent.log)."""
        err = _PermissionError(
            "You've reached your weekly (7-day) usage limit. "
            "Your quota will reset when the current 7-day window ends. "
            "To continue now, purchase extra usage or upgrade your plan: "
            "https://www.kimi.com/membership/subscription?tab=quota",
            body={
                "error": {
                    "type": "permission_error",
                    "message": "You've reached your weekly (7-day) usage limit. "
                    "Your quota will reset when the current 7-day window ends. "
                    "To continue now, purchase extra usage or upgrade your plan: "
                    "https://www.kimi.com/membership/subscription?tab=quota",
                },
                "type": "error",
            },
        )
        c = classify_api_error(err, provider="kimi-coding", model="kimi-k3")
        assert c.reason == FailoverReason.billing, (
            f"Kimi weekly-quota 403 must classify as billing, got {c.reason}"
        )
        assert not c.retryable
        assert c.should_fallback

    def test_reached_your_weekly_429_variants(self):
        """Other providers using 'reached your weekly' phrasing."""
        for msg in [
            "you have reached your weekly usage limit",
            "reached your weekly quota",
        ]:
            err = _RLError(429, msg)
            c = classify_api_error(err, provider="custom", model="some-model")
            assert not c.retryable, f"'{msg}' should be non-retryable"

    def test_transient_429_stays_retryable(self):
        """Transient RPM/burst 429 must stay retryable (backoff path)."""
        err = _RLError(429, "rate limit exceeded, please slow down")
        c = classify_api_error(err, provider="openrouter", model="some-model")
        assert c.reason == FailoverReason.rate_limit
        assert c.retryable, "Transient 429 must remain retryable"

    def test_generic_auth_403_stays_auth(self):
        """A plain invalid-API-key 403 must NOT be reclassified as billing."""
        err = _PermissionError("Invalid API key")
        c = classify_api_error(err, provider="openrouter", model="some-model")
        assert c.reason == FailoverReason.auth
        assert not c.retryable


# ─────────────────────────────────────────────────────────────────────
# S2: is_client_error gate with the new predicate shape
# ─────────────────────────────────────────────────────────────────────


class TestIsClientErrorGate:
    """The is_client_error predicate must abort on quota-exhaustion 429s."""

    def test_quota_429_retryable_false_triggers_abort(self):
        """rate_limit + retryable=False → is_client_error True (abort loop)."""
        err = _RLError(
            429,
            "you (jhashemi) have reached your weekly usage limit",
        )
        c = classify_api_error(err, provider="ollama-cloud", model="glm-5.3-flash")
        assert c.reason == FailoverReason.rate_limit
        assert not c.retryable
        assert _is_client_error(c), (
            "Quota-exhaustion 429 (retryable=False) must set is_client_error=True "
            "so the loop aborts instead of burning 5 backoff retries — t_8819eda2"
        )

    def test_transient_429_retryable_true_does_not_abort(self):
        """rate_limit + retryable=True → is_client_error False (backoff path)."""
        err = _RLError(429, "rate limit exceeded, please slow down")
        c = classify_api_error(err, provider="openrouter", model="some-model")
        assert c.retryable
        assert not _is_client_error(c), (
            "Transient 429 (retryable=True) must NOT abort — backoff-and-retry is correct"
        )

    def test_kimi_403_billing_triggers_abort(self):
        """billing + retryable=False → is_client_error True."""
        err = _PermissionError(
            "You've reached your weekly (7-day) usage limit.",
            body={
                "error": {
                    "type": "permission_error",
                    "message": "You've reached your weekly (7-day) usage limit. "
                    "Your quota will reset when the current 7-day window ends.",
                },
                "type": "error",
            },
        )
        c = classify_api_error(err, provider="kimi-coding", model="kimi-k3")
        assert c.reason == FailoverReason.billing
        assert _is_client_error(c), "Kimi billing 403 must trigger is_client_error"

    def test_overloaded_stays_excluded(self):
        """overloaded must never trigger is_client_error (backoff/retry path)."""
        err = _RLError(
            429,
            "service is temporarily overloaded, please retry later",
            body={"error": {"message": "service is temporarily overloaded"}},
        )
        c = classify_api_error(err, provider="zai", model="glm-5.3-flash")
        assert c.reason == FailoverReason.overloaded
        assert not _is_client_error(c)
