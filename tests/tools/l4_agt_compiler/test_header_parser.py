"""TDD tests for the L4 header parser."""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.l4_agt_compiler.header_parser import parse_l4_header


# ── fixtures ──────────────────────────────────────────────────────────────────


def _write_l4(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "test.l4"
    p.write_text(content, encoding="utf-8")
    return p


# ── tests ─────────────────────────────────────────────────────────────────────


def test_parse_full_header(tmp_path):
    """Full header with all fields including enabled/enforcement is parsed correctly."""
    l4 = _write_l4(
        tmp_path,
        "-- system: hermes-agent\n"
        "-- axis: governance\n"
        "-- tier: T1\n"
        "-- author: council-bot\n"
        "-- ratified: 2025-06-01T00:00:00Z\n"
        "-- council_signoffs: [alice, bob, carol]\n"
        "-- supersedes: NONE\n"
        "-- version: 1.2.3\n"
        "-- enabled: true\n"
        "-- enforcement: enforce\n"
        "\n"
        "DECLARE Foo IS A Type\n",
    )
    h = parse_l4_header(l4)

    assert h["system"] == "hermes-agent"
    assert h["axis"] == "governance"
    assert h["tier"] == "T1"
    assert h["author"] == "council-bot"
    assert h["ratified"] == "2025-06-01T00:00:00Z"
    assert h["council_signoffs"] == ["alice", "bob", "carol"]
    assert h["supersedes"] == "NONE"
    assert h["version"] == "1.2.3"
    assert h["enabled"] is True
    assert h["enforcement"] == "enforce"


def test_parse_missing_enabled_enforcement_defaults(tmp_path):
    """Missing enabled/enforcement → safe defaults (False, 'disabled')."""
    l4 = _write_l4(
        tmp_path,
        "-- system: legacy-app\n"
        "-- axis: runtime\n"
        "-- tier: T2\n"
        "-- author: dev\n"
        "-- ratified: PENDING\n"
        "-- council_signoffs: []\n"
        "-- supersedes: NONE\n"
        "-- version: 0.1.0\n",
    )
    h = parse_l4_header(l4)

    assert h["enabled"] is False
    assert h["enforcement"] == "disabled"
    assert h["system"] == "legacy-app"


def test_parse_malformed_enforcement_defaults_to_disabled(tmp_path):
    """Unknown enforcement value → silently normalised to 'disabled'."""
    l4 = _write_l4(
        tmp_path,
        "-- system: x\n"
        "-- axis: usage\n"
        "-- enabled: true\n"
        "-- enforcement: UNKNOWN_VALUE\n",
    )
    h = parse_l4_header(l4)
    assert h["enforcement"] == "disabled"
    # enabled should still parse
    assert h["enabled"] is True


def test_parse_enabled_false_explicit(tmp_path):
    """enabled: false (explicit) → False."""
    l4 = _write_l4(
        tmp_path,
        "-- system: s\n"
        "-- axis: integration\n"
        "-- enabled: false\n"
        "-- enforcement: monitor\n",
    )
    h = parse_l4_header(l4)
    assert h["enabled"] is False
    assert h["enforcement"] == "monitor"


def test_parse_council_signoffs_list_format(tmp_path):
    """council_signoffs with brackets and spaces → clean list."""
    l4 = _write_l4(
        tmp_path,
        "-- system: s\n"
        "-- council_signoffs: [ alice , bob , carol ]\n",
    )
    h = parse_l4_header(l4)
    assert h["council_signoffs"] == ["alice", "bob", "carol"]


def test_parse_council_signoffs_empty(tmp_path):
    """Empty council_signoffs → empty list."""
    l4 = _write_l4(tmp_path, "-- system: s\n-- council_signoffs: []\n")
    h = parse_l4_header(l4)
    assert h["council_signoffs"] == []


def test_parse_header_stops_at_non_comment_line(tmp_path):
    """Header parsing stops at first non-comment, non-blank line."""
    l4 = _write_l4(
        tmp_path,
        "-- system: s\n"
        "-- axis: runtime\n"
        "DECLARE Foo IS A Type\n"  # non-comment stops header
        "-- enabled: true\n",  # this line is AFTER the header; should be ignored
    )
    h = parse_l4_header(l4)
    assert h["system"] == "s"
    # enabled after the body line should NOT be parsed
    assert h["enabled"] is False


def test_parse_nonexistent_file_returns_defaults():
    """Missing file → returns all safe defaults, no exception."""
    h = parse_l4_header(Path("/nonexistent/path/policy.l4"))
    assert h["enabled"] is False
    assert h["enforcement"] == "disabled"
    assert h["system"] == ""
    assert h["council_signoffs"] == []
