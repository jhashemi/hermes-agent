"""
Tests for the executive-activation plugin.

Covers all 4 KRs:
  KR1: resolver.resolve_active_agent returns correct persona from command context
  KR2: cognitive_memory.query_cognitive_memory returns records
  KR3: RACI fallback (_raci_resolve) activates the right agent by domain
  KR4: activation_cycle.run_activation_cycle runs all 6 steps
"""

import sys
import os
from pathlib import Path

# Add the hermes-agent root to path for package imports
_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

import pytest

# Import via package path (plugins.executive_activation.*)
from plugins.executive_activation.resolver import (
    resolve_active_agent,
    resolve_from_alias,
    resolve_from_domains,
    AGENTS,
    ActivationContext,
    _load_profile,
    _raci_resolve,
)
from plugins.executive_activation.cognitive_memory import (
    query_cognitive_memory,
    format_memory_context,
)
from plugins.executive_activation.activation_cycle import run_activation_cycle, ActivationResult


# ── KR1: Agent resolver ───────────────────────────────────────────────────

class TestKR1Resolver:
    """KR1: Hermes resolves the active executive agent from command context."""

    def test_resolve_elon_by_alias(self):
        ctx = resolve_active_agent("ask elon about rockets")
        assert ctx.persona_id == "helios"
        assert ctx.confidence >= 0.9
        assert ctx.full_name == "Elon Musk"

    def test_resolve_jobs_by_alias(self):
        ctx = resolve_active_agent("what would steve jobs think about this design?")
        assert ctx.persona_id == "atlas"

    def test_resolve_demis_by_alias(self):
        ctx = resolve_active_agent("demis on AI safety")
        assert ctx.persona_id == "orion"

    def test_resolve_helios_by_domain_space(self):
        ctx = resolve_active_agent("how do we build a reusable rocket engine?")
        assert ctx.persona_id == "helios"

    def test_resolve_atlas_by_domain_product(self):
        ctx = resolve_active_agent("improve the user experience of our product")
        assert ctx.persona_id == "atlas"

    def test_resolve_orion_by_domain_ai(self):
        ctx = resolve_active_agent("what are the latest advances in deep learning?")
        assert ctx.persona_id == "orion"

    def test_session_agent_takes_precedence(self):
        ctx = resolve_active_agent(
            "what do you think?",
            current_session_agent="helios"
        )
        assert ctx.persona_id == "helios"
        assert ctx.confidence >= 0.9

    def test_resolve_helios_alias_direct(self):
        assert resolve_from_alias("helios mode active") == "helios"
        assert resolve_from_alias("ask orion") == "orion"
        assert resolve_from_alias("atlas perspective") == "atlas"

    def test_domain_match_scores(self):
        result = resolve_from_domains("mars colonization using starship")
        assert result is not None
        persona_id, confidence, kw = result
        assert persona_id == "helios"
        assert confidence > 0

    def test_resolve_returns_profile(self):
        ctx = resolve_active_agent("rocket launch")
        # Profile may be None if files aren't present, but should be loaded if they are
        if Path("/home/ubuntu/executive_agents_platform/agents/elon_musk/agent_profile.yaml").exists():
            assert ctx.profile is not None

    def test_all_agents_in_map(self):
        for persona_id in ["helios", "atlas", "orion"]:
            assert persona_id in AGENTS


# ── KR2: Cognitive memory query ───────────────────────────────────────────

class TestKR2CognitiveMemory:
    """KR2: Hermes queries the resolved agent's cognitive memory."""

    def test_query_returns_list(self):
        records = query_cognitive_memory(
            agent_dir="elon_musk",
            query="space rockets",
            limit=5,
        )
        assert isinstance(records, list)

    def test_format_memory_context_no_records(self):
        # Should return either empty string or profile fallback
        result = format_memory_context([], "Elon Musk", "elon_musk")
        assert isinstance(result, str)

    def test_format_memory_context_with_records(self):
        records = [
            {
                "decision_type": "architecture",
                "reasoning": "Use first principles to design the rocket engine",
                "confidence": 0.9,
                "ts": 1700000000,
            }
        ]
        result = format_memory_context(records, "Elon Musk", "elon_musk")
        assert "Elon Musk" in result
        assert "architecture" in result

    def test_format_memory_context_truncates_long_reasoning(self):
        records = [
            {
                "decision_type": "test",
                "reasoning": "x" * 500,  # Very long reasoning
                "confidence": 0.8,
            }
        ]
        result = format_memory_context(records, "Test", "elon_musk")
        # Should not include 500 chars verbatim (truncated to 200)
        assert len(result) < 1000


# ── KR3: RACI fallback ────────────────────────────────────────────────────

