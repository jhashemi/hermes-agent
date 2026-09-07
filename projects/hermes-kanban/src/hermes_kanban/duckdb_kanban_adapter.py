"""DuckDB-backed write adapter for the Hermes Kanban control plane.

Mirror of :mod:`hermes_kanban.kanban_db` against a DuckDB file. Public
function surface intentionally matches ``kanban_db``'s module-level
API so that :mod:`hermes_kanban.kanban_repository_facade` can route
between the two backends transparently.

ADR-012 (2026-08-16, Accepted with dual sign-off from hamilton +
helios): SQLite kanban sunset. This module is the write half of that
migration. Reads on DuckDB were already handled by
``hermes_cli.kanban_duckdb_reader``; this closes the write path.

Design contract (from ADR-012 §2.1 + §4):

* Column set is a *superset* of the SQLite schema.
* ``Task`` / ``Run`` / ``Comment`` / ``Event`` dataclasses are
  the ones defined in :mod:`hermes_kanban.kanban_db` — this module
  re-exports them so ``from hermes_kanban.duckdb_kanban_adapter import
  Task`` works.
* AUTOINCREMENT-generated ids from SQLite (``task_events.id``,
  ``task_runs.id``, ``task_comments.id``) can be *passed through*
  when this adapter runs behind the facade in ``dual`` mode. When
  running standalone (``duckdb`` mode after P3 authority flip), the
  adapter allocates its own ids via DuckDB ``SEQUENCE``.
* Concurrency: DuckDB is single-writer per file. The adapter serialises
  writes through a per-path :class:`threading.RLock` (same pattern as
  ``DuckDBOKRStorage`` from ADR-011 P0-3). Cross-process concurrency is
  handled by DuckDB's own file-level advisory locking; this module does
  not try to re-implement SQLite's ``BEGIN IMMEDIATE`` semantics.

Reused precedents:
- ADR-011 P0-3 ``DuckDBOKRStorage`` (commit ``98279e0``): context-managed
  connections + RLock. Same pattern here.
- ``hermes_cli.kanban_duckdb_reader``: schema-superset assumption; this
  file writes rows that reader can read without any schema change.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

import duckdb

# Reuse the dataclasses + constants from kanban_db so both backends return
# structurally identical values. We deliberately do NOT re-import
# `sqlite3.Row`-specific `from_row` methods; those are SQLite-only.
from hermes_kanban.kanban_db import (
    Comment,
    DEFAULT_CLAIM_TTL_SECONDS,
    Event,
    Run,
    Task,
    VALID_STATUSES,
    VALID_WORKSPACE_KINDS,
)


def _resolve_claim_ttl_seconds(ttl_seconds: Optional[int]) -> int:
    """Return the effective claim TTL for the adapter, honouring env override.

    Mirrors ``hermes_cli.kanban_db._resolve_claim_ttl_seconds`` so both
    backends behave identically when the dual-write shim forwards
    ``ttl_seconds=None`` from the outer dispatcher (its SQLite claim_task
    declares ``ttl_seconds: Optional[int] = None``, so None reaches us
    verbatim through ``**kwargs``).

    Precedence:
      1. explicit positive int from the caller
      2. ``HERMES_KANBAN_CLAIM_TTL_SECONDS`` env override (positive int)
      3. built-in ``DEFAULT_CLAIM_TTL_SECONDS`` (15 minutes)

    Non-positive / non-integer values fall back silently.
    """
    if ttl_seconds is not None:
        try:
            return max(1, int(ttl_seconds))
        except (TypeError, ValueError):
            pass  # fall through to env / default
    raw = os.environ.get("HERMES_KANBAN_CLAIM_TTL_SECONDS", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            parsed = 0
        if parsed > 0:
            return parsed
    return DEFAULT_CLAIM_TTL_SECONDS

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
#
# DuckDB dialect differs from SQLite in a few places we care about:
#   * No AUTOINCREMENT. We create SEQUENCEs and default the id column
#     to `nextval('...')`. Same monotonic semantics per table.
#   * No `IF NOT EXISTS` on CREATE INDEX in older versions — DuckDB 0.9+
#     supports it, and we require duckdb>=1.0 elsewhere in Hermes.
#   * Column types: INTEGER stays INTEGER, TEXT stays TEXT (VARCHAR
#     alias). BOOLEAN we don't use here.
#   * `PRAGMA journal_mode=WAL` is a SQLite concept and not applicable.
#
# The column set below is verbatim from ``kanban_db.SCHEMA_SQL`` plus the
# additive migrations in ``_migrate_add_optional_columns``. Ordering
# matches the SQLite baseline so a row-for-row migrator can INSERT
# tuples without column re-mapping.

SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS seq_task_comments_id START 1;
CREATE SEQUENCE IF NOT EXISTS seq_task_events_id   START 1;
CREATE SEQUENCE IF NOT EXISTS seq_task_runs_id     START 1;

CREATE TABLE IF NOT EXISTS tasks (
    id                   VARCHAR PRIMARY KEY,
    title                VARCHAR NOT NULL,
    body                 VARCHAR,
    assignee             VARCHAR,
    status               VARCHAR NOT NULL,
    priority             INTEGER DEFAULT 0,
    created_by           VARCHAR,
    created_at           BIGINT NOT NULL,
    started_at           BIGINT,
    completed_at         BIGINT,
    workspace_kind       VARCHAR NOT NULL DEFAULT 'scratch',
    workspace_path       VARCHAR,
    claim_lock           VARCHAR,
    claim_expires        BIGINT,
    tenant               VARCHAR,
    result               VARCHAR,
    idempotency_key      VARCHAR,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    worker_pid           INTEGER,
    last_failure_error   VARCHAR,
    max_runtime_seconds  INTEGER,
    last_heartbeat_at    BIGINT,
    current_run_id       BIGINT,
    workflow_template_id VARCHAR,
    current_step_key     VARCHAR,
    skills               VARCHAR,
    max_retries          INTEGER
);

CREATE TABLE IF NOT EXISTS task_links (
    parent_id  VARCHAR NOT NULL,
    child_id   VARCHAR NOT NULL,
    PRIMARY KEY (parent_id, child_id)
);

CREATE TABLE IF NOT EXISTS task_comments (
    id         BIGINT PRIMARY KEY DEFAULT nextval('seq_task_comments_id'),
    task_id    VARCHAR NOT NULL,
    author     VARCHAR NOT NULL,
    body       VARCHAR NOT NULL,
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id         BIGINT PRIMARY KEY DEFAULT nextval('seq_task_events_id'),
    task_id    VARCHAR NOT NULL,
    run_id     BIGINT,
    kind       VARCHAR NOT NULL,
    payload    VARCHAR,
    created_at BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_runs (
    id                  BIGINT PRIMARY KEY DEFAULT nextval('seq_task_runs_id'),
    task_id             VARCHAR NOT NULL,
    profile             VARCHAR,
    step_key            VARCHAR,
    status              VARCHAR NOT NULL,
    claim_lock          VARCHAR,
    claim_expires       BIGINT,
    worker_pid          INTEGER,
    max_runtime_seconds INTEGER,
    last_heartbeat_at   BIGINT,
    started_at          BIGINT NOT NULL,
    ended_at            BIGINT,
    outcome             VARCHAR,
    summary             VARCHAR,
    metadata            VARCHAR,
    error               VARCHAR
);

CREATE TABLE IF NOT EXISTS kanban_notify_subs (
    task_id       VARCHAR NOT NULL,
    platform      VARCHAR NOT NULL,
    chat_id       VARCHAR NOT NULL,
    thread_id     VARCHAR NOT NULL DEFAULT '',
    user_id       VARCHAR,
    created_at    BIGINT NOT NULL,
    last_event_id BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (task_id, platform, chat_id, thread_id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_assignee_status ON tasks(assignee, status);
CREATE INDEX IF NOT EXISTS idx_tasks_status          ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_links_child           ON task_links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent          ON task_links(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_task         ON task_comments(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_events_task           ON task_events(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_runs_task             ON task_runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS idx_notify_task           ON kanban_notify_subs(task_id);
"""

# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #

_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_META = threading.Lock()
_INITIALIZED_PATHS: set[str] = set()


def _lock_for(path: str) -> threading.RLock:
    with _LOCKS_META:
        lk = _LOCKS.get(path)
        if lk is None:
            lk = threading.RLock()
            _LOCKS[path] = lk
        return lk


def duckdb_kanban_path(sqlite_path: Path) -> Path:
    """Given a SQLite ``kanban.db`` path, return the sibling ``kanban.duckdb``.

    Board layout stays identical (ADR-012 §6): we just swap the file
    extension. Callers that already know a full DuckDB path can pass
    it directly to :func:`connect`.
    """
    p = Path(sqlite_path)
    if p.suffix == ".db":
        return p.with_suffix(".duckdb")
    # Fallback: append .duckdb (unusual, but keep the mapping total)
    return p.with_name(p.name + ".duckdb")


def connect(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Open (and initialise if needed) a DuckDB kanban file.

    Caching mirrors ``kanban_db.connect``: the first connection to a
    given resolved path runs the schema DDL, subsequent connections skip
    that step (DuckDB can't hold ``CREATE TABLE IF NOT EXISTS`` under
    an already-existing schema without issuing a re-parse we don't need).
    """
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    resolved = str(path.resolve())
    conn = duckdb.connect(str(path))
    if resolved not in _INITIALIZED_PATHS:
        conn.execute(SCHEMA_SQL)
        _INITIALIZED_PATHS.add(resolved)
    return conn


def init_db(db_path: Path) -> Path:
    """Force schema initialisation. Idempotent."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(path))
    try:
        conn.execute(SCHEMA_SQL)
        _INITIALIZED_PATHS.add(str(path.resolve()))
    finally:
        conn.close()
    return path


@contextmanager
def write_txn(conn: duckdb.DuckDBPyConnection) -> Iterator[duckdb.DuckDBPyConnection]:
    """DuckDB write transaction. Mirrors ``kanban_db.write_txn``.

    DuckDB defaults to auto-commit; we explicitly BEGIN so multi-statement
    writes stay atomic (mirrors SQLite ``BEGIN IMMEDIATE`` semantics).
    """
    conn.execute("BEGIN TRANSACTION")
    try:
        yield conn
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except Exception:
            pass
        raise
    else:
        conn.execute("COMMIT")


# --------------------------------------------------------------------------- #
# ID generation (task ids only — event/run/comment ids come from sequences)
# --------------------------------------------------------------------------- #


def _new_task_id() -> str:
    """Same shape/entropy as ``kanban_db._new_task_id``: ``t_`` + 4 hex bytes."""
    return "t_" + secrets.token_hex(4)


def _claimer_id() -> str:
    import socket
    try:
        host = socket.gethostname() or "unknown"
    except Exception:
        host = "unknown"
    return f"{host}:{os.getpid()}"


def _canonical_assignee(assignee: Optional[str]) -> Optional[str]:
    if assignee is None:
        return None
    try:
        from hermes_kanban._adapters import normalize_profile_name
        return normalize_profile_name(assignee)
    except Exception:
        return assignee


# --------------------------------------------------------------------------- #
# Row → dataclass helpers
#
# DuckDB returns tuples (not sqlite3.Row), so we build dataclass instances
# from positional column projections. This keeps DuckDB reads returning the
# same dataclass shape the SQLite path uses.
# --------------------------------------------------------------------------- #

# Column order used by _task_from_tuple. Must match SELECT list below.
_TASK_COLS = (
    "id", "title", "body", "assignee", "status", "priority", "created_by",
    "created_at", "started_at", "completed_at", "workspace_kind",
    "workspace_path", "claim_lock", "claim_expires", "tenant", "result",
    "idempotency_key", "consecutive_failures", "worker_pid",
    "last_failure_error", "max_runtime_seconds", "last_heartbeat_at",
    "current_run_id", "workflow_template_id", "current_step_key", "skills",
    "max_retries",
)
_TASK_SELECT = "SELECT " + ", ".join(_TASK_COLS) + " FROM tasks"


def _task_from_tuple(row: tuple) -> Task:
    d = dict(zip(_TASK_COLS, row))
    skills_value: Optional[list] = None
    raw_skills = d.get("skills")
    if raw_skills:
        try:
            parsed = json.loads(raw_skills)
            if isinstance(parsed, list):
                skills_value = [str(s) for s in parsed if s]
        except Exception:
            skills_value = None
    return Task(
        id=d["id"],
        title=d["title"],
        body=d["body"],
        assignee=d["assignee"],
        status=d["status"],
        priority=int(d["priority"]) if d["priority"] is not None else 0,
        created_by=d["created_by"],
        created_at=int(d["created_at"]),
        started_at=int(d["started_at"]) if d["started_at"] is not None else None,
        completed_at=(
            int(d["completed_at"]) if d["completed_at"] is not None else None
        ),
        workspace_kind=d["workspace_kind"],
        workspace_path=d["workspace_path"],
        claim_lock=d["claim_lock"],
        claim_expires=(
            int(d["claim_expires"]) if d["claim_expires"] is not None else None
        ),
        tenant=d["tenant"],
        result=d["result"],
        idempotency_key=d["idempotency_key"],
        consecutive_failures=int(d["consecutive_failures"] or 0),
        worker_pid=d["worker_pid"],
        last_failure_error=d["last_failure_error"],
        max_runtime_seconds=d["max_runtime_seconds"],
        last_heartbeat_at=(
            int(d["last_heartbeat_at"])
            if d["last_heartbeat_at"] is not None else None
        ),
        current_run_id=(
            int(d["current_run_id"])
            if d["current_run_id"] is not None else None
        ),
        workflow_template_id=d["workflow_template_id"],
        current_step_key=d["current_step_key"],
        skills=skills_value,
        max_retries=d["max_retries"],
    )


_RUN_COLS = (
    "id", "task_id", "profile", "step_key", "status", "claim_lock",
    "claim_expires", "worker_pid", "max_runtime_seconds", "last_heartbeat_at",
    "started_at", "ended_at", "outcome", "summary", "metadata", "error",
)
_RUN_SELECT = "SELECT " + ", ".join(_RUN_COLS) + " FROM task_runs"


def _run_from_tuple(row: tuple) -> Run:
    d = dict(zip(_RUN_COLS, row))
    try:
        meta = json.loads(d["metadata"]) if d["metadata"] else None
    except Exception:
        meta = None
    return Run(
        id=int(d["id"]),
        task_id=d["task_id"],
        profile=d["profile"],
        step_key=d["step_key"],
        status=d["status"],
        claim_lock=d["claim_lock"],
        claim_expires=(
            int(d["claim_expires"]) if d["claim_expires"] is not None else None
        ),
        worker_pid=d["worker_pid"],
        max_runtime_seconds=d["max_runtime_seconds"],
        last_heartbeat_at=(
            int(d["last_heartbeat_at"])
            if d["last_heartbeat_at"] is not None else None
        ),
        started_at=int(d["started_at"]),
        ended_at=int(d["ended_at"]) if d["ended_at"] is not None else None,
        outcome=d["outcome"],
        summary=d["summary"],
        metadata=meta,
        error=d["error"],
    )


# --------------------------------------------------------------------------- #
# Write ops — mirror kanban_db module-level functions
# --------------------------------------------------------------------------- #


def _find_missing_parents(
    conn: duckdb.DuckDBPyConnection, parents: Iterable[str]
) -> list[str]:
    parents = list(parents)
    if not parents:
        return []
    placeholders = ",".join("?" * len(parents))
    rows = conn.execute(
        f"SELECT id FROM tasks WHERE id IN ({placeholders})", parents
    ).fetchall()
    present = {r[0] for r in rows}
    return [p for p in parents if p not in present]


def create_task(
    conn: duckdb.DuckDBPyConnection,
    *,
    title: str,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    created_by: Optional[str] = None,
    workspace_kind: str = "scratch",
    workspace_path: Optional[str] = None,
    tenant: Optional[str] = None,
    priority: int = 0,
    parents: Iterable[str] = (),
    triage: bool = False,
    idempotency_key: Optional[str] = None,
    max_runtime_seconds: Optional[int] = None,
    skills: Optional[Iterable[str]] = None,
    max_retries: Optional[int] = None,
    # ADR-012 §4: dual-write mode passes SQLite-generated task_id through
    # so both backends carry the same primary key.
    task_id: Optional[str] = None,
) -> str:
    """Create a new task. Returns task id.

    Mirrors ``kanban_db.create_task`` semantics: initial status is
    ``triage`` if ``triage=True``, else ``ready`` (or ``todo`` if any
    parent is not yet done). Idempotency-key short-circuit is honoured.
    """
    assignee = _canonical_assignee(assignee)
    if not title or not title.strip():
        raise ValueError("title is required")
    if workspace_kind not in VALID_WORKSPACE_KINDS:
        raise ValueError(
            f"workspace_kind must be one of {sorted(VALID_WORKSPACE_KINDS)}, "
            f"got {workspace_kind!r}"
        )
    parents = tuple(p for p in parents if p)

    skills_list: Optional[list[str]] = None
    if skills is not None:
        cleaned: list[str] = []
        seen: set[str] = set()
        for s in skills:
            if not s:
                continue
            name = str(s).strip()
            if not name:
                continue
            if "," in name:
                raise ValueError(
                    f"skill name cannot contain comma: {name!r}"
                )
            if name in seen:
                continue
            seen.add(name)
            cleaned.append(name)
        skills_list = cleaned

    if idempotency_key:
        row = conn.execute(
            "SELECT id FROM tasks WHERE idempotency_key = ? "
            "AND status != 'archived' "
            "ORDER BY created_at DESC LIMIT 1",
            [idempotency_key],
        ).fetchone()
        if row:
            return row[0]

    now = int(time.time())

    for attempt in range(2):
        tid = task_id or _new_task_id()
        try:
            with write_txn(conn):
                if triage:
                    initial_status = "triage"
                else:
                    initial_status = "ready"
                    if parents:
                        missing = _find_missing_parents(conn, parents)
                        if missing:
                            raise ValueError(
                                f"unknown parent task(s): {', '.join(missing)}"
                            )
                        placeholders = ",".join("?" * len(parents))
                        rows = conn.execute(
                            f"SELECT status FROM tasks WHERE id IN ({placeholders})",
                            list(parents),
                        ).fetchall()
                        if any(r[0] != "done" for r in rows):
                            initial_status = "todo"
                if triage and parents:
                    missing = _find_missing_parents(conn, parents)
                    if missing:
                        raise ValueError(
                            f"unknown parent task(s): {', '.join(missing)}"
                        )

                conn.execute(
                    """
                    INSERT INTO tasks (
                        id, title, body, assignee, status, priority,
                        created_by, created_at, workspace_kind, workspace_path,
                        tenant, idempotency_key, max_runtime_seconds, skills,
                        max_retries
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        tid,
                        title.strip(),
                        body,
                        assignee,
                        initial_status,
                        priority,
                        created_by,
                        now,
                        workspace_kind,
                        workspace_path,
                        tenant,
                        idempotency_key,
                        int(max_runtime_seconds) if max_runtime_seconds else None,
                        json.dumps(skills_list) if skills_list is not None else None,
                        int(max_retries) if max_retries is not None else None,
                    ],
                )
                for pid in parents:
                    # DuckDB's PRIMARY KEY collision raises; catch dup and continue.
                    try:
                        conn.execute(
                            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                            [pid, tid],
                        )
                    except duckdb.ConstraintException:
                        pass
                _append_event(
                    conn,
                    tid,
                    "created",
                    {
                        "assignee": assignee,
                        "status": initial_status,
                        "parents": list(parents),
                        "tenant": tenant,
                        "skills": list(skills_list) if skills_list else None,
                    },
                )
            return tid
        except duckdb.ConstraintException:
            if attempt == 1 or task_id:
                # Caller-provided task_id must not collide silently.
                raise
            continue
    raise RuntimeError("unreachable")


def get_task(
    conn: duckdb.DuckDBPyConnection, task_id: str
) -> Optional[Task]:
    row = conn.execute(
        _TASK_SELECT + " WHERE id = ?", [task_id]
    ).fetchone()
    return _task_from_tuple(row) if row else None


def list_tasks(
    conn: duckdb.DuckDBPyConnection,
    *,
    assignee: Optional[str] = None,
    status: Optional[str] = None,
    tenant: Optional[str] = None,
    include_archived: bool = False,
    limit: Optional[int] = None,
) -> list[Task]:
    query = _TASK_SELECT + " WHERE 1=1"
    params: list[Any] = []
    if assignee is not None:
        query += " AND assignee = ?"
        params.append(_canonical_assignee(assignee))
    if status is not None:
        if status not in VALID_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
        query += " AND status = ?"
        params.append(status)
    if tenant is not None:
        query += " AND tenant = ?"
        params.append(tenant)
    if not include_archived and status != "archived":
        query += " AND status != 'archived'"
    query += " ORDER BY priority DESC, created_at ASC"
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query, params).fetchall()
    return [_task_from_tuple(r) for r in rows]


def assign_task(
    conn: duckdb.DuckDBPyConnection, task_id: str, profile: Optional[str]
) -> bool:
    profile = _canonical_assignee(profile)
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, assignee FROM tasks WHERE id = ?",
            [task_id],
        ).fetchone()
        if not row:
            return False
        status, claim_lock, assignee = row
        if claim_lock is not None and status == "running":
            raise RuntimeError(
                f"cannot reassign {task_id}: currently running (claimed)."
            )
        if assignee != profile:
            conn.execute(
                "UPDATE tasks SET assignee = ?, consecutive_failures = 0, "
                "last_failure_error = NULL WHERE id = ?",
                [profile, task_id],
            )
        else:
            conn.execute(
                "UPDATE tasks SET assignee = ? WHERE id = ?",
                [profile, task_id],
            )
        _append_event(conn, task_id, "assigned", {"assignee": profile})
        return True


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #


def _would_cycle(
    conn: duckdb.DuckDBPyConnection, parent_id: str, child_id: str
) -> bool:
    seen: set[str] = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = conn.execute(
            "SELECT child_id FROM task_links WHERE parent_id = ?", [node]
        ).fetchall()
        stack.extend(r[0] for r in rows)
    return False


def link_tasks(
    conn: duckdb.DuckDBPyConnection, parent_id: str, child_id: str
) -> None:
    if parent_id == child_id:
        raise ValueError("a task cannot depend on itself")
    with write_txn(conn):
        missing = _find_missing_parents(conn, [parent_id, child_id])
        if missing:
            raise ValueError(f"unknown task(s): {', '.join(missing)}")
        if _would_cycle(conn, parent_id, child_id):
            raise ValueError(
                f"linking {parent_id} -> {child_id} would create a cycle"
            )
        try:
            conn.execute(
                "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
                [parent_id, child_id],
            )
        except duckdb.ConstraintException:
            pass
        parent_status_row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", [parent_id]
        ).fetchone()
        parent_status = parent_status_row[0] if parent_status_row else None
        if parent_status != "done":
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'",
                [child_id],
            )
        _append_event(
            conn, child_id, "linked",
            {"parent": parent_id, "child": child_id},
        )


def unlink_tasks(
    conn: duckdb.DuckDBPyConnection, parent_id: str, child_id: str
) -> bool:
    with write_txn(conn):
        # DuckDB doesn't expose rowcount reliably from execute(); count first.
        existed = conn.execute(
            "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
            [parent_id, child_id],
        ).fetchone() is not None
        if not existed:
            return False
        conn.execute(
            "DELETE FROM task_links WHERE parent_id = ? AND child_id = ?",
            [parent_id, child_id],
        )
        _append_event(
            conn, child_id, "unlinked",
            {"parent": parent_id, "child": child_id},
        )
    # Re-promote children whose parents are now all done.
    recompute_ready(conn)
    return True


def parent_ids(
    conn: duckdb.DuckDBPyConnection, task_id: str
) -> list[str]:
    rows = conn.execute(
        "SELECT parent_id FROM task_links WHERE child_id = ? ORDER BY parent_id",
        [task_id],
    ).fetchall()
    return [r[0] for r in rows]


def child_ids(
    conn: duckdb.DuckDBPyConnection, task_id: str
) -> list[str]:
    rows = conn.execute(
        "SELECT child_id FROM task_links WHERE parent_id = ? ORDER BY child_id",
        [task_id],
    ).fetchall()
    return [r[0] for r in rows]


# --------------------------------------------------------------------------- #
# Comments & events
# --------------------------------------------------------------------------- #


def add_comment(
    conn: duckdb.DuckDBPyConnection,
    task_id: str,
    author: str,
    body: str,
    *,
    row_id: Optional[int] = None,
) -> int:
    """Insert a comment. Returns the comment id.

    ``row_id`` is the ADR-012 §4 dual-write passthrough: when this
    adapter is running as the mirror behind the facade, pass the id
    SQLite generated so parity checks compare like-for-like.
    """
    if not body or not body.strip():
        raise ValueError("comment body is required")
    if not author or not author.strip():
        raise ValueError("comment author is required")
    now = int(time.time())
    with write_txn(conn):
        exists = conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", [task_id]
        ).fetchone()
        if not exists:
            raise ValueError(f"unknown task {task_id}")
        if row_id is not None:
            conn.execute(
                "INSERT INTO task_comments (id, task_id, author, body, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [row_id, task_id, author.strip(), body.strip(), now],
            )
            new_id = int(row_id)
            _sequence_floor(conn, "task_comments")  # keep nextval above passthrough ids
        else:
            conn.execute(
                "INSERT INTO task_comments (id, task_id, author, body, created_at) "
                "SELECT COALESCE(MAX(id), 0) + 1, ?, ?, ?, ? FROM task_comments",
                [task_id, author.strip(), body.strip(), now],
            )
            r = conn.execute("SELECT MAX(id) FROM task_comments").fetchone()
            new_id = int(r[0]) if r else 0
        _append_event(
            conn, task_id, "commented",
            {"author": author, "len": len(body)},
        )
        return new_id


def list_comments(
    conn: duckdb.DuckDBPyConnection, task_id: str
) -> list[Comment]:
    rows = conn.execute(
        "SELECT id, task_id, author, body, created_at FROM task_comments "
        "WHERE task_id = ? ORDER BY created_at ASC",
        [task_id],
    ).fetchall()
    return [
        Comment(
            id=int(r[0]),
            task_id=r[1],
            author=r[2],
            body=r[3],
            created_at=int(r[4]),
        )
        for r in rows
    ]


def list_events(
    conn: duckdb.DuckDBPyConnection, task_id: str
) -> list[Event]:
    rows = conn.execute(
        "SELECT id, task_id, kind, payload, created_at, run_id "
        "FROM task_events WHERE task_id = ? "
        "ORDER BY created_at ASC, id ASC",
        [task_id],
    ).fetchall()
    out: list[Event] = []
    for r in rows:
        try:
            payload = json.loads(r[3]) if r[3] else None
        except Exception:
            payload = None
        out.append(
            Event(
                id=int(r[0]),
                task_id=r[1],
                kind=r[2],
                payload=payload,
                created_at=int(r[4]),
                run_id=int(r[5]) if r[5] is not None else None,
            )
        )
    return out


def _sequence_floor(conn: duckdb.DuckDBPyConnection, table: str) -> int:
    """Table-global MAX(id) used to floor id sequences.

    Explicit-id inserts (dual-write passthrough / backfill from SQLite) never
    advance a DEFAULT-nextval sequence, so after any mirror backfill the
    sequence can sit below rows already in the table. The next adapter-
    generated id would then collide ('Duplicate key id: 1' on every event for
    a new task -- kanban t_fd94df04, 2026-09-01).
    """
    try:
        row = conn.execute(
            f"SELECT COALESCE(MAX(id), 0) FROM {table}"
        ).fetchone()
        return int(row[0]) if row else 0
    except Exception:  # pragma: no cover - defensive; never block the write
        return 0

def _append_event(
    conn: duckdb.DuckDBPyConnection,
    task_id: str,
    kind: str,
    payload: Optional[dict] = None,
    *,
    run_id: Optional[int] = None,
    row_id: Optional[int] = None,
) -> None:
    """Insert one row into ``task_events``.

    Called from within an already-open write txn (matches
    ``kanban_db._append_event``).
    """
    now = int(time.time())
    pl = json.dumps(payload, ensure_ascii=False) if payload else None
    if row_id is not None:
        conn.execute(
            "INSERT INTO task_events (id, task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [row_id, task_id, run_id, kind, pl, now],
        )
        _sequence_floor(conn, "task_events")  # keep nextval above passthrough ids
    else:
        conn.execute(
            "INSERT INTO task_events (id, task_id, run_id, kind, payload, created_at) "
            "SELECT COALESCE(MAX(id), 0) + 1, ?, ?, ?, ?, ? FROM task_events",
            [task_id, run_id, kind, pl, now],
        )


# --------------------------------------------------------------------------- #
# Claim / heartbeat / run lifecycle
# --------------------------------------------------------------------------- #


def claim_task(
    conn: duckdb.DuckDBPyConnection,
    task_id: str,
    *,
    lock: Optional[str] = None,
    ttl_seconds: Optional[int] = None,
    profile: Optional[str] = None,
    step_key: Optional[str] = None,
    max_runtime_seconds: Optional[int] = None,
    run_id: Optional[int] = None,
) -> Optional[int]:
    """Atomic claim + open a run row. Returns the run id.

    Mirrors ``kanban_db.claim_task`` in the essentials: succeeds only if
    the row is currently free (claim_lock is NULL or claim_expires is
    in the past). On success writes:
    * ``tasks.claim_lock``, ``claim_expires``, ``status='running'``,
      ``started_at`` (first claim), ``current_run_id``.
    * a new row in ``task_runs`` with ``status='running'`` and the
      supplied profile/step_key/runtime cap.
    * a ``claimed`` event bound to the run.
    """
    lock = lock or _claimer_id()
    profile = _canonical_assignee(profile)
    now = int(time.time())
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        row = conn.execute(
            "SELECT status, claim_lock, claim_expires, started_at "
            "FROM tasks WHERE id = ?",
            [task_id],
        ).fetchone()
        if not row:
            return None
        status, existing_lock, existing_expires, started_at = row
        if existing_lock is not None and (
            existing_expires is None or existing_expires > now
        ):
            return None  # still claimed by someone else
        # Insert run row
        if run_id is not None:
            conn.execute(
                """
                INSERT INTO task_runs (id, task_id, profile, step_key, status,
                                       claim_lock, claim_expires,
                                       max_runtime_seconds, last_heartbeat_at,
                                       started_at)
                VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                [run_id, task_id, profile, step_key, lock, expires,
                 max_runtime_seconds, now, now],
            )
            new_run_id = int(run_id)
            _sequence_floor(conn, "task_runs")  # keep nextval above passthrough ids
        else:
            conn.execute(
                """
                INSERT INTO task_runs (id, task_id, profile, step_key, status,
                                       claim_lock, claim_expires,
                                       max_runtime_seconds, last_heartbeat_at,
                                       started_at)
                SELECT COALESCE(MAX(id), 0) + 1, ?, ?, ?, 'running', ?, ?, ?, ?, ?
                FROM task_runs
                """,
                [task_id, profile, step_key, lock, expires,
                 max_runtime_seconds, now, now],
            )
            r = conn.execute("SELECT MAX(id) FROM task_runs").fetchone()
            new_run_id = int(r[0]) if r else 0

        conn.execute(
            """
            UPDATE tasks
               SET status = 'running',
                   claim_lock = ?,
                   claim_expires = ?,
                   started_at = COALESCE(started_at, ?),
                   last_heartbeat_at = ?,
                   current_run_id = ?
             WHERE id = ?
            """,
            [lock, expires, now, now, new_run_id, task_id],
        )
        _append_event(
            conn, task_id, "claimed",
            {"lock": lock, "expires": expires, "run_id": new_run_id},
            run_id=new_run_id,
        )
        return new_run_id


def heartbeat_claim(
    conn: duckdb.DuckDBPyConnection,
    task_id: str,
    *,
    lock: str,
    ttl_seconds: Optional[int] = None,
    note: Optional[str] = None,
) -> bool:
    """Refresh the claim lock and heartbeat timestamp.

    Fails (returns False) if the current claim_lock has drifted away
    from ``lock`` (someone else reclaimed the task).
    """
    now = int(time.time())
    expires = now + _resolve_claim_ttl_seconds(ttl_seconds)
    with write_txn(conn):
        row = conn.execute(
            "SELECT claim_lock, current_run_id FROM tasks WHERE id = ?",
            [task_id],
        ).fetchone()
        if not row or row[0] != lock:
            return False
        run_id = row[1]
        conn.execute(
            "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
            "WHERE id = ? AND claim_lock = ?",
            [expires, now, task_id, lock],
        )
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET claim_expires = ?, last_heartbeat_at = ? "
                "WHERE id = ?",
                [expires, now, run_id],
            )
        _append_event(
            conn, task_id, "heartbeat",
            {"note": note} if note else None,
            run_id=int(run_id) if run_id is not None else None,
        )
        return True


def release_stale_claims(
    conn: duckdb.DuckDBPyConnection, *, now: Optional[int] = None
) -> int:
    """Release any claim whose ``claim_expires`` has passed.

    Sets the task back to ``ready`` (if it was ``running``) and closes
    the corresponding run row with outcome ``reclaimed``. Returns the
    number of tasks released.
    """
    t = int(now if now is not None else time.time())
    with write_txn(conn):
        rows = conn.execute(
            "SELECT id, current_run_id FROM tasks "
            "WHERE claim_lock IS NOT NULL "
            "  AND claim_expires IS NOT NULL "
            "  AND claim_expires < ?",
            [t],
        ).fetchall()
        if not rows:
            return 0
        for task_id, run_id in rows:
            conn.execute(
                "UPDATE tasks SET claim_lock = NULL, claim_expires = NULL, "
                "status = CASE WHEN status = 'running' THEN 'ready' ELSE status END, "
                "current_run_id = NULL "
                "WHERE id = ?",
                [task_id],
            )
            if run_id is not None:
                conn.execute(
                    "UPDATE task_runs SET status = 'released', outcome = 'reclaimed', "
                    "ended_at = ? WHERE id = ? AND ended_at IS NULL",
                    [t, run_id],
                )
            _append_event(
                conn, task_id, "reclaimed_stale",
                {"prev_run_id": int(run_id) if run_id is not None else None},
            )
        return len(rows)


def reclaim_task(
    conn: duckdb.DuckDBPyConnection,
    task_id: str,
    *,
    force: bool = False,
) -> bool:
    """Reclaim (release) a specific task's claim. Returns True on release."""
    now = int(time.time())
    with write_txn(conn):
        row = conn.execute(
            "SELECT claim_lock, claim_expires, current_run_id FROM tasks "
            "WHERE id = ?",
            [task_id],
        ).fetchone()
        if not row or row[0] is None:
            return False
        _, expires, run_id = row
        if not force and expires and expires > now:
            return False
        conn.execute(
            "UPDATE tasks SET claim_lock = NULL, claim_expires = NULL, "
            "status = CASE WHEN status = 'running' THEN 'ready' ELSE status END, "
            "current_run_id = NULL WHERE id = ?",
            [task_id],
        )
        if run_id is not None:
            conn.execute(
                "UPDATE task_runs SET status = 'released', outcome = 'reclaimed', "
                "ended_at = ? WHERE id = ? AND ended_at IS NULL",
                [now, run_id],
            )
        _append_event(conn, task_id, "reclaimed", {"forced": force})
        return True


def reassign_task(
    conn: duckdb.DuckDBPyConnection,
    task_id: str,
    profile: Optional[str],
) -> bool:
    """Reassign a task after forcibly releasing any stale claim."""
    reclaim_task(conn, task_id, force=True)
    return assign_task(conn, task_id, profile)


# --------------------------------------------------------------------------- #
# Run end + completion + block/unblock
# --------------------------------------------------------------------------- #


def _end_run(
    conn: duckdb.DuckDBPyConnection,
    task_id: str,
    *,
    outcome: str,
    summary: Optional[str] = None,
    error: Optional[str] = None,
    metadata: Optional[dict] = None,
    status: Optional[str] = None,
) -> Optional[int]:
    """Close the active run row for ``task_id`` and clear the pointer."""
    now = int(time.time())
    row = conn.execute(
        "SELECT current_run_id FROM tasks WHERE id = ?", [task_id]
    ).fetchone()
    if not row or not row[0]:
        return None
    run_id = int(row[0])
    conn.execute(
        """
        UPDATE task_runs
           SET status = ?,
               outcome = ?,
               summary = ?,
               error = ?,
               metadata = ?,
               ended_at = ?
         WHERE id = ?
        """,
        [
            status or outcome,
            outcome,
            summary,
            error,
            json.dumps(metadata, ensure_ascii=False) if metadata else None,
            now,
            run_id,
        ],
    )
    conn.execute(
        "UPDATE tasks SET current_run_id = NULL WHERE id = ?",
        [task_id],
    )
    return run_id


def complete_task(
    conn: duckdb.DuckDBPyConnection,
    task_id: str,
    *,
    result: Optional[str] = None,
    summary: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> bool:
    """Mark a task done. Closes the active run and re-promotes children."""
    now = int(time.time())
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", [task_id]
        ).fetchone()
        if not row:
            return False
        _end_run(
            conn, task_id,
            outcome="completed", summary=summary,
            metadata=metadata, status="done",
        )
        conn.execute(
            """
            UPDATE tasks
               SET status = 'done',
                   completed_at = ?,
                   result = COALESCE(?, result),
                   claim_lock = NULL,
                   claim_expires = NULL,
                   consecutive_failures = 0,
                   last_failure_error = NULL
             WHERE id = ?
            """,
            [now, result, task_id],
        )
        _append_event(
            conn, task_id, "completed",
            {"summary_len": len(summary) if summary else 0},
        )
        recompute_ready(conn)
        return True


def block_task(
    conn: duckdb.DuckDBPyConnection,
    task_id: str,
    *,
    reason: str,
    kind: Optional[str] = None,
) -> bool:
    """Move a task to blocked. Closes the active run."""
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", [task_id]
        ).fetchone()
        if not row:
            return False
        _end_run(
            conn, task_id,
            outcome="blocked", summary=reason,
            status="blocked",
        )
        conn.execute(
            "UPDATE tasks SET status = 'blocked', claim_lock = NULL, "
            "claim_expires = NULL WHERE id = ?",
            [task_id],
        )
        _append_event(
            conn, task_id, "blocked",
            {"reason": reason, "kind": kind},
        )
        return True


