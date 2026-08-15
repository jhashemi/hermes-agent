"""
Cognitive memory bridge for executive agent activation.

KR2: Queries the resolved agent's cognitive memory before responding.

Reads decision audit trail JSONL files and persona memory from the
executive_agents_platform agent profile directories. Injects relevant
context into the response as a formatted memory block.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Configurable base paths (env-overridable for containerized/distributed deployments)
_PLATFORM_BASE_ENV = os.environ.get("EXECUTIVE_AGENTS_PLATFORM", "/home/ubuntu/executive_agents_platform")
PLATFORM_BASE = Path(_PLATFORM_BASE_ENV)
AGENTS_BASE = PLATFORM_BASE / "agents"

# Typical locations for cognitive audit trails (env-overridable)
_HERMES_HOME = Path(os.environ.get("HERMES_HOME", os.path.expanduser("~") + "/.hermes"))
_AUDIT_SEARCH_PATHS = [
    Path(os.environ.get("EAF_HOME", os.path.expanduser("~") + "/executive_agents_framework")) / "data" / "cognitive_audit.jsonl",
    _HERMES_HOME / "cognitive_audit.jsonl",
    Path(os.path.expanduser("~")) / "cognitive_audit.jsonl",
    PLATFORM_BASE / "data" / "cognitive_audit.jsonl",
]


def _find_audit_path() -> Optional[Path]:
    """Find the cognitive audit JSONL file."""
    for p in _AUDIT_SEARCH_PATHS:
        if p.exists():
            return p
    return None


def _read_jsonl(path: Path, limit: int = 500) -> List[dict]:
    """Read last `limit` lines from a JSONL file, return as list of dicts."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        # Take last `limit` lines
        recent_lines = lines[-limit:]
        records = []
        for line in recent_lines:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return records
    except Exception as e:
        logger.warning("[cognitive_memory] Could not read %s: %s", path, e)
        return []


def _score_relevance(record: dict, query: str) -> float:
    """Score a decision record's relevance to the query (simple keyword scoring)."""
    text = " ".join([
        str(record.get("reasoning", "")),
        str(record.get("decision_type", "")),
        str(record.get("outcome", "")),
        str(record.get("context", "")),
    ]).lower()

    score = 0.0
    query_words = query.lower().split()
    for word in query_words:
        if len(word) > 3 and word in text:
            score += 1.0
    return score


def query_cognitive_memory(
    agent_dir: str,
    query: str,
    limit: int = 5,
    min_confidence: float = 0.0,
) -> List[dict]:
    """
    KR2: Query the resolved agent's cognitive memory (decision audit trail).

    Searches for decisions relevant to the query, filtered by agent and
    optionally by confidence. Returns the top `limit` most relevant records.

    Args:
        agent_dir: Agent directory name (e.g. 'elon_musk')
        query: Query string to search for
        limit: Max records to return
        min_confidence: Minimum confidence filter (0.0-1.0)

    Returns:
        List of relevant decision records (dicts)
    """
    audit_path = _find_audit_path()
    if not audit_path:
        logger.info("[cognitive_memory] No audit trail found for query '%s'", query)
        return []

    records = _read_jsonl(audit_path, limit=2000)
    if not records:
        return []

    # Filter by agent if agent_id is recorded
    agent_records = [
        r for r in records
        if not r.get("agent_id") or agent_dir in str(r.get("agent_id", ""))
    ]

    # Apply confidence filter
    if min_confidence > 0:
        agent_records = [
            r for r in agent_records
            if r.get("confidence", 1.0) >= min_confidence
        ]

    # Score relevance
    scored = [
        (r, _score_relevance(r, query))
        for r in agent_records
    ]

    # Sort: prefer relevant records; among ties, prefer recent (by timestamp)
    scored.sort(key=lambda x: (x[1], x[0].get("ts", 0)), reverse=True)

    return [r for r, _ in scored[:limit]]


def format_memory_context(
    records: List[dict],
    persona_name: str,
    agent_dir: str,
) -> str:
    """
    Format memory records as a human-readable context block to inject into response.
    """
    if not records:
        # Try to load from profile data as fallback
        profile_context = _load_profile_context(agent_dir)
        if profile_context:
            return (
                f"[{persona_name} Memory Context]\n"
                f"{profile_context}\n"
                f"[/Memory Context]"
            )
        return ""

    lines = [f"[{persona_name} Cognitive Memory — {len(records)} relevant decisions]"]
    for i, rec in enumerate(records, 1):
        dt = rec.get("decision_type", "decision")
        reasoning = rec.get("reasoning", rec.get("outcome", ""))[:200]
        confidence = rec.get("confidence", "?")
        ts = rec.get("ts", rec.get("timestamp", ""))
        if ts:
            try:
                ts_str = str(int(float(ts)))
            except Exception:
                ts_str = str(ts)[:10]
        else:
            ts_str = ""
        line = f"  {i}. [{dt}] {reasoning}"
        if confidence != "?":
            line += f" (conf={confidence:.2f})"
        if ts_str:
            line += f" @{ts_str}"
        lines.append(line)
    lines.append(f"[/Memory]")
    return "\n".join(lines)


def _load_profile_context(agent_dir: str) -> str:
    """Load key facts from agent profile as memory context fallback."""
    profile_path = AGENTS_BASE / agent_dir / "agent_profile.yaml"
    try:
        import yaml
        with open(profile_path) as f:
            profile = yaml.safe_load(f)
        if not profile:
            return ""
        lines = []
        if profile.get("bio"):
            bio = str(profile["bio"]).replace("\n", " ").strip()[:300]
            lines.append(f"Bio: {bio}")
        if profile.get("expertise_domains"):
            domains = profile["expertise_domains"]
            if isinstance(domains, list):
                lines.append(f"Expertise: {', '.join(domains[:5])}")
        if profile.get("personality_traits"):
            traits = profile["personality_traits"]
            if isinstance(traits, list):
                lines.append(f"Traits: {', '.join(traits[:4])}")
        return "\n".join(lines) if lines else ""
    except Exception as e:
        logger.debug("[cognitive_memory] Could not load profile %s: %s", agent_dir, e)
        return ""
