"""TDD tests for the ACS manifest emitter."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from tools.l4_agt_compiler.acs_emitter import (
    ACS_VERSION,
    MANIFEST_SCHEMA,
    emit_acs_manifest,
)


def test_emit_acs_manifest_baseline_shape(tmp_path):
    rego_path = tmp_path / "demo.governance.rego"
    rego_content = "package governance.demo.governance\n\ndefault allow := false\n"
    rego_path.write_text(rego_content)

    manifest = emit_acs_manifest(
        {"paths": {}},
        rego_path=rego_path,
        system="demo",
        axis="governance",
        rego_content=rego_content,
        enabled=True,
        enforcement="enforce",
    )

    assert manifest["agent_control_specification_version"] == ACS_VERSION
    assert manifest["manifest_schema"] == MANIFEST_SCHEMA
    assert "generated_at" in manifest
    assert manifest["source"]["system"] == "demo"
    assert manifest["source"]["axis"] == "governance"
    assert manifest["source"]["compiler"].startswith("tools/l4_agt_compiler@")
    assert manifest["source"]["exported_function_count"] == 0


def test_emit_acs_manifest_embeds_rego_digest(tmp_path):
    rego_path = tmp_path / "demo.governance.rego"
    rego_content = "package governance.demo.governance\n"
    rego_path.write_text(rego_content)

    manifest = emit_acs_manifest(
        None,
        rego_path=rego_path,
        system="demo",
        axis="governance",
        rego_content=rego_content,
        enabled=True,
        enforcement="enforce",
    )
    expected = "sha256:" + hashlib.sha256(rego_content.encode("utf-8")).hexdigest()
    assert manifest["policies"]["demo.governance"]["digest"] == expected


def test_emit_acs_manifest_falls_back_to_path_digest_when_content_missing(tmp_path):
    rego_path = tmp_path / "demo.governance.rego"
    rego_content = "package governance.demo.governance\n"
    rego_path.write_text(rego_content)

    manifest = emit_acs_manifest(
        None,
        rego_path=rego_path,
        system="demo",
        axis="governance",
        rego_content=None,  # force fallback
        enabled=True,
        enforcement="enforce",
    )
    expected = "sha256:" + hashlib.sha256(rego_content.encode("utf-8")).hexdigest()
    assert manifest["policies"]["demo.governance"]["digest"] == expected


def test_emit_acs_manifest_counts_export_functions(tmp_path):
    rego_path = tmp_path / "demo.governance.rego"
    rego_path.write_text("package governance.demo.governance\n")
    schema = {
        "paths": {
            "/deployments/d1/functions/permit/evaluation": {"post": {}},
            "/deployments/d1/functions/deny/evaluation": {"post": {}},
            "/health": {"get": {}},
        }
    }
    manifest = emit_acs_manifest(
        schema,
        rego_path=rego_path,
        system="demo",
        axis="governance",
        rego_content="x",
        enabled=True,
        enforcement="enforce",
    )
    assert manifest["source"]["exported_function_count"] == 2


def test_emit_acs_manifest_sets_default_decision_deny(tmp_path):
    rego_path = tmp_path / "x.rego"
    rego_path.write_text("x")
    m = emit_acs_manifest(
        {},
        rego_path=rego_path,
        system="s",
        axis="governance",
        rego_content="x",
        enabled=True,
        enforcement="enforce",
    )
    assert m["policies"]["s.governance"]["default_decision"] == "deny"
    assert m["policies"]["s.governance"]["type"] == "rego"


def test_emit_acs_manifest_rejects_empty_inputs(tmp_path):
    rego_path = tmp_path / "x.rego"
    rego_path.write_text("x")
    with pytest.raises(ValueError):
        emit_acs_manifest({}, rego_path=rego_path, system="", axis="governance")
    with pytest.raises(ValueError):
        emit_acs_manifest({}, rego_path=rego_path, system="s", axis="")


def test_emit_acs_manifest_serializes_to_json(tmp_path):
    """Sanity check: the dict round-trips through json.dumps without falling over."""
    rego_path = tmp_path / "x.rego"
    rego_path.write_text("x")
    m = emit_acs_manifest(
        {},
        rego_path=rego_path,
        system="s",
        axis="governance",
        rego_content="x",
        enabled=True,
        enforcement="enforce",
    )
    s = json.dumps(m)
    assert json.loads(s) == m


# ── new lifecycle tests ───────────────────────────────────────────────────────


def test_emit_acs_manifest_enabled_true_entry_present(tmp_path):
    """enabled=True → policy entry is present in manifest."""
    rego_path = tmp_path / "x.rego"
    rego_path.write_text("x")
    m = emit_acs_manifest(
        {},
        rego_path=rego_path,
        system="myapp",
        axis="runtime",
        rego_content="x",
        enabled=True,
        enforcement="monitor",
    )
    assert "myapp.runtime" in m["policies"]
    entry = m["policies"]["myapp.runtime"]
    assert entry["enabled"] is True
    assert entry["enforcement"] == "monitor"
    assert m["omitted_disabled_policies"] == []


def test_emit_acs_manifest_enabled_false_entry_omitted(tmp_path):
    """enabled=False → policy entry is absent; listed in omitted_disabled_policies."""
    rego_path = tmp_path / "x.rego"
    rego_path.write_text("x")
    m = emit_acs_manifest(
        {},
        rego_path=rego_path,
        system="myapp",
        axis="runtime",
        rego_content="x",
        enabled=False,
        enforcement="disabled",
    )
    assert m["policies"] == {}
    assert "myapp.runtime" in m["omitted_disabled_policies"]


def test_emit_acs_manifest_enforcement_monitor_default_allow(tmp_path):
    """enforcement=monitor → default_decision=allow."""
    rego_path = tmp_path / "x.rego"
    rego_path.write_text("x")
    m = emit_acs_manifest(
        {},
        rego_path=rego_path,
        system="s",
        axis="integration",
        rego_content="x",
        enabled=True,
        enforcement="monitor",
    )
    assert m["policies"]["s.integration"]["default_decision"] == "allow"


def test_emit_acs_manifest_enforcement_enforce_default_deny(tmp_path):
    """enforcement=enforce → default_decision=deny."""
    rego_path = tmp_path / "x.rego"
    rego_path.write_text("x")
    m = emit_acs_manifest(
        {},
        rego_path=rego_path,
        system="s",
        axis="integration",
        rego_content="x",
        enabled=True,
        enforcement="enforce",
    )
    assert m["policies"]["s.integration"]["default_decision"] == "deny"


def test_emit_acs_manifest_enforcement_disabled_default_allow(tmp_path):
    """enforcement=disabled (but enabled=True) → default_decision=allow."""
    rego_path = tmp_path / "x.rego"
    rego_path.write_text("x")
    m = emit_acs_manifest(
        {},
        rego_path=rego_path,
        system="s",
        axis="usage",
        rego_content="x",
        enabled=True,
        enforcement="disabled",
    )
    assert m["policies"]["s.usage"]["default_decision"] == "allow"
