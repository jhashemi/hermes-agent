"""
Cognitive activation cycle for executive agent activation.

KR4: When an agent is activated, runs a full cognitive cycle:
  observe   → gather context (command, history, profile, memory)
  reason    → analyze the situation using the agent's cognitive style
  plan      → decide the approach based on agent's expertise
  reward    → evaluate the approach quality
  memory    → store the activation decision for future recall
  reflect   → learn and update meta-knowledge
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from .resolver import ActivationContext, AGENTS
from .cognitive_memory import query_cognitive_memory, format_memory_context

logger = logging.getLogger(__name__)

HERMES_HOME = Path(os.path.expanduser("~")) / ".hermes"
AUDIT_PATH = HERMES_HOME / "cognitive_audit.jsonl"


@dataclass
class CognitiveState:
    """Snapshot of one activation cycle step."""
    step: str
    content: str
    ts: float = field(default_factory=time.time)
    metadata: dict = field(default_factory=dict)


@dataclass
class ActivationResult:
    """Full output of a cognitive activation cycle."""
    activation_id: str
    persona_id: str
    full_name: str
    command: str
    cycle: List[CognitiveState]           # observe/reason/plan/reward/memory/reflect
    memory_context: str                   # formatted memory from KR2
    injected_context: str                 # what to prepend to the response
    confidence: float
    via_raci: bool
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["cycle"] = [asdict(s) for s in self.cycle]
        return d


# ── Cognitive style templates per agent ──────────────────────────────────

_REASONING_TEMPLATES = {
    "helios": {
        "observe_style": "engineering systems analysis, first-principles breakdown",
        "reason_style": "physics-based reasoning, question every assumption",
        "plan_style": "rapid iteration, MVP → scale, manufacturing efficiency",
        "reward_signal": "does this move civilization forward? is it physically possible?",
        "reflect_style": "what can be made 10x better? what's the bottleneck?",
    },
    "atlas": {
        "observe_style": "product experience mapping, user journey, simplicity audit",
        "reason_style": "intersection of technology and liberal arts",
        "plan_style": "focus on the essential, cut everything else, obsess over details",
        "reward_signal": "does this delight the user? is it insanely great?",
        "reflect_style": "what would make this perfect? what's unnecessary?",
    },
    "orion": {
        "observe_style": "scientific literature review, neuroscience framing",
        "reason_style": "systems-level reasoning, empirical grounding, research rigor",
        "plan_style": "hypothesis-driven, benchmark against state-of-art, safety-aware",
        "reward_signal": "is this scientifically rigorous? does it advance human knowledge?",
        "reflect_style": "what are the limitations? what new experiments does this suggest?",
    },
}


def _default_template() -> dict:
    return {
        "observe_style": "contextual analysis",
        "reason_style": "structured reasoning",
        "plan_style": "step-by-step planning",
        "reward_signal": "quality, accuracy, helpfulness",
        "reflect_style": "identify improvements",
    }


def _write_audit(record: dict) -> None:
    """Append a decision record to the cognitive audit JSONL."""
    try:
        HERMES_HOME.mkdir(parents=True, exist_ok=True)
        with open(AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        logger.warning("[activation_cycle] Could not write audit: %s", e)


def run_activation_cycle(
    ctx: ActivationContext,
    command: str,
    history: Optional[List[dict]] = None,
) -> ActivationResult:
    """
    KR4: Run the full observe→reason→plan→reward→memory→reflect cycle.

    This is called when an agent is activated (either from context or RACI).
    The cycle builds a rich activation result that includes:
    - Memory context from KR2 (cognitive_memory.query_cognitive_memory)
    - Each cycle step documented as CognitiveState
    - Injected context string ready to prepend to LLM response
    """
    activation_id = str(uuid.uuid4())[:8]
    persona_id = ctx.persona_id or "atlas"
    full_name = ctx.full_name or "Steve Jobs"
    agent_dir = ctx.agent_dir or "steve_jobs"

    template = _REASONING_TEMPLATES.get(persona_id, _default_template())
    cycle: List[CognitiveState] = []
    now = time.time()

    # ── Step 1: OBSERVE ───────────────────────────────────────────────────
    history_summary = ""
    if history:
        recent = history[-4:]
        history_summary = "; ".join(
            f"{m.get('role','?')}: {str(m.get('content',''))[:80]}"
            for m in recent
        )

    profile_summary = ""
    if ctx.profile:
        expertise = ctx.profile.get("expertise_domains", [])
        if isinstance(expertise, list):
            profile_summary = f"expertise={','.join(str(e) for e in expertise[:4])}"

    observe_content = (
        f"Command: '{command[:200]}' | "
        f"Agent: {full_name} ({persona_id}) | "
        f"Reason: {ctx.reason} | "
        f"Confidence: {ctx.confidence:.2f} | "
        f"Via RACI: {ctx.via_raci} | "
        f"History: [{history_summary}] | "
        f"Profile: [{profile_summary}] | "
        f"Style: {template['observe_style']}"
    )
    cycle.append(CognitiveState(step="observe", content=observe_content, ts=now))
    logger.debug("[activation] observe: %s", observe_content[:100])

    # ── Step 2: REASON ────────────────────────────────────────────────────
    # Query cognitive memory (KR2)
    memory_records = query_cognitive_memory(
        agent_dir=agent_dir,
        query=command,
        limit=5,
    )
    memory_context = format_memory_context(memory_records, full_name, agent_dir)

    reason_content = (
        f"Applying {template['reason_style']}. "
        f"Resolved {full_name} as active agent "
        f"({'RACI accountable' if ctx.via_raci else 'context-matched'}). "
        f"Retrieved {len(memory_records)} relevant memory records. "
        f"{'Memory context injected.' if memory_context else 'No prior memory; using profile.'}"
    )
    cycle.append(CognitiveState(step="reason", content=reason_content, ts=time.time()))

    # ── Step 3: PLAN ──────────────────────────────────────────────────────
    plan_content = (
        f"Plan: {template['plan_style']}. "
        f"{'Activating ' + full_name + ' via RACI (' + ctx.reason + ').' if ctx.via_raci else 'Continuing with active agent ' + full_name + '.'} "
        f"Will inject {len(memory_records)} memory records + profile context into response."
    )
    cycle.append(CognitiveState(step="plan", content=plan_content, ts=time.time()))

    # ── Step 4: REWARD ────────────────────────────────────────────────────
    reward_score = ctx.confidence * (1.0 if memory_records else 0.8)
    reward_content = (
        f"Reward signal: [{template['reward_signal']}]. "
        f"Activation quality score: {reward_score:.2f}. "
        f"Confidence: {ctx.confidence:.2f}. "
        f"Memory richness: {len(memory_records)}/5 records retrieved."
    )
    cycle.append(CognitiveState(step="reward", content=reward_content, ts=time.time()))

    # ── Step 5: MEMORY ────────────────────────────────────────────────────
    decision_record = {
        "activation_id": activation_id,
        "agent_id": agent_dir,
        "decision_type": "agent_activation",
        "reasoning": f"Activated {full_name} for command: '{command[:100]}'. {ctx.reason}",
        "outcome": f"cycle_initiated persona={persona_id} confidence={ctx.confidence:.2f}",
        "confidence": ctx.confidence,
        "via_raci": ctx.via_raci,
        "ts": now,
        "command_preview": command[:100],
    }
    _write_audit(decision_record)

    memory_content = (
        f"Decision recorded to cognitive audit: "
        f"agent={agent_dir} activation_id={activation_id} "
        f"confidence={ctx.confidence:.2f} ts={int(now)}"
    )
    cycle.append(CognitiveState(step="memory", content=memory_content, ts=time.time()))

    # ── Step 6: REFLECT ───────────────────────────────────────────────────
    reflect_insights = []
    if ctx.via_raci:
        reflect_insights.append(
            f"No explicit agent in context — RACI resolved {full_name} as accountable. "
            "Consider establishing a persistent session agent."
        )
    if not memory_records:
        reflect_insights.append(
            f"No prior cognitive memory found for {full_name}. "
            "This is likely the first activation for this command domain."
        )
    if ctx.confidence < 0.7:
        reflect_insights.append(
            f"Low confidence ({ctx.confidence:.2f}) — command may span multiple domains. "
            "Monitoring for domain drift."
        )

    reflect_content = (
        template["reflect_style"] + ". " +
        ("; ".join(reflect_insights) if reflect_insights else "High-confidence activation, no anomalies.")
    )
    cycle.append(CognitiveState(step="reflect", content=reflect_content, ts=time.time()))

    # ── Assemble injected context ─────────────────────────────────────────
    context_parts = []
    if memory_context:
        context_parts.append(memory_context)

    profile = ctx.profile or {}
    if profile:
        bio = str(profile.get("bio", "")).replace("\n", " ").strip()[:200]
        if bio:
            context_parts.append(f"[{full_name} Profile] {bio} [/Profile]")

    injected_context = "\n".join(context_parts)

    return ActivationResult(
        activation_id=activation_id,
        persona_id=persona_id,
        full_name=full_name,
        command=command[:200],
        cycle=cycle,
        memory_context=memory_context,
        injected_context=injected_context,
        confidence=ctx.confidence,
        via_raci=ctx.via_raci,
        ts=now,
    )
