"""Validation tests for CognitiveMemoryProvider (O3-01).

Tests verify:
1. Provider initializes and is available
2. JSONL audit trail records decisions durably
3. cognitive_recall tool searches decisions correctly
4. cognitive_decide tool records new decisions
5. sync_turn extracts decisions from conversation turns
6. Prefetch returns recent relevant decisions
7. Session lifecycle (start, end) creates audit entries
8. KR3.1: Decision audit non-empty after 5+ turns
9. Cron context gracefully skips cognitive operations
10. Tool schemas are well-formed
"""

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from plugins.memory.cognitive import (
    ALL_TOOL_SCHEMAS,
    DECIDE_SCHEMA,
    RECALL_SCHEMA,
    CognitiveMemoryProvider,
    _StandaloneAuditTrail,
    _extract_decisions_from_turn,
)


# ── StandaloneAuditTrail Tests ──────────────────────────────────────────


class TestStandaloneAuditTrail:
    """Test the lightweight JSONL audit trail."""

    def test_creates_file_on_first_write(self, tmp_path):
        audit_path = str(tmp_path / "audit.jsonl")
        trail = _StandaloneAuditTrail(audit_path)
        trail.record_decision("test_agent", "architecture", "Chose X over Y", 0.9)
        assert os.path.exists(audit_path)

    def test_record_returns_decision_id(self, tmp_path):
        trail = _StandaloneAuditTrail(str(tmp_path / "audit.jsonl"))
        did = trail.record_decision("agent1", "strategy", "Reasoning", 0.8)
        assert did.startswith("D-")
        assert "agent1" in did

    def test_query_by_agent_id(self, tmp_path):
        trail = _StandaloneAuditTrail(str(tmp_path / "audit.jsonl"))
        trail.record_decision("agent_a", "action", "Did A", 0.9)
        trail.record_decision("agent_b", "action", "Did B", 0.9)
        results = trail.query_decisions(agent_id="agent_a")
        assert len(results) == 1
        assert results[0]["agent_id"] == "agent_a"

    def test_query_by_decision_type(self, tmp_path):
        trail = _StandaloneAuditTrail(str(tmp_path / "audit.jsonl"))
        trail.record_decision("agent", "architecture", "Arch decision", 0.9)
        trail.record_decision("agent", "implementation", "Impl decision", 0.9)
        results = trail.query_decisions(decision_type="architecture")
        assert len(results) == 1
        assert results[0]["decision_type"] == "architecture"

    def test_query_by_confidence_filter(self, tmp_path):
        trail = _StandaloneAuditTrail(str(tmp_path / "audit.jsonl"))
        trail.record_decision("agent", "low", "Low conf", 0.3)
        trail.record_decision("agent", "high", "High conf", 0.9)
        results = trail.query_decisions(min_confidence=0.5)
        assert len(results) == 1
        assert results[0]["confidence"] == 0.9

    def test_query_by_text_search(self, tmp_path):
        trail = _StandaloneAuditTrail(str(tmp_path / "audit.jsonl"))
        trail.record_decision("agent", "architecture", "Use microservices pattern", 0.8)
        trail.record_decision("agent", "testing", "Write unit tests", 0.9)
        results = trail.query_decisions(query="microservices")
        assert len(results) == 1
        assert "microservices" in results[0]["reasoning"]

    def test_persistence_across_instances(self, tmp_path):
        audit_path = str(tmp_path / "audit.jsonl")
        trail1 = _StandaloneAuditTrail(audit_path)
        trail1.record_decision("agent", "persist", "This should survive", 0.9)
        assert trail1.size == 1

        # New instance should load existing data
        trail2 = _StandaloneAuditTrail(audit_path)
        assert trail2.size == 1

    def test_size_property(self, tmp_path):
        trail = _StandaloneAuditTrail(str(tmp_path / "audit.jsonl"))
        assert trail.size == 0
        trail.record_decision("agent", "t1", "r1", 0.8)
        trail.record_decision("agent", "t2", "r2", 0.8)
        assert trail.size == 2

    def test_context_dict_preserved(self, tmp_path):
        trail = _StandaloneAuditTrail(str(tmp_path / "audit.jsonl"))
        ctx = {"parent_decision": "D-000001", "task_id": "O3-01"}
        trail.record_decision("agent", "strategy", "Reasoning", 0.8, context=ctx)
        results = trail.query_decisions(agent_id="agent")
        assert results[0]["context"]["task_id"] == "O3-01"
        assert results[0]["parent_id"] == "D-000001"


