#!/usr/bin/env python3
"""
Memory Consolidator — Post-session fact extraction.

Analyzes conversation transcripts to identify durable facts worth
persisting to long-term memory. Inspired by cheetahclaws' memory
consolidator pattern: extract → score → deduplicate → write.

Design:
- extract_facts(): Parses session text to identify durable facts
- write_facts(): Persists high-confidence facts to MemoryStore
- Threshold: only facts with confidence >= 0.7 are persisted
- Deduplication: skips facts already present in memory
"""

import re
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Patterns that indicate ephemeral task instructions (NOT durable facts)
_EPHEMERAL_PATTERNS = [
    r"\b(run|execute|start|stop|kill|build|deploy|test)\b.*\b(now|please|quickly)\b",
    r"\bfix\b.*\b(failing|broken|bug|error)\b",
    r"\b(check|verify|confirm|investigate|debug)\b",
    r"\b(add|remove|delete|update|change)\b.*\b(line|file|code|function)\b",
    r"^.*(TODO|FIXME|HACK|XXX).*$",
    r"\b(show|list|display|print|echo)\b",
]

# Patterns that indicate durable facts worth persisting
_DURABLE_PATTERNS = [
    r"\b(i|we|the (?:project|team|org|company))\s+(prefer|use|rely on|always|never)\b",
    r"\b(convention|standard|policy|rule|requirement|constraint)\b.*\b(is|are|must|should)\b",
    r"\b(deploy|install|configure|setup)\b.*\b(with|using|via|on)\b",
    r"\b(runs?|works?|tested|compatible)\b.*\b(on|with|under|via)\b",
]


class MemoryConsolidator:
    """
    Extracts durable facts from session transcripts and writes them
    to long-term memory if they pass confidence and dedup checks.
    """

    def __init__(self, memory_dir: Optional[Path] = None):
        self.memory_dir = memory_dir
        self._ephemeral_re = [re.compile(p, re.IGNORECASE) for p in _EPHEMERAL_PATTERNS]
        self._durable_re = [re.compile(p, re.IGNORECASE) for p in _DURABLE_PATTERNS]

    def extract_facts(self, session_text: str) -> List[Dict[str, Any]]:
        """
        Parse session text to identify durable facts.

        Returns a list of {content: str, confidence: float} dicts.
        Facts are extracted from user statements only (not assistant
        responses), filtered against ephemeral patterns, and scored
        by how many durable patterns they match.
        """
        facts = []
        seen_content = set()

        # Extract user statements
        user_lines = self._extract_user_statements(session_text)

        for line in user_lines:
            line = line.strip()
            if not line or len(line) < 15:
                continue

            # Skip ephemeral content
            if self._is_ephemeral(line):
                continue

            # Score durability
            score = self._score_durability(line)
            if score <= 0:
                continue

            # Deduplicate within this extraction
            normalized = line.lower().strip()
            if normalized in seen_content:
                continue
            seen_content.add(normalized)

            facts.append({
                "content": line,
                "confidence": min(1.0, score * 0.3 + 0.5),  # scale to [0.5, 1.0]
            })

        return facts

    def write_facts(
        self,
        target: str,
        facts: List[Dict[str, Any]],
        store: Optional[Any] = None,
        confidence_threshold: float = 0.7,
    ) -> int:
        """
        Write high-confidence facts to memory. Returns count of facts written.

        Args:
            target: "memory" or "user"
            facts: list of {content, confidence} dicts
            store: optional MemoryStore instance (created if not provided)
            confidence_threshold: minimum confidence to persist
        """
        if store is None:
            from tools.memory_tool import MemoryStore
            store = MemoryStore()

        written = 0
        for fact in facts:
            if fact.get("confidence", 0) < confidence_threshold:
                logger.debug("Skipping low-confidence fact: %s", fact["content"][:60])
                continue

            result = store.add(
                target,
                fact["content"],
                confidence=fact.get("confidence", 1.0),
            )
            if result.get("success"):
                written += 1

        return written

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _extract_user_statements(self, text: str) -> List[str]:
        """Extract the text of user messages from a session transcript."""
        lines = []
        # Pattern: "User: ..." or "USER: ..." or "> ..." (CLI prompts)
        for match in re.finditer(
            r"(?:User|USER|>):\s*(.+?)(?=(?:User|USER|Assistant|ASSISTANT|>):|$)",
            text,
            re.DOTALL,
        ):
            content = match.group(1).strip()
            # Flatten multi-line statements into single lines
            content = " ".join(content.split())
            if content:
                lines.append(content)
        return lines

    def _is_ephemeral(self, text: str) -> bool:
        """Check if text matches ephemeral patterns (task instructions, etc)."""
        for pattern in self._ephemeral_re:
            if pattern.search(text):
                return True
        return False

    def _score_durability(self, text: str) -> int:
        """Count how many durable patterns match. Higher = more durable."""
        score = 0
        for pattern in self._durable_re:
            if pattern.search(text):
                score += 1
        return score
