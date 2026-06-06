"""TDD tests for the L4→AGT compiler CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.l4_agt_compiler import cli, jl4_client


@pytest.fixture
def fake_l4(tmp_path) -> Path:
    """A throwaway .l4 file in policies/<system>/<axis>.l4 layout."""
    sys_dir = tmp_path / "policies" / "demo-system"
    sys_dir.mkdir(parents=True)
    src = sys_dir / "governance.l4"
    src.write_text("-- system: demo-system\n-- axis: governance\n")
    return src


def test_cli_round_trip_writes_rego_and_acs(monkeypatch, fake_l4, tmp_path):
    """End-to-end: CLI invokes compile, emits both files."""
    out = tmp_path / "dist"

    def fake_compile_bundle(*args, **kwargs):
        return jl4_client.CompileResult(
            status="applied",
            deployment_id=kwargs.get("deployment_id"),
            errors=[],
            openapi_schema={
                "paths": {
                    "/deployments/d/functions/permit/evaluation": {"post": {}}
                }
            },
        )

    monkeypatch.setattr(cli, "compile_bundle", fake_compile_bundle)
    monkeypatch.setattr(cli, "health_check", lambda *_args, **_kw: True)

    rc = cli.main(
        [str(fake_l4), "--out", str(out), "--quiet"]
    )
    assert rc == 0

    rego = out / "demo-system.governance.rego"
    acs = out / "demo-system.governance.acs.json"
    assert rego.is_file(), list(out.iterdir())
    assert acs.is_file(), list(out.iterdir())

    rego_text = rego.read_text()
    assert "package governance.demo_system.governance" in rego_text
    assert "input.permit == true" in rego_text

    manifest = json.loads(acs.read_text())
    assert manifest["source"]["system"] == "demo-system"
    assert manifest["source"]["axis"] == "governance"
    assert manifest["policies"]["demo-system.governance"]["type"] == "rego"


def test_cli_ci_mode_returns_nonzero_on_compile_failed(monkeypatch, fake_l4, tmp_path):
    """--ci-mode + compile_failed → exit code 1, no rego emitted."""
    out = tmp_path / "dist"

    def fake_compile_bundle(*args, **kwargs):
        return jl4_client.CompileResult(
            status="compile_failed",
            deployment_id=kwargs.get("deployment_id"),
            errors=["type error: undefined Foo"],
            openapi_schema=None,
        )

    monkeypatch.setattr(cli, "compile_bundle", fake_compile_bundle)
    monkeypatch.setattr(cli, "health_check", lambda *_args, **_kw: True)

    rc = cli.main(
        [str(fake_l4), "--out", str(out), "--ci-mode", "--quiet"]
    )
    assert rc == 1
    # No rego emitted on CI failure
    assert not (out / "demo-system.governance.rego").exists()


def test_cli_non_ci_mode_writes_failure_stub(monkeypatch, fake_l4, tmp_path):
    """Non-CI mode: still exits 1 but writes a compile_failed stub for tooling."""
    out = tmp_path / "dist"

    def fake_compile_bundle(*args, **kwargs):
        return jl4_client.CompileResult(
            status="compile_failed",
            deployment_id="d",
            errors=["E1"],
            openapi_schema=None,
        )

    monkeypatch.setattr(cli, "compile_bundle", fake_compile_bundle)
    monkeypatch.setattr(cli, "health_check", lambda *_args, **_kw: True)

    rc = cli.main([str(fake_l4), "--out", str(out), "--quiet"])
    assert rc == 1
    stub = out / "demo-system.governance.compile_failed.json"
    assert stub.is_file()
    assert json.loads(stub.read_text())["status"] == "compile_failed"


def test_cli_rejects_missing_source(tmp_path):
    rc = cli.main([str(tmp_path / "does-not-exist.l4"), "--out", str(tmp_path / "out"), "--quiet"])
    assert rc == 2


def test_cli_rejects_invalid_axis_inference(tmp_path):
    """A .l4 file whose stem is not a known axis must fail without --axis."""
    weird = tmp_path / "policies" / "sys" / "weird-name.l4"
    weird.parent.mkdir(parents=True)
    weird.write_text("-- system: sys\n")
    with pytest.raises(ValueError):
        cli._infer_system_axis(weird, axis_override=None)


def test_cli_axis_override(tmp_path):
    src = tmp_path / "policies" / "sys" / "weird-name.l4"
    src.parent.mkdir(parents=True)
    src.write_text("-- system: sys\n")
    sys_, axis = cli._infer_system_axis(src, axis_override="runtime")
    assert sys_ == "sys"
    assert axis == "runtime"
