"""Tests for the min_resources column + YAML front-matter parser on tasks table.

Covers:
- Schema migration: min_resources column added on existing DBs (idempotent).
- create_task: body with YAML front-matter containing min_resources populates the column.
- create_task: body without front-matter leaves the column NULL.
- get_task_min_resources: returns declared values when present, defaults when NULL.
- Task.from_row: min_resources field is populated from the JSON column.
- Edge cases: partial keys, malformed YAML, front-matter without min_resources key.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

import hermes_state
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _connect(kanban_home):
    """Return a connection to the kanban DB under *kanban_home*."""
    db_path = kanban_home / "kanban.db"
    return kb.connect(db_path)


# ---------------------------------------------------------------------------
# Schema migration
# ---------------------------------------------------------------------------


def test_min_resources_column_exists_after_init(kanban_home):
    """The min_resources column is present on the tasks table after init."""
    with _connect(kanban_home) as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "min_resources" in cols


def test_min_resources_migration_on_legacy_db(tmp_path):
    """A DB created without min_resources gets the column after re-init."""
    # Build a DB with the old schema (no min_resources column)
    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
            priority INTEGER DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER,
            tenant TEXT,
            result TEXT,
            idempotency_key TEXT,
            consecutive_failures INTEGER NOT NULL DEFAULT 0,
            block_recurrences INTEGER NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_links (
            parent_id TEXT NOT NULL,
            child_id TEXT NOT NULL,
            PRIMARY KEY (parent_id, child_id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            author TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            payload TEXT,
            created_at INTEGER NOT NULL
        )
    """)
    conn.commit()
    conn.close()

    # Now init_db should add the min_resources column
    import os
    os.environ["HERMES_HOME"] = str(tmp_path)
    kb.init_db(db_path)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    conn.close()
    assert "min_resources" in cols


def test_migration_is_idempotent(kanban_home):
    """Re-running init_db doesn't error and the column is still there."""
    kb.init_db()  # second call
    with _connect(kanban_home) as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "min_resources" in cols


# ---------------------------------------------------------------------------
# create_task with front-matter parsing
# ---------------------------------------------------------------------------


