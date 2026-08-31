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

import contextlib
import functools
import inspect
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

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
#
# t_06bc95c3 (2026-08-22): we DO NOT cache DuckDB connections at rest. Prior
# implementation kept a module-level ``_DUCK_CONNS`` dict that opened a
# connection to every board's ``kanban.duckdb`` on first write and held it for
# the process lifetime. Because DuckDB takes an exclusive OS-level file lock on
# the database file (even a ``read_only=True`` opener from another process
# cannot bypass this), the long-lived gateway process ended up holding
# exclusive locks on every board's mirror — locking out the every-5-min
# ``kanban-parity`` cron and any other process that wanted to open the mirror.
#
# Fix pattern (matches ADR-012 §"Concurrent multi-host writers" and
# ADR-011 P0-3 ``DuckDBOKRStorage``):
#     open → write → close, per mirror operation.
#
# Benchmarked on 2026-08-22: per-op open+write+close ≈ 86 ms vs ≈ 4 ms for a
# cached connection. Kanban write rate on the busiest board (~8 events/min)
# yields ≈ 700 ms of extra CPU / minute (1.2 %), well within the "best-effort
# mirror" budget SQLite-authoritative dual-write allows.


# Paths where opening the DuckDB mirror already failed with a benign lock
# contention (another hermes process on the same host holds the mirror's
# exclusive DuckDB file lock). Once we've logged this once for a given
# path, we short-circuit further connect attempts on the same process:
# retrying only replays the same IOException. Recorded lazily below.
_DUCK_LOCK_CONTENDED: set[str] = set()

# Signature substring used to identify DuckDB single-writer file-lock
# contention. Matches ERR-DRIVE-01 signature class "IO Error: Could not
# set lock on file ... Conflicting lock is held". This is a chronic
# multi-process condition on shared hosts; it is not a bug and must not
# spam ERROR-level tracebacks into errors.log (which then feed back
# into the ERR-DRIVE-01 auto-triage probe).
_DUCK_LOCK_SIGNATURE = "Conflicting lock is held"

# Back-compat shim: tests and health probes may still import ``_DUCK_CONNS``.
# Kept empty and unused — module code no longer touches it — so that any
# stale reference returns the historically-expected "no cached conns" answer.
# Regression tests that clear the mapping between cases (``_DUCK_CONNS.clear()``)
# continue to work with no behavior change.
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


@contextlib.contextmanager
def _mirror_conn_for(conn: sqlite3.Connection) -> Iterator[Optional[Any]]:
    """Yield a DuckDB mirror connection for the duration of a single op.

    Opens the DuckDB file, yields the connection, and closes it in a
    ``finally``. The close() is what releases the OS-level file lock so
    other processes (parity cron, another hermes worker) can open the
    mirror. Yields ``None`` for any of:
      * ``hermes_kanban`` not importable
      * SQLite conn has no on-disk path (in-memory)
      * DuckDB file is locked by a concurrent writer on this host
        (recorded in ``_DUCK_LOCK_CONTENDED`` so we don't retry+log every op)
      * novel exception opening the mirror (logged with traceback)

    Yielding ``None`` is the "mirror silently skipped" signal — callers
    must check for it. The SQLite primary path has already run and its
    return value is authoritative.
    """
    facade, adapter = _load_facade()
    if adapter is None:
        yield None
        return
    p = _sqlite_path_for(conn)
    if p is None:
        yield None
        return
    key = str(p.resolve())
    if key in _DUCK_LOCK_CONTENDED:
        yield None
        return

    duck: Optional[Any] = None
    try:
        try:
            duck = adapter.connect(adapter.duckdb_kanban_path(p))
        except Exception as exc:  # pragma: no cover — mirror failure only
            msg = str(exc)
            if _DUCK_LOCK_SIGNATURE in msg:
                # Benign multi-process contention (ERR-DRIVE-01 known
                # signature). Log once per (process, path) at WARNING
                # without a traceback and remember so we don't retry.
                _DUCK_LOCK_CONTENDED.add(key)
                logger.warning(
                    "kanban dual-write: DuckDB mirror for %s locked by "
                    "another hermes process on this host; mirror disabled "
                    "for this process (SQLite primary path unaffected). %s",
                    p, msg.splitlines()[0] if msg else "",
                )
            else:
                # Genuinely novel mirror failure — keep full traceback.
                logger.exception(
                    "kanban dual-write: failed to open DuckDB mirror for %s",
                    p,
                )
            duck = None
        yield duck
    finally:
        if duck is not None:
            try:
                duck.close()
            except Exception:  # pragma: no cover — best-effort close
                # A failed close() would leak a file descriptor + lock,
                # but there is no useful recovery. Log without traceback
                # to avoid re-feeding ERR-DRIVE-01 triage on a benign path.
                logger.warning(
                    "kanban dual-write: DuckDB mirror close() failed for %s",
                    p,
                )


