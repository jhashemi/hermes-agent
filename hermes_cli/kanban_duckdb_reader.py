"""Read-only DuckDB-backed Kanban reader for the hermes-agent harness.

This module provides a thin read-side shim so the dispatcher can query
task state directly from a DuckDB file instead of going through the SQLite
mirror written by ``SQLiteKanbanAdapter``.  Write operations are NOT
implemented here — they continue to go through the legacy SQLite path until
ADR-012/Phase-2 retires it.

Backend selection is controlled by the ``HERMES_KANBAN_BACKEND`` env var:

  * ``duckdb``  — always use DuckDB (raises if the file is missing/unreachable)
  * ``sqlite``  — always use SQLite via the standard ``kanban_db`` path
  * ``auto``    — use DuckDB when ``HERMES_KANBAN_DUCKDB_PATH`` (or the default
                  ``data/kanban.duckdb`` relative to ``HERMES_HOME``) exists;
                  fall back to SQLite otherwise.

Default is ``auto``.

The DuckDB file path is resolved from ``HERMES_KANBAN_DUCKDB_PATH`` when
set, otherwise ``<HERMES_HOME>/data/kanban.duckdb`` (or
``~/.hermes/data/kanban.duckdb`` when ``HERMES_HOME`` is unset).

Schema
------
The DuckDB file is expected to carry a ``tasks`` table whose column set is
a superset of the SQLite schema defined in ``kanban_db.SCHEMA_SQL``.
Columns missing from the DuckDB file are silently treated as ``None``/0/False
by :meth:`DuckDBKanbanReader.get_task` and
:meth:`DuckDBKanbanReader.list_tasks`, preserving the same ``Task`` dataclass
contract the rest of the codebase relies on.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# DuckDB import — soft-optional so the module is importable in environments
# where duckdb is not installed (the caller falls back to SQLite).
# ---------------------------------------------------------------------------

try:
    import duckdb as _duckdb
    _DUCKDB_AVAILABLE = True
except ImportError:  # pragma: no cover
    _duckdb = None  # type: ignore[assignment]
    _DUCKDB_AVAILABLE = False

from hermes_cli.kanban_db import Task, VALID_STATUSES, VALID_SORT_ORDERS


# ---------------------------------------------------------------------------
# Config constants
# ---------------------------------------------------------------------------

#: Environment variable that selects the read backend.
ENV_KANBAN_BACKEND = "HERMES_KANBAN_BACKEND"

#: Environment variable that pins the DuckDB file path directly.
ENV_KANBAN_DUCKDB_PATH = "HERMES_KANBAN_DUCKDB_PATH"

#: Valid backend names.
VALID_BACKENDS = {"duckdb", "sqlite", "auto"}

#: Default relative path for the DuckDB file under HERMES_HOME.
_DEFAULT_DUCKDB_RELPATH = Path("data") / "kanban.duckdb"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

def resolve_duckdb_path() -> Path:
    """Return the absolute path to the DuckDB kanban file.

    Precedence:
    1. ``HERMES_KANBAN_DUCKDB_PATH`` env var
    2. ``<HERMES_HOME>/data/kanban.duckdb``
    3. ``~/.hermes/data/kanban.duckdb``
    """
    explicit = os.environ.get(ENV_KANBAN_DUCKDB_PATH)
    if explicit:
        return Path(explicit)
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        return Path(hermes_home) / _DEFAULT_DUCKDB_RELPATH
    return Path.home() / ".hermes" / _DEFAULT_DUCKDB_RELPATH


def get_backend() -> str:
    """Return the active backend name: ``duckdb``, ``sqlite``, or ``auto``.

    Reads ``HERMES_KANBAN_BACKEND``; defaults to ``auto``.
    Raises ``ValueError`` on unrecognised values.
    """
    raw = os.environ.get(ENV_KANBAN_BACKEND, "auto").strip().lower()
    if raw not in VALID_BACKENDS:
        raise ValueError(
            f"HERMES_KANBAN_BACKEND must be one of {sorted(VALID_BACKENDS)}; got {raw!r}"
        )
    return raw


def should_use_duckdb() -> bool:
    """Return True if the current config says to read from DuckDB.

    * ``duckdb`` → True (raises ``RuntimeError`` if duckdb not importable)
    * ``sqlite``  → False
    * ``auto``    → True iff the DuckDB file exists AND duckdb is importable
    """
    backend = get_backend()
    if backend == "sqlite":
        return False
    if backend == "duckdb":
        if not _DUCKDB_AVAILABLE:
            raise RuntimeError(
                "HERMES_KANBAN_BACKEND=duckdb requires the 'duckdb' package; "
                "install it with: pip install duckdb"
            )
        return True
    # auto
    if not _DUCKDB_AVAILABLE:
        return False
    return resolve_duckdb_path().exists()


# ---------------------------------------------------------------------------
# Row → Task conversion
# ---------------------------------------------------------------------------

def _task_from_duckdb_row(row: tuple, columns: list[str]) -> Task:
    """Convert a raw DuckDB row tuple + column list into a ``Task``."""
    r: dict[str, Any] = dict(zip(columns, row))

    skills_value: Optional[list] = None
    raw_skills = r.get("skills")
    if raw_skills:
        try:
            parsed = json.loads(raw_skills)
            if isinstance(parsed, list):
                skills_value = [str(s) for s in parsed if s]
        except Exception:
            skills_value = None

    def _get(key: str, default: Any = None) -> Any:
        return r.get(key, default)

    return Task(
        id=r["id"],
        title=r["title"],
        body=_get("body"),
        assignee=_get("assignee"),
        status=r["status"],
        priority=int(_get("priority") or 0),
        created_by=_get("created_by"),
        created_at=int(r["created_at"]),
        started_at=_get("started_at"),
        completed_at=_get("completed_at"),
        workspace_kind=_get("workspace_kind") or "scratch",
        workspace_path=_get("workspace_path"),
        claim_lock=_get("claim_lock"),
        claim_expires=_get("claim_expires"),
        tenant=_get("tenant"),
        branch_name=_get("branch_name"),
        project_id=_get("project_id"),
        result=_get("result"),
        idempotency_key=_get("idempotency_key"),
        consecutive_failures=int(_get("consecutive_failures") or 0),
        worker_pid=_get("worker_pid"),
        last_failure_error=_get("last_failure_error"),
        max_runtime_seconds=_get("max_runtime_seconds"),
        last_heartbeat_at=_get("last_heartbeat_at"),
        current_run_id=_get("current_run_id"),
        workflow_template_id=_get("workflow_template_id"),
        current_step_key=_get("current_step_key"),
        skills=skills_value,
        model_override=_get("model_override") or None,
        provider_override=_get("provider_override") or None,
        max_retries=_get("max_retries"),
        goal_mode=bool(_get("goal_mode")),
        goal_max_turns=_get("goal_max_turns"),
        session_id=_get("session_id"),
        block_kind=_get("block_kind") or None,
        block_recurrences=int(_get("block_recurrences") or 0),
    )


# ---------------------------------------------------------------------------
# Reader class
# ---------------------------------------------------------------------------

@dataclass
class DuckDBKanbanReader:
    """Read-only Kanban reader backed by a DuckDB file.

    Opens the file in ``read_only=True`` mode; all writes are rejected at
    the DuckDB level, so there is no risk of accidentally mutating the SSOT.

    Usage::

        with DuckDBKanbanReader() as reader:
            task = reader.get_task("t_abc123")
            tasks = reader.list_tasks(status="ready")

    Or without the context manager (the connection is closed on GC)::

        reader = DuckDBKanbanReader.open()
        tasks = reader.list_tasks()
        reader.close()
    """

    _path: Path
    _conn: Any  # duckdb.DuckDBPyConnection

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def open(cls, path: Optional[Path] = None) -> "DuckDBKanbanReader":
        """Open a read-only connection to the DuckDB kanban file.

        Args:
            path: Explicit path to the ``.duckdb`` file.  When omitted the
                  path is resolved from env vars via :func:`resolve_duckdb_path`.

        Raises:
            FileNotFoundError: If the resolved path does not exist.
            RuntimeError: If the ``duckdb`` package is not installed.
        """
        if not _DUCKDB_AVAILABLE:
            raise RuntimeError(
                "The 'duckdb' package is required for DuckDBKanbanReader. "
                "Install it with: pip install duckdb"
            )
        resolved = path or resolve_duckdb_path()
        if not resolved.exists():
            raise FileNotFoundError(
                f"DuckDB kanban file not found: {resolved}. "
                "Set HERMES_KANBAN_DUCKDB_PATH to the correct path, or "
                "use HERMES_KANBAN_BACKEND=sqlite to fall back to SQLite."
            )
        conn = _duckdb.connect(str(resolved), read_only=True)
        return cls(_path=resolved, _conn=conn)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "DuckDBKanbanReader":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying DuckDB connection."""
        try:
            self._conn.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _columns(self, table: str = "tasks") -> list[str]:
        """Return the column names for a table (cached per connection)."""
        rows = self._conn.execute(f"DESCRIBE {table}").fetchall()
        return [r[0] for r in rows]

    # ------------------------------------------------------------------
    # Public read interface (mirrors kanban_db read functions)
    # ------------------------------------------------------------------

    def get_task(self, task_id: str) -> Optional[Task]:
        """Return the :class:`~hermes_cli.kanban_db.Task` for *task_id*, or None.

        Mirrors :func:`hermes_cli.kanban_db.get_task`.
        """
        cols = self._columns()
        rows = self._conn.execute(
            "SELECT * FROM tasks WHERE id = ?", [task_id]
        ).fetchall()
        if not rows:
            return None
        return _task_from_duckdb_row(rows[0], cols)

    def list_tasks(
        self,
        *,
        assignee: Optional[str] = None,
        status: Optional[str] = None,
        tenant: Optional[str] = None,
        session_id: Optional[str] = None,
        include_archived: bool = False,
        limit: Optional[int] = None,
        order_by: Optional[str] = None,
        workflow_template_id: Optional[str] = None,
        current_step_key: Optional[str] = None,
    ) -> list[Task]:
        """Return tasks matching the given filters.

        Mirrors :func:`hermes_cli.kanban_db.list_tasks` — same parameter names
        and semantics, same ``Task`` return type.
        """
        query = "SELECT * FROM tasks WHERE 1=1"
        params: list[Any] = []

        if assignee is not None:
            query += " AND assignee = ?"
            params.append(assignee)
        if status is not None:
            if status not in VALID_STATUSES:
                raise ValueError(f"status must be one of {sorted(VALID_STATUSES)}")
            query += " AND status = ?"
            params.append(status)
        if tenant is not None:
            query += " AND tenant = ?"
            params.append(tenant)
        if session_id is not None:
            query += " AND session_id = ?"
            params.append(session_id)
        if workflow_template_id is not None:
            query += " AND workflow_template_id = ?"
            params.append(workflow_template_id)
        if current_step_key is not None:
            query += " AND current_step_key = ?"
            params.append(current_step_key)
        if not include_archived and status != "archived":
            query += " AND status != 'archived'"
        if order_by is not None:
            order_by = order_by.strip().lower()
            if order_by not in VALID_SORT_ORDERS:
                raise ValueError(
                    f"order_by must be one of {sorted(VALID_SORT_ORDERS.keys())}"
                )
            query += f" ORDER BY {VALID_SORT_ORDERS[order_by]}"
        else:
            query += " ORDER BY priority DESC, created_at ASC"
        if limit is not None:
            query += f" LIMIT {int(limit)}"

        cols = self._columns()
        rows = self._conn.execute(query, params).fetchall()
        return [_task_from_duckdb_row(r, cols) for r in rows]


