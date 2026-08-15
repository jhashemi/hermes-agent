"""
Executive Agent Activation — resolver module.

KR1: Resolves the active executive agent from command context.
     Takes command text + optional conversation history and returns the active persona.

KR3: RACI fallback — if no agent is active, resolves accountable agent by domain.

Personas:
  helios → Elon Musk   (space, rockets, manufacturing, sustainable energy, physics)
  atlas  → Steve Jobs  (product, design, UX, business, brand, consumer)
  orion  → Demis Hassabis (AI, research, neuroscience, science, AGI)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, List, Dict

import yaml

logger = logging.getLogger(__name__)

# ── Agent definitions ─────────────────────────────────────────────────────

AGENTS = {
    "helios": {
        "persona_id": "helios",
        "full_name": "Elon Musk",
        "agent_dir": "elon_musk",
        "domains": [
            "space", "rocket", "falcon", "starship", "spacex",
            "tesla", "manufacturing", "solar", "energy", "battery",
            "neuralink", "brain", "boring", "tunnel", "physics",
            "first principles", "manufacturing", "efficiency",
            "mars", "satellite", "starlink", "launch", "propulsion",
        ],
        "raci_domains": ["space", "rockets", "manufacturing", "energy", "physics"],
        "aliases": ["elon", "helios", "musk", "elon_musk"],
    },
    "atlas": {
        "persona_id": "atlas",
        "full_name": "Steve Jobs",
        "agent_dir": "steve_jobs",
        "domains": [
            "product", "design", "ux", "ui", "user experience",
            "apple", "iphone", "mac", "brand", "marketing",
            "simplicity", "focus", "retail", "store", "launch",
            "presentation", "keynote", "consumer", "customer",
            "business", "strategy", "real estate", "boutique",
        ],
        "raci_domains": ["product", "design", "UX", "business", "brand"],
        "aliases": ["steve", "atlas", "jobs", "steve_jobs"],
    },
    "orion": {
        "persona_id": "orion",
        "full_name": "Demis Hassabis",
        "agent_dir": "demis_hassabis",
        "domains": [
            "ai", "artificial intelligence", "machine learning", "deep learning",
            "research", "science", "neuroscience", "agi", "safety",
            "deepmind", "alphago", "alphafold", "protein", "chess",
            "reinforcement learning", "rl", "transformer", "model",
            "algorithm", "experiment", "paper", "publication", "benchmark",
        ],
        "raci_domains": ["AI", "research", "science", "AGI"],
        "aliases": ["demis", "orion", "hassabis", "demis_hassabis"],
    },
}

# Reverse alias lookup
ALIAS_MAP: Dict[str, str] = {}
for persona_id, info in AGENTS.items():
    for alias in info["aliases"]:
        ALIAS_MAP[alias.lower()] = persona_id

# Domain keyword → persona (ordered by priority, more specific first)
DOMAIN_MAP: List[tuple] = []
for persona_id, info in AGENTS.items():
    for kw in info["domains"]:
        DOMAIN_MAP.append((kw.lower(), persona_id))
# Sort by keyword length desc so longer phrases match first
DOMAIN_MAP.sort(key=lambda x: len(x[0]), reverse=True)

PROFILES_BASE = Path("/home/ubuntu/executive_agents_platform/agents")


@dataclass
class ActivationContext:
    """Result of an agent resolution."""
    persona_id: Optional[str]        # helios | atlas | orion | None
    full_name: Optional[str]
    agent_dir: Optional[str]
    confidence: float                # 0.0-1.0
    reason: str                      # human-readable explanation
    via_raci: bool = False           # True if resolved via RACI fallback
    profile: Optional[dict] = field(default=None, repr=False)


def _load_profile(agent_dir: str) -> Optional[dict]:
    """Load agent_profile.yaml for the given agent directory name."""
    path = PROFILES_BASE / agent_dir / "agent_profile.yaml"
    try:
        with open(path) as f:
            return yaml.safe_load(f)
    except Exception as e:
        logger.warning("[activation] Could not load profile %s: %s", path, e)
        return None


def resolve_from_alias(text: str) -> Optional[str]:
    """Check if the text directly names a persona via alias."""
    lower = text.lower()
    # Check direct word matches
    for alias, persona_id in ALIAS_MAP.items():
        pattern = r"\b" + re.escape(alias) + r"\b"
        if re.search(pattern, lower):
            return persona_id
    return None


def resolve_from_domains(text: str) -> Optional[tuple]:
    """Resolve persona from domain keyword matches. Returns (persona_id, confidence, keyword)."""
    lower = text.lower()
    scores: Dict[str, float] = {}
    matched_kw: Dict[str, str] = {}

    for kw, persona_id in DOMAIN_MAP:
        if kw in lower:
            scores[persona_id] = scores.get(persona_id, 0.0) + (len(kw) / 20.0)
            if persona_id not in matched_kw:
                matched_kw[persona_id] = kw

    if not scores:
        return None

    best = max(scores, key=lambda k: scores[k])
    confidence = min(scores[best], 1.0)
    return best, confidence, matched_kw[best]


def resolve_active_agent(
    command: str,
    history: Optional[List[dict]] = None,
    current_session_agent: Optional[str] = None,
) -> ActivationContext:
    """
    KR1: Resolve active executive agent from command context.

    Priority order:
    1. current_session_agent (already active in this session)
    2. Explicit alias mention in command (e.g. "ask elon", "helios mode")
    3. Domain keyword match in command text
    4. Domain keyword match in recent history (last 3 turns)
    5. RACI fallback (KR3)

    Returns ActivationContext with the resolved agent (or None if RACI also fails).
    """
    # 1. Already-active agent in session
    if current_session_agent and current_session_agent in AGENTS:
        info = AGENTS[current_session_agent]
        profile = _load_profile(info["agent_dir"])
        return ActivationContext(
            persona_id=current_session_agent,
            full_name=info["full_name"],
            agent_dir=info["agent_dir"],
            confidence=0.95,
            reason=f"session-active agent: {current_session_agent}",
            profile=profile,
        )

    # 2. Alias mention in command
    alias_match = resolve_from_alias(command)
    if alias_match:
        info = AGENTS[alias_match]
        profile = _load_profile(info["agent_dir"])
        return ActivationContext(
            persona_id=alias_match,
            full_name=info["full_name"],
            agent_dir=info["agent_dir"],
            confidence=0.98,
            reason=f"explicit alias mention in command",
            profile=profile,
        )

    # 3. Domain match in command
    domain_result = resolve_from_domains(command)
    if domain_result:
        persona_id, confidence, kw = domain_result
        info = AGENTS[persona_id]
        profile = _load_profile(info["agent_dir"])
        return ActivationContext(
            persona_id=persona_id,
            full_name=info["full_name"],
            agent_dir=info["agent_dir"],
            confidence=confidence,
            reason=f"domain keyword '{kw}' in command",
            profile=profile,
        )

    # 4. Domain match in recent history
    if history:
        recent_text = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str) else ""
            for m in history[-6:]
        )
        alias_match = resolve_from_alias(recent_text)
        if alias_match:
            info = AGENTS[alias_match]
            profile = _load_profile(info["agent_dir"])
            return ActivationContext(
                persona_id=alias_match,
                full_name=info["full_name"],
                agent_dir=info["agent_dir"],
                confidence=0.75,
                reason="alias mention in recent history",
                profile=profile,
            )

        domain_result = resolve_from_domains(recent_text)
        if domain_result:
            persona_id, confidence, kw = domain_result
            info = AGENTS[persona_id]
            profile = _load_profile(info["agent_dir"])
            return ActivationContext(
                persona_id=persona_id,
                full_name=info["full_name"],
                agent_dir=info["agent_dir"],
                confidence=confidence * 0.7,
                reason=f"domain keyword '{kw}' in history",
                profile=profile,
            )

    # 5. RACI fallback (KR3)
    return _raci_resolve(command)


# ── RACI matrix ───────────────────────────────────────────────────────────

# Each entry: (domain_keywords, persona_id, description)
RACI_MATRIX = [
    # helios: space/rockets/manufacturing/energy/physics
    (["space", "rocket", "launch", "orbit", "satellite", "mars", "moon",
      "propulsion", "starship", "falcon", "manufacturing", "factory",
      "solar energy", "battery", "grid", "physics", "engineering"], "helios",
     "space/rockets/manufacturing/energy"),
    # orion: AI/research/science/AGI
    (["ai ", "artificial intelligence", "machine learning", "deep learning",
      "llm", "gpt", "model training", "research", "neuroscience", "agi",
      "safety", "alignment", "reinforcement learning", "algorithm",
      "data science", "experiment", "paper", "science", "neural"],
     "orion", "AI/research/science"),
    # atlas: product/design/UX/business (default/broadest)
    (["product", "design", "ux", "user", "interface", "brand", "marketing",
      "business", "strategy", "customer", "launch", "sales", "revenue",
      "retail", "store", "app", "website", "real estate", "boutique"],
     "atlas", "product/design/business"),
]


def _raci_resolve(command: str) -> ActivationContext:
    """
    KR3: Resolve accountable agent via RACI matrix when no agent is active.
    Falls back to atlas (product/business) as default accountable agent.
    """
    lower = command.lower()
    for keywords, persona_id, domain_desc in RACI_MATRIX:
        for kw in keywords:
            if kw in lower:
                info = AGENTS[persona_id]
                profile = _load_profile(info["agent_dir"])
                return ActivationContext(
                    persona_id=persona_id,
                    full_name=info["full_name"],
                    agent_dir=info["agent_dir"],
                    confidence=0.6,
                    reason=f"RACI matrix: keyword '{kw.strip()}' → domain '{domain_desc}'",
                    via_raci=True,
                    profile=profile,
                )

    # Ultimate fallback: atlas (product/business)
    info = AGENTS["atlas"]
    profile = _load_profile(info["agent_dir"])
    return ActivationContext(
        persona_id="atlas",
        full_name=info["full_name"],
        agent_dir=info["agent_dir"],
        confidence=0.4,
        reason="RACI default fallback → atlas (product/business)",
        via_raci=True,
        profile=profile,
    )