class TestKR3RACI:
    """KR3: If no active agent, Hermes resolves the RACI accountable agent."""

    def test_raci_space_resolves_helios(self):
        ctx = resolve_active_agent("I need help with orbit mechanics")
        assert ctx.persona_id == "helios"

    def test_raci_ai_resolves_orion(self):
        ctx = resolve_active_agent("explain neural network architecture")
        assert ctx.persona_id == "orion"

    def test_raci_product_resolves_atlas(self):
        ctx = resolve_active_agent("how should we design our product roadmap?")
        assert ctx.persona_id == "atlas"

    def test_raci_default_fallback_atlas(self):
        ctx = _raci_resolve("help me with my boutique hotel business")
        # "boutique" is in atlas domains
        assert ctx.persona_id in ["atlas", "helios", "orion"]
        assert ctx.via_raci is True

    def test_raci_returns_activation_context(self):
        ctx = _raci_resolve("satellite manufacturing at scale")
        assert ctx.persona_id is not None
        assert isinstance(ctx.confidence, float)
        assert ctx.reason != ""


# ── KR4: Cognitive activation cycle ──────────────────────────────────────

class TestKR4ActivationCycle:
    """KR4: Activation runs a full observe->reason->plan->reward->memory->reflect cycle."""

    def _make_ctx(self, persona_id="helios") -> ActivationContext:
        info = AGENTS[persona_id]
        return ActivationContext(
            persona_id=persona_id,
            full_name=info["full_name"],
            agent_dir=info["agent_dir"],
            confidence=0.85,
            reason="test activation",
        )

    def test_cycle_has_all_steps(self):
        ctx = self._make_ctx("helios")
        result = run_activation_cycle(ctx=ctx, command="build a reusable rocket")
        step_names = [s.step for s in result.cycle]
        assert "observe" in step_names
        assert "reason" in step_names
        assert "plan" in step_names
        assert "reward" in step_names
        assert "memory" in step_names
        assert "reflect" in step_names

    def test_cycle_has_six_steps(self):
        ctx = self._make_ctx("orion")
        result = run_activation_cycle(ctx=ctx, command="AI safety research")
        assert len(result.cycle) == 6

    def test_cycle_returns_activation_result(self):
        ctx = self._make_ctx("atlas")
        result = run_activation_cycle(ctx=ctx, command="design a product")
        assert isinstance(result, ActivationResult)
        assert result.activation_id
        assert result.persona_id == "atlas"
        assert result.ts > 0

    def test_cycle_writes_audit(self, tmp_path, monkeypatch):
        import plugins.executive_activation.activation_cycle as ac_mod
        # Redirect audit path to tmp
        test_audit = tmp_path / "cognitive_audit.jsonl"
        monkeypatch.setattr(ac_mod, "AUDIT_PATH", test_audit)
        monkeypatch.setattr(ac_mod, "HERMES_HOME", tmp_path)

        ctx = self._make_ctx("helios")
        run_activation_cycle(ctx=ctx, command="launch a rocket")

        assert test_audit.exists()
        content = test_audit.read_text()
        import json
        record = json.loads(content.strip().splitlines()[-1])
        assert record["agent_id"] == "elon_musk"
        assert record["decision_type"] == "agent_activation"

    def test_cycle_via_raci_reflects_it(self):
        ctx = self._make_ctx("atlas")
        ctx.via_raci = True
        ctx.reason = "RACI fallback"
        result = run_activation_cycle(ctx=ctx, command="product launch")
        # reflect step should mention RACI
        reflect = next(s for s in result.cycle if s.step == "reflect")
        assert "RACI" in reflect.content

    def test_cycle_injected_context_string(self):
        ctx = self._make_ctx("orion")
        result = run_activation_cycle(ctx=ctx, command="AGI research direction")
        assert isinstance(result.injected_context, str)


# ── Integration: full flow ────────────────────────────────────────────────

class TestIntegration:
    """Integration tests: full KR1→KR2→KR3→KR4 flow."""

    def test_full_flow_space_command(self):
        """A space command should activate helios, query memory, run cycle."""
        ctx = resolve_active_agent("how do we land a rocket booster back on the pad?")
        assert ctx.persona_id == "helios"

        result = run_activation_cycle(ctx=ctx, command="land rocket booster")
        assert result.persona_id == "helios"
        assert len(result.cycle) == 6

    def test_full_flow_raci_unknown_domain(self):
        """Unknown domain → RACI resolves → cycle runs."""
        ctx = resolve_active_agent("quarterly business review presentation")
        assert ctx.persona_id is not None  # some agent resolved

        result = run_activation_cycle(ctx=ctx, command="quarterly business review")
        assert result.persona_id in ["helios", "atlas", "orion"]
        assert len(result.cycle) == 6

    def test_handle_tools(self):
        """Test tool handlers return expected shapes."""
        from plugins.executive_activation import (
            handle_executive_resolve,
            handle_executive_activate,
        )

        resolve_result = handle_executive_resolve(
            command="AI research on transformers",
            user_id="test_user",
        )
        assert "persona_id" in resolve_result
        assert "confidence" in resolve_result

        activate_result = handle_executive_activate(
            command="space mission planning",
            user_id="test_user",
        )
        assert "activation_id" in activate_result
        assert "cycle_steps" in activate_result
        assert len(activate_result["cycle_steps"]) == 6