# ---------------------------------------------------------------------------
# Backend-aware convenience layer
# ---------------------------------------------------------------------------

def get_task_with_backend(
    sqlite_conn: Any,
    task_id: str,
    *,
    duckdb_path: Optional[Path] = None,
) -> Optional[Task]:
    """Return a Task using whichever backend is active.

    When :func:`should_use_duckdb` returns True, opens a read-only
    DuckDB connection, fetches the task, then closes it.  Otherwise
    delegates to :func:`hermes_cli.kanban_db.get_task` using *sqlite_conn*.

    Args:
        sqlite_conn: An open ``sqlite3.Connection`` — used when the SQLite
                     backend is active.  Ignored (but must not be None) when
                     the DuckDB backend is selected.
        task_id:     The task id to look up.
        duckdb_path: Optional explicit path to the DuckDB file; resolved via
                     :func:`resolve_duckdb_path` when omitted.

    Returns:
        A :class:`~hermes_cli.kanban_db.Task` instance, or ``None`` when the
        task is not found.
    """
    from hermes_cli import kanban_db as _kb  # local import avoids circular

    if should_use_duckdb():
        with DuckDBKanbanReader.open(path=duckdb_path) as reader:
            return reader.get_task(task_id)
    return _kb.get_task(sqlite_conn, task_id)


