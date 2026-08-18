"""Tests for kanban DB corruption repair, backup retention, WAL checkpointing,
and the ``hermes kanban repair`` CLI verb."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _build_board_db(db_path: Path, tasks: int = 12) -> None:
    """Create a real board DB with data so indexes have entries."""
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db(db_path=db_path)
    with kb.connect(db_path=db_path) as conn:
        for i in range(tasks):
            kb.create_task(conn, title=f"task-{i}")
    conn.close()
    # Force the next connect() to re-run the health guard.
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))


def _corrupt_index(db_path: Path, index_name: str) -> None:
    """Make ``index_name`` disagree with its table → 'wrong # of entries'.

    writable_schema approach: temporarily rewrite the index's schema SQL to
    a partial index matching no rows, REINDEX under that lie (emptying the
    index b-tree), then restore the original SQL. integrity_check now sees a
    non-partial index whose b-tree is missing every row — exactly the
    index-scoped corruption class ('wrong # of entries in index <name>' +
    'row N missing from index <name>') with intact table b-trees.
    """
    conn = sqlite3.connect(db_path, isolation_level=None)
    original_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE name = ?", (index_name,)
    ).fetchone()[0]
    lie = original_sql + " WHERE 0"
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute(
        "UPDATE sqlite_master SET sql = ? WHERE name = ?", (lie, index_name)
    )
    conn.execute("PRAGMA writable_schema=OFF")
    conn.close()
    # New connection so the rewritten schema is what REINDEX parses.
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.execute(f'REINDEX "{index_name}"')
    conn.execute("PRAGMA writable_schema=ON")
    conn.execute(
        "UPDATE sqlite_master SET sql = ? WHERE name = ?",
        (original_sql, index_name),
    )
    conn.execute("PRAGMA writable_schema=OFF")
    conn.close()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))


def _write_page_corrupt_db(path: Path) -> bytes:
    """Valid SQLite header, garbage pages — NON-index corruption class."""
    header = b"SQLite format 3\x00" + b"\x10\x00\x02\x02\x00\x40\x20\x20"
    header += b"\x00\x00\x00\x0c\x00\x00\x23\x46\x00\x00\x00\x00"
    header = header.ljust(100, b"\x00")
    blob = header + b"definitely not a valid sqlite page \x00\x01\x02\x03" * 64
    path.write_bytes(blob)
    kb._INITIALIZED_PATHS.discard(str(path.resolve()))
    return blob


def _integrity_messages(db_path: Path) -> list[str]:
    conn = sqlite3.connect(db_path)
    try:
        return [r[0] for r in conn.execute("PRAGMA integrity_check").fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Index-error parsing (generic, no hardcoded index names)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Narrow auto-repair in the connect-time guard
# ---------------------------------------------------------------------------

def test_connect_auto_repairs_index_only_corruption(tmp_path, caplog):
    """Index-only integrity errors are REINDEXed and connect proceeds."""
    import logging

    db_path = tmp_path / "kanban.db"
    _build_board_db(db_path)
    _corrupt_index(db_path, "idx_tasks_status")

    # Precondition: the fixture really produced the index-scoped class.
    messages = _integrity_messages(db_path)
    assert any(m.startswith("wrong # of entries in index") for m in messages)
    assert kb._repairable_index_names(messages) == ["idx_tasks_status"]

    with caplog.at_level(logging.WARNING, logger="hermes_cli.kanban_db"):
        conn = kb.connect(db_path=db_path)
    try:
        # DB is clean again and data survived.
        row = conn.execute("PRAGMA integrity_check").fetchone()
        assert row[0] == "ok"
        titles = {t.title for t in kb.list_tasks(conn)}
        assert "task-0" in titles and "task-11" in titles
    finally:
        conn.close()
    assert "auto-repaired via REINDEX" in caplog.text

    # The corrupt bytes were quarantined BEFORE the repair mutated the file.
    backups = list(tmp_path.glob("kanban.db.corrupt.*.bak"))
    assert len(backups) == 1
    backup_messages_db = backups[0]
    # The backup still exhibits the pre-repair corruption.
    pre = _integrity_messages(backup_messages_db)
    assert any(m.startswith("wrong # of entries in index") for m in pre)








# ---------------------------------------------------------------------------
# Corrupt-backup retention cap
# ---------------------------------------------------------------------------

def test_corrupt_backup_retention_cap_prunes_oldest(tmp_path, monkeypatch):
    """Mutating corruption can't accumulate quarantine files forever.

    Regression for the field report of 124 ``.corrupt.*.bak`` files: each
    distinct corrupt byte-state mints a new content-addressed backup, so a
    board whose file keeps changing between failures grows one backup per
    mutation. The cap keeps only the newest ``_CORRUPT_BACKUP_RETENTION``.
    """
    monkeypatch.setattr(kb, "_CORRUPT_BACKUP_RETENTION", 3)
    db_path = tmp_path / "kanban.db"
    _write_page_corrupt_db(db_path)

    minted: list[Path] = []
    for i in range(8):
        # Mutate the corrupt bytes → new sha → new backup each round.
        with db_path.open("r+b") as fh:
            fh.seek(200)
            fh.write(bytes([i]) * 16)
        kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
        with pytest.raises(kb.KanbanDbCorruptError) as excinfo:
            kb.connect(db_path=db_path)
        assert excinfo.value.backup_path is not None
        minted.append(excinfo.value.backup_path)
        # The just-created backup always survives its own prune pass.
        assert excinfo.value.backup_path.exists()

    remaining = sorted(tmp_path.glob("kanban.db.corrupt.*.bak"))
    assert len(remaining) == 3, (
        f"expected retention cap of 3, found {len(remaining)}: {remaining}"
    )
    # The newest backup (this round's) is among the survivors.
    assert minted[-1] in remaining






# ---------------------------------------------------------------------------
# Periodic WAL checkpoint on the dispatcher tick path
# ---------------------------------------------------------------------------

class _ConnProxy:
    """Delegating wrapper so tests can observe/deny wal_checkpoint PRAGMAs.

    ``sqlite3.Connection`` is an immutable C type — its methods cannot be
    monkeypatched — so the spy wraps the connection object instead.
    """

    def __init__(self, conn, recorded, fail_checkpoint=False):
        self._conn = conn
        self._recorded = recorded
        self._fail_checkpoint = fail_checkpoint

    def execute(self, sql, *args, **kwargs):
        if "wal_checkpoint" in str(sql).lower():
            self._recorded.append(str(sql))
            if self._fail_checkpoint:
                raise sqlite3.OperationalError("database is locked")
        return self._conn.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._conn, name)


def test_dispatch_tick_runs_wal_checkpoint_at_interval(tmp_path, monkeypatch):
    """First tick checkpoints; ticks inside the interval don't; after the
    interval elapses the next tick checkpoints again."""
    db_path = tmp_path / "kanban.db"
    _build_board_db(db_path, tasks=1)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(db_path))
    # Fresh per-path clock so previous tests can't have claimed the slot.
    monkeypatch.setattr(kb, "_LAST_WAL_CHECKPOINT", {})

    executed: list[str] = []
    conn = kb.connect(db_path=db_path)
    proxy = _ConnProxy(conn, executed)
    try:
        kb.dispatch_once(proxy, spawn_fn=lambda *a, **k: None, dry_run=True)
        assert len(executed) == 1, "first tick should checkpoint"

        kb.dispatch_once(proxy, spawn_fn=lambda *a, **k: None, dry_run=True)
        kb.dispatch_once(proxy, spawn_fn=lambda *a, **k: None, dry_run=True)
        assert len(executed) == 1, "ticks inside the interval must not checkpoint"

        # Age the per-path timestamp past the interval → next tick fires.
        key = str(db_path.resolve())
        kb._LAST_WAL_CHECKPOINT[key] -= (
            kb._WAL_CHECKPOINT_INTERVAL_SECONDS + 1.0
        )
        kb.dispatch_once(proxy, spawn_fn=lambda *a, **k: None, dry_run=True)
        assert len(executed) == 2, "tick after the interval should checkpoint"
        assert all("TRUNCATE" in sql.upper() for sql in executed)
    finally:
        conn.close()






# ---------------------------------------------------------------------------
# repair_db() API + `hermes kanban repair` CLI verb
# ---------------------------------------------------------------------------

def _run_kanban_cli(argv: list[str]) -> int:
    """Drive the real argparse surface exactly like `hermes kanban …`."""
    import argparse

    from hermes_cli import kanban as kc

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", *argv])
    return kc.kanban_command(args)


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME so kanban_db_path() resolves inside tmp_path."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return home








def test_cli_repair_json_shape(cli_home, capsys):
    db_path = kb.kanban_db_path()
    _build_board_db(db_path)
    _corrupt_index(db_path, "idx_tasks_status")

    rc = _run_kanban_cli(["repair", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "repaired"
    assert payload["reindexed"] == ["idx_tasks_status"]
    assert payload["backup_path"]
    assert Path(payload["backup_path"]).exists()


# ---------------------------------------------------------------------------
# Observability signal — VFE-KANBAN-CORRUPTION-02
# ---------------------------------------------------------------------------
#
# Every ``.corrupt.<hash>.bak`` quarantine event fires two always-on
# signals that operators/plugins can pick up without hermes-agent core
# growing a prometheus/NATS dependency:
#
# 1. A structured WARNING log record on ``hermes_cli.kanban_db`` with a
#    ``event="kanban_db_corrupt_quarantine"`` extra.
# 2. The ``kanban_db_corrupt_quarantine`` lifecycle hook, so any plugin
#    can translate it into a Prometheus counter / NATS event / alert.
#
# Both surfaces are wired at every corruption-detection site — the
# connect-time guard AND both branches of ``repair_db``.

def _read_quarantine_log_records(caplog) -> list:
    """Return the subset of caplog records that are quarantine notifications."""
    return [
        r
        for r in caplog.records
        if getattr(r, "event", None) == "kanban_db_corrupt_quarantine"
    ]


def test_board_slug_from_db_path_covers_layouts(tmp_path):
    """The reverse-map from DB path to board slug handles every real layout."""
    default_db = tmp_path / "kanban.db"
    default_db.touch()
    assert kb._board_slug_from_db_path(default_db) == kb.DEFAULT_BOARD

    named_db = tmp_path / "kanban" / "boards" / "my-board" / "kanban.db"
    named_db.parent.mkdir(parents=True)
    named_db.touch()
    assert kb._board_slug_from_db_path(named_db) == "my-board"

    # An HERMES_KANBAN_DB override or a test-only layout doesn't match either
    # convention — we surface a stable ``unknown`` label rather than raising.
    weird = tmp_path / "some-other-place.db"
    weird.touch()
    assert kb._board_slug_from_db_path(weird) == "unknown"


def test_guard_fires_quarantine_signals_on_page_corruption(tmp_path, caplog, monkeypatch):
    """Connect-time guard fires the log + hook when a DB fails integrity_check.

    Uses the page-corrupt DB fixture so this exercises the fail-closed path
    (no REINDEX repair possible). The hook must fire with the correct board
    label and backup path.
    """
    db_path = tmp_path / "kanban" / "boards" / "vfe-corrupt-02-test" / "kanban.db"
    db_path.parent.mkdir(parents=True)
    _write_page_corrupt_db(db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    hook_calls: list[dict] = []

    def fake_hook(event: str, task_id: str, **fields):
        hook_calls.append({"event": event, "task_id": task_id, **fields})

    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", fake_hook)

    with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
        with pytest.raises(kb.KanbanDbCorruptError):
            kb._guard_existing_db_is_healthy(db_path)

    # Log signal: exactly one structured record with the right extras.
    records = _read_quarantine_log_records(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    r = records[0]
    assert r.board == "vfe-corrupt-02-test"
    assert r.db_path == str(db_path)
    # The backup path may be non-empty (successful) or empty (backup failed);
    # both are legitimate — the signal fires either way.
    assert hasattr(r, "backup_path")
    assert r.reason  # a non-empty explanation string

    # Hook signal: fired exactly once, with the same fields.
    quarantine_calls = [c for c in hook_calls if c["event"] == "kanban_db_corrupt_quarantine"]
    assert len(quarantine_calls) == 1
    call = quarantine_calls[0]
    assert call["task_id"] == ""  # board-scoped, not task-scoped
    assert call["board"] == "vfe-corrupt-02-test"
    assert call["db_path"] == str(db_path)
    assert call["reason"]


def test_guard_fires_quarantine_signal_once_on_index_repair_success(
    tmp_path, caplog, monkeypatch
):
    """Index-only corruption that REINDEX fixes STILL fires the signal.

    Operators need to see the corruption event even when we auto-recover,
    because the counter must reflect DB damage rate, not un-recovered damage
    rate.
    """
    db_path = tmp_path / "kanban" / "boards" / "vfe-index-repair-test" / "kanban.db"
    db_path.parent.mkdir(parents=True)
    _build_board_db(db_path)
    _corrupt_index(db_path, "idx_tasks_status")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    hook_calls: list[dict] = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda event, task_id, **fields: hook_calls.append(
            {"event": event, "task_id": task_id, **fields}
        ),
    )

    with caplog.at_level("WARNING", logger="hermes_cli.kanban_db"):
        # No exception: REINDEX repair succeeds.
        kb._guard_existing_db_is_healthy(db_path)

    records = _read_quarantine_log_records(caplog)
    assert len(records) == 1, [r.getMessage() for r in caplog.records]
    assert records[0].board == "vfe-index-repair-test"

    quarantine_calls = [c for c in hook_calls if c["event"] == "kanban_db_corrupt_quarantine"]
    assert len(quarantine_calls) == 1
    assert quarantine_calls[0]["board"] == "vfe-index-repair-test"


def test_repair_db_fires_quarantine_signal_on_page_corruption(tmp_path, monkeypatch):
    """``repair_db`` fires the same signal on the sqlite-refused-to-open branch."""
    db_path = tmp_path / "kanban" / "boards" / "vfe-repair-refuse-test" / "kanban.db"
    db_path.parent.mkdir(parents=True)
    _write_page_corrupt_db(db_path)

    hook_calls: list[dict] = []
    monkeypatch.setattr(
        kb,
        "_fire_kanban_lifecycle_hook",
        lambda event, task_id, **fields: hook_calls.append(
            {"event": event, "task_id": task_id, **fields}
        ),
    )

    result = kb.repair_db(db_path=db_path)
    assert result.status == "corrupt"

    quarantine_calls = [c for c in hook_calls if c["event"] == "kanban_db_corrupt_quarantine"]
    assert len(quarantine_calls) == 1
    assert quarantine_calls[0]["board"] == "vfe-repair-refuse-test"


def test_broken_hook_does_not_break_quarantine(tmp_path, monkeypatch):
    """A plugin observer that raises must not stop us quarantining a corrupt DB.

    The corruption path is safety-critical: an angry plugin cannot be allowed
    to prevent the fail-closed backup + raise. Both signals are best-effort.
    """
    db_path = tmp_path / "kanban" / "boards" / "vfe-broken-hook-test" / "kanban.db"
    db_path.parent.mkdir(parents=True)
    _write_page_corrupt_db(db_path)
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    def angry_hook(event, task_id, **fields):
        raise RuntimeError("plugin exploded")

    monkeypatch.setattr(kb, "_fire_kanban_lifecycle_hook", angry_hook)

    with pytest.raises(kb.KanbanDbCorruptError):
        kb._guard_existing_db_is_healthy(db_path)

    # And the ``.corrupt.<hash>.bak`` backup landed on disk despite the
    # plugin blowing up — the whole point of best-effort observability.
    backups = list(db_path.parent.glob(f"{db_path.name}.corrupt.*.bak"))
    assert len(backups) == 1


