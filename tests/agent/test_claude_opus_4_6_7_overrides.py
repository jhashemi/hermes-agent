"""Tests for Claude Opus 4.6 / 4.7 per-model compression-threshold override.

Opus 4.6/4.7 is extremely expensive ($15/$75 per MTok in/out) and prompt-caching
is most effective when context grows incrementally on cache hits. Compress
earlier (40% of the 1M window ≈ 400K tokens) to preserve cache prefixes longer
and avoid full-context replays.

The detector must match the model across every route Hermes uses: native
Anthropic (``claude-opus-4-7``), OpenRouter (``anthropic/claude-opus-4.7``),
Bedrock direct (``anthropic.claude-opus-4-6-20250514-v1:0``), Bedrock cross-
region (``us.anthropic.claude-opus-4-7-20251001-v1:0``), and the Bedrock
"global" inference profile (``global.anthropic.claude-opus-4-7``).

It must NOT hit sibling Claude models — Sonnet, Haiku, Opus 3, Opus 4.0, or
Opus 4.5 — which keep the user's configured global threshold.
"""

from __future__ import annotations

import pytest

from agent.auxiliary_client import (
    _compression_threshold_for_model,
    _is_claude_opus_4_6_or_7,
)


@pytest.mark.parametrize(
    "model",
    [
        # Native Anthropic
        "claude-opus-4-7",
        "claude-opus-4-6",
        # OpenRouter (dot-versioned)
        "anthropic/claude-opus-4.7",
        "anthropic/claude-opus-4.6",
        # Bedrock direct + cross-region
        "anthropic.claude-opus-4-7-20251001-v1:0",
        "anthropic.claude-opus-4-6-20250514-v1:0",
        "us.anthropic.claude-opus-4-7-20251001-v1:0",
        "us.anthropic.claude-opus-4-6-20250514-v1:0",
        # Bedrock global inference profile
        "global.anthropic.claude-opus-4-7",
        # Case / whitespace tolerance
        "  Anthropic/Claude-Opus-4.7  ",
    ],
)
def test_is_claude_opus_4_6_or_7_matches(model: str) -> None:
    assert _is_claude_opus_4_6_or_7(model) is True


@pytest.mark.parametrize(
    "model",
    [
        None,
        "",
        # Sibling Claude families — must NOT trigger the override.
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "claude-sonnet-4-7",
        "anthropic/claude-sonnet-4.6",
        "us.anthropic.claude-haiku-4-5-20251001-v1:0",
        # Older Opus generations — no 1M context, no aggressive override.
        "claude-opus-4",
        "claude-opus-4-5",
        "claude-3-opus",
        "anthropic.claude-3-opus-20240229-v1:0",
        # Other vendors
        "gpt-5",
        "kimi-k2",
        "trinity-large-thinking",
    ],
)
def test_is_claude_opus_4_6_or_7_rejects_non_matches(model) -> None:
    assert _is_claude_opus_4_6_or_7(model) is False


@pytest.mark.parametrize(
    "model",
    [
        "claude-opus-4-7",
        "claude-opus-4-6",
        "anthropic/claude-opus-4.7",
        "us.anthropic.claude-opus-4-7-20251001-v1:0",
        "anthropic.claude-opus-4-6-20250514-v1:0",
        "global.anthropic.claude-opus-4-7",
    ],
)
def test_compression_threshold_opus_4_6_4_7_returns_0_40(model: str) -> None:
    """Opus 4.6/4.7 must compress at 40% — protects cache prefixes."""
    assert _compression_threshold_for_model(model) == 0.40


@pytest.mark.parametrize(
    "model",
    [
        "claude-haiku-4-5",
        "claude-sonnet-4-6",
        "anthropic/claude-sonnet-4.6",
        "claude-opus-4-5",
        "claude-3-opus",
        "gpt-5",
    ],
)
def test_compression_threshold_other_claude_models_unchanged(model: str) -> None:
    """Sibling Claude / other-vendor models keep the user's global threshold."""
    assert _compression_threshold_for_model(model) is None
