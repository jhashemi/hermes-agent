"""Behavioural tests for scripts/kanban-rollback-to-sqlite.sh.

Covers ADR-012 §5 rollback contract. The script is bash; there is no python
module to import, so these tests drive it as a subprocess against isolated
HERMES_HOME trees under tmp_path. Every scenario asserts:
  * exit code
  * on-disk state (files renamed / left alone)
  * config write (env.kanban_write_backend=sqlite)
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "kanban-rollback-to-sqlite.sh"


def _run(args, hermes_home, hermes_config=None):
    env = os.environ.copy()
    env["HERMES_HOME"] = str(hermes_home)
    if hermes_config is not None:
        env["HERMES_CONFIG"] = str(hermes_config)
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _mkboard(root: Path, slug: str) -> Path:
    d = root / "kanban" / "boards" / slug
    d.mkdir(parents=True)
    return d


def test_script_is_executable():
    assert SCRIPT.exists(), f"missing: {SCRIPT}"
    assert SCRIPT.stat().st_mode & stat.S_IXUSR, "script must be +x"


def test_bash_syntax_ok():
    r = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_no_args_exits_1(tmp_path):
    r = _run([], tmp_path)
    assert r.returncode == 1
    assert "must specify" in r.stderr


def test_unknown_arg_exits_1(tmp_path):
    r = _run(["--bogus"], tmp_path)
    assert r.returncode == 1
    assert "unknown arg" in r.stderr


def test_help_exits_0(tmp_path):
    r = _run(["--help"], tmp_path)
    assert r.returncode == 0
    assert "rollback path" in r.stdout.lower()


def test_dry_run_makes_no_changes(tmp_path):
    board = _mkboard(tmp_path, "t")
    retired = board / "kanban.db.retired.1786866598"
    retired.write_text("SNAPSHOT")
    cfg = tmp_path / "config.yaml"
    r = _run(["--board=t", "--dry-run"], tmp_path, cfg)
    assert r.returncode == 0
    assert retired.exists(), "dry-run must not move files"
    assert not (board / "kanban.db").exists()
    assert not cfg.exists(), "dry-run must not write config"


def test_real_run_restores_retired_file(tmp_path):
    board = _mkboard(tmp_path, "t")
    retired = board / "kanban.db.retired.1786866598"
    retired.write_text("SNAPSHOT")
    cfg = tmp_path / "config.yaml"
    r = _run(["--board=t"], tmp_path, cfg)
    # exit 0 (clean) since there is no kanban.duckdb to parity-check
    assert r.returncode == 0, f"stdout={r.stdout}\nstderr={r.stderr}"
    assert not retired.exists()
    restored = board / "kanban.db"
    assert restored.exists() and restored.read_text() == "SNAPSHOT"
    assert "kanban_write_backend: sqlite" in cfg.read_text()


def test_picks_latest_retired_snapshot(tmp_path):
    board = _mkboard(tmp_path, "t")
    (board / "kanban.db.retired.1000000000").write_text("OLD")
    (board / "kanban.db.retired.2000000000").write_text("NEW")
    r = _run(["--board=t"], tmp_path, tmp_path / "cfg.yaml")
    assert r.returncode == 0
    assert (board / "kanban.db").read_text() == "NEW"


def test_missing_sqlite_exits_2(tmp_path):
    _mkboard(tmp_path, "t")  # empty board dir, no .db and no .retired.*
    r = _run(["--board=t", "--dry-run"], tmp_path, tmp_path / "cfg.yaml")
    assert r.returncode == 2
    assert "SQLite file missing" in r.stdout


def test_existing_db_left_in_place(tmp_path):
    board = _mkboard(tmp_path, "t")
    (board / "kanban.db").write_text("LIVE")
    r = _run(["--board=t"], tmp_path, tmp_path / "cfg.yaml")
    assert r.returncode == 0
    assert (board / "kanban.db").read_text() == "LIVE"


@pytest.mark.skipif(
    subprocess.run(["python3", "-c", "import duckdb"], capture_output=True).returncode != 0,
    reason="duckdb not importable",
)
def test_parity_error_on_bogus_duckdb_exits_3(tmp_path):
    board = _mkboard(tmp_path, "t")
    (board / "kanban.db").write_text("")
    (board / "kanban.duckdb").write_text("")  # not a valid duckdb file
    r = _run(["--board=t"], tmp_path, tmp_path / "cfg.yaml")
    assert r.returncode == 3
    assert "PARTIAL ROLLBACK" in r.stdout or "PARITY" in r.stdout


def test_all_boards_enumerates_every_board(tmp_path):
    for slug in ("a", "b", "c"):
        (_mkboard(tmp_path, slug) / "kanban.db").write_text("x")
    r = _run(["--all-boards", "--dry-run"], tmp_path, tmp_path / "cfg.yaml")
    assert r.returncode == 0
    for slug in ("a", "b", "c"):
        assert f"/boards/{slug}/kanban.db" in r.stdout