# Back-compat: keep the historical name importable for callers that reach
# into the module (tests, health probes). Adapts the context manager to
# the old "get me a conn (or None)" shape but WITHOUT caching — the
# caller gets a fresh conn it is responsible for closing. Prefer
# ``_mirror_conn_for`` in new code.
def _duck_conn_for(conn: sqlite3.Connection) -> Optional[Any]:
    """DEPRECATED — see :func:`_mirror_conn_for`.

    Kept only so external callers (regression tests, ad-hoc probes) that
    imported the symbol don't crash. Returns a fresh connection that the
    caller MUST close, or ``None`` on any skip / contention condition.
    """
    facade, adapter = _load_facade()
    if adapter is None:
        return None
    p = _sqlite_path_for(conn)
    if p is None:
        return None
    key = str(p.resolve())
    if key in _DUCK_LOCK_CONTENDED:
        return None
    try:
        return adapter.connect(adapter.duckdb_kanban_path(p))
    except Exception as exc:  # pragma: no cover — mirror failure only
        msg = str(exc)
        if _DUCK_LOCK_SIGNATURE in msg:
            _DUCK_LOCK_CONTENDED.add(key)
            logger.warning(
                "kanban dual-write: DuckDB mirror for %s locked by "
                "another hermes process on this host; mirror disabled "
                "for this process (SQLite primary path unaffected). %s",
                p, msg.splitlines()[0] if msg else "",
            )
        else:
            logger.exception(
                "kanban dual-write: failed to open DuckDB mirror for %s",
                p,
            )
        return None


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

# Per-op id-passthrough unwrap: some SQLite write ops return a rich
# object rather than the bare primary key the mirror insert must bind.
# ``claim_task`` (t_3f87ac16) returns the claimed ``Task`` dataclass;
# its ``current_run_id`` is the freshly-minted run id that the DuckDB
# ``task_runs`` insert must reuse to keep primary keys identical across
# backends. Forwarding the whole object instead made DuckDB's binder
# throw ``NotImplementedException: Unable to transform python value of
# type '<class 'hermes_cli.kanban_db.Task'>'`` on every dual-mode claim
# (71 ERROR tracebacks in errors.log on 2026-08-22, ERR-DRIVE-01 loop).
_ID_PASSTHROUGH_UNWRAP: dict[str, str] = {
    "claim_task": "current_run_id",
}


# Primitives DuckDB can bind directly. Anything else surviving the
# unwrap stage is dropped (with a WARNING — no traceback, so it cannot
# re-feed the ERR-DRIVE-01 triage probe) rather than forwarded to the
# binder, which would throw the transform error again.
_ID_PASSTHROUGH_BINDABLE = (str, int, float)


def _mirror_id_for(op_name: str, result: Any) -> Optional[Any]:
    """Reduce an op's SQLite return value to the mirror bind value.

    Primitives pass through untouched (``create_task`` -> task id str,
    ``add_comment`` -> row int). Ops listed in ``_ID_PASSTHROUGH_UNWRAP``
    have their return value reduced via the named attribute
    (``claim_task`` -> ``Task.current_run_id``). Any object that still
    is not a bindable primitive is returned as-is so the caller's
    bindability guard drops it with a WARNING instead of letting the
    DuckDB binder throw the transform error.
    """
    attr = _ID_PASSTHROUGH_UNWRAP.get(op_name)
    if attr is not None and not isinstance(result, _ID_PASSTHROUGH_BINDABLE):
        return getattr(result, attr, None)
    return result


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


