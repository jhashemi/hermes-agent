"""Regression tests for the fallback max_tokens clamp.

RCA (t_8ec6fb76, 2026-09-07): a primary tuned for a huge output budget
(``model.max_tokens: 131072`` on ollama-cloud glm) failed over to
``us.anthropic.claude-sonnet-4-6`` via bedrock (128,000 ceiling). The clamp
helper only consulted ``providers.<id>.max_output`` from the config dict —
but worker profiles declare the cap INLINE on the ``fallback_providers``
entry (``max_output: 64000``) with no ``providers:`` section at all. The
helper therefore returned ``None``, the clamp never fired, and every
failover call died with ``ValidationException`` until retries exhausted —
the kanban worker crash loop.

Covers the resolution order (inline entry > providers-section), the
provider gate, no-op cases, and failure containment.
"""

from types import SimpleNamespace
from unittest.mock import patch

from agent.chat_completion_helpers import _apply_fallback_max_output_clamp


def _agent(max_tokens=131072):
    return SimpleNamespace(max_tokens=max_tokens)


class TestInlineFallbackEntryCap:
    """The cap rides on the fallback_providers entry itself (profile shape)."""

    def test_inline_cap_clamps_oversized_max_tokens(self):
        agent = _agent(131072)
        fb = {"provider": "bedrock", "model": "us.anthropic.claude-sonnet-4-6",
              "max_output": 64000}
        _apply_fallback_max_output_clamp(agent, fb, "bedrock", fb["model"])
        assert agent.max_tokens == 64000

    def test_inline_cap_wins_over_providers_section(self):
        agent = _agent(131072)
        fb = {"provider": "bedrock", "model": "m", "max_output": 32000}
        with patch(
            "agent.chat_completion_helpers._get_provider_max_output",
            return_value=999999,
        ) as lookup:
            _apply_fallback_max_output_clamp(agent, fb, "bedrock", "m")
        assert agent.max_tokens == 32000
        lookup.assert_not_called()

    def test_providers_section_used_when_no_inline_cap(self):
        agent = _agent(131072)
        fb = {"provider": "bedrock", "model": "m"}
        with patch(
            "agent.chat_completion_helpers._get_provider_max_output",
            return_value=64000,
        ):
            _apply_fallback_max_output_clamp(agent, fb, "bedrock", "m")
        assert agent.max_tokens == 64000

    def test_section_lookup_skipped_when_inline_cap_invalid(self):
        agent = _agent(5000)
        fb = {"provider": "bedrock", "model": "m", "max_output": "not-a-number"}
        with patch(
            "agent.chat_completion_helpers._get_provider_max_output",
            return_value=4000,
        ):
            _apply_fallback_max_output_clamp(agent, fb, "bedrock", "m")
        assert agent.max_tokens == 4000


class TestNoopCases:
    def test_noop_when_max_tokens_under_cap(self):
        agent = _agent(32000)
        fb = {"provider": "bedrock", "model": "m", "max_output": 64000}
        _apply_fallback_max_output_clamp(agent, fb, "bedrock", "m")
        assert agent.max_tokens == 32000

    def test_noop_when_max_tokens_missing(self):
        agent = SimpleNamespace(max_tokens=None)
        fb = {"provider": "bedrock", "model": "m", "max_output": 64000}
        _apply_fallback_max_output_clamp(agent, fb, "bedrock", "m")
        assert agent.max_tokens is None

    def test_noop_when_entry_has_no_cap_anywhere(self):
        agent = _agent(131072)
        with patch(
            "agent.chat_completion_helpers._get_provider_max_output",
            return_value=None,
        ):
            _apply_fallback_max_output_clamp(
                agent, {"provider": "bedrock", "model": "m"}, "bedrock", "m"
            )
        assert agent.max_tokens == 131072


class TestProviderGate:
    def test_non_hard_ceiling_provider_is_untouched(self):
        # The clamp stays gated to providers with hard provider-side output
        # ceilings (bedrock, anthropic) — ollama/zai/kimi budgets are
        # advisory and must not be re-capped at failover time.
        agent = _agent(131072)
        fb = {"provider": "ollama-cloud", "model": "glm-5.3-flash",
              "max_output": 64000}
        _apply_fallback_max_output_clamp(agent, fb, "ollama-cloud", fb["model"])
        assert agent.max_tokens == 131072

    def test_native_anthropic_provider_is_clamped(self):
        agent = _agent(131072)
        fb = {"provider": "anthropic", "model": "claude-sonnet-4-6",
              "max_output": 64000}
        _apply_fallback_max_output_clamp(agent, fb, "anthropic", fb["model"])
        assert agent.max_tokens == 64000


class TestFailureContainment:
    def test_lookup_exception_is_contained(self):
        agent = _agent(131072)
        fb = {"provider": "bedrock", "model": "m"}
        with patch(
            "agent.chat_completion_helpers._get_provider_max_output",
            side_effect=RuntimeError("config exploded"),
        ):
            # Must not raise — clamp is an optimization, failover must
            # proceed even when the cap lookup explodes.
            _apply_fallback_max_output_clamp(agent, fb, "bedrock", "m")
        assert agent.max_tokens == 131072