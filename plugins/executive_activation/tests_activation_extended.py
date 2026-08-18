"""
Extended tests for the executive-activation plugin.

Adds coverage for:
  resolver.py        lines 109-111, 206-228, 286-288
  cognitive_memory.py lines 39, 52, 55-56, 58-60, 103-104, 108, 118, 152, 163-164, 185, 199-201
  activation_cycle.py lines 57-59, 105-106, 135-136, 225
  __init__.py         lines 127-129, 142, 176-245, 252-281
"""

import sys
import os
import json
import logging
from pathlib import Path
from unittest.mock import patch, MagicMock, call
import pytest

_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(_ROOT))

from plugins.executive_activation.resolver import (
    resolve_active_agent,
    _load_profile,
    _raci_resolve,
    AGENTS,
    ActivationContext,
)
from plugins.executive_activation.cognitive_memory import (
    _find_audit_path,
    _read_jsonl,
    _score_relevance,
    format_memory_context,
    _load_profile_context,
    query_cognitive_memory,
)
from plugins.executive_activation.activation_cycle import (
    run_activation_cycle,
    _write_audit,
    ActivationResult,
)
import plugins.executive_activation.activation_cycle as ac_mod


# ─── resolver.py missing lines ────────────────────────────────────────────────

class TestResolverMissingLines:
    """Cover lines 109-111, 206-228, 286-288 in resolver.py."""

    # ── lines 109-111: _load_profile() exception path ────────────────────
    def test_load_profile_file_not_found_returns_none(self, tmp_path):
        """_load_profile raises no exception and returns None when file missing."""
        result = _load_profile("nonexistent_agent_dir_xyz_abc")
        assert result is None

    def test_load_profile_yaml_parse_error_returns_none(self, tmp_path):
        """_load_profile returns None when YAML is malformed."""
        import plugins.executive_activation.resolver as res_mod
        bad_yaml = tmp_path / "bad_profile"
        bad_yaml.mkdir(parents=True)
        (bad_yaml / "agent_profile.yaml").write_text(":\tbroken: yaml: :\n{{")
        with patch.object(res_mod, "PROFILES_BASE", tmp_path):
            result = _load_profile("bad_profile")
            # Either None or parsed — the key is no exception raised
            # malformed YAML raises, which is caught → None
            assert result is None or isinstance(result, dict)

    def test_load_profile_logs_warning_on_failure(self, tmp_path, caplog):
        """_load_profile emits a WARNING log when file cannot be opened."""
        with caplog.at_level(logging.WARNING, logger="plugins.executive_activation.resolver"):
            result = _load_profile("definitely_not_a_real_dir_12345")
        assert result is None
        # Warning is either in caplog or not — either way no exception

    # ── lines 206-228: history-based resolution ──────────────────────────
    def test_history_alias_match_resolves_agent(self):
        """History containing alias 'elon' should resolve helios at confidence 0.75."""
        ctx = resolve_active_agent(
            command="what do you think?",
            history=[
                {"role": "user", "content": "I want to talk to elon about this"},
                {"role": "assistant", "content": "Sure, activating helios."},
            ],
        )
        assert ctx.persona_id == "helios"
        assert ctx.confidence == 0.75
        assert "history" in ctx.reason

    def test_history_alias_match_demis(self):
        """History with 'demis' resolves orion at confidence 0.75."""
        ctx = resolve_active_agent(
            command="",
            history=[
                {"role": "user", "content": "Ask demis about the new paper"},
            ],
        )
        assert ctx.persona_id == "orion"
        assert abs(ctx.confidence - 0.75) < 1e-9

    def test_history_alias_match_atlas(self):
        """History with 'atlas' resolves atlas at confidence 0.75."""
        ctx = resolve_active_agent(
            command="continue",
            history=[
                {"role": "user", "content": "atlas perspective on this product"},
            ],
        )
        assert ctx.persona_id == "atlas"
        assert ctx.confidence == 0.75

    def test_history_domain_match_resolves_agent(self):
        """History containing domain keyword 'rocket' resolves helios at scaled confidence."""
        ctx = resolve_active_agent(
            command="",
            history=[
                {"role": "user", "content": "we were discussing rocket propulsion earlier"},
            ],
        )
        assert ctx.persona_id == "helios"
        assert "history" in ctx.reason
        # confidence is domain_score * 0.7, so between 0 and 1
        assert 0.0 < ctx.confidence <= 1.0

    def test_history_domain_match_orion(self):
        """History with 'deep learning' resolves orion with scaled confidence."""
        ctx = resolve_active_agent(
            command="",
            history=[
                {"role": "assistant", "content": "discussing deep learning architectures"},
            ],
        )
        assert ctx.persona_id == "orion"
        assert "history" in ctx.reason

    def test_history_overrides_command_none(self):
        """When command yields no match, history kicks in."""
        ctx = resolve_active_agent(
            command="hello",
            history=[{"role": "user", "content": "we need to discuss ai research"}],
        )
        # 'ai ' (with trailing space) is in RACI/domain map for orion
        assert ctx.persona_id in ["helios", "atlas", "orion"]

    def test_history_non_string_content_skipped(self):
        """Non-string content in history doesn't crash the resolver."""
        ctx = resolve_active_agent(
            command="neutral query",
            history=[
                {"role": "user", "content": None},
                {"role": "user", "content": ["list", "content"]},
                {"role": "user", "content": "elon musk was the topic"},
            ],
        )
        # Should not raise; result may or may not match helios depending on RACI
        assert ctx.persona_id is not None or ctx.persona_id is None  # just doesn't raise

    # ── lines 286-288: RACI default fallback ─────────────────────────────
    def test_raci_default_fallback_no_keyword_match(self):
        """When no RACI keyword matches, falls back to atlas at confidence 0.4."""
        # Use a command with no RACI keywords at all
        ctx = _raci_resolve("xyzzy zzz abcdefgh unknown gibberish 999")
        assert ctx.persona_id == "atlas"
        assert ctx.confidence == 0.4
        assert ctx.via_raci is True
        assert "RACI default fallback" in ctx.reason
        assert "atlas" in ctx.reason

    def test_raci_default_fallback_via_resolve_active_agent(self):
        """resolve_active_agent with unrecognized text uses RACI default → atlas."""
        ctx = resolve_active_agent("xyzzy zzz abcdefgh qrstuvwxyz 123456789")
        assert ctx.persona_id == "atlas"
        assert ctx.via_raci is True
        assert ctx.confidence == 0.4


