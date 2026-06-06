"""TDD tests for the L4→AGT compiler CLI."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.l4_agt_compiler import cli, jl4_client
from tools.l4_agt_compiler.policy_state import load_state


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
    # With default enabled=False the policy entry is omitted
    assert "omitted_disabled_policies" in manifest


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


# ── policy subcommand tests ───────────────────────────────────────────────────


@pytest.fixture
def state_path(tmp_path) -> Path:
    return tmp_path / ".state.json"


def test_policy_enable_updates_state(state_path, capsys):
    """policy enable foo.bar → state file has enabled=True, enforcement=monitor."""
    rc = cli.main(
        ["policy", "enable", "foo.bar", "--changed-by", "alice",
         "--state-path", str(state_path)]
    )
    assert rc == 0
    state = load_state(state_path)
    entry = state["policies"]["foo.bar"]
    assert entry["enabled"] is True
    assert entry["enforcement"] == "monitor"
    assert entry["changed_by"] == "alice"

    out = capsys.readouterr().out
    assert "enabled" in out.lower()


def test_policy_disable_sets_reason_and_auto_revert(state_path, capsys):
    """policy disable foo.bar --reason X → disabled, reason set, auto_revert_at set."""
    rc = cli.main(
        ["policy", "disable", "foo.bar",
         "--reason", "break-glass incident",
         "--changed-by", "ops",
         "--state-path", str(state_path)]
    )
    assert rc == 0
    state = load_state(state_path)
    entry = state["policies"]["foo.bar"]
    assert entry["enabled"] is False
    assert entry["enforcement"] == "disabled"
    assert entry["reason"] == "break-glass incident"
    assert "auto_revert_at" in entry

    out = capsys.readouterr().out
    assert "DISABLED" in out


def test_policy_disable_requires_reason(tmp_path):
    """policy disable without --reason must exit non-zero (argparse error)."""
    sp = tmp_path / ".state.json"
    with pytest.raises(SystemExit) as exc_info:
        cli.main(
            ["policy", "disable", "foo.bar",
             "--state-path", str(sp)]
        )
    assert exc_info.value.code != 0


def test_policy_monitor(state_path, capsys):
    """policy monitor sets enabled=True, enforcement=monitor."""
    rc = cli.main(
        ["policy", "monitor", "app.runtime",
         "--changed-by", "dev",
         "--state-path", str(state_path)]
    )
    assert rc == 0
    state = load_state(state_path)
    entry = state["policies"]["app.runtime"]
    assert entry["enabled"] is True
    assert entry["enforcement"] == "monitor"


def test_policy_enforce(state_path, capsys):
    """policy enforce sets enabled=True, enforcement=enforce."""
    rc = cli.main(
        ["policy", "enforce", "app.governance",
         "--changed-by", "council",
         "--state-path", str(state_path)]
    )
    assert rc == 0
    state = load_state(state_path)
    entry = state["policies"]["app.governance"]
    assert entry["enabled"] is True
    assert entry["enforcement"] == "enforce"


def test_policy_status_lists_all(state_path, capsys):
    """policy status (no arg) lists all policies when state has entries."""
    # Pre-populate state
    cli.main(
        ["policy", "enable", "sys.governance",
         "--changed-by", "alice",
         "--state-path", str(state_path)]
    )
    cli.main(
        ["policy", "monitor", "sys.runtime",
         "--changed-by", "bob",
         "--state-path", str(state_path)]
    )
    capsys.readouterr()  # clear previous output

    rc = cli.main(["policy", "status", "--state-path", str(state_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "sys.governance" in out
    assert "sys.runtime" in out


def test_policy_status_single_policy(state_path, capsys):
    """policy status <id> shows that policy's details."""
    cli.main(
        ["policy", "disable", "app.usage",
         "--reason", "testing",
         "--changed-by", "ops",
         "--state-path", str(state_path)]
    )
    capsys.readouterr()

    rc = cli.main(["policy", "status", "app.usage", "--state-path", str(state_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "app.usage" in out
    assert "disabled" in out.lower()
    assert "testing" in out


def test_policy_status_empty_state_no_crash(state_path, capsys):
    """policy status with empty/missing state file must not crash."""
    rc = cli.main(["policy", "status", "--state-path", str(state_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No policies" in out
