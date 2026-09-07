"""Acceptance tests for hermes_cli.kanban_duckdb_reader.

Covers the four acceptance scenarios from AUDIT-6:

  1. Seed both backends with 10 tasks.
     Read with HERMES_KANBAN_BACKEND=duckdb; assert all 10 visible.
  2. Read with HERMES_KANBAN_BACKEND=sqlite; assert all 10 visible.
  3. Read with HERMES_KANBAN_BACKEND=auto; assert DuckDB path chosen.
  4. Negative paths: bad backend value, missing DuckDB file, etc.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Optional

import pytest

import duckdb

import hermes_state  # noqa: F401 — needed to initialise hermes_state before kanban_db
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_duckdb_reader import (
    DuckDBKanbanReader,
    ENV_KANBAN_BACKEND,
    ENV_KANBAN_DUCKDB_PATH,
    VALID_BACKENDS,
    get_backend,
    get_task_with_backend,
    list_tasks_with_backend,
    resolve_duckdb_path,
    should_use_duckdb,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TASK_COLUMNS = (
    "id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, "
    "status TEXT NOT NULL, priority INTEGER DEFAULT 0, created_by TEXT, "
    "created_at INTEGER NOT NULL, started_at INTEGER, completed_at INTEGER, "
    "workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT, "
    "branch_name TEXT, project_id TEXT, claim_lock TEXT, claim_expires INTEGER, "
    "tenant TEXT, result TEXT, idempotency_key TEXT, "
    "consecutive_failures INTEGER NOT NULL DEFAULT 0, worker_pid INTEGER, "
    "last_failure_error TEXT, max_runtime_seconds INTEGER, "
    "last_heartbeat_at INTEGER, current_run_id INTEGER, "
    "workflow_template_id TEXT, current_step_key TEXT, skills TEXT, "
    "model_override TEXT, provider_override TEXT, max_retries INTEGER, "
    "goal_mode INTEGER NOT NULL DEFAULT 0, goal_max_turns INTEGER, "
    "session_id TEXT, block_kind TEXT, "
    "block_recurrences INTEGER NOT NULL DEFAULT 0"
)


def _create_duckdb_schema(conn: duckdb.DuckDBPyConnection) -> None:
    conn.execute(f"CREATE TABLE IF NOT EXISTS tasks ({_TASK_COLUMNS})")


def _insert_tasks_into_duckdb(
    conn: duckdb.DuckDBPyConnection, tasks: list[dict]
) -> None:
    for t in tasks:
        conn.execute(
            """
            INSERT INTO tasks (id, title, status, created_at, priority, workspace_kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [t["id"], t["title"], t["status"], t["created_at"], t.get("priority", 0), "scratch"],
        )


def _seed_tasks(n: int, prefix: str = "t_seed_") -> list[dict]:
    """Generate *n* simple task dicts."""
    now = int(time.time())
    return [
        {
            "id": f"{prefix}{i:04d}",
            "title": f"Task {i}",
            "status": "ready",
            "created_at": now + i,
            "priority": i,
        }
        for i in range(n)
    ]


