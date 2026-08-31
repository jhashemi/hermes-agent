"""Regression tests for t_014409c4 — kimi fallback hop must use Anthropic wire.

The primary path in ``hermes_cli/runtime_provider._detect_api_mode_for_url``
already maps ``api.kimi.com/coding`` → ``anthropic_messages`` (the endpoint
only serves Claude Code's native Messages wire; the OpenAI ``/chat/completions``
shim isn't there and returns a deterministic HTTP 404).

The fallback path in ``agent.chat_completion_helpers.try_activate_fallback``
re-implements api-mode detection for the fallback target with a separate
elif chain, and it originally omitted the kimi rule. When
``fallback_providers: [..., {provider: kimi, model: k3}, ...]`` activated,
Hermes POST'd the OpenAI wire to ``api.kimi.com/coding/chat/completions``
and burned the full 8-attempt retry budget on deterministic 404s before
advancing to the next hop (~4.4 min of wasted latency).

Same bug class as #32243 / #49247 — the fallback's elif chain was missing
a host rule that the primary path enforced via ``_detect_api_mode_for_url``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from run_agent import AIAgent


# ---------------------------------------------------------------------------
# Sanity: the primary-path detector, referenced from the fix comment, still
# maps the kimi coding endpoint to anthropic_messages. This is the invariant
# the fallback branch now mirrors.
# ---------------------------------------------------------------------------


class TestDetectApiModeForKimiCoding:
    """Primary path — the single source of truth for host→wire mapping."""

    def test_kimi_coding_host_returns_anthropic_messages(self):
        from hermes_cli.runtime_provider import _detect_api_mode_for_url

        assert (
            _detect_api_mode_for_url("https://api.kimi.com/coding")
            == "anthropic_messages"
        )
        assert (
            _detect_api_mode_for_url("https://api.kimi.com/coding/v1/messages")
            == "anthropic_messages"
        )

    def test_kimi_root_without_coding_is_not_anthropic(self):
        # Only the /coding route speaks Anthropic. Bare api.kimi.com is not
        # covered by this rule.
        from hermes_cli.runtime_provider import _detect_api_mode_for_url

        assert _detect_api_mode_for_url("https://api.kimi.com/v1") is None


# ---------------------------------------------------------------------------
# Regression: fallback activation onto a kimi/k3 entry must resolve
# fb_api_mode = anthropic_messages so the native Anthropic client is built
# (instead of leaving the OpenAI client attached, which routes to
# /chat/completions → 404).
# ---------------------------------------------------------------------------


def _make_bare_agent() -> AIAgent:
    """Minimal AIAgent stub, __init__ skipped — same recipe as
    tests/run_agent/test_compressor_fallback_update.py.
    """
    agent = AIAgent.__new__(AIAgent)

    # Primary model settings (something that isn't kimi — the primary was
    # zai/glm during the recorded incident).
    agent.model = "glm-5.3"
    agent.provider = "zai"
    agent.base_url = "https://api.z.ai/v1"
    agent.api_key = "sk-primary"
    agent.api_mode = "chat_completions"
    agent.client = MagicMock()
    agent.quiet_mode = True
    agent.requested_provider = "zai"

    # Fallback chain: single kimi/k3 entry, matching the configured
    # ``fallback_providers`` shape.
    agent._fallback_chain = [{"provider": "kimi", "model": "k3"}]
    agent._fallback_index = 0
    agent._fallback_activated = False
    agent._unavailable_fallback_keys = set()
    agent._primary_runtime = {"provider": "zai", "model": "glm-5.3"}

    # State the fallback path touches on the anthropic_messages branch.
    agent._config_context_length = None
    agent._transport_cache = MagicMock()
    agent._credential_pool = None
    agent._credential_pool_entry_id = None
    agent._is_anthropic_oauth = False
    agent._use_prompt_caching = False
    agent._use_native_cache_layout = False
    agent.reasoning_config = None
    agent.context_compressor = None  # skips the compressor-update block

    # Callables the branch invokes.
    agent._is_azure_openai_url = lambda url: False
    agent._is_direct_openai_url = lambda url: False
    agent._provider_model_requires_responses_api = lambda model, provider=None: False
    agent._anthropic_prompt_cache_policy = lambda **_kwargs: (False, False)
    agent._ensure_lmstudio_runtime_loaded = lambda: None
    agent._buffer_status = lambda msg: None
    agent._emit_status = lambda msg: None
    agent._replace_primary_openai_client = lambda reason=None: None

    return agent


@patch("agent.anthropic_adapter.build_anthropic_client")
@patch("agent.auxiliary_client.resolve_provider_client")
def test_fallback_to_kimi_k3_uses_anthropic_messages_wire(
    mock_resolve, mock_build_anthropic,
):
    """Fallback activation for provider=kimi/model=k3 → fb_api_mode=anthropic_messages.

    Reproduces the 2026-08-31 01:00-01:05 UTC incident: without the fix,
    fb_api_mode stayed at ``chat_completions`` and the code took the
    else-branch that reuses the OpenAI client, so subsequent requests
    POST'd ``api.kimi.com/coding/chat/completions`` → 404 x 8.

    After the fix, the elif chain sets ``anthropic_messages`` when the
    fallback client's base_url is ``api.kimi.com/coding``, and the
    anthropic_messages branch builds the native Anthropic client
    (``agent.client`` is cleared to signal "don't route through the
    OpenAI SDK").
    """
    agent = _make_bare_agent()

    fb_client = MagicMock()
    fb_client.base_url = "https://api.kimi.com/coding/"
    fb_client.api_key = "sk-kimi-fallback"
    mock_resolve.return_value = (fb_client, "k3")

    anthropic_client = MagicMock()
    mock_build_anthropic.return_value = anthropic_client

    result = agent._try_activate_fallback()

    # Fallback activation succeeded.
    assert result is True
    assert agent._fallback_activated is True

    # The critical invariant: api_mode is anthropic_messages, not chat_completions.
    assert agent.api_mode == "anthropic_messages", (
        f"kimi fallback resolved to api_mode={agent.api_mode!r}; expected "
        "'anthropic_messages'. Without this, Hermes POSTs OpenAI wire to "
        "api.kimi.com/coding/chat/completions → deterministic 404."
    )

    # The anthropic_messages branch was actually taken: native Anthropic
    # client was built and the OpenAI client was cleared.
    assert agent.client is None
    assert agent._anthropic_client is anthropic_client
    assert agent._anthropic_base_url == "https://api.kimi.com/coding/"
    assert agent._anthropic_api_key == "sk-kimi-fallback"

    # Provider identity swapped as expected.
    assert agent.provider == "kimi"
    assert agent.model == "k3"
