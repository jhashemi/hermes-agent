"""Tests for GLM-5.x model-specific compression threshold overrides.

Regression guard — prevents drift in the _is_glm_5 detector and the
_compression_threshold_for_model override for GLM-5.x models.
"""

import pytest

from agent.auxiliary_client import _compression_threshold_for_model, _is_glm_5


class TestIsGlm5:
    """Positive and negative matches for _is_glm_5()."""

    @pytest.mark.parametrize(
        "model",
        [
            # Z.AI / Nvidia route
            "z-ai/glm-5.1",
            "z-ai/glm-5.0",
            # Bare names
            "glm-5.1",
            "glm-5-1",
            "glm-5.0",
            # Case insensitive
            "GLM-5.1",
            "GLM-5.0",
            # With provider prefix
            "nvidia/glm-5.1",
        ],
    )
    def test_positive_match(self, model: str) -> None:
        assert _is_glm_5(model) is True

    @pytest.mark.parametrize(
        "model",
        [
            # Older GLM versions
            "glm-4.9",
            "glm-4",
            "glm-3",
            # Unrelated models
            "gpt-4",
            "claude-sonnet-4-6",
            "deepseek-v3",
            # Empty / None
            "",
            None,
            # Substring trap — "glm-5" should NOT appear in these
            "chatglm-5b",
        ],
    )
    def test_negative_match(self, model: str) -> None:
        assert _is_glm_5(model) is False


class TestGlm5CompressionThreshold:
    """Verify the 0.70 compression threshold for GLM-5.x."""

    @pytest.mark.parametrize(
        "model",
        [
            "z-ai/glm-5.1",
            "glm-5.1",
            "GLM-5.0",
        ],
    )
    def test_glm5_threshold(self, model: str) -> None:
        assert _compression_threshold_for_model(model) == 0.70

    def test_opus_unchanged(self) -> None:
        """Opus 4.6/4.7 still gets 0.40 — not affected by GLM addition."""
        assert _compression_threshold_for_model("us.anthropic.claude-opus-4-7-20251001-v1:0") == 0.40
        assert _compression_threshold_for_model("anthropic/claude-opus-4.6") == 0.40

    def test_other_models_unchanged(self) -> None:
        """Non-GLM, non-Opus models get None (use global config)."""
        assert _compression_threshold_for_model("claude-sonnet-4-6") is None
        assert _compression_threshold_for_model("gpt-4") is None