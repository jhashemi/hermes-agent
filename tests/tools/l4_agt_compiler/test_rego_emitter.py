"""TDD tests for the Rego emitter."""
from __future__ import annotations

import pytest

from tools.l4_agt_compiler.rego_emitter import emit_rego, _extract_export_function_names


def test_emit_rego_emits_header_and_default_deny():
    """Empty schema still produces a parseable, default-deny Rego module."""
    out = emit_rego({}, system="hrv-pacemaker", axis="governance")
    assert "package governance.hrv_pacemaker.governance" in out
    assert "default allow := false" in out
    assert "import future.keywords.if" in out
    assert "No @export'd L4 decisions found" in out


def test_emit_rego_replaces_dashes_in_package_segments():
    """Rego packages must be alphanumeric+underscore; dashes get rewritten."""
    out = emit_rego({}, system="post-mortem-governance", axis="governance")
    assert "package governance.post_mortem_governance.governance" in out
    # System name in comment header should still show the canonical dashed form
    assert "system: post-mortem-governance" in out


def test_emit_rego_emits_one_allow_per_export():
    schema = {
        "paths": {
            "/deployments/d1/functions/permit/evaluation": {"post": {}},
            "/deployments/d1/functions/is_eligible/evaluation": {"post": {}},
            "/deployments/d1/functions/parameterized/{arg}/evaluation": {"post": {}},  # ignored
            "/health": {"get": {}},  # ignored
        }
    }
    out = emit_rego(schema, system="demo", axis="governance")
    assert "allow if {\n    input.permit == true\n}" in out
    assert "allow if {\n    input.is_eligible == true\n}" in out
    assert "parameterized" not in out
    assert "# from L4 @export permit" in out


def test_emit_rego_rejects_empty_system_or_axis():
    with pytest.raises(ValueError):
        emit_rego({}, system="", axis="governance")
    with pytest.raises(ValueError):
        emit_rego({}, system="demo", axis="")


def test_extract_dedupes_function_names():
    schema = {
        "paths": {
            "/deployments/d1/functions/permit/evaluation": {"post": {}},
            "/deployments/d1/functions/permit/evaluation/batch": {"post": {}},
        }
    }
    names = _extract_export_function_names(schema)
    # 'permit/evaluation' matches; 'permit/evaluation/batch' does not (path doesn't end in /evaluation)
    assert names == ["permit"]


def test_extract_handles_none_and_non_dict():
    assert _extract_export_function_names(None) == []
    assert _extract_export_function_names("not a dict") == []  # type: ignore[arg-type]
    assert _extract_export_function_names({"paths": "not a dict"}) == []  # type: ignore[arg-type]
