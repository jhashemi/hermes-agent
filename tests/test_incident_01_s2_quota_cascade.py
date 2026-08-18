"""INCIDENT-01 S2 — quota-class 429/402/413 must cascade immediately to
fallback provider, not burn 5 same-model retries.

Reproduces the 2026-08-18 incident where ollama-cloud returned:

    HTTP 429: you (jhashemi) have reached your session usage limit,
    add extra usage: https://ollama.com/settings

on the FIRST model call, and the agent burned 5 same-model retries
(2.2s → 4.1s → 10.4s → 20.7s → 39.4s of backoff) before dying with
``max_retries_exhausted``. The configured fallback chain never engaged.

Root cause: ``agent/error_classifier.py`` classifies HTTP 429 uniformly
as ``FailoverReason.rate_limit`` with ``retryable=True,
should_fallback=True``. The retry loop in ``agent/conversation_loop.py``
does have an eager-fallback branch for rate_limit at lines ~4278-4322,
BUT it only fires when:

    _fallback_index < len(_fallback_chain)   AND   not pool_may_recover

If the chain is empty OR the pool has multiple entries OR the branch's
guards otherwise refuse, execution falls through to the top-of-loop
backoff-sleep-and-retry path which burns the retry budget on the SAME
exhausted-quota endpoint.

Quota-class bodies ("session usage limit", "reached your ... limit",
"quota exceeded", "insufficient_quota", plus 402 billing / 413 hard-
cap) are hard exhaustions — the primary quota will NOT recover within
a 30-second retry window. They must be classified as
``retryable=False`` so the retry loop's top-of-iteration guard bails
out to the fallback path on attempt 1 rather than backoff-looping.

Acceptance criteria:
  - "session usage limit" 429 body → retryable=False, should_fallback=True
  - "reached your ... limit" 429 body → retryable=False, should_fallback=True
  - "quota exceeded" 429 body → retryable=False, should_fallback=True
  - HTTP 402 (Payment Required) → retryable=False, should_fallback=True
  - Generic rate-limit 429 (transient, no quota body) → retryable=True
    (backoff-and-retry is still the right behavior for transient bursts)
"""

from __future__ import annotations

from types import SimpleNamespace

from agent.error_classifier import (
    FailoverReason,
    classify_api_error,
)


# ─────────────────────────────────────────────────────────────────────
# Error-shape helpers matching what OpenAI SDK / httpx raise
# ─────────────────────────────────────────────────────────────────────


class _RLError(Exception):
    """Mimics openai.RateLimitError with status_code + body dict."""

    def __init__(self, status_code: int, message: str, body: dict | None = None):
        super().__init__(f"Error code: {status_code} - {message}")
        self.status_code = status_code
        self.message = message
        self.body = body or {"error": {"message": message}}
        self.response = SimpleNamespace(headers={}, status_code=status_code)


# ─────────────────────────────────────────────────────────────────────
# S2 CORE: quota-exhaustion 429 bodies must be non-retryable
# ─────────────────────────────────────────────────────────────────────


def test_ollama_session_usage_limit_429_is_non_retryable():
    """Exact string from the incident log.

    ``ollama-cloud`` returns this when the free-tier daily quota is
    exhausted. Retrying does not help — the quota window is measured
    in hours, not seconds. Must classify as retryable=False so the
    top-of-loop guard bails on attempt 1 instead of burning 5 retries.
    """
    err = _RLError(
        status_code=429,
        message=(
            "you (jhashemi) have reached your session usage limit, "
            "add extra usage: https://ollama.com/settings "
            "(ref: c8a3126d-4db6-4c36-a8df-03c14e173636)"
        ),
    )
    classified = classify_api_error(
        err,
        provider="ollama-cloud",
        model="glm-5.2",
    )

    assert classified.status_code == 429
    assert classified.retryable is False, (
        "S2 REGRESSION: ollama 'session usage limit' 429 is currently "
        "classified as retryable=True. This burns 5 same-model retries "
        "before the fallback chain gets a chance — the exact incident "
        "on 2026-08-18 (ticket t_3e1634d9). Quota-exhaustion bodies "
        "must be retryable=False so retry_count exceeds max_retries "
        "on attempt 1 and the outer failover path activates."
    )
    assert classified.should_fallback is True, (
        "Quota exhaustion must still request fallback — that's the "
        "recovery path once retry is off the table."
    )
    # Rate-limit family: any of rate_limit, billing, or a new
    # ``quota_exhausted`` reason is acceptable, as long as the retry
    # loop's eager-fallback branch treats it the same.
    assert classified.reason in {
        FailoverReason.rate_limit,
        FailoverReason.billing,
    }, f"Unexpected reason: {classified.reason}"


def test_reached_your_limit_variants_are_non_retryable():
    """Provider-agnostic phrasing: any 429 whose body contains
    ``reached your ... limit`` or ``usage limit`` is a quota exhaustion,
    regardless of provider.
    """
    variants = [
        "you have reached your usage limit for this account",
        "you have reached your daily request limit",
        "your monthly token limit has been reached",
        "session usage limit exceeded",
    ]
    for msg in variants:
        err = _RLError(status_code=429, message=msg)
        classified = classify_api_error(err, provider="anyprov", model="anymod")
        assert classified.retryable is False, (
            f"Quota body {msg!r} must be retryable=False"
        )
        assert classified.should_fallback is True


def test_generic_quota_exceeded_429_is_non_retryable():
    """OpenAI/Anthropic-style ``quota exceeded`` / ``insufficient_quota``."""
    for msg in [
        "quota exceeded for this billing period",
        "insufficient_quota: You exceeded your current quota",
    ]:
        err = _RLError(status_code=429, message=msg)
        classified = classify_api_error(err, provider="openai", model="gpt-4")
        assert classified.retryable is False, (
            f"Quota body {msg!r} must be non-retryable"
        )
        assert classified.should_fallback is True


def test_http_402_payment_required_is_non_retryable_and_falls_back():
    """402 is definitionally non-transient (billing failure). Retrying
    without a card update will never succeed; falling back to a
    different provider might.
    """
    err = _RLError(
        status_code=402,
        message="Payment Required — account balance depleted",
    )
    classified = classify_api_error(err, provider="anyprov", model="anymod")
    assert classified.retryable is False, (
        "S2: HTTP 402 must be retryable=False (billing exhaustion)."
    )
    assert classified.should_fallback is True


# ─────────────────────────────────────────────────────────────────────
# Guard: don't over-generalize
# ─────────────────────────────────────────────────────────────────────


def test_transient_429_without_quota_body_stays_retryable():
    """Ordinary transient bursts (short-window rate limits, no quota
    body) must remain retryable=True — a 2s backoff on those really
    does help, and we don't want to over-fall-back on transient hiccups.
    """
    for msg in [
        "rate limit exceeded",
        "Too many requests, please slow down",
        "429: request rate too high",
    ]:
        err = _RLError(status_code=429, message=msg)
        classified = classify_api_error(err, provider="anyprov", model="anymod")
        # Transient bursts should remain retryable=True so the backoff
        # loop gets a chance to ride them out.
        assert classified.retryable is True, (
            f"Transient rate-limit body {msg!r} unexpectedly non-retryable — "
            "S2 fix must NOT over-generalize."
        )
        assert classified.should_fallback is True
