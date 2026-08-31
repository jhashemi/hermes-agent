"""Regression: t_3f87ac16 — claim_task mirror must bind the run id, not
the whole Task dataclass.

Bug: the outer SQLite ``kanban_db.claim_task`` returns the claimed
``Task`` dataclass (not the run id). The dual-write shim's id-passthrough
site blindly assigned that return value into ``mirror_kwargs["run_id"]``,
which was then bound as a parameter in the DuckDB adapter's
``INSERT INTO task_runs`` — and DuckDB has no transformation for an
arbitrary Python dataclass:

    _duckdb.NotImplementedException: Not implemented Error: Unable to
    transform python value of type '<class 'hermes_cli.kanban_db.Task'>'
    to DuckDB LogicalType

Every dual-mode ``claim_task`` emitted a full ERROR traceback (71 hits in
backend-eng/jeff_dean errors.log on 2026-08-22) feeding the ERR-DRIVE-01
auto-triage probe, and the mirror never received run rows for claims —
permanently stale for parity checks on ``task_runs`` / ``current_run_id``.

Fix: the shim unwraps the id from the op's return value before binding
(``_ID_PASSTHROUGH_UNWRAP``: claim_task -> ``Task.current_run_id``) and
drops (WARNING, no traceback) any passthrough value that still is not a
bindable primitive, so a future object-returning op can never re-trigger
the bind storm.

Verify:
  1. Unit: the unwrap helper reduces a Task-like object to its
     ``current_run_id`` for claim_task and leaves primitives untouched
     for the other passthrough ops (create_task -> str, add_comment ->
     int).
  2. End-to-end (REAL SQLite board + REAL DuckDB mirror + REAL adapter,
     no fakes): a dual-mode claim produces NO ERROR log and lands a
     mirror ``task_runs`` row whose id equals SQLite's run id (primary-
     key parity), with ``tasks.current_run_id`` matching on both sides.
  3. Belt-and-braces: with the unwrap map emptied, the same call path
     reproduces the ticket's exact failure shape (non-primitive run_id
     reaches the adapter; ERROR logged; mirror not written).
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_dual_write as dw


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _TaskLike:
    """Minimal stand-in for kanban_db.Task carrying current_run_id."""

    def __init__(self, current_run_id: Optional[int]) -> None:
        self.current_run_id = current_run_id


class _BindCheckingAdapter:
    """Stand-in for hermes_kanban.duckdb_kanban_adapter.

    Records claim_task kwargs and reproduces the ticket's failure mode:
    raises ``NotImplementedError`` (the duckdb bind error class seen in
    errors.log) when ``run_id`` is bound as a non-primitive — exactly
    what the real adapter's INSERT does with a Task dataclass.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.bind_failures: int = 0

    def duckdb_kanban_path(self, sqlite_path: Path) -> Path:
        return sqlite_path.with_suffix(".duckdb")

    def connect(self, path):  # pragma: no cover — unused in unit tests
        return MagicMock(name="duckdb_conn")

    def claim_task(self, conn, task_id, *, run_id=None, **_: Any) -> Optional[int]:
        self.calls.append({"task_id": task_id, "run_id": run_id})
        if run_id is not None and not isinstance(run_id, int):
            self.bind_failures += 1
            raise NotImplementedError(
                "Not implemented Error: Unable to transform python value "
                f"of type '{type(run_id)}' to DuckDB LogicalType"
            )
        return run_id


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
# Unit: unwrap helper
# --------------------------------------------------------------------------- #


def test_unwrap_claim_task_result_to_run_id():
    """claim_task's Task return must reduce to current_run_id."""
    out = dw._mirror_id_for("claim_task", _TaskLike(current_run_id=1187))
    assert out == 1187


def test_unwrap_claim_task_result_without_run_id_is_dropped():
    """Defensive: a Task-like with no run id cannot be bound — drop to
    None rather than forwarding the object."""
    out = dw._mirror_id_for("claim_task", _TaskLike(current_run_id=None))
    assert out is None


