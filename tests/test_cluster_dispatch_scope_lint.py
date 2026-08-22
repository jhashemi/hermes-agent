"""Unit tests for gateway/cluster_dispatch.py scope-lint helpers:
compute_out_of_scope_boards() and log_out_of_scope_boards_at_startup().

Covers the two operator failure modes:
  1. cluster_dispatch=True + board not in whitelist → severity=warn
  2. cluster_dispatch=False + non-empty whitelist omits board → severity=info

Plus edge cases: empty whitelist, missing whitelist key, whitelist with only
empty strings, zero active tickets (excluded), config load failure (returns []),
malformed config (returns []).

All disk state is a tmp_path board tree — no real production DBs are touched.
"""
from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, "/home/ubuntu/hermes-agent")

from gateway import cluster_dispatch as cd


# ---------------------------------------------------------------------------
# Helpers — build a fake board tree with a real sqlite tasks table so
# _active_ticket_count exercises the same code path production uses.
# ---------------------------------------------------------------------------

def _make_board(root: Path, slug: str, statuses: list[str]) -> Path:
    """Create ``root/kanban/boards/<slug>/kanban.db`` with a tasks table
    populated by ``statuses``. Returns the DB path."""
    bdir = root / "kanban" / "boards" / slug
    bdir.mkdir(parents=True, exist_ok=True)
    db = bdir / "kanban.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "CREATE TABLE tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL)"
        )
        for i, s in enumerate(statuses):
            conn.execute(
                "INSERT INTO tasks (id, status) VALUES (?, ?)",
                (f"t_{slug}_{i}", s),
            )
        conn.commit()
    finally:
        conn.close()
    return db


class _FakeKB:
    """Minimal kanban_db surface consumed by compute_out_of_scope_boards.

    ``list_boards`` returns dicts with a ``slug`` key. ``kanban_db_path``
    maps slug → tmp path (so ``_active_ticket_count`` reads the fake DB).
    """
    def __init__(self, slug_to_db: dict[str, Path]):
        self._slug_to_db = slug_to_db

    def list_boards(self, *, include_archived: bool = False):
        return [{"slug": s} for s in self._slug_to_db]

    def kanban_db_path(self, slug: str) -> Path:
        return self._slug_to_db[slug]


@pytest.fixture
def board_env(tmp_path, monkeypatch):
    """Factory that returns (cfg, kb_mock_context) for a given layout.

    Usage::

        with board_env({"board-a": ["todo"], "board-b": ["done"]},
                       cfg={"kanban": {...}}) as (cfg,):
            entries = cd.compute_out_of_scope_boards(cfg=cfg)
    """
    import contextlib

    @contextlib.contextmanager
    def _factory(layout: dict[str, list[str]]):
        slug_to_db = {slug: _make_board(tmp_path, slug, sts) for slug, sts in layout.items()}
        fake_kb = _FakeKB(slug_to_db)
        # Compose_out_of_scope_boards does ``from hermes_cli import kanban_db as _kb``
        # each call — patch the module attr so the import returns our fake.
        with mock.patch.dict(sys.modules, {"hermes_cli.kanban_db": fake_kb}):
            yield
    return _factory


# ---------------------------------------------------------------------------
# _active_ticket_count
# ---------------------------------------------------------------------------

class TestActiveTicketCount:
    def test_counts_todo_ready_running(self, tmp_path):
        db = _make_board(tmp_path, "b", ["todo", "ready", "running", "done", "blocked"])
        assert cd._active_ticket_count(db) == 3

    def test_zero_for_all_done(self, tmp_path):
        db = _make_board(tmp_path, "b", ["done", "done", "archived"])
        assert cd._active_ticket_count(db) == 0

    def test_empty_table(self, tmp_path):
        db = _make_board(tmp_path, "b", [])
        assert cd._active_ticket_count(db) == 0

    def test_missing_db_returns_zero(self, tmp_path):
        # File doesn't exist — must not raise.
        assert cd._active_ticket_count(tmp_path / "nope.db") == 0

    def test_corrupt_db_returns_zero(self, tmp_path):
        p = tmp_path / "corrupt.db"
        p.write_bytes(b"not a database")
        assert cd._active_ticket_count(p) == 0


# ---------------------------------------------------------------------------
# compute_out_of_scope_boards — main behaviour matrix
# ---------------------------------------------------------------------------