def unblock_task(
    conn: duckdb.DuckDBPyConnection, task_id: str
) -> bool:
    """Move a blocked task back to ready (or todo if parents pending)."""
    with write_txn(conn):
        row = conn.execute(
            "SELECT status FROM tasks WHERE id = ?", [task_id]
        ).fetchone()
        if not row or row[0] != "blocked":
            return False
        conn.execute(
            "UPDATE tasks SET status = 'ready' WHERE id = ?", [task_id]
        )
        _append_event(conn, task_id, "unblocked", None)
        recompute_ready(conn)
        return True


# --------------------------------------------------------------------------- #
# recompute_ready — promotes todo→ready when all parents are done
# --------------------------------------------------------------------------- #


def recompute_ready(conn: duckdb.DuckDBPyConnection) -> int:
    """Promote ``todo`` tasks whose parents are all ``done`` to ``ready``.

    Returns the number of tasks promoted.
    """
    # Find todo tasks whose every parent is done.
    rows = conn.execute(
        """
        SELECT t.id FROM tasks t
        WHERE t.status = 'todo'
          AND NOT EXISTS (
              SELECT 1 FROM task_links l
              JOIN tasks p ON p.id = l.parent_id
              WHERE l.child_id = t.id AND p.status != 'done'
          )
        """
    ).fetchall()
    if not rows:
        return 0
    ids = [r[0] for r in rows]
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE tasks SET status = 'ready' WHERE id IN ({placeholders})",
        ids,
    )
    for tid in ids:
        _append_event(conn, tid, "promoted", None)
    return len(ids)


