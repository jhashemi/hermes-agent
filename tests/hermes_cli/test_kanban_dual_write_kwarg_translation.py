"""Regression: kanban_dual_write must translate the SQLite kwarg name
``claimer=`` to the DuckDB adapter's ``lock=`` before invoking the mirror
op, for the two claim-lifecycle ops that use it (``claim_task`` and
``heartbeat_claim``).

Bug (t_192a3e6b): The DuckDB ``heartbeat_claim`` adapter signature made
``lock`` a required keyword-only argument. The outer SQLite dispatcher
passes ``claimer=<lock_id>`` in ``**kwargs``; the shim's
``_filtered_kwargs`` strips it (DuckDB's function has no ``claimer``
parameter) and the adapter call blows up:

    TypeError: heartbeat_claim() missing 1 required
               keyword-only argument: 'lock'

Every failure emitted ``logger.exception`` at ERROR level, which the
``hermes_log_error_scan`` probe re-classified as a YELLOW kanban ticket
— exactly the loop 93127c1115 was designed to prevent, just for a
different exception class.

Fix: ``_MIRROR_KWARG_RENAMES`` + ``_rename_kwargs`` translate outer
kwargs (``claimer``) to adapter kwargs (``lock``) before the signature
filter. Verify:

  1. The pure translation helper ``_rename_kwargs`` renames known aliases
     for the two mapped ops and leaves everything else untouched.
  2. The end-to-end wrapper path for ``heartbeat_claim`` invokes the
     DuckDB adapter with ``lock=<claimer_value>`` and no ``TypeError``
     leaks to logs.
  3. ``claim_task`` also translates — the mirror row must land under the
     SAME lock identity as SQLite so subsequent heartbeat mirrors match
     on the ``claim_lock`` comparison in ``heartbeat_claim``.
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_dual_write as dw


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _RecordingAdapter:
    """Stand-in for hermes_kanban.duckdb_kanban_adapter.

    Exposes ``heartbeat_claim`` and ``claim_task`` with signatures that
    mirror the real DuckDB adapter (``lock`` — required for
    ``heartbeat_claim``, defaulted for ``claim_task``). Records the exact
    kwargs each call receives so tests can assert the shim translated
    them correctly before invoking us.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def duckdb_kanban_path(self, sqlite_path: Path) -> Path:
        return sqlite_path.with_suffix(".duckdb")

    def connect(self, path):  # returns a bare object used as ``duck``
        return MagicMock(name="duckdb_conn")

    def heartbeat_claim(
        self,
        conn: Any,
        task_id: str,
        *,
        lock: str,  # required — matches real DuckDB signature
        ttl_seconds: Optional[int] = None,
        note: Optional[str] = None,
    ) -> bool:
        self.calls.append(
            ("heartbeat_claim", (task_id,),
             {"lock": lock, "ttl_seconds": ttl_seconds, "note": note}),
        )
        return True

    def claim_task(
        self,
        conn: Any,
        task_id: str,
        *,
        lock: Optional[str] = None,
        ttl_seconds: Optional[int] = None,
        profile: Optional[str] = None,
        step_key: Optional[str] = None,
        max_runtime_seconds: Optional[int] = None,
        run_id: Optional[int] = None,
    ) -> Optional[int]:
        self.calls.append(
            ("claim_task", (task_id,),
             {
                 "lock": lock,
                 "ttl_seconds": ttl_seconds,
                 "profile": profile,
                 "step_key": step_key,
                 "max_runtime_seconds": max_runtime_seconds,
                 "run_id": run_id,
             }),
        )
        return run_id or 42


@pytest.fixture(autouse=True)
def _reset_state():
    dw._DUCK_CONNS.clear()
    dw._DUCK_LOCK_CONTENDED.clear()
    yield
    dw._DUCK_CONNS.clear()
    dw._DUCK_LOCK_CONTENDED.clear()


@pytest.fixture
def sqlite_conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE probe (x INT)")
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# Unit: pure helper
# --------------------------------------------------------------------------- #


def test_rename_kwargs_renames_claimer_to_lock_for_heartbeat_claim():
    """The pure helper must rename ``claimer`` → ``lock`` for the two
    mapped ops and leave the rest of the kwargs untouched."""
    out = dw._rename_kwargs(
        "heartbeat_claim",
        {"claimer": "worker-abc", "ttl_seconds": 900, "note": "hb"},
    )
    assert out == {"lock": "worker-abc", "ttl_seconds": 900, "note": "hb"}


def test_rename_kwargs_renames_claimer_to_lock_for_claim_task():
    out = dw._rename_kwargs(
        "claim_task",
        {"claimer": "worker-abc", "ttl_seconds": 900, "profile": "jeff_dean"},
    )
    assert out == {"lock": "worker-abc", "ttl_seconds": 900, "profile": "jeff_dean"}


def test_rename_kwargs_no_map_returns_input_untouched():
    """Unknown op → identity."""
    inp = {"claimer": "x", "ttl_seconds": 60}
    out = dw._rename_kwargs("complete_task", inp)
    assert out == inp


def test_rename_kwargs_no_source_key_returns_input_untouched():
    """Mapped op but no ``claimer`` present → identity (no-op)."""
    inp = {"ttl_seconds": 60, "note": "n"}
    out = dw._rename_kwargs("heartbeat_claim", inp)
    assert out == inp


def test_rename_kwargs_collision_prefers_explicit_destination():
    """If both ``claimer`` and ``lock`` are set, keep ``lock`` and
    drop the alias — defensive against future call sites that migrate
    to the canonical name."""
    out = dw._rename_kwargs(
        "heartbeat_claim",
        {"claimer": "old-name", "lock": "new-name", "ttl_seconds": 60},
    )
    assert out == {"lock": "new-name", "ttl_seconds": 60}