class TestComputeOutOfScope:
    def test_cluster_enabled_board_not_in_whitelist_is_warn(self, board_env):
        with board_env({
            "adr-006b-phase-2": ["ready", "todo"],
            "campaignforge-phase5": ["todo", "todo", "running"],
        }):
            cfg = {"kanban": {
                "cluster_dispatch": True,
                "cluster_dispatch_board": ["adr-006b-phase-2"],
            }}
            out = cd.compute_out_of_scope_boards(cfg=cfg)

        assert len(out) == 1
        entry = out[0]
        assert entry["slug"] == "campaignforge-phase5"
        assert entry["active_count"] == 3
        assert entry["severity"] == "warn"
        assert "cluster_dispatch=true" in entry["reason"]
        assert any("campaignforge-phase5" in h for h in entry["fix_hints"])

    def test_cluster_disabled_whitelist_omits_board_is_info(self, board_env):
        with board_env({
            "adr-006b-phase-2": ["ready"],
            "campaignforge-phase5": ["todo", "ready"],
        }):
            cfg = {"kanban": {
                "cluster_dispatch": False,
                "cluster_dispatch_board": ["adr-006b-phase-2"],
            }}
            out = cd.compute_out_of_scope_boards(cfg=cfg)

        assert len(out) == 1
        entry = out[0]
        assert entry["slug"] == "campaignforge-phase5"
        assert entry["severity"] == "info"
        assert "cluster_dispatch=false" in entry["reason"]

    def test_cluster_enabled_empty_whitelist_no_warn(self, board_env):
        # Empty whitelist under cluster_dispatch=true is a broader config
        # issue but not a per-board scope drift. Helper stays silent.
        with board_env({"campaignforge-phase5": ["ready"]}):
            cfg = {"kanban": {"cluster_dispatch": True, "cluster_dispatch_board": []}}
            out = cd.compute_out_of_scope_boards(cfg=cfg)
        assert out == []

    def test_cluster_disabled_no_whitelist_key_no_output(self, board_env):
        with board_env({"campaignforge-phase5": ["todo"]}):
            cfg = {"kanban": {"cluster_dispatch": False}}
            out = cd.compute_out_of_scope_boards(cfg=cfg)
        assert out == []

    def test_board_on_whitelist_never_reported(self, board_env):
        with board_env({"good": ["ready", "todo"]}):
            cfg = {"kanban": {
                "cluster_dispatch": True,
                "cluster_dispatch_board": ["good"],
            }}
            out = cd.compute_out_of_scope_boards(cfg=cfg)
        assert out == []

    def test_boards_with_zero_active_excluded(self, board_env):
        with board_env({"quiet": ["done", "archived"]}):
            cfg = {"kanban": {
                "cluster_dispatch": True,
                "cluster_dispatch_board": ["other"],
            }}
            out = cd.compute_out_of_scope_boards(cfg=cfg)
        assert out == []

    def test_multiple_boards_sorted_warn_first_then_alpha(self, board_env):
        with board_env({
            "a-info": ["ready"],
            "z-warn": ["ready"],
            "a-warn": ["todo"],
        }):
            # cluster_dispatch=true, whitelist has only 'a-info'
            # 'z-warn' and 'a-warn' are warns; 'a-info' is on whitelist so silent.
            cfg = {"kanban": {
                "cluster_dispatch": True,
                "cluster_dispatch_board": ["a-info"],
            }}
            out = cd.compute_out_of_scope_boards(cfg=cfg)
        slugs = [e["slug"] for e in out]
        # Alphabetical within same severity
        assert slugs == ["a-warn", "z-warn"]
        assert all(e["severity"] == "warn" for e in out)

    def test_malformed_whitelist_treated_as_empty(self, board_env):
        with board_env({"b": ["ready"]}):
            cfg = {"kanban": {
                "cluster_dispatch": True,
                "cluster_dispatch_board": "not-a-list",  # malformed
            }}
            out = cd.compute_out_of_scope_boards(cfg=cfg)
        # Malformed → empty set; empty whitelist under cluster=true → silent
        assert out == []

    def test_whitelist_strips_empty_strings(self, board_env):
        with board_env({"b": ["ready"]}):
            cfg = {"kanban": {
                "cluster_dispatch": True,
                "cluster_dispatch_board": ["", "  ", "b"],
            }}
            out = cd.compute_out_of_scope_boards(cfg=cfg)
        assert out == []  # 'b' is on the whitelist after strip

    def test_cfg_none_loads_from_config_module(self, board_env, monkeypatch):
        # Simulate a real load_config path.
        fake_cfg_mod = mock.MagicMock()
        fake_cfg_mod.load_config.return_value = {"kanban": {
            "cluster_dispatch": True,
            "cluster_dispatch_board": ["allowed"],
        }}
        with mock.patch.dict(sys.modules, {"hermes_cli.config": fake_cfg_mod}):
            with board_env({"allowed": ["ready"], "orphan": ["todo"]}):
                out = cd.compute_out_of_scope_boards()  # no cfg passed
        slugs = [e["slug"] for e in out]
        assert slugs == ["orphan"]

    def test_cfg_load_failure_returns_empty(self, monkeypatch):
        fake_cfg_mod = mock.MagicMock()
        fake_cfg_mod.load_config.side_effect = RuntimeError("boom")
        with mock.patch.dict(sys.modules, {"hermes_cli.config": fake_cfg_mod}):
            assert cd.compute_out_of_scope_boards() == []

    def test_non_dict_cfg_returns_empty(self):
        assert cd.compute_out_of_scope_boards(cfg=["not", "a", "dict"]) == []
        assert cd.compute_out_of_scope_boards(cfg=None) is not None  # loads instead

    def test_list_boards_failure_returns_empty(self, monkeypatch):
        fake_kb = mock.MagicMock()
        fake_kb.list_boards.side_effect = RuntimeError("db down")
        with mock.patch.dict(sys.modules, {"hermes_cli.kanban_db": fake_kb}):
            out = cd.compute_out_of_scope_boards(cfg={"kanban": {"cluster_dispatch": True}})
        assert out == []