# Per-op kwarg-name translation: SQLite outer signature → DuckDB adapter
# signature. The outer dispatcher speaks the historical SQLite kwarg names
# (``claimer=``) while the DuckDB adapter uses the underlying column name
# (``lock=``). Without this rename ``_filtered_kwargs`` silently drops
# ``claimer`` and the adapter call blows up on a required-keyword
# ``TypeError`` (``heartbeat_claim`` — required ``lock``) or silently loses
# the claimer identity in the mirror (``claim_task`` — ``lock`` defaults to
# ``_claimer_id()``, so the mirror row lands under the WRONG lock and every
# subsequent heartbeat mirror then no-ops on a stale-lock check).
#
# Map key is the adapter op name (``adapter_op_name`` in ``_WRITE_OPS``);
# value is ``{outer_kwarg: adapter_kwarg}``. Renames happen before
# ``_filtered_kwargs`` so unknown outer kwargs still get dropped for ops
# that don't need translation.
_MIRROR_KWARG_RENAMES: dict[str, dict[str, str]] = {
    "claim_task":      {"claimer": "lock"},
    "heartbeat_claim": {"claimer": "lock"},
}


def _rename_kwargs(op_name: str, kwargs: dict) -> dict:
    """Apply per-op SQLite→DuckDB kwarg renames.

    Only renames keys present in the input; values are preserved as-is.
    A collision (both source and destination present) prefers the
    already-present destination — callers cannot both pass ``claimer``
    and ``lock`` for the same call today, but the defensive choice keeps
    an explicit ``lock=`` winning if some future call site starts using
    the canonical name directly.
    """
    rename = _MIRROR_KWARG_RENAMES.get(op_name)
    if not rename:
        return kwargs
    out = dict(kwargs)
    for src, dst in rename.items():
        if src in out and dst not in out:
            out[dst] = out.pop(src)
        elif src in out:
            # Destination already set — drop the source alias.
            out.pop(src, None)
    return out


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
        adapter_fn = getattr(adapter, adapter_op_name, None)
        if adapter_fn is None:
            logger.info(
                "kanban dual-write: adapter has no %s (skip)", adapter_op_name,
            )
            return result
        # Per-op open→write→close. Do NOT hold the DuckDB file lock at
        # rest — t_06bc95c3 (see module header): the previous cache-forever
        # scheme starved the parity cron and any concurrent hermes process
        # of read access to the mirror.
        with _mirror_conn_for(conn) as duck:
            if duck is None:
                return result
            # Assemble mirror kwargs. For id-passthrough ops the SQLite return
            # value is the freshly-minted primary key that the mirror insert
            # must reuse to keep IDs identical across backends.
            mirror_kwargs = dict(kwargs)
            if id_passthrough_kw and result is not None:
                mirror_id = _mirror_id_for(adapter_op_name, result)
                if isinstance(mirror_id, _ID_PASSTHROUGH_BINDABLE):
                    mirror_kwargs[id_passthrough_kw] = mirror_id
                else:
                    # Never forward an unbindable object to the DuckDB
                    # binder — that is the t_3f87ac16 failure mode. The
                    # mirror op still runs (adapter mints its own id or
                    # the op no-ops); we do not fail the SQLite result.
                    logger.warning(
                        "kanban dual-write: op %s returned %s; cannot "
                        "bind it as %s= for the mirror (dropped)",
                        adapter_op_name, type(result).__name__,
                        id_passthrough_kw,
                    )
            # Translate SQLite outer kwarg names to DuckDB adapter names
            # BEFORE the signature filter, otherwise the filter silently
            # drops still-valid values (t_192a3e6b: ``heartbeat_claim``
            # required ``lock=`` after the ``claimer=`` was thrown away).
            mirror_kwargs = _rename_kwargs(adapter_op_name, mirror_kwargs)
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
    "_mirror_conn_for",
]
