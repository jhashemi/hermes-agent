"""Regression: kanban_dual_write must not log ERROR+traceback on the
known ERR-DRIVE-01 signature of DuckDB single-writer lock contention.

Chronic issue: two hermes processes on the same host each try to open
the DuckDB mirror at ``<board>/kanban.duckdb`` with an exclusive file
lock. One wins, the loser used to log a full ``logger.exception`` at
ERROR, which the ``hermes_log_error_scan`` probe then re-filed as a
YELLOW kanban ticket every ~30 minutes.

The mirror is best-effort (SQLite is authoritative), so the contention
is benign. Verify:

  1. IOException("... Conflicting lock is held ...") triggers a single
     WARNING (no traceback) and returns None.
  2. Subsequent calls short-circuit — no repeat log, no repeat connect.
  3. Non-contention exceptions still hit ``logger.exception`` at ERROR.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_dual_write as dw


class _FakeAdapter:
    """Stand-in for hermes_kanban.duckdb_kanban_adapter."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc
        self.connect_calls = 0

    def duckdb_kanban_path(self, sqlite_path: Path) -> Path:
        return sqlite_path.with_suffix(".duckdb")

    def connect(self, path):
        self.connect_calls += 1
        raise self._exc


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


def test_lock_contention_logs_warning_no_traceback(tmp_path, caplog):
    fake_adapter = _FakeAdapter(
        IOError(
            'IO Error: Could not set lock on file '
            '"kanban.duckdb": Conflicting lock is held in '
            '/usr/bin/python3.12 (PID 3092157) by user ubuntu.'
        )
    )
    conn = _sqlite_conn(tmp_path)

    with patch.object(dw, "_load_facade", return_value=(MagicMock(), fake_adapter)):
        with caplog.at_level(logging.DEBUG, logger=dw.logger.name):
            result = dw._duck_conn_for(conn)

    assert result is None
    # Exactly one WARNING, no ERROR, no traceback string in the message.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
    assert len(errors) == 0, f"expected 0 ERROR, got {len(errors)}"
    assert warnings[0].exc_info is None, "WARNING must not carry a traceback"
    assert "Conflicting lock is held" in warnings[0].getMessage()


def test_lock_contention_short_circuits_on_repeat(tmp_path, caplog):
    fake_adapter = _FakeAdapter(
        IOError("IO Error ... Conflicting lock is held in /usr/bin/python3.12")
    )
    conn = _sqlite_conn(tmp_path)

    with patch.object(dw, "_load_facade", return_value=(MagicMock(), fake_adapter)):
        with caplog.at_level(logging.DEBUG, logger=dw.logger.name):
            for _ in range(5):
                assert dw._duck_conn_for(conn) is None

    # adapter.connect() called exactly once; second call short-circuits
    # via _DUCK_LOCK_CONTENDED before hitting adapter.
    assert fake_adapter.connect_calls == 1
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, "should only WARN once per (process, path)"


def test_non_contention_exception_still_logs_traceback(tmp_path, caplog):
    fake_adapter = _FakeAdapter(RuntimeError("something else entirely"))
    conn = _sqlite_conn(tmp_path)

    with patch.object(dw, "_load_facade", return_value=(MagicMock(), fake_adapter)):
        with caplog.at_level(logging.DEBUG, logger=dw.logger.name):
            result = dw._duck_conn_for(conn)

    assert result is None
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, "novel exceptions still get logger.exception"
    # logger.exception attaches sys.exc_info() to the record.
    assert errors[0].exc_info is not None