# ---------------------------------------------------------------------------
# log_out_of_scope_boards_at_startup — verifies gateway-startup logging
# ---------------------------------------------------------------------------

class TestLogOutOfScopeStartup:
    def test_warn_entries_log_at_warning_level(self, caplog, board_env, monkeypatch):
        fake_cfg_mod = mock.MagicMock()
        fake_cfg_mod.load_config.return_value = {"kanban": {
            "cluster_dispatch": True,
            "cluster_dispatch_board": ["allowed"],
        }}
        with mock.patch.dict(sys.modules, {"hermes_cli.config": fake_cfg_mod}):
            with board_env({"allowed": ["ready"], "orphan": ["todo", "todo"]}):
                with caplog.at_level(logging.WARNING, logger="gateway.cluster_dispatch"):
                    cd.log_out_of_scope_boards_at_startup()

        warn_records = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "orphan" in r.getMessage()
        ]
        assert len(warn_records) == 1
        msg = warn_records[0].getMessage()
        assert "orphan" in msg
        assert "2 active ticket" in msg
        assert "cluster_dispatch_board" in msg  # fix hint surfaced

    def test_info_entries_log_at_info_level(self, caplog, board_env):
        fake_cfg_mod = mock.MagicMock()
        fake_cfg_mod.load_config.return_value = {"kanban": {
            "cluster_dispatch": False,
            "cluster_dispatch_board": ["allowed"],
        }}
        with mock.patch.dict(sys.modules, {"hermes_cli.config": fake_cfg_mod}):
            with board_env({"allowed": ["ready"], "orphan": ["ready"]}):
                with caplog.at_level(logging.INFO, logger="gateway.cluster_dispatch"):
                    cd.log_out_of_scope_boards_at_startup()

        info_records = [
            r for r in caplog.records
            if r.levelno == logging.INFO and "orphan" in r.getMessage()
        ]
        assert len(info_records) == 1

    def test_no_entries_no_log_lines(self, caplog, board_env):
        fake_cfg_mod = mock.MagicMock()
        fake_cfg_mod.load_config.return_value = {"kanban": {
            "cluster_dispatch": True,
            "cluster_dispatch_board": ["only"],
        }}
        with mock.patch.dict(sys.modules, {"hermes_cli.config": fake_cfg_mod}):
            with board_env({"only": ["ready"]}):
                with caplog.at_level(logging.DEBUG, logger="gateway.cluster_dispatch"):
                    cd.log_out_of_scope_boards_at_startup()

        # No scope-related log lines should fire when everything is in scope.
        scope_lines = [
            r for r in caplog.records
            if "active ticket" in r.getMessage()
        ]
        assert scope_lines == []

    def test_exception_swallowed(self, caplog, monkeypatch):
        # compute_out_of_scope_boards raises → startup logger swallows.
        monkeypatch.setattr(
            cd, "compute_out_of_scope_boards",
            mock.MagicMock(side_effect=RuntimeError("boom")),
        )
        with caplog.at_level(logging.DEBUG, logger="gateway.cluster_dispatch"):
            cd.log_out_of_scope_boards_at_startup()
        # Debug line acknowledges the skip; no exception propagates.
        assert any("scope lint failed" in r.getMessage() for r in caplog.records)
