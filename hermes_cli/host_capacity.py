"""Worker-side host-capacity admission control.

Before a kanban worker begins executing its claimed task it calls
``check_and_defer_if_saturated()``.  If the local host is saturated the
function releases the claim back to ``ready`` (so another tick can try
on a healthier host or later when the host recovers) and returns ``True``
(meaning "deferred — caller should exit now").  It returns ``False`` when
the host looks healthy and the worker should proceed normally.

Gate is always **fail-open**: if the PSI files cannot be read, if nproc
is not available, or if any error occurs during the probe, the function
returns ``False`` and the worker proceeds.  Availability over protection.

Thresholds (all configurable via ``kanban.host_capacity`` in config.yaml):

  cpu_full_avg10_pct   — max acceptable cpu.full avg10 value (0–100).
                         Default: 5.0
  mem_full_avg10_pct   — max acceptable memory.full avg10 value (0–100).
                         Default: 20.0
  load_per_core        — load average ratio vs nproc at which cpu PSI
                         pressure is *also* required before deferring.
                         Default: 2.0 (i.e. load >= 2 × nproc triggers
                         combined with cpu.full avg10 breach).

Rejection logic (matching the task spec):
  - Defer if ``cpu.full avg10 > cpu_full_avg10_pct``
    AND ``load_1min >= load_per_core × nproc``  (both must be true).
  - Defer if ``memory.full avg10 > mem_full_avg10_pct``  (stand-alone).

This follows the worker-side claim veto design: the kernel is not
modified; the admission probe lives entirely in the worker process.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PSI / load probes
# ---------------------------------------------------------------------------

def _read_psi_avg10(kind: str) -> Optional[float]:
    """Return the ``full avg10`` value from ``/proc/pressure/<kind>``.

    Returns ``None`` on any read or parse error (fail-open caller).
    ``kind`` is ``"cpu"`` or ``"memory"``.
    """
    try:
        path = f"/proc/pressure/{kind}"
        with open(path) as fh:
            for line in fh:
                # Format: "full avg10=N.NN avg60=... avg300=... total=..."
                if line.startswith("full "):
                    for token in line.split():
                        if token.startswith("avg10="):
                            return float(token.split("=", 1)[1])
        return None
    except Exception:
        return None


def _read_load1() -> Optional[float]:
    """Return the 1-minute load average from ``/proc/loadavg``."""
    try:
        with open("/proc/loadavg") as fh:
            return float(fh.read().split()[0])
    except Exception:
        return None


def _nproc() -> Optional[int]:
    """Return the number of CPUs available to the process."""
    try:
        # os.cpu_count() may return None; prefer sched_getaffinity on Linux
        return len(os.sched_getaffinity(0))
    except AttributeError:
        pass
    try:
        n = os.cpu_count()
        return n if n else None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Sentinel meaning "not yet read from config"
_UNSET = object()


def _load_thresholds() -> dict:
    """Read thresholds from ``kanban.host_capacity`` in config.yaml.

    Falls back to defaults if the section or individual keys are absent.
    Always returns a complete dict (fail-open for misconfigured values).
    """
    defaults = {
        "cpu_full_avg10_pct": 5.0,
        "mem_full_avg10_pct": 20.0,
        "load_per_core": 2.0,
    }
    try:
        from hermes_cli.config import load_config_readonly
        cfg = load_config_readonly()
        sec = (cfg or {}).get("kanban", {}).get("host_capacity", {})
        if not isinstance(sec, dict):
            return defaults

        def _float(key: str, fallback: float) -> float:
            v = sec.get(key, fallback)
            try:
                f = float(v)
                return f if f >= 0 else fallback
            except (TypeError, ValueError):
                return fallback

        return {
            "cpu_full_avg10_pct": _float("cpu_full_avg10_pct", defaults["cpu_full_avg10_pct"]),
            "mem_full_avg10_pct": _float("mem_full_avg10_pct", defaults["mem_full_avg10_pct"]),
            "load_per_core": _float("load_per_core", defaults["load_per_core"]),
        }
    except Exception:
        return defaults


# ---------------------------------------------------------------------------
# Admission check
# ---------------------------------------------------------------------------

def probe_host_saturated() -> tuple[bool, str]:
    """Return ``(saturated: bool, reason: str)``.

    Fail-open: returns ``(False, "")`` on any probe error.

    ``reason`` is a human-readable description suitable for a task event
    payload; it is empty when the host is not saturated.
    """
    try:
        thresholds = _load_thresholds()
        cpu_limit = thresholds["cpu_full_avg10_pct"]
        mem_limit = thresholds["mem_full_avg10_pct"]
        load_ratio = thresholds["load_per_core"]

        # Memory pressure: stand-alone gate (no load condition required).
        mem_full = _read_psi_avg10("memory")
        if mem_full is not None and mem_full > mem_limit:
            return True, (
                f"memory.full avg10={mem_full:.2f}% > threshold {mem_limit:.2f}%"
            )

        # CPU pressure: only triggers when load is also oversubscribed.
        cpu_full = _read_psi_avg10("cpu")
        if cpu_full is not None and cpu_full > cpu_limit:
            load1 = _read_load1()
            nproc = _nproc()
            if load1 is not None and nproc is not None and nproc > 0:
                if load1 >= load_ratio * nproc:
                    return True, (
                        f"cpu.full avg10={cpu_full:.2f}% > threshold {cpu_limit:.2f}%"
                        f" with load={load1:.2f} >= {load_ratio}×nproc({nproc})"
                        f"={load_ratio * nproc:.1f}"
                    )

        return False, ""
    except Exception as exc:
        _log.debug("host_capacity probe failed (fail-open): %s", exc)
        return False, ""


def _fire_plugin_veto(
    task_id: str,
    run_id: Optional[int],
) -> tuple[bool, str]:
    """Fire the ``kanban_worker_pre_execute`` plugin hook.

    Returns ``(vetoed: bool, reason: str)``.  Fail-open: returns
    ``(False, "")`` if the hook machinery is unavailable or every
    callback abstains.
    """
    try:
        from hermes_cli.plugins import get_plugin_manager
        from hermes_cli import kanban_db as kb
        mgr = get_plugin_manager()
        if not mgr.has_hook("kanban_worker_pre_execute"):
            return False, ""

        board = None
        assignee = None
        try:
            board = kb.get_current_board()
        except Exception:
            pass
        try:
            conn = kb.connect()
            try:
                t = kb.get_task(conn, task_id)
                if t:
                    assignee = t.assignee
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
        except Exception:
            pass

        profile_name = os.environ.get("HERMES_PROFILE", "")
        kwargs = dict(
            task_id=task_id,
            run_id=run_id,
            board=board,
            assignee=assignee,
            profile_name=profile_name,
        )
        results = mgr.invoke_hook("kanban_worker_pre_execute", **kwargs)

        veto_reasons = []
        veto_sources = []
        for r in (results or []):
            if isinstance(r, dict) and r.get("veto") is True:
                reason = r.get("reason") or "no reason given"
                source = r.get("source") or ""
                veto_reasons.append(reason)
                if source:
                    veto_sources.append(source)

        if veto_reasons:
            combined_reason = "; ".join(veto_reasons)
            combined_source = ",".join(veto_sources) if veto_sources else ""
            full = combined_reason
            if combined_source:
                full = f"{combined_reason} (source: {combined_source})"
            return True, full

        return False, ""
    except Exception as exc:
        _log.debug("kanban_worker_pre_execute hook failed (fail-open): %s", exc)
        return False, ""


def check_and_defer_if_saturated(
    task_id: str,
    *,
    run_id: Optional[int] = None,
) -> bool:
    """Probe the host and, if saturated, release the claim and return True.

    Also fires the ``kanban_worker_pre_execute`` plugin hook — a plugin
    callback may also veto execution (e.g. a policy gate) by returning
    ``{"veto": True, "reason": "..."}``.

    Called at worker startup before the agent runs.  When this returns
    ``True`` the caller MUST exit without doing any task work so the
    dispatcher can re-queue the task on the next tick.

    Returns ``False`` when the host is healthy (or the probe failed /
    PSI unavailable) — the worker should proceed normally.

    Fail-open: any exception in the release path is caught and logged;
    we still return ``True`` so the worker exits cleanly rather than
    doing double work.
    """
    saturated, reason = probe_host_saturated()

    # Also consult plugin callbacks.
    plugin_vetoed, plugin_reason = _fire_plugin_veto(task_id, run_id)
    if plugin_vetoed and not saturated:
        saturated = True
        reason = plugin_reason

    if not saturated:
        return False

    _log.warning(
        "kanban worker host-capacity veto: task=%s reason=%r — releasing claim",
        task_id, reason,
    )
    try:
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
        try:
            with kb.write_txn(conn):
                # Release claim back to ready without incrementing failures.
                conn.execute(
                    """
                    UPDATE tasks
                       SET status = 'ready',
                           claim_lock = NULL,
                           claim_expires = NULL,
                           worker_pid = NULL
                     WHERE id = ? AND status = 'running'
                    """,
                    (task_id,),
                )
                # Audit event so operators can diagnose load-induced deferrals.
                kb._append_event(
                    conn,
                    task_id,
                    "host_capacity_deferred",
                    {
                        "reason": reason,
                        "run_id": run_id,
                        "source": "worker_host_admission",
                    },
                )
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as exc:
        _log.error(
            "kanban worker host-capacity: claim release failed for %s: %s",
            task_id, exc,
        )
    return True
