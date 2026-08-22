"""Regression: t_06bc95c3 — kanban_dual_write must NOT hold a persistent
DuckDB connection.

Chronic issue (parent audit t_fee22c61, check 7): the long-lived
``hermes-gateway`` process opened one DuckDB connection per kanban board
mirror on first write and stashed it in a module-level ``_DUCK_CONNS``
dict for the process lifetime. Because DuckDB takes an exclusive OS
file lock even for RW connections, every 5-min ``kanban-parity`` cron
run failed to open any of the ~14 board mirrors with:

    IO Error: Could not set lock on file "…/kanban.duckdb":
    Conflicting lock is held in /usr/bin/python3.12 (PID …) by user ubuntu.

The fix is to open→write→close per mirror op. This module verifies:

  1. ``_mirror_conn_for`` yields a fresh connection and closes it in
     ``finally`` — the returned conn is unusable after the ``with`` block.
  2. Multiple sequential calls each get a fresh connection; the module
     does not cache handles in ``_DUCK_CONNS``.
  3. The wrapped write ops installed by ``install()`` release the mirror
     connection after each call — a second consumer (parity, sibling
     hermes process) can open the same file immediately after.
  4. Lock-contention short-circuit still works (loser-side WARN once).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_dual_write as dw


class _FakeAdapter:
    """Stand-in for hermes_kanban.duckdb_kanban_adapter with an in-memory
    "duckdb" that just tracks connect/close calls and can raise on demand.
    """

    def __init__(self, exc: Exception | None = None) -> None:
        self._exc = exc
        self.connects: list[str] = []
        self.closes: int = 0
        self.mock_conn = MagicMock(name="duck_conn")
        # Wire close() so we can count it.
        def _close():
            self.closes += 1
        self.mock_conn.close.side_effect = _close

    def duckdb_kanban_path(self, sqlite_path: Path) -> Path:
        return Path(str(sqlite_path).replace(".db", ".duckdb"))

    def connect(self, path):
        self.connects.append(str(path))
        if self._exc is not None:
            raise self._exc
        return self.mock_conn


@pytest.fixture(autouse=True)
def _reset_state():
    dw._DUCK_CONNS.clear()
    dw._DUCK_LOCK_CONTENDED.clear()
    yield
    dw._DUCK_CONNS.clear()
    dw._DUCK_LOCK_CONTENDED.clear()


def _sqlite_conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE probe (x INT)")
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# _mirror_conn_for behavior — the core context manager
# --------------------------------------------------------------------------- #


def test_mirror_conn_for_yields_and_closes(tmp_path):
    """Fresh conn per call; close() runs in the finally block."""
    adapter = _FakeAdapter()
    conn = _sqlite_conn(tmp_path)

    with patch.object(dw, "_load_facade", return_value=(MagicMock(), adapter)):
        with dw._mirror_conn_for(conn) as duck:
            assert duck is adapter.mock_conn
            # Close must NOT have run yet — we're inside the with-block.
            assert adapter.closes == 0
        # Exit → close() runs.
        assert adapter.closes == 1

    # Also: nothing was cached at rest.
    assert not dw._DUCK_CONNS


def test_mirror_conn_for_closes_even_if_body_raises(tmp_path):
    """close() must run when the body raises — this is what releases the OS
    file lock so other processes can open the DuckDB mirror."""
    adapter = _FakeAdapter()
    conn = _sqlite_conn(tmp_path)

    with patch.object(dw, "_load_facade", return_value=(MagicMock(), adapter)):
        with pytest.raises(RuntimeError):
            with dw._mirror_conn_for(conn) as duck:
                assert duck is adapter.mock_conn
                raise RuntimeError("mirror op failed mid-flight")
        assert adapter.closes == 1


def test_mirror_conn_for_no_cache_across_calls(tmp_path):
    """Every call opens a fresh conn — no `_DUCK_CONNS` caching. This is
    the ADR-012 §"Concurrent multi-host writers" fix — DuckDB's file lock
    is exclusive, so we MUST release it between ops."""
    adapter = _FakeAdapter()
    conn = _sqlite_conn(tmp_path)

    with patch.object(dw, "_load_facade", return_value=(MagicMock(), adapter)):
        for _ in range(4):
            with dw._mirror_conn_for(conn) as duck:
                assert duck is adapter.mock_conn

    assert len(adapter.connects) == 4, (
        "expected 4 fresh opens, got %d — caching regressed" % len(adapter.connects)
    )
    assert adapter.closes == 4


def test_mirror_conn_for_lock_contention_yields_none(tmp_path, caplog):
    """When the mirror file is already locked by another hermes process,
    yield None and short-circuit subsequent calls without re-logging."""
    lock_exc = IOError(
        'IO Error: Could not set lock on file "kanban.duckdb": '
        'Conflicting lock is held in /usr/bin/python3.12 (PID 99999) by user ubuntu.'
    )
    adapter = _FakeAdapter(exc=lock_exc)
    conn = _sqlite_conn(tmp_path)

    with patch.object(dw, "_load_facade", return_value=(MagicMock(), adapter)):
        with caplog.at_level(logging.DEBUG, logger=dw.logger.name):
            with dw._mirror_conn_for(conn) as duck:
                assert duck is None
            with dw._mirror_conn_for(conn) as duck:
                assert duck is None

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(warnings) == 1, f"expected 1 WARNING on first contention, got {len(warnings)}"
    assert len(errors) == 0
    # Second call should have short-circuited BEFORE opening the adapter.
    assert len(adapter.connects) == 1


def test_mirror_conn_for_none_when_adapter_missing(tmp_path):
    """No hermes_kanban → mirror silently disabled."""
    conn = _sqlite_conn(tmp_path)
    with patch.object(dw, "_load_facade", return_value=(None, None)):
        with dw._mirror_conn_for(conn) as duck:
            assert duck is None


def test_mirror_conn_for_none_when_sqlite_in_memory():
    """In-memory SQLite has no on-disk path → mirror skipped."""
    conn = sqlite3.connect(":memory:")
    adapter = _FakeAdapter()
    with patch.object(dw, "_load_facade", return_value=(MagicMock(), adapter)):
        with dw._mirror_conn_for(conn) as duck:
            assert duck is None
    assert adapter.connects == []


# --------------------------------------------------------------------------- #
# _make_mirror_wrapper — end-to-end: each wrapped op opens+closes
# --------------------------------------------------------------------------- #


def _fake_kanban_module(sqlite_return=42):
    """A minimal fake ``hermes_cli.kanban_db``-like module with one write op."""
    mod = ModuleType("_fake_kanban_module")

    def create_task(conn, title, body=None, **kwargs):
        return sqlite_return  # pretend SQLite returned a fresh task id

    mod.create_task = create_task
    return mod


def test_wrapped_op_opens_and_closes_per_call(tmp_path, monkeypatch):
    """The wrapped op must open a mirror conn, use it, then close it BEFORE
    returning. This is what stops the gateway from holding lock N forever."""
    monkeypatch.setenv("HERMES_KANBAN_WRITE_BACKEND", "dual")
    adapter = _FakeAdapter()
    adapter.create_task = MagicMock()  # adapter's mirror op

    fake_mod = _fake_kanban_module(sqlite_return=42)
    conn = _sqlite_conn(tmp_path)

    with patch.object(dw, "_load_facade", return_value=(MagicMock(), adapter)):
        dw.install(fake_mod)
        # Call the wrapped op 3 times.
        for _ in range(3):
            r = fake_mod.create_task(conn, "hello")
            assert r == 42  # SQLite return still authoritative

    # Every call opened one mirror conn and closed it.
    assert len(adapter.connects) == 3
    assert adapter.closes == 3
    # And the adapter's mirror op ran 3 times.
    assert adapter.create_task.call_count == 3
    # `_DUCK_CONNS` remains empty — no cache leak.
    assert not dw._DUCK_CONNS


def test_wrapped_op_closes_even_when_mirror_op_raises(tmp_path, monkeypatch):
    """If the adapter's write op raises, we STILL close the mirror conn.
    Missing this is what caused the original bug — a raised close-path
    would leave the file lock held forever."""
    monkeypatch.setenv("HERMES_KANBAN_WRITE_BACKEND", "dual")
    adapter = _FakeAdapter()
    adapter.create_task = MagicMock(side_effect=RuntimeError("mirror kaboom"))

    fake_mod = _fake_kanban_module(sqlite_return=7)
    conn = _sqlite_conn(tmp_path)

    with patch.object(dw, "_load_facade", return_value=(MagicMock(), adapter)):
        dw.install(fake_mod)
        r = fake_mod.create_task(conn, "hello")

    # Best-effort mirror: SQLite return is authoritative and unaffected.
    assert r == 7
    # Adapter open + close ran exactly once each — the raised mirror op did
    # not skip the close.
    assert len(adapter.connects) == 1
    assert adapter.closes == 1


def test_wrapped_op_in_sqlite_mode_never_opens_mirror(tmp_path, monkeypatch):
    """`HERMES_KANBAN_WRITE_BACKEND=sqlite` (or unset) → wrappers still
    installed but never touch DuckDB. Preserved from the pre-fix behavior."""
    monkeypatch.delenv("HERMES_KANBAN_WRITE_BACKEND", raising=False)  # default sqlite
    adapter = _FakeAdapter()
    fake_mod = _fake_kanban_module(sqlite_return=99)
    conn = _sqlite_conn(tmp_path)

    with patch.object(dw, "_load_facade", return_value=(MagicMock(), adapter)):
        dw.install(fake_mod)
        r = fake_mod.create_task(conn, "hello")

    assert r == 99
    assert adapter.connects == []
    assert adapter.closes == 0
