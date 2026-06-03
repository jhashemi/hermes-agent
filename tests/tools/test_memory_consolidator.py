"""Tests for post-session memory consolidator (Phase 5)."""

import json
import os
import pytest

from tools.memory_consolidator import MemoryConsolidator


@pytest.fixture
def consolidator(tmp_path, monkeypatch):
    """Create a MemoryConsolidator with isolated memory dir."""
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    return MemoryConsolidator(memory_dir=tmp_path)


class TestConsolidatorExtract:
    """Tests for the extract_facts method."""

    def test_extract_facts_from_session_text(self, consolidator):
        """extract_facts identifies durable facts from session transcript."""
        session_text = """
        User: I prefer vim over emacs for editing.
        Assistant: Noted. Let me check the project structure.
        User: The project uses pytest with xdist for parallel testing.
        Assistant: Running pytest -n 4...
        """
        facts = consolidator.extract_facts(session_text)
        assert len(facts) >= 1
        # Should extract at least the user preferences and project facts
        texts = [f["content"].lower() for f in facts]
        assert any("vim" in t for t in texts)

    def test_extract_facts_ignores_ephemeral(self, consolidator):
        """extract_facts skips temporary task state and instructions."""
        session_text = """
        User: Run the tests now.
        Assistant: Running pytest...
        User: Fix the failing test in auth.py.
        Assistant: Fixed. All tests pass now.
        """
        facts = consolidator.extract_facts(session_text)
        # Should NOT extract "Run the tests now" or "Fix the failing test"
        # These are ephemeral task instructions, not durable facts
        texts = [f["content"].lower() for f in facts]
        assert not any("run the tests" in t for t in texts)

    def test_extract_facts_returns_confidence(self, consolidator):
        """Each extracted fact includes a confidence score."""
        session_text = "User: I always use Python 3.11 for this project."
        facts = consolidator.extract_facts(session_text)
        assert len(facts) >= 1
        for fact in facts:
            assert "confidence" in fact
            assert 0.0 <= fact["confidence"] <= 1.0


class TestConsolidatorWrite:
    """Tests for writing consolidated facts to memory."""

    def test_write_high_confidence_facts(self, consolidator, tmp_path):
        """write_facts stores facts with confidence >= 0.7."""
        facts = [
            {"content": "User prefers vim", "confidence": 0.9},
            {"content": "Maybe uses docker", "confidence": 0.3},
        ]
        consolidator.write_facts("memory", facts)
        # Only the high-confidence fact should be persisted
        from tools.memory_tool import MemoryStore
        store = MemoryStore()
        entries = store._entries_for("memory")
        # The low-confidence fact should not be written
        assert not any("docker" in e.lower() for e in entries)

    def test_write_skips_duplicates(self, consolidator):
        """write_facts does not add facts already in memory."""
        from tools.memory_tool import MemoryStore
        store = MemoryStore()
        store.add("memory", "User prefers vim", confidence=0.9)
        facts = [
            {"content": "User prefers vim", "confidence": 0.9},
        ]
        consolidator.write_facts("memory", facts, store=store)
        entries = store._entries_for("memory")
        # Should not duplicate
        vim_count = sum(1 for e in entries if "vim" in e.lower())
        assert vim_count <= 1