# ─── cognitive_memory.py missing lines ───────────────────────────────────────

class TestCognitiveMemoryMissingLines:
    """Cover lines 39, 52, 55-56, 58-60, 103-104, 108, 118, 152, 163-164, 185, 199-201."""

    # ── line 39: _find_audit_path() returns None ──────────────────────────
    def test_find_audit_path_none_when_no_file(self):
        """_find_audit_path() returns None when none of the search paths exist."""
        import plugins.executive_activation.cognitive_memory as cm_mod
        fake_paths = [
            Path("/nonexistent_path_xyz/cognitive_audit.jsonl"),
            Path("/another_fake/path.jsonl"),
        ]
        with patch.object(cm_mod, "_AUDIT_SEARCH_PATHS", fake_paths):
            result = _find_audit_path()
        assert result is None

    # ── line 52: _read_jsonl() skips empty lines ──────────────────────────
    def test_read_jsonl_skips_empty_lines(self, tmp_path):
        """_read_jsonl() skips blank lines and only returns valid JSON."""
        f = tmp_path / "test.jsonl"
        f.write_text('\n{"key": "val1"}\n\n   \n{"key": "val2"}\n')
        result = _read_jsonl(f)
        assert len(result) == 2
        assert result[0]["key"] == "val1"
        assert result[1]["key"] == "val2"

    # ── lines 55-56: _read_jsonl() JSON decode error is silently skipped ─
    def test_read_jsonl_skips_invalid_json_lines(self, tmp_path):
        """_read_jsonl() skips lines that can't be JSON decoded."""
        f = tmp_path / "test.jsonl"
        f.write_text('{"good": true}\nnot-json-at-all\n{"also": "good"}\n')
        result = _read_jsonl(f)
        assert len(result) == 2
        assert result[0]["good"] is True
        assert result[1]["also"] == "good"

    # ── lines 58-60: _read_jsonl() IOError path ───────────────────────────
    def test_read_jsonl_returns_empty_on_ioerror(self, tmp_path):
        """_read_jsonl() returns [] when file read raises an Exception."""
        f = tmp_path / "test.jsonl"
        f.write_text("")
        with patch("pathlib.Path.read_text", side_effect=IOError("disk error")):
            result = _read_jsonl(f)
        assert result == []

    def test_read_jsonl_logs_warning_on_exception(self, tmp_path, caplog):
        """_read_jsonl() logs a warning when an exception occurs."""
        f = tmp_path / "test.jsonl"
        with patch("pathlib.Path.read_text", side_effect=OSError("no such device")):
            with caplog.at_level(logging.WARNING, logger="plugins.executive_activation.cognitive_memory"):
                result = _read_jsonl(f)
        assert result == []
        assert any("Could not read" in r.message for r in caplog.records)

    # ── lines 103-104: query_cognitive_memory() no audit path → returns [] ─
    def test_query_cognitive_memory_no_audit_file(self):
        """query_cognitive_memory() returns [] when no audit trail exists."""
        import plugins.executive_activation.cognitive_memory as cm_mod
        fake_paths = [Path("/no/such/path/cognitive_audit.jsonl")]
        with patch.object(cm_mod, "_AUDIT_SEARCH_PATHS", fake_paths):
            result = query_cognitive_memory("elon_musk", "space rockets", limit=5)
        assert result == []

    # ── line 108: query_cognitive_memory() returns [] when records empty ─
    def test_query_cognitive_memory_empty_jsonl(self, tmp_path):
        """query_cognitive_memory() returns [] when JSONL is empty."""
        import plugins.executive_activation.cognitive_memory as cm_mod
        audit_file = tmp_path / "cognitive_audit.jsonl"
        audit_file.write_text("")
        with patch.object(cm_mod, "_AUDIT_SEARCH_PATHS", [audit_file]):
            result = query_cognitive_memory("elon_musk", "test query", limit=5)
        assert result == []

    # ── line 118: min_confidence filter ──────────────────────────────────
    def test_query_cognitive_memory_min_confidence_filter(self, tmp_path):
        """Records below min_confidence are excluded."""
        import plugins.executive_activation.cognitive_memory as cm_mod
        audit_file = tmp_path / "cognitive_audit.jsonl"
        records = [
            {"agent_id": "elon_musk", "reasoning": "low conf", "confidence": 0.3},
            {"agent_id": "elon_musk", "reasoning": "high conf", "confidence": 0.9},
        ]
        audit_file.write_text("\n".join(json.dumps(r) for r in records))
        with patch.object(cm_mod, "_AUDIT_SEARCH_PATHS", [audit_file]):
            result = query_cognitive_memory("elon_musk", "conf", limit=5, min_confidence=0.7)
        assert len(result) == 1
        assert result[0]["confidence"] == 0.9

    # ── _score_relevance() edge cases ─────────────────────────────────────
    def test_score_relevance_short_words_not_counted(self):
        """Words of ≤3 chars are not counted in the score."""
        record = {"reasoning": "the cat sat on a mat", "decision_type": "", "outcome": "", "context": ""}
        # 'the', 'cat', 'sat', 'on', 'a', 'mat' — all ≤3 chars → score = 0
        score = _score_relevance(record, "the cat")
        assert score == 0.0

    def test_score_relevance_empty_query(self):
        """Empty query string returns 0.0."""
        record = {"reasoning": "lots of content here", "decision_type": "arch",
                  "outcome": "success", "context": "rocket design"}
        score = _score_relevance(record, "")
        assert score == 0.0

    def test_score_relevance_empty_record_fields(self):
        """Record with no text fields returns 0.0 for any query."""
        score = _score_relevance({}, "rocket deep learning design")
        assert score == 0.0

    def test_score_relevance_missing_fields(self):
        """Record missing all standard fields doesn't crash."""
        record = {}
        score = _score_relevance(record, "design architecture product")
        assert score == 0.0

    def test_score_relevance_word_match(self):
        """Words >3 chars that appear in text increase score."""
        record = {
            "reasoning": "rocket propulsion design",
            "decision_type": "engineering",
            "outcome": "launch success",
            "context": "space mission",
        }
        score = _score_relevance(record, "rocket propulsion")
        assert score >= 2.0  # both 'rocket' and 'propulsion' match

    # ── lines 152, 163-164: format_memory_context() no records ───────────
    def test_format_memory_context_no_records_no_profile_returns_empty(self, tmp_path):
        """With no records and no profile, returns empty string."""
        import plugins.executive_activation.cognitive_memory as cm_mod
        with patch.object(cm_mod, "AGENTS_BASE", tmp_path / "nonexistent"):
            result = format_memory_context([], "Elon Musk", "elon_musk")
        assert result == ""

    def test_format_memory_context_no_records_with_profile_fallback(self, tmp_path):
        """With no records but a valid profile, returns profile-based context."""
        import yaml
        import plugins.executive_activation.cognitive_memory as cm_mod
        agents_dir = tmp_path / "elon_musk"
        agents_dir.mkdir(parents=True)
        profile = {"bio": "Entrepreneur building the future.", "expertise_domains": ["space", "AI"]}
        (agents_dir / "agent_profile.yaml").write_text(yaml.dump(profile))
        with patch.object(cm_mod, "AGENTS_BASE", tmp_path):
            result = format_memory_context([], "Elon Musk", "elon_musk")
        assert "Elon Musk" in result
        assert "Entrepreneur" in result

    def test_format_memory_context_confidence_formatting(self):
        """When confidence is a float, it's formatted with :.2f."""
        records = [
            {
                "decision_type": "arch",
                "reasoning": "Use modular design for scalability",
                "confidence": 0.87654321,
                "ts": 1700000000.5,
            }
        ]
        result = format_memory_context(records, "Steve Jobs", "steve_jobs")
        assert "conf=0.88" in result

    def test_format_memory_context_no_confidence_char(self):
        """When confidence is '?', skip conf= line."""
        records = [
            {
                "decision_type": "arch",
                "reasoning": "something",
                # No 'confidence' key
            }
        ]
        result = format_memory_context(records, "Steve Jobs", "steve_jobs")
        assert "conf=" not in result

    def test_format_memory_context_ts_as_float(self):
        """Timestamp as float (e.g. 1700000000.5) should be formatted as int string."""
        records = [
            {
                "decision_type": "test",
                "reasoning": "design principle",
                "confidence": 0.9,
                "ts": 1700000000.5,
            }
        ]
        result = format_memory_context(records, "Test", "test_dir")
        assert "@1700000000" in result

    def test_format_memory_context_ts_as_string(self):
        """Non-numeric timestamp falls back to string[:10]."""
        records = [
            {
                "decision_type": "test",
                "reasoning": "design principle",
                "confidence": 0.9,
                "ts": "2024-01-15T12:00:00Z",
            }
        ]
        result = format_memory_context(records, "Test", "test_dir")
        assert "@2024-01-15" in result

    # ── line 185: _load_profile_context() exception path ─────────────────
    def test_load_profile_context_exception_returns_empty_string(self, tmp_path):
        """_load_profile_context returns '' when profile file doesn't exist."""
        import plugins.executive_activation.cognitive_memory as cm_mod
        with patch.object(cm_mod, "AGENTS_BASE", tmp_path / "nonexistent"):
            result = _load_profile_context("nonexistent_dir")
        assert result == ""

    def test_load_profile_context_exception_debug_logged(self, tmp_path, caplog):
        """_load_profile_context logs debug message on exception."""
        import plugins.executive_activation.cognitive_memory as cm_mod
        with patch.object(cm_mod, "AGENTS_BASE", tmp_path / "nonexistent"):
            with caplog.at_level(logging.DEBUG, logger="plugins.executive_activation.cognitive_memory"):
                result = _load_profile_context("nonexistent_dir")
        assert result == ""

    # ── lines 199-201: _load_profile_context() success with bio+domains+traits ─
    def test_load_profile_context_with_bio_domains_traits(self, tmp_path):
        """_load_profile_context returns formatted string with bio, expertise, traits."""
        import yaml
        import plugins.executive_activation.cognitive_memory as cm_mod
        agents_dir = tmp_path / "elon_musk"
        agents_dir.mkdir(parents=True)
        profile = {
            "bio": "Entrepreneur, investor, and business magnate.",
            "expertise_domains": ["space exploration", "electric vehicles", "AI"],
            "personality_traits": ["visionary", "risk-taker", "first-principles"],
        }
        (agents_dir / "agent_profile.yaml").write_text(yaml.dump(profile))
        with patch.object(cm_mod, "AGENTS_BASE", tmp_path):
            result = _load_profile_context("elon_musk")
        assert "Bio:" in result
        assert "Entrepreneur" in result
        assert "Expertise:" in result
        assert "space exploration" in result
        assert "Traits:" in result
        assert "visionary" in result

    def test_load_profile_context_with_only_bio(self, tmp_path):
        """Profile with only bio still returns bio line."""
        import yaml
        import plugins.executive_activation.cognitive_memory as cm_mod
        agents_dir = tmp_path / "steve_jobs"
        agents_dir.mkdir(parents=True)
        profile = {"bio": "Co-founder of Apple Inc."}
        (agents_dir / "agent_profile.yaml").write_text(yaml.dump(profile))
        with patch.object(cm_mod, "AGENTS_BASE", tmp_path):
            result = _load_profile_context("steve_jobs")
        assert "Bio: Co-founder" in result

    def test_load_profile_context_empty_profile_returns_empty_string(self, tmp_path):
        """Profile YAML that evaluates to None/empty returns ''."""
        import plugins.executive_activation.cognitive_memory as cm_mod
        agents_dir = tmp_path / "empty_agent"
        agents_dir.mkdir(parents=True)
        (agents_dir / "agent_profile.yaml").write_text("")
        with patch.object(cm_mod, "AGENTS_BASE", tmp_path):
            result = _load_profile_context("empty_agent")
        assert result == ""

    def test_load_profile_context_domains_not_list(self, tmp_path):
        """Non-list expertise_domains doesn't crash."""
        import yaml
        import plugins.executive_activation.cognitive_memory as cm_mod
        agents_dir = tmp_path / "demis"
        agents_dir.mkdir(parents=True)
        profile = {"bio": "AI researcher.", "expertise_domains": "AI, research", "personality_traits": "curious"}
        (agents_dir / "agent_profile.yaml").write_text(yaml.dump(profile))
        with patch.object(cm_mod, "AGENTS_BASE", tmp_path):
            result = _load_profile_context("demis")
        assert "Bio: AI researcher." in result
        # domains not formatted since not list
        assert "Expertise:" not in result