def test_rename_kwargs_does_not_mutate_input():
    """Helper returns a fresh dict; callers must not observe aliasing."""
    inp = {"claimer": "x"}
    out = dw._rename_kwargs("heartbeat_claim", inp)
    assert inp == {"claimer": "x"}  # untouched
    assert out == {"lock": "x"}
    assert out is not inp


# --------------------------------------------------------------------------- #
# Integration: end-to-end wrapper path
# --------------------------------------------------------------------------- #


def _fake_outer_heartbeat_claim(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> bool:
    """Stand-in for the outer SQLite ``heartbeat_claim`` signature.

    Matches ``hermes_cli.kanban_db.heartbeat_claim`` — critically uses
    ``claimer=`` (not ``lock=``). The wrapper's job is to translate
    that name before the DuckDB adapter is called.
    """
    return True


def _fake_outer_claim_task(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[int]:
    return 42


def test_wrapper_translates_claimer_to_lock_for_heartbeat_claim(
    sqlite_conn, caplog
):
    """End-to-end: the wrapper must invoke DuckDB ``heartbeat_claim``
    with ``lock=<claimer_value>`` and NOT raise/log a TypeError.
    """
    adapter = _RecordingAdapter()
    wrapped = dw._make_mirror_wrapper(
        _fake_outer_heartbeat_claim,
        adapter_op_name="heartbeat_claim",
        id_passthrough_kw="",
    )

    with patch.object(dw, "mirror_enabled", return_value=True), \
         patch.object(dw, "_load_facade", return_value=(MagicMock(), adapter)):
        with caplog.at_level(logging.DEBUG, logger=dw.logger.name):
            result = wrapped(
                sqlite_conn,
                "t_abc123",
                ttl_seconds=900,
                claimer="hermes2:1625799",
            )

    # SQLite side succeeded.
    assert result is True

    # DuckDB adapter invoked exactly once with ``lock=`` translated
    # from ``claimer=`` — this is the whole point of the fix.
    assert len(adapter.calls) == 1
    op, args, kwargs = adapter.calls[0]
    assert op == "heartbeat_claim"
    assert args == ("t_abc123",)
    assert kwargs["lock"] == "hermes2:1625799"
    assert kwargs["ttl_seconds"] == 900
    # And the original alias must not leak through.
    assert "claimer" not in kwargs

    # No ERROR-level logs — pre-fix this path emitted logger.exception
    # for TypeError("missing 1 required keyword-only argument: 'lock'").
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == [], (
        "wrapper must not log ERROR — got: "
        + repr([r.getMessage() for r in errors])
    )


def test_wrapper_translates_claimer_to_lock_for_claim_task(sqlite_conn, caplog):
    """``claim_task`` translation is silent-drop, not TypeError (DuckDB
    has ``lock: Optional[str] = None``), but the mirror row would land
    under the wrong lock id — sanity-check the rename applies here too.
    """
    adapter = _RecordingAdapter()
    wrapped = dw._make_mirror_wrapper(
        _fake_outer_claim_task,
        adapter_op_name="claim_task",
        id_passthrough_kw="run_id",
    )

    with patch.object(dw, "mirror_enabled", return_value=True), \
         patch.object(dw, "_load_facade", return_value=(MagicMock(), adapter)):
        with caplog.at_level(logging.DEBUG, logger=dw.logger.name):
            result = wrapped(
                sqlite_conn,
                "t_xyz789",
                ttl_seconds=900,
                claimer="hermes2:2001",
            )

    assert result == 42
    assert len(adapter.calls) == 1
    op, args, kwargs = adapter.calls[0]
    assert op == "claim_task"
    assert kwargs["lock"] == "hermes2:2001"
    # run_id passthrough from SQLite return value still works alongside
    # the rename.
    assert kwargs["run_id"] == 42
    assert "claimer" not in kwargs

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == []


def test_wrapper_regression_reproduces_pre_fix_typeerror_without_rename(
    sqlite_conn, caplog
):
    """Belt-and-braces: with the rename map emptied, the same call path
    must once again produce the exact TypeError logged in the bug
    ticket. This proves the rename is what fixes the bug (not some
    incidental change elsewhere).
    """
    adapter = _RecordingAdapter()
    wrapped = dw._make_mirror_wrapper(
        _fake_outer_heartbeat_claim,
        adapter_op_name="heartbeat_claim",
        id_passthrough_kw="",
    )

    empty_map: dict[str, dict[str, str]] = {}
    with patch.object(dw, "mirror_enabled", return_value=True), \
         patch.object(dw, "_load_facade", return_value=(MagicMock(), adapter)), \
         patch.object(dw, "_MIRROR_KWARG_RENAMES", empty_map):
        with caplog.at_level(logging.DEBUG, logger=dw.logger.name):
            result = wrapped(
                sqlite_conn,
                "t_abc123",
                ttl_seconds=900,
                claimer="hermes2:1625799",
            )

    # SQLite side still succeeds — mirror failure is non-fatal.
    assert result is True

    # Exactly one ERROR-level log carrying the exact TypeError signature
    # from the ticket.
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, (
        f"pre-fix path must emit 1 ERROR; got {len(errors)}"
    )
    assert errors[0].exc_info is not None
    exc_type, exc_val, _ = errors[0].exc_info
    assert exc_type is TypeError
    assert "'lock'" in str(exc_val)

    # Adapter was NOT successfully called.
    assert adapter.calls == []