def _insert_tasks_into_sqlite(
    conn: sqlite3.Connection, tasks: list[dict]
) -> None:
    for t in tasks:
        conn.execute(
            """
            INSERT INTO tasks (id, title, status, created_at, priority, workspace_kind)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (t["id"], t["title"], t["status"], t["created_at"], t.get("priority", 0), "scratch"),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty SQLite kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def duckdb_path(tmp_path):
    """Path for a fresh DuckDB kanban file."""
    return tmp_path / "kanban.duckdb"


@pytest.fixture
def seeded_duckdb(duckdb_path):
    """A DuckDB file seeded with 10 ready tasks."""
    conn = duckdb.connect(str(duckdb_path))
    _create_duckdb_schema(conn)
    _insert_tasks_into_duckdb(conn, _seed_tasks(10, prefix="t_duck_"))
    conn.close()
    return duckdb_path


@pytest.fixture
def seeded_sqlite(hermes_home):
    """A SQLite kanban DB seeded with 10 ready tasks (different ids)."""
    with kb.connect_closing() as conn:
        _insert_tasks_into_sqlite(conn, _seed_tasks(10, prefix="t_sqlt_"))
    return hermes_home


# ---------------------------------------------------------------------------
# Acceptance test 1 — HERMES_KANBAN_BACKEND=duckdb, all 10 visible
# ---------------------------------------------------------------------------

class TestDuckdbBackend:
    def test_list_tasks_returns_all_10(
        self, seeded_duckdb, seeded_sqlite, monkeypatch
    ):
        """BACKEND=duckdb: list_tasks returns all 10 DuckDB tasks."""
        monkeypatch.setenv(ENV_KANBAN_BACKEND, "duckdb")
        monkeypatch.setenv(ENV_KANBAN_DUCKDB_PATH, str(seeded_duckdb))

        with DuckDBKanbanReader.open(path=seeded_duckdb) as reader:
            tasks = reader.list_tasks()

        assert len(tasks) == 10
        ids = {t.id for t in tasks}
        # All ids should be from the DuckDB seed (prefix t_duck_)
        assert all(tid.startswith("t_duck_") for tid in ids)

    def test_get_task_returns_correct_task(
        self, seeded_duckdb, monkeypatch
    ):
        """BACKEND=duckdb: get_task returns the right task."""
        monkeypatch.setenv(ENV_KANBAN_BACKEND, "duckdb")
        monkeypatch.setenv(ENV_KANBAN_DUCKDB_PATH, str(seeded_duckdb))

        with DuckDBKanbanReader.open(path=seeded_duckdb) as reader:
            task = reader.get_task("t_duck_0003")

        assert task is not None
        assert task.id == "t_duck_0003"
        assert task.title == "Task 3"
        assert task.status == "ready"

    def test_get_task_returns_none_for_missing(self, seeded_duckdb):
        """get_task returns None when the task does not exist."""
        with DuckDBKanbanReader.open(path=seeded_duckdb) as reader:
            result = reader.get_task("t_nonexistent_id")
        assert result is None

    def test_should_use_duckdb_when_backend_set(
        self, seeded_duckdb, monkeypatch
    ):
        monkeypatch.setenv(ENV_KANBAN_BACKEND, "duckdb")
        monkeypatch.setenv(ENV_KANBAN_DUCKDB_PATH, str(seeded_duckdb))
        assert should_use_duckdb() is True

    def test_get_task_with_backend_uses_duckdb(
        self, seeded_duckdb, seeded_sqlite, monkeypatch
    ):
        """get_task_with_backend routes to DuckDB when configured."""
        monkeypatch.setenv(ENV_KANBAN_BACKEND, "duckdb")
        monkeypatch.setenv(ENV_KANBAN_DUCKDB_PATH, str(seeded_duckdb))

        with kb.connect_closing() as sqlite_conn:
            task = get_task_with_backend(
                sqlite_conn, "t_duck_0005", duckdb_path=seeded_duckdb
            )
        assert task is not None
        assert task.id == "t_duck_0005"

    def test_list_tasks_with_backend_uses_duckdb(
        self, seeded_duckdb, seeded_sqlite, monkeypatch
    ):
        """list_tasks_with_backend returns DuckDB tasks when configured."""
        monkeypatch.setenv(ENV_KANBAN_BACKEND, "duckdb")
        monkeypatch.setenv(ENV_KANBAN_DUCKDB_PATH, str(seeded_duckdb))

        with kb.connect_closing() as sqlite_conn:
            tasks = list_tasks_with_backend(
                sqlite_conn, duckdb_path=seeded_duckdb
            )
        assert len(tasks) == 10
        assert all(t.id.startswith("t_duck_") for t in tasks)


# ---------------------------------------------------------------------------
# Acceptance test 2 — HERMES_KANBAN_BACKEND=sqlite, all 10 visible
# ---------------------------------------------------------------------------

class TestSqliteBackend:
    def test_list_tasks_returns_all_10_sqlite(
        self, seeded_sqlite, seeded_duckdb, monkeypatch
    ):
        """BACKEND=sqlite: list_tasks returns all 10 SQLite tasks."""
        monkeypatch.setenv(ENV_KANBAN_BACKEND, "sqlite")
        monkeypatch.setenv(ENV_KANBAN_DUCKDB_PATH, str(seeded_duckdb))

        with kb.connect_closing() as conn:
            tasks = kb.list_tasks(conn)

        assert len(tasks) == 10
        assert all(t.id.startswith("t_sqlt_") for t in tasks)

    def test_should_use_duckdb_is_false_when_sqlite(
        self, seeded_duckdb, monkeypatch
    ):
        monkeypatch.setenv(ENV_KANBAN_BACKEND, "sqlite")
        monkeypatch.setenv(ENV_KANBAN_DUCKDB_PATH, str(seeded_duckdb))
        assert should_use_duckdb() is False

    def test_get_task_with_backend_uses_sqlite(
        self, seeded_sqlite, seeded_duckdb, monkeypatch
    ):
        """get_task_with_backend routes to SQLite when configured."""
        monkeypatch.setenv(ENV_KANBAN_BACKEND, "sqlite")
        monkeypatch.setenv(ENV_KANBAN_DUCKDB_PATH, str(seeded_duckdb))

        with kb.connect_closing() as conn:
            task = get_task_with_backend(conn, "t_sqlt_0007", duckdb_path=seeded_duckdb)

        assert task is not None
        assert task.id == "t_sqlt_0007"

    def test_list_tasks_with_backend_uses_sqlite(
        self, seeded_sqlite, seeded_duckdb, monkeypatch
    ):
        """list_tasks_with_backend returns SQLite tasks when configured."""
        monkeypatch.setenv(ENV__KANBAN_BACKEND := ENV_KANBAN_BACKEND, "sqlite")
        monkeypatch.setenv(ENV_KANBAN_DUCKDB_PATH, str(seeded_duckdb))

        with kb.connect_closing() as conn:
            tasks = list_tasks_with_backend(conn, duckdb_path=seeded_duckdb)

        assert len(tasks) == 10
        assert all(t.id.startswith("t_sqlt_") for t in tasks)


# ---------------------------------------------------------------------------
# Acceptance test 3 — HERMES_KANBAN_BACKEND=auto, DuckDB chosen when exists
# ---------------------------------------------------------------------------

class TestAutoBackend:
    def test_auto_chooses_duckdb_when_file_exists(
        self, seeded_duckdb, monkeypatch
    ):
        """BACKEND=auto: DuckDB file present → should_use_duckdb() is True."""
        monkeypatch.setenv(ENV_KANBAN_BACKEND, "auto")
        monkeypatch.setenv(ENV_KANBAN_DUCKDB_PATH, str(seeded_duckdb))
        assert should_use_duckdb() is True

    def test_auto_falls_back_to_sqlite_when_no_file(
        self, seeded_sqlite, tmp_path, monkeypatch
    ):
        """BACKEND=auto: DuckDB file absent → should_use_duckdb() is False."""
        monkeypatch.setenv(ENV_KANBAN_BACKEND, "auto")
        # Point to a path that does not exist.
        monkeypatch.setenv(
            ENV_KANBAN_DUCKDB_PATH, str(tmp_path / "no_such.duckdb")
        )
        assert should_use_duckdb() is False

    def test_auto_reads_from_duckdb_all_10(
        self, seeded_duckdb, seeded_sqlite, monkeypatch
    ):
        """BACKEND=auto with DuckDB present: reader sees all 10 DuckDB tasks."""
        monkeypatch.setenv(ENV_KANBAN_BACKEND, "auto")
        monkeypatch.setenv(ENV_KANBAN_DUCKDB_PATH, str(seeded_duckdb))

        # should_use_duckdb returns True, so get_task_with_backend uses DuckDB.
        with kb.connect_closing() as conn:
            tasks = list_tasks_with_backend(conn, duckdb_path=seeded_duckdb)

        assert len(tasks) == 10
        assert all(t.id.startswith("t_duck_") for t in tasks), (
            "BACKEND=auto should prefer DuckDB when the file exists"
        )

    def test_default_backend_is_auto(self, monkeypatch):
        """When HERMES_KANBAN_BACKEND is unset, default is 'auto'."""
        monkeypatch.delenv(ENV_KANBAN_BACKEND, raising=False)
        assert get_backend() == "auto"


# ---------------------------------------------------------------------------
# Negative / edge-case tests
# ---------------------------------------------------------------------------

class TestNegativePaths:
    def test_invalid_backend_raises(self, monkeypatch):
        """Unrecognised backend value raises ValueError."""
        monkeypatch.setenv(ENV_KANBAN_BACKEND, "mysql")
        with pytest.raises(ValueError, match="HERMES_KANBAN_BACKEND must be one of"):
            get_backend()

    def test_open_missing_file_raises_file_not_found(self, tmp_path):
        """DuckDBKanbanReader.open() raises FileNotFoundError for absent file."""
        missing = tmp_path / "ghost.duckdb"
        with pytest.raises(FileNotFoundError, match="DuckDB kanban file not found"):
            DuckDBKanbanReader.open(path=missing)

    def test_context_manager_closes_on_exit(self, seeded_duckdb):
        """Context manager closes the connection without raising."""
        with DuckDBKanbanReader.open(path=seeded_duckdb) as reader:
            _ = reader.get_task("t_duck_0000")
        # Calling close() again is a no-op (idempotent).
        reader.close()

    def test_list_tasks_invalid_status_raises(self, seeded_duckdb):
        """list_tasks with an invalid status raises ValueError."""
        with DuckDBKanbanReader.open(path=seeded_duckdb) as reader:
            with pytest.raises(ValueError, match="status must be one of"):
                reader.list_tasks(status="not_a_status")

    def test_list_tasks_invalid_order_by_raises(self, seeded_duckdb):
        """list_tasks with an invalid order_by raises ValueError."""
        with DuckDBKanbanReader.open(path=seeded_duckdb) as reader:
            with pytest.raises(ValueError, match="order_by must be one of"):
                reader.list_tasks(order_by="nonexistent")

    def test_resolve_duckdb_path_respects_env(self, tmp_path, monkeypatch):
        """resolve_duckdb_path honours HERMES_KANBAN_DUCKDB_PATH."""
        custom = tmp_path / "custom.duckdb"
        monkeypatch.setenv(ENV_KANBAN_DUCKDB_PATH, str(custom))
        assert resolve_duckdb_path() == custom

    def test_resolve_duckdb_path_uses_hermes_home(self, tmp_path, monkeypatch):
        """resolve_duckdb_path uses HERMES_HOME when no explicit path set."""
        monkeypatch.delenv(ENV_KANBAN_DUCKDB_PATH, raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        expected = tmp_path / "data" / "kanban.duckdb"
        assert resolve_duckdb_path() == expected

    def test_valid_backends_set(self):
        """VALID_BACKENDS must contain exactly the three documented values."""
        assert VALID_BACKENDS == {"duckdb", "sqlite", "auto"}

    def test_duckdb_read_only_rejects_write(self, seeded_duckdb):
        """DuckDB file opened read-only must reject INSERT."""
        with DuckDBKanbanReader.open(path=seeded_duckdb) as reader:
            with pytest.raises(Exception):
                reader._conn.execute(
                    "INSERT INTO tasks (id, title, status, created_at, workspace_kind) "
                    "VALUES ('x', 't', 'ready', 1, 'scratch')"
                )


# ---------------------------------------------------------------------------
# Task dataclass field completeness
# ---------------------------------------------------------------------------

class TestTaskFieldMapping:
    def test_task_fields_populated(self, seeded_duckdb):
        """Task returned from DuckDB reader has the expected field types."""
        with DuckDBKanbanReader.open(path=seeded_duckdb) as reader:
            task = reader.get_task("t_duck_0009")

        assert task is not None
        assert isinstance(task.id, str)
        assert isinstance(task.title, str)
        assert isinstance(task.status, str)
        assert isinstance(task.created_at, int)
        assert isinstance(task.priority, int)
        assert isinstance(task.workspace_kind, str)
        assert isinstance(task.consecutive_failures, int)
        assert isinstance(task.goal_mode, bool)
        assert isinstance(task.block_recurrences, int)
        # Optional fields that should be None for seeded tasks.
        assert task.body is None
        assert task.assignee is None
        assert task.skills is None
        assert task.model_override is None

    def test_list_tasks_filter_by_status(self, tmp_path):
        """list_tasks(status=...) filters correctly in DuckDB."""
        db_path = tmp_path / "filter_test.duckdb"
        conn = duckdb.connect(str(db_path))
        _create_duckdb_schema(conn)
        now = int(time.time())
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, workspace_kind) "
            "VALUES (?, ?, ?, ?, ?)",
            ["t_r1", "Ready1", "ready", now, "scratch"],
        )
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, workspace_kind) "
            "VALUES (?, ?, ?, ?, ?)",
            ["t_d1", "Done1", "done", now + 1, "scratch"],
        )
        conn.close()

        with DuckDBKanbanReader.open(path=db_path) as reader:
            ready_tasks = reader.list_tasks(status="ready")
            done_tasks = reader.list_tasks(status="done")

        assert len(ready_tasks) == 1
        assert ready_tasks[0].id == "t_r1"
        assert len(done_tasks) == 1
        assert done_tasks[0].id == "t_d1"

    def test_list_tasks_limit(self, seeded_duckdb):
        """list_tasks(limit=3) returns at most 3 tasks."""
        with DuckDBKanbanReader.open(path=seeded_duckdb) as reader:
            tasks = reader.list_tasks(limit=3)
        assert len(tasks) == 3

    def test_list_tasks_include_archived(self, tmp_path):
        """include_archived=False (default) hides archived tasks."""
        db_path = tmp_path / "archive_test.duckdb"
        conn = duckdb.connect(str(db_path))
        _create_duckdb_schema(conn)
        now = int(time.time())
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, workspace_kind) "
            "VALUES (?, ?, ?, ?, ?)",
            ["t_a1", "Archived1", "archived", now, "scratch"],
        )
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at, workspace_kind) "
            "VALUES (?, ?, ?, ?, ?)",
            ["t_r1", "Ready1", "ready", now + 1, "scratch"],
        )
        conn.close()

        with DuckDBKanbanReader.open(path=db_path) as reader:
            all_visible = reader.list_tasks(include_archived=True)
            non_archived = reader.list_tasks(include_archived=False)

        assert len(all_visible) == 2
        assert len(non_archived) == 1
        assert non_archived[0].id == "t_r1"
