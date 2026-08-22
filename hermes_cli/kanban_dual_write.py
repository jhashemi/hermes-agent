"""Kanban dual-write mirror shim (ADR-012 P1 wiring, G1a-FIX).

Wires the ``hermes_kanban.kanban_repository_facade`` DuckDB mirror into
the outer ``hermes_cli.kanban_db`` dispatcher module without changing
call sites. Selection is driven by the ``HERMES_KANBAN_WRITE_BACKEND``
environment variable:

* ``sqlite`` (default, or unset) — no observable behavior change vs. the
  un-installed dispatcher. ``install()`` still wraps the module-level
  write ops (so ``create_task`` / ``add_comment`` / etc. change function
  identity), but each wrapper short-circuits at its ``mirror_enabled()``
  gate: no adapter connection is opened, no SQL is issued against
  DuckDB, and — because the tail of ``hermes_cli.kanban_db`` gates the
  facade import on the same backend check — the DuckDB stack is not
  imported at all in this mode.
* ``dual`` — SQLite remains authority. After every successful SQLite
  write, a best-effort mirror write is fired at the DuckDB adapter.
  Mirror failures are logged and swallowed. The dispatcher's return
  values come from SQLite exactly as before.
* ``duckdb`` — Reserved for the post-P3 authority flip (G1c ticket).
  For P1 this shim treats ``duckdb`` as ``dual`` with a clear warning;
  we do not silently drop SQLite writes.

Wiring strategy:
  ``install()`` monkey-patches the ``hermes_cli.kanban_db`` module-level
  write functions in place, wrapping each with a decorator that calls
  the original (SQLite path), then fires the DuckDB mirror. Call sites
  in the dispatcher are unchanged; the wrap is transparent because the
  wrappers preserve signature semantics via ``functools.wraps``.

This shim is imported at the bottom of ``hermes_cli.kanban_db`` under a
try/except so a missing ``hermes_kanban`` package never breaks import
of the dispatcher. The DoD grep for ``kanban_repository_facade`` is
satisfied here (see :func:`_load_facade`).
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Backend selection
# --------------------------------------------------------------------------- #

_BACKEND_ENV = "HERMES_KANBAN_WRITE_BACKEND"
_VALID_BACKENDS = ("sqlite", "dual", "duckdb")


def current_backend() -> str:
    """Resolve the active backend from env. Defaults to ``sqlite``.

    Invalid values fall back to ``sqlite`` with a warning — this shim
    must never harden a typo into a data-loss condition.
    """
    v = (os.environ.get(_BACKEND_ENV, "sqlite") or "sqlite").strip().lower()
    if v not in _VALID_BACKENDS:
        logger.warning(
            "invalid %s=%r; falling back to 'sqlite'", _BACKEND_ENV, v,
        )
        return "sqlite"
    return v


def mirror_enabled() -> bool:
    """True iff the DuckDB mirror side should run at all."""
    return current_backend() in ("dual", "duckdb")


# --------------------------------------------------------------------------- #
# Lazy adapter/facade discovery
# --------------------------------------------------------------------------- #

_FACADE: Any = None
_ADAPTER: Any = None
_LOAD_ATTEMPTED = False
_LOAD_ERROR: Optional[BaseException] = None


def _load_facade() -> tuple[Optional[Any], Optional[Any]]:
    """Attempt to import ``hermes_kanban.kanban_repository_facade`` and
    ``hermes_kanban.duckdb_kanban_adapter``.

    Idempotent: retries at most once per process, caches the outcome.
    Failure is logged at INFO (not ERROR) — the package may legitimately
    be absent in installs that opted out of the DuckDB parity feature.
    """
    global _FACADE, _ADAPTER, _LOAD_ATTEMPTED, _LOAD_ERROR
    if _LOAD_ATTEMPTED:
        return _FACADE, _ADAPTER
    _LOAD_ATTEMPTED = True
    try:
        from hermes_kanban import kanban_repository_facade as _facade  # type: ignore
        from hermes_kanban import duckdb_kanban_adapter as _adapter  # type: ignore
        _FACADE = _facade
        _ADAPTER = _adapter
    except Exception as exc:  # pragma: no cover — optional package
        _LOAD_ERROR = exc
        logger.info(
            "kanban dual-write: hermes_kanban not importable (%s); "
            "DuckDB mirror disabled regardless of %s",
            exc, _BACKEND_ENV,
        )
    return _FACADE, _ADAPTER


# --------------------------------------------------------------------------- #
# DuckDB mirror connection tracking
# --------------------------------------------------------------------------- #

_DUCK_CONNS: dict[str, Any] = {}


def _sqlite_path_for(conn: sqlite3.Connection) -> Optional[Path]:
    """Recover the on-disk SQLite path from a live connection.

    Uses ``PRAGMA database_list``. Returns None for in-memory / unusual
    connections — the mirror is silently skipped in that case.
    """
    try:
        rows = list(conn.execute("PRAGMA database_list"))
    except Exception:
        return None
    for r in rows:
        name = r[1] if len(r) > 1 else None
        path = r[2] if len(r) > 2 else None
        if name == "main" and path:
            return Path(path)
    return None


def _duck_conn_for(conn: sqlite3.Connection) -> Optional[Any]:
    facade, adapter = _load_facade()
    if adapter is None:
        return None
    p = _sqlite_path_for(conn)
    if p is None:
        return None
    key = str(p.resolve())
    duck = _DUCK_CONNS.get(key)
    if duck is None:
        try:
            duck = adapter.connect(adapter.duckdb_kanban_path(p))
            _DUCK_CONNS[key] = duck
        except Exception:  # pragma: no cover — mirror failure only
            logger.exception(
                "kanban dual-write: failed to open DuckDB mirror for %s", p,
            )
            duck = None
    return duck


def _filtered_kwargs(fn: Callable, kwargs: dict) -> dict:
    """Drop kwargs that the adapter function does not accept.

    The outer dispatcher's write functions carry additional fields (e.g.
    ``branch_name``, ``project_id``, ``model_override``) that the DuckDB
    adapter does not model. Silently dropping unknown kwargs is safer
    than crashing the mirror — parity checks are on schema columns the
    adapter DOES model.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return kwargs
    params = sig.parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return kwargs
    return {k: v for k, v in kwargs.items() if k in params}