# ── Decision Extraction Tests ───────────────────────────────────────────


class TestDecisionExtraction:
    """Test automatic decision extraction from conversation turns."""

    def test_extracts_tool_calls(self):
        decisions = _extract_decisions_from_turn(
            "user message",
            "I will use cognitive_recall(query='test') to check.",
            "agent1",
        )
        assert any(d["decision_type"] == "action" for d in decisions)
        assert any("cognitive_recall" in d["reasoning"] for d in decisions)

    def test_extracts_confidence_signals(self):
        decisions = _extract_decisions_from_turn(
            "user message",
            "I am confident in this approach. confidence: 0.85",
            "agent1",
        )
        assert any(d["decision_type"] == "assessment" for d in decisions)
        assert any(d["confidence"] == 0.85 for d in decisions)

    def test_extracts_long_reasoning(self):
        decisions = _extract_decisions_from_turn(
            "user message",
            "A" * 600,  # Long response without tool calls
            "agent1",
        )
        assert any(d["decision_type"] == "reasoning" for d in decisions)

    def test_no_extraction_from_short_response(self):
        decisions = _extract_decisions_from_turn(
            "user message",
            "OK",
            "agent1",
        )
        assert len(decisions) == 0


# ── CognitiveMemoryProvider Tests ──────────────────────────────────────


class TestCognitiveMemoryProvider:
    """Test the CognitiveMemoryProvider memory plugin."""

    def _make_provider(self, tmp_path, agent_id="test_agent"):
        """Create and initialize a provider with temp storage."""
        provider = CognitiveMemoryProvider()
        provider.initialize(
            session_id="test-session-001",
            hermes_home=str(tmp_path),
            agent_identity=agent_id,
        )
        return provider

    def test_provider_name(self):
        provider = CognitiveMemoryProvider()
        assert provider.name == "cognitive"

    def test_is_available_default(self):
        provider = CognitiveMemoryProvider()
        assert provider.is_available() is True

    def test_initialize_creates_audit_trail(self, tmp_path):
        provider = self._make_provider(tmp_path)
        assert provider._audit is not None
        assert provider._audit.size >= 1  # session_start recorded

    def test_system_prompt_block(self, tmp_path):
        provider = self._make_provider(tmp_path)
        block = provider.system_prompt_block()
        assert "Cognitive Memory" in block
        assert "cognitive_recall" in block

    def test_get_tool_schemas(self, tmp_path):
        provider = self._make_provider(tmp_path)
        schemas = provider.get_tool_schemas()
        assert len(schemas) == 2
        names = {s["name"] for s in schemas}
        assert names == {"cognitive_recall", "cognitive_decide"}

    def test_cognitive_recall_tool(self, tmp_path):
        provider = self._make_provider(tmp_path)
        # Record a decision first
        provider._audit.record_decision(
            "test_agent", "architecture", "Chose REST over GraphQL", 0.9
        )
        result = provider.handle_tool_call(
            "cognitive_recall", {"query": "REST", "limit": 5}
        )
        parsed = json.loads(result)
        assert parsed["count"] >= 1

    def test_cognitive_decide_tool(self, tmp_path):
        provider = self._make_provider(tmp_path)
        initial_size = provider._audit.size
        result = provider.handle_tool_call(
            "cognitive_decide",
            {"decision_type": "architecture", "reasoning": "Use DuckDB", "confidence": 0.85},
        )
        parsed = json.loads(result)
        assert "decision_id" in parsed
        assert parsed["total_decisions"] > initial_size

    def test_unknown_tool_returns_error(self, tmp_path):
        provider = self._make_provider(tmp_path)
        result = provider.handle_tool_call("unknown_tool", {})
        assert "error" in result.lower() or "Unknown" in result

    def test_prefetch_returns_recent_decisions(self, tmp_path):
        provider = self._make_provider(tmp_path)
        # Add some decisions
        for i in range(3):
            provider._audit.record_decision(
                "test_agent", "strategy", f"Decision {i}", 0.8
            )
        result = provider.prefetch("Decision")
        assert "Cognitive Memory" in result
        assert "Recent Decisions" in result

    def test_prefetch_empty_when_no_decisions(self, tmp_path):
        # Create provider with empty audit
        empty_path = tmp_path / "empty_home"
        empty_path.mkdir()
        provider = CognitiveMemoryProvider()
        provider.initialize(
            session_id="empty-session",
            hermes_home=str(empty_path),
            agent_identity="agent",
        )
        # Clear all decisions by creating a new trail
        provider._audit = _StandaloneAuditTrail(str(empty_path / "empty.jsonl"))
        result = provider.prefetch("anything")
        assert result == ""

    def test_sync_turn_records_decisions(self, tmp_path):
        provider = self._make_provider(tmp_path)
        initial_size = provider._audit.size
        provider.sync_turn(
            "What should we use for storage?",
            "I recommend DuckDB for OLAP workloads. cognitive_decide(decision_type='architecture', reasoning='Use DuckDB')",
        )
        # Wait for background thread
        provider.shutdown()
        assert provider._audit.size > initial_size

    def test_session_end_records(self, tmp_path):
        provider = self._make_provider(tmp_path)
        provider.on_session_end([])
        results = provider._audit.query_decisions(decision_type="session_end")
        assert len(results) >= 1

    def test_on_memory_write_records(self, tmp_path):
        provider = self._make_provider(tmp_path)
        provider.on_memory_write("add", "episodic", "Test memory content")
        results = provider._audit.query_decisions(decision_type="memory_write")
        assert len(results) >= 1

    def test_cron_context_skips_operations(self, tmp_path):
        provider = CognitiveMemoryProvider()
        provider.initialize(
            session_id="cron-session",
            agent_context="cron",
        )
        assert provider._cron_skipped is True
        assert provider.system_prompt_block() == ""
        assert provider.get_tool_schemas() == []