def test_unwrap_primitives_pass_through_untouched():
    """create_task returns the id str; add_comment returns row int. The
    helper must not disturb them."""
    assert dw._mirror_id_for("create_task", "t_abcd1234") == "t_abcd1234"
    assert dw._mirror_id_for("add_comment", 42) == 42


def test_unwrap_none_stays_none():
    """Failed ops (None return) skip passthrough entirely."""
    assert dw._mirror_id_for("claim_task", None) is None


# --------------------------------------------------------------------------- #
# Wrapper path (fake adapter): bindability enforced before the adapter
# --------------------------------------------------------------------------- #


def _fake_outer_claim_task(conn, task_id, *, ttl_seconds=None, claimer=None):
    """Stand-in returning a Task-like, as the real SQLite op does."""
    return _TaskLike(current_run_id=1187)


def test_wrapper_binds_run_id_not_task_object(sqlite_conn, caplog):
    """The mirror adapter must receive run_id=<int>, never the Task."""
    adapter = _BindCheckingAdapter()
    wrapped = dw._make_mirror_wrapper(
        _fake_outer_claim_task,
        adapter_op_name="claim_task",
        id_passthrough_kw="run_id",
    )

    with patch.object(dw, "mirror_enabled", return_value=True), \
         patch.object(dw, "_load_facade", return_value=(MagicMock(), adapter)):
        with caplog.at_level(logging.DEBUG, logger=dw.logger.name):
            result = wrapped(sqlite_conn, "t_abc123")

    # SQLite return value is authoritative and untouched.
    assert isinstance(result, _TaskLike)
    # Adapter got the primitive run id — and no bind failure.
    assert len(adapter.calls) == 1
    assert adapter.calls[0]["run_id"] == 1187
    assert adapter.bind_failures == 0
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == [], (
        "wrapper must not log ERROR — got: "
        + repr([r.getMessage() for r in errors])
    )


def test_wrapper_regression_reproduces_pre_fix_bind_error(sqlite_conn, caplog):
    """Belt-and-braces: with the unwrap map emptied (simulating a
    regression of the t_3f87ac16 fix's first layer), the bindability
    guard must STILL keep the object away from the DuckDB binder — a
    WARNING (no traceback), the adapter never sees a non-primitive
    run_id, and no ERROR is logged. The pre-fix bind storm
    (NotImplementedException on every claim) is therefore unreachable
    even if only one of the two layers survives."""
    adapter = _BindCheckingAdapter()
    wrapped = dw._make_mirror_wrapper(
        _fake_outer_claim_task,
        adapter_op_name="claim_task",
        id_passthrough_kw="run_id",
    )

    with patch.object(dw, "mirror_enabled", return_value=True), \
         patch.object(dw, "_load_facade", return_value=(MagicMock(), adapter)), \
         patch.object(dw, "_ID_PASSTHROUGH_UNWRAP", {}):
        with caplog.at_level(logging.DEBUG, logger=dw.logger.name):
            result = wrapped(sqlite_conn, "t_abc123")

    # SQLite side still succeeds — mirror failure is non-fatal.
    assert isinstance(result, _TaskLike)

    # Adapter WAS invoked (mirror still attempted) but never received a
    # bindable object: run_id either absent or None, zero bind failures.
    assert len(adapter.calls) == 1
    assert not isinstance(adapter.calls[0]["run_id"], _TaskLike)
    assert adapter.bind_failures == 0

    # Exactly one WARNING — and NO ERROR-level record at all (the
    # ERR-DRIVE-01 probe keys on ERROR tracebacks).
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == [], (
        "even with the unwrap map regressed, no ERROR may be logged; got: "
        + repr([r.getMessage() for r in errors])
    )
    warns = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "cannot bind" in r.getMessage()
    ]
    assert len(warns) == 1


# --------------------------------------------------------------------------- #
# End-to-end: real SQLite board + real DuckDB mirror + real adapter
# --------------------------------------------------------------------------- #