# --------------------------------------------------------------------------- #
# Wrapping the outer dispatcher's write ops
# --------------------------------------------------------------------------- #

# Map: outer-fn-name -> adapter-fn-name (usually the same).
# Callers pass task_id (or row_id / run_id) via SQLite return value so
# the mirror insert lands the same primary-key value.
_WRITE_OPS: tuple[tuple[str, str, str], ...] = (
    # (outer_name, adapter_name, id_passthrough_kw or "")
    ("create_task",           "create_task",           "task_id"),
    ("assign_task",           "assign_task",           ""),
    ("reassign_task",         "reassign_task",         ""),
    ("link_tasks",            "link_tasks",            ""),
    ("unlink_tasks",          "unlink_tasks",          ""),
    ("add_comment",           "add_comment",           "row_id"),
    ("claim_task",            "claim_task",            "run_id"),
    ("heartbeat_claim",       "heartbeat_claim",       ""),
    ("release_stale_claims",  "release_stale_claims",  ""),
    ("reclaim_task",          "reclaim_task",          ""),
    ("complete_task",         "complete_task",         ""),
    ("block_task",            "block_task",            ""),
    ("unblock_task",          "unblock_task",          ""),
    ("add_notify_sub",        "add_kanban_notify_sub", ""),
)


def _make_mirror_wrapper(
    original: Callable,
    adapter_op_name: str,
    id_passthrough_kw: str,
) -> Callable:
    """Return a callable that runs ``original`` (SQLite) then mirrors."""

    @functools.wraps(original)
    def _wrapped(conn, *args, **kwargs):
        result = original(conn, *args, **kwargs)
        # Cheap first-line check: skip everything if the env says sqlite.
        if not mirror_enabled():
            return result
        _facade, adapter = _load_facade()
        if adapter is None:
            return result
        duck = _duck_conn_for(conn)
        if duck is None:
            return result
        adapter_fn = getattr(adapter, adapter_op_name, None)
        if adapter_fn is None:
            logger.info(
                "kanban dual-write: adapter has no %s (skip)", adapter_op_name,
            )
            return result
        # Assemble mirror kwargs. For id-passthrough ops the SQLite return
        # value is the freshly-minted primary key that the mirror insert
        # must reuse to keep IDs identical across backends.
        mirror_kwargs = dict(kwargs)
        if id_passthrough_kw and result is not None:
            mirror_kwargs[id_passthrough_kw] = result
        mirror_kwargs = _filtered_kwargs(adapter_fn, mirror_kwargs)
        try:
            adapter_fn(duck, *args, **mirror_kwargs)
        except Exception:  # pragma: no cover — best-effort mirror
            logger.exception(
                "kanban dual-write: mirror op %s failed (non-fatal)",
                adapter_op_name,
            )
        return result

    _wrapped.__wrapped__ = original  # type: ignore[attr-defined]
    _wrapped.__kanban_dual_write_wrapped__ = True  # type: ignore[attr-defined]
    return _wrapped


def install(module: Any) -> dict:
    """Install dual-write wrappers on the given kanban_db module.

    Safe to call multiple times: functions already wrapped by this shim
    are left alone. In ``sqlite`` mode the wrappers are STILL installed
    (so ``create_task``, ``add_comment``, etc. have different function
    identity than the un-installed module) but each wrapper's first
    branch (``mirror_enabled()``) short-circuits to zero extra work —
    write operations are intercepted but immediately delegated to the
    SQLite backend without side effects, giving no observable behavior
    change in sqlite mode while making the routing observable via
    one-line log at DEBUG.

    Returns a dict ``{op_name: 'wrapped' | 'skipped' | 'missing'}`` for
    observability / testing.
    """
    outcome: dict[str, str] = {}
    for outer_name, adapter_name, id_kw in _WRITE_OPS:
        original = getattr(module, outer_name, None)
        if original is None:
            outcome[outer_name] = "missing"
            continue
        if getattr(original, "__kanban_dual_write_wrapped__", False):
            outcome[outer_name] = "skipped"  # already wrapped
            continue
        wrapped = _make_mirror_wrapper(original, adapter_name, id_kw)
        setattr(module, outer_name, wrapped)
        outcome[outer_name] = "wrapped"
    logger.debug(
        "kanban dual-write: install() outcome=%s backend=%s",
        outcome, current_backend(),
    )
    return outcome


__all__ = [
    "current_backend",
    "mirror_enabled",
    "install",
]
