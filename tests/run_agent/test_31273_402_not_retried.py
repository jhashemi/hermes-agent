"""Regression guard for #31273: HTTP 402 (billing exhaustion) must abort
after credential-pool rotation and provider fallback have failed.

Before the fix, ``FailoverReason.billing`` was in the exclusion set that
prevents the loop's ``is_client_error`` branch from firing.  When a user
ran a pay-per-token provider (OpenRouter, etc.) with no credential pool
and no fallback configured, a single 402 cascaded into
``agent.api_max_retries`` paid requests against an exhausted balance.
Real-world impact: ~$40 burned in 48h on a 24/7 gateway routing Telegram
+ Discord traffic.

The fix removes ``FailoverReason.billing`` from the exclusion set.  By
the time control reaches the ``is_client_error`` check:
  * credential-pool rotation has already run (and either ``continue``d
    on rotation, or returned False because the pool is exhausted/absent).
  * the eager-fallback branch for billing has also run (and either
    ``continue``d on fallback activation, or fell through because no
    fallback is configured).
Falling through to the retry-backoff path from here just burns paid
requests with no recovery mechanism left.  Aborting mirrors how 401/403
(also ``should_fallback=True``) already behave once their recovery paths
have failed.
"""
from __future__ import annotations


class TestBillingTriggersClientErrorAbort:
    """Mirror the ``is_client_error`` predicate shape used in
    ``agent/conversation_loop.py`` and verify ``FailoverReason.billing``
    now resolves to True (i.e. aborts the loop).
    """

    def _mirror_is_client_error(
        self,
        *,
        classified_retryable: bool,
        classified_reason,
        classified_should_compress: bool = False,
        is_local_validation_error: bool = False,
        is_context_length_error: bool = False,
    ) -> bool:
        """Exact shape of conversation_loop.py's is_client_error check.

        Kept in lock-step with the source.  If you change one, change
        both — or, better, refactor the predicate into a shared helper
        and have both sites import it.

        Updated for t_8819eda2: FailoverReason.rate_limit is now only
        excluded from the abort set when classified_retryable=True
        (transient RPM/burst 429s).  Quota-exhaustion 429s have
        retryable=False and must fall through to the abort/fallback path.
        """
        from agent.error_classifier import FailoverReason

        return (
            is_local_validation_error
            or (
                not classified_retryable
                and not classified_should_compress
                and classified_reason not in {
                    FailoverReason.rate_limit if classified_retryable else None,
                    FailoverReason.overloaded,
                    FailoverReason.context_overflow,
                    FailoverReason.payload_too_large,
                    FailoverReason.long_context_tier,
                    FailoverReason.thinking_signature,
                }
            )
        ) and not is_context_length_error

    def test_billing_now_aborts_the_loop(self):
        """402 with no fallback / no pool entry → ``is_client_error`` True."""
        from agent.error_classifier import FailoverReason

        # This is what classify_api_error() returns for a plain 402:
        #   reason=billing, retryable=False, should_compress=False
        assert self._mirror_is_client_error(
            classified_retryable=False,
            classified_reason=FailoverReason.billing,
        ), (
            "FailoverReason.billing must trigger is_client_error abort after "
            "credential-pool rotation and provider fallback have failed — see #31273."
        )

    def test_quota_exhausted_429_now_aborts_the_loop(self):
        """Quota-exhaustion 429 (retryable=False) → ``is_client_error`` True.

        INCIDENT-01 / t_8819eda2 fix: rate_limit with retryable=False must
        reach the abort path rather than burning 5 retries in the backoff loop.
        """
        from agent.error_classifier import FailoverReason

        # What classify_api_error() returns for an ollama/kimi weekly-quota 429:
        #   reason=rate_limit, retryable=False, should_compress=False
        assert self._mirror_is_client_error(
            classified_retryable=False,
            classified_reason=FailoverReason.rate_limit,
        ), (
            "FailoverReason.rate_limit with retryable=False (quota exhaustion) "
            "must trigger is_client_error so the loop aborts after pool-rotation "
            "and fallback-chain have failed, not burn 5 retries — see t_8819eda2."
        )

    def test_transient_429_stays_in_backoff_loop(self):
        """Transient RPM/burst 429 (retryable=True) → NOT is_client_error.

        Transient rate limits must still go through backoff-and-retry.
        Only quota-exhaustion 429s (retryable=False) reach the abort path.
        """
        from agent.error_classifier import FailoverReason

        # Transient 429: reason=rate_limit, retryable=True
        assert not self._mirror_is_client_error(
            classified_retryable=True,
            classified_reason=FailoverReason.rate_limit,
        ), (
            "FailoverReason.rate_limit with retryable=True (transient burst 429) "
            "must NOT be classified as a client error — it should backoff-and-retry."
        )

    def test_context_overflow_still_falls_through_to_compression(self):
        """Sanity check: context-overflow must NOT be classified as
        client error — compression is the recovery path."""
        from agent.error_classifier import FailoverReason

        assert not self._mirror_is_client_error(
            classified_retryable=True,
            classified_reason=FailoverReason.context_overflow,
            classified_should_compress=True,
        )