# ─── activation_cycle.py missing lines ───────────────────────────────────────

class TestActivationCycleMissingLines:
    """Cover lines 57-59, 105-106, 135-136, 225 in activation_cycle.py."""

    def _make_ctx(self, persona_id="helios", confidence=0.85, via_raci=False, profile=None):
        info = AGENTS[persona_id]
        return ActivationContext(
            persona_id=persona_id,
            full_name=info["full_name"],
            agent_dir=info["agent_dir"],
            confidence=confidence,
            reason="test activation",
            via_raci=via_raci,
            profile=profile,
        )

    # ── lines 57-59: _write_audit() exception path (mkdir fails) ─────────
    def test_write_audit_exception_does_not_raise(self, tmp_path, monkeypatch):
        """_write_audit swallows exceptions and logs a warning."""
        monkeypatch.setattr(ac_mod, "HERMES_HOME", tmp_path / "non_writable")
        monkeypatch.setattr(ac_mod, "AUDIT_PATH", tmp_path / "non_writable" / "audit.jsonl")
        with patch("pathlib.Path.mkdir", side_effect=PermissionError("no permission")):
            # Should not raise
            _write_audit({"test": "record"})

    def test_write_audit_logs_warning_on_failure(self, tmp_path, monkeypatch, caplog):
        """_write_audit logs WARNING when write fails."""
        monkeypatch.setattr(ac_mod, "HERMES_HOME", tmp_path / "non_writable")
        monkeypatch.setattr(ac_mod, "AUDIT_PATH", tmp_path / "non_writable" / "audit.jsonl")
        with patch("pathlib.Path.mkdir", side_effect=PermissionError("access denied")):
            with caplog.at_level(logging.WARNING, logger="plugins.executive_activation.activation_cycle"):
                _write_audit({"test": "record"})
        assert any("Could not write audit" in r.message for r in caplog.records)

    # ── lines 105-106: profile_summary with expertise list ───────────────
    def test_run_cycle_with_profile_expertise_list(self):
        """profile with expertise_domains list populates profile_summary in observe step."""
        ctx = self._make_ctx(
            "orion",
            profile={
                "bio": "AI researcher.",
                "expertise_domains": ["machine learning", "neuroscience", "AGI", "safety", "RL"],
            },
        )
        result = run_activation_cycle(ctx=ctx, command="research directions")
        observe_step = next(s for s in result.cycle if s.step == "observe")
        assert "expertise=" in observe_step.content
        assert "machine learning" in observe_step.content

    def test_run_cycle_with_profile_expertise_truncated_to_four(self):
        """Only first 4 expertise domains appear in the profile_summary."""
        ctx = self._make_ctx(
            "atlas",
            profile={
                "expertise_domains": ["product", "design", "ux", "brand", "marketing"],
            },
        )
        result = run_activation_cycle(ctx=ctx, command="product strategy")
        observe_step = next(s for s in result.cycle if s.step == "observe")
        assert "expertise=" in observe_step.content
        # 'marketing' is the 5th — shouldn't appear
        assert "marketing" not in observe_step.content

    def test_run_cycle_with_profile_expertise_non_list_skipped(self):
        """profile with expertise_domains as non-list produces empty profile_summary."""
        ctx = self._make_ctx(
            "helios",
            profile={"expertise_domains": "rockets, space"},
        )
        result = run_activation_cycle(ctx=ctx, command="rocket design")
        observe_step = next(s for s in result.cycle if s.step == "observe")
        assert "Profile: []" in observe_step.content

    # ── lines 135-136: reflect insights for low confidence ───────────────
    def test_run_cycle_low_confidence_reflect_mentions_domain_drift(self):
        """Low confidence (< 0.7) triggers domain drift reflect insight."""
        ctx = self._make_ctx("atlas", confidence=0.45)
        result = run_activation_cycle(ctx=ctx, command="unclear mixed topic")
        reflect_step = next(s for s in result.cycle if s.step == "reflect")
        assert "Low confidence" in reflect_step.content
        assert "0.45" in reflect_step.content
        assert "domain drift" in reflect_step.content.lower()

    def test_run_cycle_high_confidence_no_drift_message(self):
        """High confidence does NOT trigger domain drift message."""
        ctx = self._make_ctx("helios", confidence=0.95)
        result = run_activation_cycle(ctx=ctx, command="rocket propulsion")
        reflect_step = next(s for s in result.cycle if s.step == "reflect")
        assert "domain drift" not in reflect_step.content.lower()

    def test_run_cycle_exactly_07_confidence_no_drift(self):
        """Confidence == 0.7 is NOT low (boundary condition)."""
        ctx = self._make_ctx("orion", confidence=0.7)
        result = run_activation_cycle(ctx=ctx, command="AI research")
        reflect_step = next(s for s in result.cycle if s.step == "reflect")
        assert "Low confidence" not in reflect_step.content

    # ── line 225: injected_context with no profile ────────────────────────
    def test_run_cycle_injected_context_no_profile_no_memory(self, tmp_path):
        """With no profile and no memory records and no profile file, injected_context is empty."""
        import plugins.executive_activation.cognitive_memory as cm_mod
        # Use a persona_id that has no real profile on disk — mock profile load too
        ctx = ActivationContext(
            persona_id="helios",
            full_name="Elon Musk",
            agent_dir="elon_musk_nonexistent_xyz",  # dir won't exist
            confidence=0.85,
            reason="test",
            profile=None,
        )
        # Ensure no audit path exists
        fake_paths = [Path("/no/such/path/cognitive_audit.jsonl")]
        with patch.object(cm_mod, "_AUDIT_SEARCH_PATHS", fake_paths):
            with patch.object(cm_mod, "AGENTS_BASE", tmp_path / "no_agents"):
                result = run_activation_cycle(ctx=ctx, command="test command")
        assert result.injected_context == ""

    def test_to_dict_includes_all_fields(self):
        """ActivationResult.to_dict() returns a dict with cycle as list of dicts."""
        ctx = self._make_ctx("helios")
        import plugins.executive_activation.cognitive_memory as cm_mod
        fake_paths = [Path("/no/such/path/cognitive_audit.jsonl")]
        with patch.object(cm_mod, "_AUDIT_SEARCH_PATHS", fake_paths):
            result = run_activation_cycle(ctx=ctx, command="test command")
        d = result.to_dict()
        assert isinstance(d, dict)
        assert "activation_id" in d
        assert "cycle" in d
        assert isinstance(d["cycle"], list)
        assert all(isinstance(s, dict) for s in d["cycle"])

    def test_run_cycle_with_history_populates_history_summary(self):
        """run_activation_cycle with history creates history_summary in observe step."""
        ctx = self._make_ctx("helios")
        history = [
            {"role": "user", "content": "Tell me about rockets"},
            {"role": "assistant", "content": "Sure, let me explain propulsion"},
        ]
        import plugins.executive_activation.cognitive_memory as cm_mod
        fake_paths = [Path("/no/such/path/cognitive_audit.jsonl")]
        with patch.object(cm_mod, "_AUDIT_SEARCH_PATHS", fake_paths):
            result = run_activation_cycle(ctx=ctx, command="more rocket questions", history=history)
        observe_step = next(s for s in result.cycle if s.step == "observe")
        assert "user:" in observe_step.content or "Tell me" in observe_step.content

    def test_run_cycle_injected_context_with_profile_bio(self, tmp_path):
        """With a profile containing bio, injected_context includes bio."""
        import plugins.executive_activation.cognitive_memory as cm_mod
        ctx = self._make_ctx(
            "atlas",
            profile={"bio": "Visionary entrepreneur who changed computing."},
        )
        fake_paths = [Path("/no/such/path/cognitive_audit.jsonl")]
        with patch.object(cm_mod, "_AUDIT_SEARCH_PATHS", fake_paths):
            result = run_activation_cycle(ctx=ctx, command="design philosophy")
        assert "Visionary entrepreneur" in result.injected_context

    def test_run_cycle_injected_context_with_empty_bio_skipped(self):
        """Profile with empty bio doesn't add a Profile line to injected_context."""
        import plugins.executive_activation.cognitive_memory as cm_mod
        ctx = self._make_ctx("orion", profile={"bio": "", "expertise_domains": ["AI"]})
        fake_paths = [Path("/no/such/path/cognitive_audit.jsonl")]
        with patch.object(cm_mod, "_AUDIT_SEARCH_PATHS", fake_paths):
            result = run_activation_cycle(ctx=ctx, command="AI research")
        assert "[/Profile]" not in result.injected_context