def test_create_task_with_min_resources_front_matter(kanban_home):
    """A task body with YAML front-matter populates the min_resources column."""
    body = """\
---
min_resources:
  mem_gb: 4.0
  cpu_cores: 8
  bedrock_tpm_reservation: 50000
---
This is the task body after the front-matter.
"""
    with _connect(kanban_home) as conn:
        task_id = kb.create_task(
            conn,
            title="Test task with resources",
            body=body,
            assignee="test-worker",
        )
        row = conn.execute(
            "SELECT min_resources FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    assert row["min_resources"] is not None
    parsed = json.loads(row["min_resources"])
    assert parsed["mem_gb"] == 4.0
    assert parsed["cpu_cores"] == 8
    assert parsed["bedrock_tpm_reservation"] == 50000


def test_create_task_without_front_matter_leaves_null(kanban_home):
    """A task body without front-matter leaves min_resources NULL."""
    body = "Just a regular task body, no front-matter here."
    with _connect(kanban_home) as conn:
        task_id = kb.create_task(
            conn,
            title="Plain task",
            body=body,
            assignee="test-worker",
        )
        row = conn.execute(
            "SELECT min_resources FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    assert row["min_resources"] is None


def test_create_task_with_no_body_leaves_null(kanban_home):
    """A task with no body at all leaves min_resources NULL."""
    with _connect(kanban_home) as conn:
        task_id = kb.create_task(
            conn,
            title="No-body task",
            body=None,
            assignee="test-worker",
        )
        row = conn.execute(
            "SELECT min_resources FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    assert row["min_resources"] is None


def test_create_task_front_matter_without_min_resources_key(kanban_home):
    """Front-matter without a min_resources key leaves the column NULL."""
    body = """\
---
assignee: someone
priority: 5
---
Body text here.
"""
    with _connect(kanban_home) as conn:
        task_id = kb.create_task(
            conn,
            title="FM without min_resources",
            body=body,
            assignee="test-worker",
        )
        row = conn.execute(
            "SELECT min_resources FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    assert row["min_resources"] is None


def test_create_task_partial_min_resources(kanban_home):
    """Only declared keys are stored; others are omitted from the JSON."""
    body = """\
---
min_resources:
  mem_gb: 2.0
---
Body text.
"""
    with _connect(kanban_home) as conn:
        task_id = kb.create_task(
            conn,
            title="Partial resources",
            body=body,
            assignee="test-worker",
        )
        row = conn.execute(
            "SELECT min_resources FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    assert row["min_resources"] is not None
    parsed = json.loads(row["min_resources"])
    assert parsed == {"mem_gb": 2.0}
    # cpu_cores and bedrock_tpm_reservation should NOT be in the stored JSON
    assert "cpu_cores" not in parsed
    assert "bedrock_tpm_reservation" not in parsed


def test_create_task_malformed_yaml_front_matter(kanban_home):
    """Malformed YAML front-matter is silently skipped (column stays NULL)."""
    body = """\
---
min_resources: [invalid yaml structure
  - broken
---
Body text.
"""
    with _connect(kanban_home) as conn:
        task_id = kb.create_task(
            conn,
            title="Malformed YAML",
            body=body,
            assignee="test-worker",
        )
        row = conn.execute(
            "SELECT min_resources FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    # Malformed YAML should result in None (parser swallows the error)
    assert row["min_resources"] is None


def test_create_task_unknown_keys_dropped(kanban_home):
    """Unknown keys in min_resources are silently dropped."""
    body = """\
---
min_resources:
  mem_gb: 1.0
  gpu_count: 4
  unknown_thing: "hello"
---
Body text.
"""
    with _connect(kanban_home) as conn:
        task_id = kb.create_task(
            conn,
            title="Unknown keys",
            body=body,
            assignee="test-worker",
        )
        row = conn.execute(
            "SELECT min_resources FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()

    parsed = json.loads(row["min_resources"])
    assert parsed == {"mem_gb": 1.0}
    assert "gpu_count" not in parsed


# ---------------------------------------------------------------------------
# get_task_min_resources
# ---------------------------------------------------------------------------


def test_get_task_min_resources_returns_declared(kanban_home):
    """get_task_min_resources returns the declared values (merged with defaults)."""
    body = """\
---
min_resources:
  mem_gb: 8.0
  cpu_cores: 4
  bedrock_tpm_reservation: 20000
---
Body.
"""
    with _connect(kanban_home) as conn:
        task_id = kb.create_task(
            conn,
            title="Get resources test",
            body=body,
            assignee="test-worker",
        )
        result = kb.get_task_min_resources(conn, task_id)

    assert result == {
        "mem_gb": 8.0,
        "cpu_cores": 4,
        "bedrock_tpm_reservation": 20000,
    }


def test_get_task_min_resources_returns_default_when_null(kanban_home):
    """get_task_min_resources returns defaults when column is NULL."""
    with _connect(kanban_home) as conn:
        task_id = kb.create_task(
            conn,
            title="Default resources test",
            body="No front-matter",
            assignee="test-worker",
        )
        result = kb.get_task_min_resources(conn, task_id)

    assert result == kb.DEFAULT_MIN_RESOURCES
    assert result["mem_gb"] == 0.5
    assert result["cpu_cores"] == 1
    assert result["bedrock_tpm_reservation"] == 10000


def test_get_task_min_resources_partial_declared_uses_default_for_missing(kanban_home):
    """Partial declaration fills missing keys from the default."""
    body = """\
---
min_resources:
  mem_gb: 16.0
---
Body.
"""
    with _connect(kanban_home) as conn:
        task_id = kb.create_task(
            conn,
            title="Partial fill test",
            body=body,
            assignee="test-worker",
        )
        result = kb.get_task_min_resources(conn, task_id)

    assert result["mem_gb"] == 16.0  # declared
    assert result["cpu_cores"] == 1  # default
    assert result["bedrock_tpm_reservation"] == 10000  # default


def test_get_task_min_resources_nonexistent_task_returns_default(kanban_home):
    """A non-existent task id returns the default dict."""
    with _connect(kanban_home) as conn:
        result = kb.get_task_min_resources(conn, "t_nonexistent")

    assert result == kb.DEFAULT_MIN_RESOURCES


# ---------------------------------------------------------------------------
# Task.from_row
# ---------------------------------------------------------------------------


def test_task_from_row_populates_min_resources(kanban_home):
    """Task.from_row correctly parses the min_resources JSON column."""
    body = """\
---
min_resources:
  mem_gb: 3.5
  cpu_cores: 2
---
Body.
"""
    with _connect(kanban_home) as conn:
        task_id = kb.create_task(
            conn,
            title="From_row test",
            body=body,
            assignee="test-worker",
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.min_resources is not None
    assert task.min_resources["mem_gb"] == 3.5
    assert task.min_resources["cpu_cores"] == 2
    # bedrock_tpm_reservation was not declared, should not be in the stored dict
    assert "bedrock_tpm_reservation" not in task.min_resources


def test_task_from_row_min_resources_null(kanban_home):
    """Task.from_row sets min_resources to None when the column is NULL."""
    with _connect(kanban_home) as conn:
        task_id = kb.create_task(
            conn,
            title="Null min_resources",
            body="No front-matter",
            assignee="test-worker",
        )
        task = kb.get_task(conn, task_id)

    assert task is not None
    assert task.min_resources is None