# --------------------------------------------------------------------------- #
# Notify-subs
# --------------------------------------------------------------------------- #


def add_kanban_notify_sub(
    conn: duckdb.DuckDBPyConnection,
    *,
    task_id: str,
    platform: str,
    chat_id: str,
    thread_id: str = "",
    user_id: Optional[str] = None,
) -> bool:
    """Register a gateway notification subscription. Idempotent."""
    now = int(time.time())
    with write_txn(conn):
        try:
            conn.execute(
                """
                INSERT INTO kanban_notify_subs
                    (task_id, platform, chat_id, thread_id, user_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [task_id, platform, chat_id, thread_id, user_id, now],
            )
            return True
        except duckdb.ConstraintException:
            return False


# --------------------------------------------------------------------------- #
# Public function names exported to match kanban_db module surface
# --------------------------------------------------------------------------- #

__all__ = [
    "SCHEMA_SQL",
    "Task", "Run", "Comment", "Event",
    "connect", "init_db", "write_txn",
    "duckdb_kanban_path",
    "create_task", "get_task", "list_tasks",
    "assign_task", "reassign_task",
    "link_tasks", "unlink_tasks", "parent_ids", "child_ids",
    "add_comment", "list_comments", "list_events",
    "_append_event",
    "claim_task", "heartbeat_claim",
    "release_stale_claims", "reclaim_task",
    "_end_run",
    "complete_task", "block_task", "unblock_task",
    "recompute_ready",
    "add_kanban_notify_sub",
]