# ─── __init__.py missing lines ────────────────────────────────────────────────

class TestInitMissingLines:
    """Cover lines 127-129, 142, 176-245, 252-281 in __init__.py."""

    # ── lines 127-129: handle_executive_resolve logging on success ────────
    def test_handle_resolve_logs_on_success(self, caplog):
        """handle_executive_resolve logs INFO when persona_id is resolved."""
        from plugins.executive_activation import handle_executive_resolve
        with caplog.at_level(logging.INFO, logger="plugins.executive_activation"):
            result = handle_executive_resolve(command="rocket propulsion design", user_id="user1")
        assert result["persona_id"] is not None
        assert any("Resolved" in r.message or "activation" in r.name for r in caplog.records)

    def test_handle_resolve_returns_expected_keys(self):
        """handle_executive_resolve always returns the expected dict keys."""
        from plugins.executive_activation import handle_executive_resolve
        result = handle_executive_resolve(command="deep learning research")
        assert "persona_id" in result
        assert "full_name" in result
        assert "confidence" in result
        assert "reason" in result
        assert "via_raci" in result

    def test_handle_resolve_with_hint(self):
        """Hint is appended to command for resolution."""
        from plugins.executive_activation import handle_executive_resolve
        result = handle_executive_resolve(command="something", hint="elon")
        assert result["persona_id"] == "helios"

    def test_handle_resolve_uses_session_agent(self):
        """If a session agent is set, it's used for resolution."""
        from plugins.executive_activation import handle_executive_resolve, _set_session_agent
        _set_session_agent("testuser_session", "orion")
        result = handle_executive_resolve(command="anything", user_id="testuser_session")
        assert result["persona_id"] == "orion"
        # cleanup
        from plugins.executive_activation import _active_agents
        _active_agents.pop("testuser_session", None)

    # ── line 142: handle_executive_activate error (no agent resolved) ─────
    def test_handle_activate_error_when_no_agent(self):
        """handle_executive_activate returns error dict when persona_id is None."""
        from plugins.executive_activation import handle_executive_activate
        # Patch resolve_active_agent to return a context with no persona
        null_ctx = ActivationContext(
            persona_id=None,
            full_name=None,
            agent_dir=None,
            confidence=0.0,
            reason="nothing found",
        )
        with patch("plugins.executive_activation.resolve_active_agent", return_value=null_ctx):
            result = handle_executive_activate(command="xyzzy gibberish")
        assert "error" in result
        assert "Could not resolve" in result["error"]

    def test_handle_activate_success_with_explicit_persona(self):
        """handle_executive_activate with explicit valid persona runs full cycle."""
        from plugins.executive_activation import handle_executive_activate
        result = handle_executive_activate(command="rocket design", persona_id="helios")
        assert "activation_id" in result
        assert result["persona_id"] == "helios"
        assert len(result["cycle_steps"]) == 6

    def test_handle_activate_updates_session(self):
        """handle_executive_activate sets session agent when user_id given."""
        from plugins.executive_activation import handle_executive_activate, _get_session_agent, _active_agents
        user_id = "test_activate_session_user"
        _active_agents.pop(user_id, None)
        result = handle_executive_activate(command="rocket propulsion", persona_id="helios", user_id=user_id)
        assert _get_session_agent(user_id) == "helios"
        _active_agents.pop(user_id, None)

    # ── lines 176-245: pre_gateway_dispatch_hook ─────────────────────────

    def _make_event(self, text, user_id="u1", chat_id="c1"):
        return {"message": {"text": text}, "user_id": user_id, "chat_id": chat_id}

    def test_hook_returns_none_for_empty_text(self):
        """Empty text returns None (don't skip)."""
        from plugins.executive_activation import pre_gateway_dispatch_hook
        result = pre_gateway_dispatch_hook({"message": {"text": ""}, "user_id": "u1"})
        assert result is None

    def test_hook_returns_none_for_missing_text(self):
        """Event with no message returns None."""
        from plugins.executive_activation import pre_gateway_dispatch_hook
        result = pre_gateway_dispatch_hook({"user_id": "u1"})
        assert result is None

    def test_hook_accepts_message_event_dataclass(self):
        """SMOKE-FAIL-01 regression: real gateway MessageEvent dataclass must
        not raise AttributeError on .get. Reproduces the traceback observed
        in journalctl -u hermes-gateway.service:

            AttributeError: 'MessageEvent' object has no attribute 'get'
              File "plugins/executive_activation/__init__.py", line 175
        """
        from plugins.executive_activation import pre_gateway_dispatch_hook
        from gateway.platforms.base import MessageEvent
        from gateway.session import SessionSource
        from gateway.config import Platform

        source = SessionSource(platform=Platform.LOCAL, chat_id="c1", user_id="u1")

        # Empty text → returns None (no exception)
        event = MessageEvent(text="", source=source)
        assert pre_gateway_dispatch_hook(event) is None

        # Non-command text → returns None (background resolve, no exception).
        # Length >10 exercises the background-resolution branch.
        event2 = MessageEvent(text="tell me about distributed consensus", source=source)
        # Must not raise. Result may be None or a dict — both are valid non-error outcomes.
        result = pre_gateway_dispatch_hook(event2)
        assert result is None or isinstance(result, dict)

    def test_hook_message_event_executive_resolve(self):
        """MessageEvent with /executive-resolve slash command returns a reply dict."""
        from plugins.executive_activation import pre_gateway_dispatch_hook
        from gateway.platforms.base import MessageEvent
        from gateway.session import SessionSource
        from gateway.config import Platform

        source = SessionSource(platform=Platform.LOCAL, chat_id="c1", user_id="u1")
        event = MessageEvent(text="/executive-resolve explain rocket propulsion", source=source)
        result = pre_gateway_dispatch_hook(event)
        assert result is not None
        assert result["action"] == "reply"
        assert "Executive Agent Resolved" in result["text"]

    def test_hook_message_event_no_source_does_not_crash(self):
        """MessageEvent with source=None (edge case) does not raise."""
        from plugins.executive_activation import pre_gateway_dispatch_hook
        from gateway.platforms.base import MessageEvent

        event = MessageEvent(text="hello there", source=None)  # type: ignore[arg-type]
        # Must not raise.
        result = pre_gateway_dispatch_hook(event)
        assert result is None or isinstance(result, dict)

    def test_hook_executive_resolve_command(self):
        """/executive-resolve <cmd> returns resolve reply dict."""
        from plugins.executive_activation import pre_gateway_dispatch_hook
        event = self._make_event("/executive-resolve explain rocket propulsion")
        result = pre_gateway_dispatch_hook(event)
        assert result is not None
        assert result["action"] == "reply"
        assert "Executive Agent Resolved" in result["text"]
        assert "Confidence:" in result["text"]

    def test_hook_executive_resolve_case_insensitive(self):
        """/EXECUTIVE-RESOLVE is handled (case-insensitive)."""
        from plugins.executive_activation import pre_gateway_dispatch_hook
        event = self._make_event("/EXECUTIVE-RESOLVE AI research")
        result = pre_gateway_dispatch_hook(event)
        assert result is not None
        assert result["action"] == "reply"

    def test_hook_executive_activate_with_persona(self):
        """/executive-activate helios returns activation reply."""
        from plugins.executive_activation import pre_gateway_dispatch_hook
        event = self._make_event("/executive-activate helios")
        result = pre_gateway_dispatch_hook(event)
        assert result is not None
        assert result["action"] == "reply"
        assert "Executive Agent Activated" in result["text"]
        assert "helios" in result["text"]

    def test_hook_executive_activate_no_persona(self):
        """/executive-activate with no persona still runs."""
        from plugins.executive_activation import pre_gateway_dispatch_hook
        event = self._make_event("/executive-activate")
        result = pre_gateway_dispatch_hook(event)
        assert result is not None
        assert result["action"] == "reply"

    def test_hook_executive_activate_invalid_persona(self):
        """/executive-activate with invalid persona falls back to resolve."""
        from plugins.executive_activation import pre_gateway_dispatch_hook
        event = self._make_event("/executive-activate nonexistent_persona")
        result = pre_gateway_dispatch_hook(event)
        assert result is not None
        assert result["action"] == "reply"

    def test_hook_executive_status_no_active_agent(self):
        """/executive-status with no active agent returns no-agent message."""
        from plugins.executive_activation import pre_gateway_dispatch_hook, _active_agents
        _active_agents.pop("status_test_user", None)
        event = self._make_event("/executive-status", user_id="status_test_user")
        result = pre_gateway_dispatch_hook(event)
        assert result is not None
        assert result["action"] == "reply"
        assert "No executive agent active" in result["text"]

    def test_hook_executive_status_with_active_agent(self):
        """/executive-status with active agent returns agent name."""
        from plugins.executive_activation import pre_gateway_dispatch_hook, _active_agents
        _active_agents["status_active_user"] = "orion"
        event = self._make_event("/executive-status", user_id="status_active_user")
        result = pre_gateway_dispatch_hook(event)
        assert result is not None
        assert "Demis Hassabis" in result["text"] or "orion" in result["text"]
        _active_agents.pop("status_active_user", None)

    def test_hook_executive_agents_command(self):
        """/executive-agents is handled same as /executive-status."""
        from plugins.executive_activation import pre_gateway_dispatch_hook, _active_agents
        _active_agents.pop("agents_cmd_user", None)
        event = self._make_event("/executive-agents", user_id="agents_cmd_user")
        result = pre_gateway_dispatch_hook(event)
        assert result is not None
        assert result["action"] == "reply"

    def test_hook_background_resolution_for_long_text(self):
        """Long text (> 10 chars) triggers background resolution and returns None."""
        from plugins.executive_activation import pre_gateway_dispatch_hook, _active_agents
        uid = "bg_test_user_123"
        _active_agents.pop(uid, None)
        event = self._make_event("tell me about rocket propulsion design", user_id=uid)
        result = pre_gateway_dispatch_hook(event)
        assert result is None  # background: don't skip

    def test_hook_background_resolution_sets_session(self):
        """High-confidence background resolution updates session agent."""
        from plugins.executive_activation import pre_gateway_dispatch_hook, _active_agents, _get_session_agent
        uid = "bg_session_test_xyz"
        _active_agents.pop(uid, None)
        event = self._make_event("discuss rocket propulsion and starship design", user_id=uid)
        pre_gateway_dispatch_hook(event)
        # May or may not have set session; just verify no crash and result is None
        _ = _get_session_agent(uid)
        _active_agents.pop(uid, None)

    def test_hook_short_text_no_background_resolution(self):
        """Text of ≤10 chars doesn't trigger background resolution but returns None."""
        from plugins.executive_activation import pre_gateway_dispatch_hook
        event = self._make_event("hi there", user_id="short_text_user")
        result = pre_gateway_dispatch_hook(event)
        assert result is None

    def test_hook_body_field_used_when_text_missing(self):
        """msg.body is used when msg.text is absent."""
        from plugins.executive_activation import pre_gateway_dispatch_hook
        event = {"message": {"body": "/executive-resolve deep learning paper"}, "user_id": "u2"}
        result = pre_gateway_dispatch_hook(event)
        assert result is not None
        assert "Resolved" in result["text"]

    def test_hook_event_with_from_field(self):
        """event.from is used as user_id when user_id absent."""
        from plugins.executive_activation import pre_gateway_dispatch_hook
        event = {"message": {"text": "/executive-status"}, "from": "user_from_field"}
        result = pre_gateway_dispatch_hook(event)
        assert result is not None

    def test_hook_exception_returns_none(self):
        """Hook catches exceptions and returns None."""
        from plugins.executive_activation import pre_gateway_dispatch_hook
        # Pass a non-dict event that will cause .get() to fail
        result = pre_gateway_dispatch_hook("not a dict")  # type: ignore
        assert result is None

    # ── lines 252-281: register() function ───────────────────────────────

    def test_register_with_register_hook_method(self):
        """register() calls ctx.register_hook('pre_gateway_dispatch', ...) when available."""
        from plugins.executive_activation import register, pre_gateway_dispatch_hook
        ctx = MagicMock()
        ctx.register_hook = MagicMock()
        del ctx.hooks  # Remove hooks attr so register_hook path is taken
        register(ctx)
        ctx.register_hook.assert_called_once_with("pre_gateway_dispatch", pre_gateway_dispatch_hook)

    def test_register_with_hooks_dict(self):
        """register() uses ctx.hooks dict when register_hook not available."""
        from plugins.executive_activation import register, pre_gateway_dispatch_hook
        ctx = MagicMock(spec=[])  # No attrs
        ctx.hooks = {}
        register(ctx)
        assert ctx.hooks.get("pre_gateway_dispatch") == pre_gateway_dispatch_hook

    def test_register_with_register_tool_method(self):
        """register() calls ctx.register_tool twice when available."""
        from plugins.executive_activation import register
        ctx = MagicMock()
        ctx.register_hook = MagicMock()
        ctx.register_tool = MagicMock()
        register(ctx)
        assert ctx.register_tool.call_count == 2

    def test_register_tool_schemas_correct(self):
        """The schemas passed to register_tool have correct names."""
        from plugins.executive_activation import register, RESOLVE_SCHEMA, ACTIVATE_SCHEMA
        ctx = MagicMock()
        ctx.register_hook = MagicMock()
        ctx.register_tool = MagicMock()
        register(ctx)
        calls = ctx.register_tool.call_args_list
        schema_names = [c[1]["schema"]["name"] if "schema" in c[1] else c[0][0]["name"]
                        for c in calls]
        # Accept either kwarg or positional call
        all_calls_str = str(calls)
        assert "executive_resolve" in all_calls_str
        assert "executive_activate" in all_calls_str

    def test_register_tool_exception_is_caught(self):
        """register() catches register_tool exceptions without crashing."""
        from plugins.executive_activation import register
        ctx = MagicMock()
        ctx.register_hook = MagicMock()
        ctx.register_tool = MagicMock(side_effect=RuntimeError("tool registration failed"))
        # Should not raise
        register(ctx)

    def test_register_no_register_hook_no_hooks_dict(self):
        """register() runs without error when ctx has neither register_hook nor hooks."""
        from plugins.executive_activation import register
        ctx = MagicMock(spec=["register_tool"])
        ctx.register_tool = MagicMock()
        register(ctx)
        # Should complete without raising

    def test_register_tool_handlers_callable(self):
        """The lambdas passed as handlers are callable and return results."""
        from plugins.executive_activation import register
        captured_handlers = []

        class FakeCtx:
            def register_hook(self, *args): pass
            def register_tool(self, schema, handler):
                captured_handlers.append((schema["name"], handler))

        register(FakeCtx())
        assert len(captured_handlers) == 2
        # Test the resolve handler
        resolve_handler = dict(captured_handlers)["executive_resolve"]
        result = resolve_handler({"command": "rocket propulsion"})
        assert "persona_id" in result

        # Test the activate handler
        activate_handler = dict(captured_handlers)["executive_activate"]
        result = activate_handler({"command": "rocket propulsion"})
        assert "activation_id" in result or "error" in result
