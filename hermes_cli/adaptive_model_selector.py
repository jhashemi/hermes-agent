"""Task-adaptive model selection for kanban workers.

Hexagonal: a small classifier port + a deterministic rule-based adapter
that maps (task title / priority / skills) → tier → (model, provider).
Wired into ``hermes_cli.kanban_db._default_spawn`` so dispatched workers
get an explicit ``-m <model> --provider <provider>`` override appropriate
to the task class.

Defaults follow user directive 2026-06-05:
    - cheap default: ``glm-5.1`` (wafer.ai openrouter proxy, $0.98/$3.08 per 1M)
    - high-stakes: ``claude-sonnet-4-6`` via Bedrock IAM
    - trivial:     ``claude-haiku-4-5`` via Bedrock IAM

This module is **safe to import at any time** — no I/O, no DB calls, no
network. Pure functions over a small ``TaskSignal`` dataclass. The kanban
dispatcher constructs the signal from the row it just claimed.

Falsifier (run from CLI):
    python -m hermes_cli.adaptive_model_selector \\
        --title "fix critical safety regression" --priority 9
    → tier=HIGH_STAKES model=claude-sonnet-4-6 provider=bedrock

Override hooks:
    HERMES_KANBAN_FORCE_MODEL=glm-5.1   (env override — disables classifier)
    HERMES_KANBAN_FORCE_PROVIDER=...

Disable entirely:
    HERMES_KANBAN_ADAPTIVE=0  (worker uses profile defaults exactly)
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Sequence


class TaskTier(str, Enum):
    HIGH_STAKES = "high_stakes"
    DEFAULT = "default"
    TRIVIAL = "trivial"


@dataclass(frozen=True)
class TaskSignal:
    """Cheap-to-compute features the dispatcher already has at claim time."""
    title: str = ""
    priority: int = 0
    skills: Sequence[str] = field(default_factory=tuple)
    assignee: str = ""


@dataclass(frozen=True)
class ModelChoice:
    tier: TaskTier
    model: str
    provider: str
    reason: str  # human-readable trigger ("priority>=8", "skill:adr", ...)


# ---------------------------------------------------------------------------
# Tier → (model, provider) mapping
# ---------------------------------------------------------------------------

# Per user directive 2026-06-05: workers default to glm-5.1, escalate to
# sonnet for high-stakes. Bedrock IAM gives us a no-API-key escape hatch
# that's reachable as long as AWS credentials are present.
TIER_MODELS: dict[TaskTier, tuple[str, str]] = {
    # tier:                 (model,                              provider)
    TaskTier.HIGH_STAKES:  ("us.anthropic.claude-sonnet-4-6",   "bedrock"),
    TaskTier.DEFAULT:      ("glm-5.1",                          "openrouter"),
    TaskTier.TRIVIAL:      ("us.anthropic.claude-haiku-4-5",    "bedrock"),
}


# ---------------------------------------------------------------------------
# Classifier rules (deterministic, ordered: first match wins)
# ---------------------------------------------------------------------------

_HIGH_STAKES_TITLE_PATTERNS = [
    re.compile(r"\b(critical|crit|p0|p1|outage|incident)\b", re.IGNORECASE),
    re.compile(r"\b(safety|safety-critical|production|prod-only)\b", re.IGNORECASE),
    re.compile(r"\b(security|auth|credentials|secrets)\b", re.IGNORECASE),
    re.compile(r"\b(architect|architecture|adr|migration|schema)\b", re.IGNORECASE),
    re.compile(r"\b(consensus|byzantine|raft|distributed)\b", re.IGNORECASE),
]

_TRIVIAL_TITLE_PATTERNS = [
    re.compile(r"\b(label|tag|categori[sz]e|sort)\b", re.IGNORECASE),
    re.compile(r"\b(cleanup|tidy|format|prettif|lint-fix)\b", re.IGNORECASE),
    re.compile(r"\b(typo|fix-typo|rename|grammar)\b", re.IGNORECASE),
    re.compile(r"\b(docstring-only|comment-only)\b", re.IGNORECASE),
]

# Skills known to imply high-stakes work
_HIGH_STAKES_SKILLS: frozenset[str] = frozenset({
    "safety-critical-patterns",
    "api-review",
    "pre-implementation-codebase-audit",
    "adr",
    "rfc",
    "architecture-review",
    "migration",
    "auth-implementation-patterns",
    "production-distributed-systems",
    "executive-council-consensus",
})

# Trivial-implying skills (rare — most skills are mid-tier)
_TRIVIAL_SKILLS: frozenset[str] = frozenset({
    "commit-messages",
    "changelog-generator",
})


def classify(signal: TaskSignal) -> ModelChoice:
    """Map a task signal to a tier + concrete (model, provider).

    Pure function; deterministic; first-match-wins. The order is:
        1. Skill-based high-stakes match
        2. Priority >= 8
        3. Title regex high-stakes
        4. Skill-based trivial match
        5. Title regex trivial
        6. Default (glm-5.1)
    """
    skills_set = {s.lower() for s in (signal.skills or ()) if s}

    # 1. high-stakes by skill
    overlap = skills_set & _HIGH_STAKES_SKILLS
    if overlap:
        m, p = TIER_MODELS[TaskTier.HIGH_STAKES]
        return ModelChoice(TaskTier.HIGH_STAKES, m, p,
                           reason=f"skill:{sorted(overlap)[0]}")

    # 2. high-stakes by priority
    if signal.priority is not None and signal.priority >= 8:
        m, p = TIER_MODELS[TaskTier.HIGH_STAKES]
        return ModelChoice(TaskTier.HIGH_STAKES, m, p,
                           reason=f"priority>={signal.priority}")

    # 3. high-stakes by title
    title = signal.title or ""
    for pat in _HIGH_STAKES_TITLE_PATTERNS:
        if pat.search(title):
            m, p = TIER_MODELS[TaskTier.HIGH_STAKES]
            return ModelChoice(TaskTier.HIGH_STAKES, m, p,
                               reason=f"title-pattern:{pat.pattern}")

    # 4. trivial by skill
    triv_overlap = skills_set & _TRIVIAL_SKILLS
    if triv_overlap:
        m, p = TIER_MODELS[TaskTier.TRIVIAL]
        return ModelChoice(TaskTier.TRIVIAL, m, p,
                           reason=f"skill:{sorted(triv_overlap)[0]}")

    # 5. trivial by title
    for pat in _TRIVIAL_TITLE_PATTERNS:
        if pat.search(title):
            m, p = TIER_MODELS[TaskTier.TRIVIAL]
            return ModelChoice(TaskTier.TRIVIAL, m, p,
                               reason=f"title-pattern:{pat.pattern}")

    # 6. default
    m, p = TIER_MODELS[TaskTier.DEFAULT]
    return ModelChoice(TaskTier.DEFAULT, m, p, reason="default")


# ---------------------------------------------------------------------------
# Spawn-time integration helpers
# ---------------------------------------------------------------------------

def adaptive_args_for_task(
    title: str,
    priority: int,
    skills: Iterable[str],
    assignee: str = "",
) -> tuple[list[str], Optional[ModelChoice]]:
    """Return ``["-m", model, "--provider", provider]`` (or empty list).

    Returns:
        (cli_args, ModelChoice|None) — second element None when adaptive
        selection is disabled via env so the caller can log accordingly.
    """
    # Env-driven kill-switch
    if os.environ.get("HERMES_KANBAN_ADAPTIVE", "1") == "0":
        return ([], None)

    # Forced override (debugging)
    forced_model = os.environ.get("HERMES_KANBAN_FORCE_MODEL", "").strip()
    forced_provider = os.environ.get("HERMES_KANBAN_FORCE_PROVIDER", "").strip()
    if forced_model:
        provider = forced_provider or "openrouter"
        choice = ModelChoice(TaskTier.DEFAULT, forced_model, provider,
                             reason="env-forced")
        return (
            ["-m", forced_model, "--provider", provider],
            choice,
        )

    sig = TaskSignal(
        title=title or "",
        priority=int(priority or 0),
        skills=tuple(s for s in (skills or ()) if s),
        assignee=assignee or "",
    )
    choice = classify(sig)
    return (
        ["-m", choice.model, "--provider", choice.provider],
        choice,
    )


# ---------------------------------------------------------------------------
# CLI falsifier
# ---------------------------------------------------------------------------

def _main() -> int:
    import argparse
    import json
    p = argparse.ArgumentParser(description="Classify a task → model/provider")
    p.add_argument("--title", default="")
    p.add_argument("--priority", type=int, default=0)
    p.add_argument("--skills", nargs="*", default=[])
    p.add_argument("--assignee", default="")
    p.add_argument("--json", action="store_true")
    args = p.parse_args()

    sig = TaskSignal(args.title, args.priority,
                     tuple(args.skills), args.assignee)
    choice = classify(sig)
    out = {
        "tier": choice.tier.value,
        "model": choice.model,
        "provider": choice.provider,
        "reason": choice.reason,
    }
    if args.json:
        print(json.dumps(out))
    else:
        for k, v in out.items():
            print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