try:  # hermes_kanban is an optional install — skip E2E when absent
    import duckdb  # noqa: F401
    import hermes_kanban.duckdb_kanban_adapter as _real_adapter  # noqa: F401
    _HAVE_DUCK = True
except Exception:  # pragma: no cover
    _HAVE_DUCK = False


@pytest.fixture
def dual_board(tmp_path, monkeypatch):
    """Real SQLite kanban DB in a tmp home with dual-write active.

    Mirrors the ``kanban_home`` fixture from test_kanban_db.py but opts
    into HERMES_KANBAN_WRITE_BACKEND=dual and (re)installs the shim so
    the wrappers are live regardless of import-time env.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Pin board resolution into the isolated home too — without this,
    # kanban_home() warns that a tempdir HERMES_HOME without
    # HERMES_KANBAN_HOME may resolve board writes elsewhere.
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.init_db()
    monkeypatch.setenv("HERMES_KANBAN_WRITE_BACKEND", "dual")
    # Force a fresh facade resolution against the REAL adapter (other
    # tests patch _load_facade; the module caches one attempt/process).
    dw._LOAD_ATTEMPTED = False
    dw._FACADE = None
    dw._ADAPTER = None
    dw.install(kb)
    yield db_path
    dw._LOAD_ATTEMPTED = False
    dw._FACADE = None
    dw._ADAPTER = None


@pytest.mark.skipif(not _HAVE_DUCK, reason="hermes_kanban / duckdb not installed")
def test_claim_task_mirrors_run_row_with_id_parity(dual_board, caplog):
    """THE regression: dual-mode claim writes the mirror run row with the
    SAME id SQLite minted, no ERROR traceback."""
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="claim mirror e2e", assignee="worker")
        # create_task lands 'running' by default; the dispatcher path
        # claims from 'ready' — flip it the way requeue does.
        conn.execute(
            "UPDATE tasks SET status='ready', current_run_id=NULL, "
            "claim_lock=NULL, claim_expires=NULL WHERE id = ?",
            (tid,),
        )
        conn.commit()

        with caplog.at_level(logging.DEBUG, logger=dw.logger.name):
            claimed = kb.claim_task(conn, tid)

    # SQLite side: claim succeeded and returned the Task.
    assert claimed is not None, "SQLite claim must succeed (mirror is best-effort)"
    assert claimed.status == "running"

    # No ERROR from the shim — pre-fix this is the NotImplementedException.
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert errors == [], (
        "dual-write claim must not log ERROR — got: "
        + repr([r.getMessage() for r in errors])
    )

    # SQLite's run row (the id the mirror must reuse).
    with kb.connect() as conn:
        row = conn.execute(
            "SELECT current_run_id FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
    sqlite_run_id = int(row["current_run_id"])
    assert sqlite_run_id > 0

    # DuckDB mirror: run row landed under the same primary key, and the
    # task pointer matches — this is the parity the mirror exists for.
    import duckdb as _ddb

    duck_path = dual_board.with_suffix(".duckdb")
    assert duck_path.exists(), "mirror file was never created"
    dconn = _ddb.connect(str(duck_path), read_only=True)
    try:
        runs = dconn.execute(
            "SELECT id, task_id, status FROM task_runs WHERE task_id = ?",
            [tid],
        ).fetchall()
        assert len(runs) == 1, f"expected exactly 1 mirror run row, got {len(runs)}"
        assert int(runs[0][0]) == sqlite_run_id, (
            f"mirror run id {runs[0][0]} != sqlite run id {sqlite_run_id}"
        )
        assert runs[0][2] == "running"

        trow = dconn.execute(
            "SELECT status, current_run_id FROM tasks WHERE id = ?", [tid]
        ).fetchone()
        assert trow is not None, "mirror task row missing (create_task mirror failed?)"
        assert trow[0] == "running"
        assert int(trow[1]) == sqlite_run_id
    finally:
        dconn.close()
