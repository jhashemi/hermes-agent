"""Tests for memory confidence scoring and conflict detection (Phase 3)."""

import json
import os
import pytest

from tools.memory_tool import MemoryStore


# Use pytest fixtures like the existing tests — monkeypatch get_memory_dir

@pytest.fixture
def store(tmp_path, monkeypatch):
    """Create a MemoryStore with an isolated temp directory."""
    monkeypatch.setattr("tools.memory_tool.get_memory_dir", lambda: tmp_path)
    return MemoryStore()


class TestConfidenceScoring:
    """Tests for confidence field on memory entries."""

    def test_add_with_confidence(self, store):
        """add() accepts an optional confidence score (0.0-1.0)."""
        result = store.add("memory", "User prefers vim", confidence=0.9)
        assert result["success"] is True
        entries = store._entries_for("memory")
        assert entries
        assert entries[-1] == "User prefers vim"

    def test_add_confidence_defaults_to_1(self, store):
        """add() without confidence defaults to 1.0 (certain)."""
        result = store.add("memory", "Project uses pytest")
        assert result["success"] is True
        meta = store.get_confidence("memory", "Project uses pytest")
        assert meta == 1.0

    def test_add_confidence_clamped(self, store):
        """Confidence values outside [0,1] are clamped."""
        result = store.add("memory", "Risky fact", confidence=1.5)
        assert result["success"] is True
        assert store.get_confidence("memory", "Risky fact") == 1.0

        result2 = store.add("memory", "Negative conf", confidence=-0.1)
        assert result2["success"] is True
        assert store.get_confidence("memory", "Negative conf") == 0.0

    def test_add_low_confidence_warns_in_response(self, store):
        """Adding with confidence < 0.5 includes a warning."""
        result = store.add("memory", "Maybe true", confidence=0.3)
        assert result["success"] is True
        assert "low confidence" in result.get("message", "").lower()

    def test_get_confidence_unknown_entry(self, store):
        """get_confidence for non-existent entry returns None."""
        assert store.get_confidence("memory", "nonexistent") is None


class TestConflictDetection:
    """Tests for conflict detection when adding memory entries."""

    def test_add_conflicting_fact_warns(self, store):
        """Adding a fact that contradicts an existing one triggers a warning."""
        store.add("memory", "User prefers vim", confidence=0.9)
        result = store.add("memory", "User prefers emacs", confidence=0.8)
        assert result["success"] is True  # still added
        assert "conflict" in result.get("message", "").lower() or "contradicts" in result.get("message", "").lower()

    def test_add_non_conflicting_fact_no_warning(self, store):
        """Adding a non-conflicting fact produces no conflict warning."""
        store.add("memory", "Project uses pytest", confidence=0.9)
        result = store.add("memory", "User prefers vim", confidence=0.8)
        assert result["success"] is True
        msg = result.get("message", "").lower()
        assert "conflict" not in msg and "contradicts" not in msg

    def test_replace_with_conflicting_fact_warns(self, store):
        """Replacing with contradictory content — conflict check exists."""
        store.add("memory", "User prefers vim", confidence=0.9)
        store.add("memory", "Deploy with k8s", confidence=0.9)
        result = store.replace("memory", "Deploy with k8s", "Deploy with docker compose")
        assert result["success"] is True


class TestConfidenceFormatForPrompt:
    """Confidence info should surface in rendered memory blocks."""

    def test_low_confidence_entries_marked(self, store):
        """Entries with confidence < 0.7 should be marked as uncertain."""
        store.add("memory", "Certain fact", confidence=1.0)
        store.add("memory", "Uncertain fact", confidence=0.4)
        text = store._render_block("memory", store._entries_for("memory"))
        assert "uncertain" in text.lower()
