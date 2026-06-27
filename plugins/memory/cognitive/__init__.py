"""Cognitive Memory Adapter — decision audit trail as default memory provider.

Records every cognitive decision (tool call, reasoning step, key response) in a
durable JSONL audit trail. Recalls past decisions in prefetch to give the agent
continuity across turns. Satisfies OKR KR3.1: "CognitiveMemoryAdapter is default,
decision audit non-empty after 5+ turns."

Architecture:
  - JSONL persistence via executive_agents DecisionAuditTrail (or standalone fallback)
  - Prefetch recalls N most recent decisions for the current agent profile
  - sync_turn extracts decisions from each turn (tool calls, confidence signals)
  - Exposes tools: cognitive_recall (search decisions), cognitive_decide (log a decision)
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider
from tools.registry import tool_error

logger = logging.getLogger(__name__)


# ── Tool schemas ──────────────────────────────────────────────────────────

RECALL_SCHEMA = {
    "name": "cognitive_recall",
    "description": (
        "Search the cognitive decision audit trail for past decisions, reasoning, "
        "and outcomes. Returns decisions matching the query, ranked by recency. "
        "Use this to recall why a decision was made, what alternatives were considered, "
        "or what happened in previous turns."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query — keywords, decision type, or agent name.",
            },
            "limit": {
                "type": "integer",
                "description": "Max decisions to return (default 5, max 20).",
                "default": 5,
            },
            "min_confidence": {
                "type": "number",
                "description": "Minimum confidence filter (0.0-1.0, default 0.0).",
                "default": 0.0,
            },
        },
        "required": ["query"],
    },
}

DECIDE_SCHEMA = {
    "name": "cognitive_decide",
    "description": (
        "Record a deliberate cognitive decision in the audit trail. Use this when "
        "you make a significant choice — architectural, strategic, or tactical — "
        "that should be recalled later. Include your reasoning and confidence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "decision_type": {
                "type": "string",
                "description": "Category of decision (e.g. 'architecture', 'implementation', 'debugging', 'strategy').",
            },
            "reasoning": {
                "type": "string",
                "description": "Why this decision was made — alternatives considered, trade-offs.",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence in this decision (0.0-1.0).",
                "default": 0.8,
            },
            "context": {
                "type": "object",
                "description": "Additional context (e.g. parent decision, task ID, file path).",
            },
        },
        "required": ["decision_type", "reasoning"],
    },
}

ALL_TOOL_SCHEMAS = [RECALL_SCHEMA, DECIDE_SCHEMA]


# ── Standalone JSONL audit trail (no EAF dependency) ──────────────────────

class _StandaloneAuditTrail:
    """Lightweight JSONL audit trail — standalone, no EAF dependency.

    Drop-in replacement for executive_agents.infrastructure.systems.decision_audit
    when running without the EAF package installed.
    """

    def __init__(self, storage_path: str):
        self._storage_path = storage_path
        self._log: list[dict] = []
        self._load_existing()

    def _load_existing(self):
        path = Path(self._storage_path)
        if path.exists():
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            self._log.append(json.loads(line))
                        except Exception:
                            pass

    def _persist(self, entry: dict):
        path = Path(self._storage_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def record_decision(self, agent_id: str, decision_type: str, reasoning: str,
                        confidence: float, context: dict = None) -> str:
        decision_id = f"D-{len(self._log):06d}-{agent_id[:8]}"
        entry = {
            "id": decision_id,
            "agent_id": agent_id,
            "decision_type": decision_type,
            "reasoning": reasoning,
            "confidence": confidence,
            "context": context or {},
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "parent_id": (context or {}).get("parent_decision"),
        }
        self._log.append(entry)
        self._persist(entry)
        return decision_id

    def query_decisions(self, agent_id: str = None, since: str = None,
                        min_confidence: float = 0.0,
                        decision_type: str = None,
                        query: str = None) -> list:
        results = []
        q_lower = (query or "").lower()
        for entry in self._log:
            if agent_id and entry.get("agent_id") != agent_id:
                continue
            if since and entry.get("timestamp", "") < since:
                continue
            if entry.get("confidence", 0) < min_confidence:
                continue
            if decision_type and entry.get("decision_type") != decision_type:
                continue
            if q_lower:
                searchable = f"{entry.get('reasoning','')} {entry.get('decision_type','')} {json.dumps(entry.get('context',{}))}".lower()
                if q_lower not in searchable:
                    continue
            results.append(entry)
        return results

    @property
    def size(self) -> int:
        return len(self._log)


# ── Decision extraction from turns ────────────────────────────────────────

_TOOL_CALL_RE = re.compile(r"`?(\w+)\(([^)]*)\)`?")
_CONFIDENCE_RE = re.compile(r"confidence[:\s]+([0-9.]+)", re.IGNORECASE)


def _extract_decisions_from_turn(user_content: str, assistant_content: str,
                                 agent_id: str) -> list[dict]:
    """Extract cognitive decisions from a conversation turn.

    Extracts:
      - Tool calls as 'action' decisions
      - Confidence signals as 'assessment' decisions
      - Long reasoning as 'reasoning' decisions
    """
    decisions = []

    # Extract tool calls from assistant content
    tool_calls = _TOOL_CALL_RE.findall(assistant_content)
    for tool_name, args_str in tool_calls:
        decisions.append({
            "agent_id": agent_id,
            "decision_type": "action",
            "reasoning": f"Invoked tool: {tool_name}({args_str[:100]})",
            "confidence": 0.9,
            "context": {"tool": tool_name, "source": "auto_extracted"},
        })

    # Extract confidence signals
    conf_matches = _CONFIDENCE_RE.findall(assistant_content)
    for conf_str in conf_matches:
        try:
            conf = float(conf_str)
            decisions.append({
                "agent_id": agent_id,
                "decision_type": "assessment",
                "reasoning": assistant_content[:200],
                "confidence": conf,
                "context": {"source": "auto_extracted"},
            })
        except ValueError:
            pass

    # If assistant response is substantial (>500 chars), record as reasoning
    if len(assistant_content) > 500 and not tool_calls:
        decisions.append({
            "agent_id": agent_id,
            "decision_type": "reasoning",
            "reasoning": assistant_content[:300],
            "confidence": 0.7,
            "context": {"source": "auto_extracted"},
        })

    return decisions


# ── CognitiveMemoryProvider ────────────────────────────────────────────────

class CognitiveMemoryProvider(MemoryProvider):
    """Cognitive Memory Adapter — decision audit trail as default memory.

    Records cognitive decisions in JSONL, recalls them in prefetch,
    and exposes cognitive_recall + cognitive_decide tools.

    Config (via $HERMES_HOME/cognitive.json or config.yaml memory.cognitive):
      - enabled: bool (default True)
      - max_prefetch: int (default 5) — decisions injected in system prompt
      - audit_path: str — override JSONL path (default: $HERMES_HOME/cognitive_audit.jsonl)
    """

    def __init__(self):
        self._audit: Optional[_StandaloneAuditTrail] = None
        self._agent_id: str = "hermes"
        self._session_id: str = ""
        self._hermes_home: str = ""
        self._max_prefetch: int = 5
        self._enabled: bool = True
        self._prefetch_result: str = ""
        self._sync_thread: Optional[threading.Thread] = None
        self._cron_skipped: bool = False
        self._turn_count: int = 0

    @property
    def name(self) -> str:
        return "cognitive"

    def is_available(self) -> bool:
        """Always available — uses local JSONL, no external deps."""
        return self._enabled

    def record_decision(
        self,
        agent_id: str,
        decision_type: str,
        reasoning: str,
        confidence: float,
        context: dict | None = None,
    ) -> str | None:
        """β₉ (S4/H4) — Plan ↦ Audit functor delegate. Forward to the
        underlying _StandaloneAuditTrail so dispatchers can record allocations
        without dipping into provider internals. No-op if audit not initialized
        (e.g. before initialize()) — fail-soft so dispatcher never crashes on
        a missing audit sink.
        """
        if self._audit is None:
            try:
                # Auto-bootstrap with default path so callers from outside
                # the gateway initialize loop still get audit coverage.
                from pathlib import Path

                home = Path(self._hermes_home or Path.home() / ".hermes")
                home.mkdir(parents=True, exist_ok=True)
                self._audit = _StandaloneAuditTrail(
                    storage_path=str(home / "cognitive_audit.jsonl")
                )
            except Exception:
                return None
        return self._audit.record_decision(
            agent_id=agent_id,
            decision_type=decision_type,
            reasoning=reasoning,
            confidence=confidence,
            context=context,
        )

    def initialize(self, session_id: str, **kwargs) -> None:
        agent_context = kwargs.get("agent_context", "")
        platform = kwargs.get("platform", "cli")
        if agent_context in ("cron", "flush") or platform == "cron":
            logger.debug("Cognitive memory skipped: cron/flush context")
            self._cron_skipped = True
            return

        self._session_id = session_id
        self._hermes_home = kwargs.get("hermes_home", str(Path.home() / ".hermes"))
        self._agent_id = kwargs.get("agent_identity", "hermes")

        # Load config
        config_path = Path(self._hermes_home) / "cognitive.json"
        cfg = {}
        if config_path.exists():
            try:
                cfg = json.loads(config_path.read_text())
            except Exception:
                pass
        self._max_prefetch = int(cfg.get("max_prefetch", 5))
        self._enabled = cfg.get("enabled", True)

        # Initialize audit trail
        audit_path = cfg.get(
            "audit_path",
            str(Path(self._hermes_home) / "cognitive_audit.jsonl"),
        )
        self._audit = _StandaloneAuditTrail(audit_path)

        # Record session start
        self._audit.record_decision(
            agent_id=self._agent_id,
            decision_type="session_start",
            reasoning=f"Session {session_id} initialized",
            confidence=1.0,
            context={"session_id": session_id, "platform": platform},
        )
        logger.info(
            "CognitiveMemoryAdapter initialized: agent=%s audit=%s (%d existing decisions)",
            self._agent_id, audit_path, self._audit.size,
        )

    def system_prompt_block(self) -> str:
        if self._cron_skipped or not self._audit:
            return ""
        count = self._audit.size
        return (
            f"[Cognitive Memory] Decision audit active: {count} decisions recorded. "
            f"Use cognitive_recall to search past decisions, cognitive_decide to record new ones."
        )

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall recent decisions relevant to the current query."""
        if self._cron_skipped or not self._audit:
            return ""

        # Search by query keywords
        results = self._audit.query_decisions(
            agent_id=self._agent_id,
            query=query,
        )

        # If no query-specific results, return most recent
        if not results:
            results = self._audit.query_decisions(
                agent_id=self._agent_id,
            )

        # Take most recent N
        recent = results[-self._max_prefetch:] if len(results) > self._max_prefetch else results

        if not recent:
            return ""

        lines = ["[Cognitive Memory — Recent Decisions]"]
        for d in recent:
            ts = d.get("timestamp", "?")[:19]
            dtype = d.get("decision_type", "?")
            reasoning = (d.get("reasoning", "")[:120]).replace("\n", " ")
            conf = d.get("confidence", 0)
            lines.append(f"  [{ts}] {dtype} (conf={conf:.1f}): {reasoning}")

        return "\n".join(lines)

    def sync_turn(self, user_content: str, assistant_content: str, *, session_id: str = "") -> None:
        """Extract and record decisions from the completed turn."""
        if self._cron_skipped or not self._audit:
            return

        def _sync():
            decisions = _extract_decisions_from_turn(
                user_content, assistant_content, self._agent_id
            )
            for d in decisions:
                try:
                    self._audit.record_decision(**d)
                except Exception as e:
                    logger.debug("Cognitive sync_turn record failed: %s", e)

        self._sync_thread = threading.Thread(target=_sync, daemon=True, name="cognitive-sync")
        self._sync_thread.start()

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        self._turn_count = turn_number

    def on_memory_write(self, action: str, target: str, content: str, metadata=None) -> None:
        """Mirror built-in memory writes as cognitive decisions."""
        if self._cron_skipped or not self._audit:
            return
        if action == "add" and content:
            self._audit.record_decision(
                agent_id=self._agent_id,
                decision_type="memory_write",
                reasoning=content[:200],
                confidence=0.9,
                context={"target": target, "action": action},
            )

    def on_session_end(self, messages: list) -> None:
        if self._cron_skipped or not self._audit:
            return
        self._audit.record_decision(
            agent_id=self._agent_id,
            decision_type="session_end",
            reasoning=f"Session ended after {self._turn_count} turns, {self._audit.size} total decisions",
            confidence=1.0,
            context={"turn_count": self._turn_count, "total_decisions": self._audit.size},
        )

    def get_tool_schemas(self) -> list:
        if self._cron_skipped:
            return []
        return list(ALL_TOOL_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: dict, **kwargs) -> str:
        if self._cron_skipped:
            return tool_error("Cognitive memory not active (cron context).")
        if not self._audit:
            return tool_error("Cognitive memory not initialized.")

        try:
            if tool_name == "cognitive_recall":
                query = args.get("query", "")
                limit = min(int(args.get("limit", 5)), 20)
                min_conf = float(args.get("min_confidence", 0.0))
                results = self._audit.query_decisions(
                    agent_id=self._agent_id,
                    query=query,
                    min_confidence=min_conf,
                )
                results = results[-limit:]
                if not results:
                    return json.dumps({"result": "No matching decisions found.", "count": 0})
                formatted = []
                for d in results:
                    formatted.append({
                        "id": d.get("id"),
                        "type": d.get("decision_type"),
                        "reasoning": d.get("reasoning", "")[:200],
                        "confidence": d.get("confidence"),
                        "timestamp": d.get("timestamp"),
                    })
                return json.dumps({"result": formatted, "count": len(formatted)})

            elif tool_name == "cognitive_decide":
                dtype = args.get("decision_type", "general")
                reasoning = args.get("reasoning", "")
                conf = float(args.get("confidence", 0.8))
                ctx = args.get("context", {})
                did = self._audit.record_decision(
                    agent_id=self._agent_id,
                    decision_type=dtype,
                    reasoning=reasoning,
                    confidence=conf,
                    context=ctx,
                )
                return json.dumps({
                    "result": f"Decision {did} recorded.",
                    "decision_id": did,
                    "total_decisions": self._audit.size,
                })

            return tool_error(f"Unknown cognitive tool: {tool_name}")

        except Exception as e:
            logger.error("Cognitive tool %s failed: %s", tool_name, e)
            return tool_error(f"Cognitive {tool_name} failed: {e}")

    def shutdown(self) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)


# ── Plugin entry point ─────────────────────────────────────────────────────

def register(ctx) -> None:
    """Register CognitiveMemoryAdapter as a memory provider plugin."""
    ctx.register_memory_provider(CognitiveMemoryProvider())