def list_tasks_with_backend(
    sqlite_conn: Any,
    *,
    duckdb_path: Optional[Path] = None,
    assignee: Optional[str] = None,
    status: Optional[str] = None,
    tenant: Optional[str] = None,
    session_id: Optional[str] = None,
    include_archived: bool = False,
    limit: Optional[int] = None,
    order_by: Optional[str] = None,
    workflow_template_id: Optional[str] = None,
    current_step_key: Optional[str] = None,
) -> list[Task]:
    """Return tasks using whichever backend is active.

    Same semantics as :func:`get_task_with_backend` — DuckDB when configured,
    SQLite otherwise.  All keyword arguments are forwarded to the active
    backend's ``list_tasks`` implementation.
    """
    from hermes_cli import kanban_db as _kb  # local import avoids circular

    if should_use_duckdb():
        with DuckDBKanbanReader.open(path=duckdb_path) as reader:
            return reader.list_tasks(
                assignee=assignee,
                status=status,
                tenant=tenant,
                session_id=session_id,
                include_archived=include_archived,
                limit=limit,
                order_by=order_by,
                workflow_template_id=workflow_template_id,
                current_step_key=current_step_key,
            )
    return _kb.list_tasks(
        sqlite_conn,
        assignee=assignee,
        status=status,
        tenant=tenant,
        session_id=session_id,
        include_archived=include_archived,
        limit=limit,
        order_by=order_by,
        workflow_template_id=workflow_template_id,
        current_step_key=current_step_key,
    )