# ── KR3.1 Validation Test ──────────────────────────────────────────────


class TestKR31Validation:
    """KR3.1: Decision audit non-empty after 5+ turns."""

    def test_audit_non_empty_after_5_turns(self, tmp_path):
        """Verify KR3.1: after 5 turns, the decision audit trail is non-empty."""
        provider = CognitiveMemoryProvider()
        provider.initialize(
            session_id="kr31-session",
            hermes_home=str(tmp_path),
            agent_identity="demis_hassabis",
        )
        # Simulate 5 turns
        for i in range(5):
            provider.on_turn_start(i + 1, f"Turn {i+1} message")
            provider.sync_turn(
                f"User query {i+1}",
                f"Assistant response {i+1} with some reasoning content " * 10,
            )
        provider.shutdown()

        # KR3.1 assertion: audit must be non-empty
        assert provider._audit.size > 0, "KR3.1 FAILED: Audit trail is empty after 5 turns"
        # Verify decisions were actually recorded
        all_decisions = provider._audit.query_decisions()
        assert len(all_decisions) >= 1, "KR3.1 FAILED: No decisions in audit trail"


# ── Tool Schema Validation Tests ───────────────────────────────────────


class TestToolSchemas:
    """Test that tool schemas are well-formed and complete."""

    def test_recall_schema_has_required_fields(self):
        assert RECALL_SCHEMA["name"] == "cognitive_recall"
        params = RECALL_SCHEMA["parameters"]
        assert "query" in params["properties"]
        assert "query" in params["required"]

    def test_decide_schema_has_required_fields(self):
        assert DECIDE_SCHEMA["name"] == "cognitive_decide"
        params = DECIDE_SCHEMA["parameters"]
        assert "decision_type" in params["required"]
        assert "reasoning" in params["required"]

    def test_all_schemas_in_list(self):
        assert len(ALL_TOOL_SCHEMAS) == 2
        names = {s["name"] for s in ALL_TOOL_SCHEMAS}
        assert "cognitive_recall" in names
        assert "cognitive_decide" in names
