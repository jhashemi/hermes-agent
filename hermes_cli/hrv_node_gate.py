"""HRV-driven node evaluation gate for kanban dispatcher.

Extends the dispatch-once node gate with 5 nervous-system probe conditions:
1. memory_pressure (swap_pct >= 90% OR OOM kill in last 5min)
2. kanban_dispatcher_health (systemd activating OR crashed in last 10min)
3. bedrock_rate_limit_saturation for the task's pinned model
4. hrv.status.digest interval_class == 'urgent' AND task priority < P0
5. min_resources overflow: node.available < task.min_resources

Data source: NATS KV bucket hrv_node_state (populated by hrv-autoheal probes)
and hrv.status.digest (cached from NATS).

Cache: latest-per-node with 60s staleness TTL.
Fail-open: stale/missing signals do NOT cause rejection (avoid total halt).

Usage:
    gate = HRVNodeGate()
    rejected_reason = gate.evaluate_node(task_id, node_hostname, model_name)
    if rejected_reason:
        logger.info(f"Node rejected: {rejected_reason}")
        metrics_emit("dispatch.node_rejected", tags={"reason": rejected_reason})
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Bedrock TPM saturation threshold used by condition-3 (bedrock_rate_limit_saturation).
# Also used to clamp condition-5 (min_resources_overflow) so that a node with TPM
# between this threshold and the task's declared reservation isn't rejected by
# condition-5 while being accepted by condition-3. See starvation-edge fix in
# t_d18420a6 / review comment on t_55fb6cb6.
BEDROCK_TPM_SATURATION_THRESHOLD = 1000

# OOM scan window: how far back to look for OOM kills. Condition 1b spec: last 5 minutes.
OOM_SCAN_WINDOW_SECONDS = 300

# Subprocess timeout for external probes (journalctl / systemctl / dmesg).
# Kept short so a hung journal never blocks dispatcher decisions — on timeout
# the probe reports UNKNOWN and the gate fails open.
_SUBPROC_TIMEOUT_SECONDS = 3.0

# Systemd unit name the gate checks for condition-2 (kanban_dispatcher_health).
# Overridable via env HRV_DISPATCHER_UNIT_NAME. When the unit isn't installed
# on the host, `systemctl is-active` returns "inactive" for unknown units on
# some systemd versions and "unknown" on others — we treat both as UNKNOWN →
# fail-open so a host without a dedicated dispatcher unit isn't universally
# rejected.
_DEFAULT_DISPATCHER_UNIT_NAME = "hermes-kanban-dispatcher.service"

# systemctl states that count as "dispatcher unhealthy" for condition-2.
# 'activating' = starting/reloading (transient); 'failed' = crashed;
# 'deactivating' = shutting down (also don't want new work).
_DISPATCHER_UNHEALTHY_STATES = frozenset({"activating", "failed", "deactivating"})

# systemctl states that mean "no answer" (host doesn't have the unit installed
# or systemctl is unavailable) — treat as UNKNOWN → fail-open.
_DISPATCHER_UNKNOWN_STATES = frozenset({"unknown", "inactive"})

# TTL for the node-independent probe cache (OOM-in-window, dispatcher-health).
# These probes read host-global state, so their result is the same for every
# (task, node) pair the gate evaluates in a dispatch tick. On a busy dispatcher
# with N nodes × M tasks the uncached path fans out N×M subprocesses per tick;
# TTL-caching drops that to O(1) per TTL window while preserving fail-open
# semantics (UNKNOWN results are NEVER cached — see _cached_bool_probe).
_HOST_GLOBAL_PROBE_TTL_SECONDS = 30.0


@dataclass
class NodeProbeSnapshot:
    """Latest probe snapshot for a single node (from NATS KV)."""
    hostname: str
    swap_pct: Optional[float] = None
    mem_gb_available: Optional[float] = None
    load_1m: Optional[float] = None
    load_5m: Optional[float] = None
    disk_free_gb: Optional[float] = None
    active_workers: Optional[int] = None
    max_workers: Optional[int] = None
    bedrock_tpm_remaining: Optional[int] = None
    ts: Optional[str] = None  # ISO8601 UTC
    
    def age_seconds(self, now: Optional[float] = None) -> Optional[float]:
        """Return age of snapshot in seconds, or None if ts is missing."""
        if not self.ts:
            return None
        if now is None:
            now = time.time()
        try:
            # Parse ISO8601 timestamp
            import datetime
            ts_str = self.ts
            # Handle Z suffix (UTC)
            if ts_str.endswith('Z'):
                ts_str = ts_str[:-1] + '+00:00'
            dt = datetime.datetime.fromisoformat(ts_str)
            # Ensure tz-aware datetime
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            ts_unix = dt.timestamp()
            return now - ts_unix
        except Exception:
            return None
    
    def is_stale(self, max_age_seconds: float = 60.0) -> bool:
        """True if snapshot is older than max_age_seconds."""
        age = self.age_seconds()
        return age is None or age > max_age_seconds


@dataclass
class HRVDigestSnapshot:
    """Cached HRV digest state (from hrv.status.digest NATS event)."""
    interval_class: Optional[str] = None  # calm/alert/anxious/urgent
    ts: Optional[str] = None
    
    def age_seconds(self, now: Optional[float] = None) -> Optional[float]:
        """Return age in seconds."""
        if not self.ts:
            return None
        if now is None:
            now = time.time()
        try:
            import datetime
            ts_str = self.ts
            if ts_str.endswith('Z'):
                ts_str = ts_str[:-1] + '+00:00'
            dt = datetime.datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            ts_unix = dt.timestamp()
            return now - ts_unix
        except Exception:
            return None
    
    def is_stale(self, max_age_seconds: float = 60.0) -> bool:
        """True if snapshot is older than max_age_seconds."""
        age = self.age_seconds()
        return age is None or age > max_age_seconds


class HRVNodeGate:
    """Multi-condition node evaluation gate using HRV probes."""
    
    def __init__(
        self,
        max_probe_age_seconds: float = 60.0,
        host_global_probe_ttl_seconds: float = _HOST_GLOBAL_PROBE_TTL_SECONDS,
    ):
        """
        Initialize the gate.
        
        Args:
            max_probe_age_seconds: reject on unknown/stale probes.
                When stale, treat as UNKNOWN → fail-open (do NOT reject).
            host_global_probe_ttl_seconds: TTL for cached results of
                node-independent probes (OOM-in-window, dispatcher-health).
                These probes read local host state, so the answer is the same
                for every (task, node) pair evaluated in the same window.
                Caching only stores DEFINITIVE True/False answers; UNKNOWN
                results (None) are never cached so a cache MISS or expired
                entry always re-probes rather than serving a stale UNKNOWN.
                Set to 0 to disable caching entirely.
        """
        self.max_probe_age_seconds = max_probe_age_seconds
        self.host_global_probe_ttl_seconds = host_global_probe_ttl_seconds

        # In-memory cache: hostname -> NodeProbeSnapshot
        self._node_cache: Dict[str, NodeProbeSnapshot] = {}
        self._cache_last_refresh: Optional[float] = None

        # HRV digest cache
        self._digest: Optional[HRVDigestSnapshot] = None

        # TTL cache slots for node-independent host-global probes.
        # Each entry is (result: bool, expires_at_monotonic: float) OR None.
        # We use time.monotonic() so wall-clock jumps (NTP, DST) don't confuse
        # expiry — the TTL is a real elapsed interval regardless of clock ops.
        self._oom_cache_entry: Optional[tuple[bool, float]] = None
        self._dispatcher_cache_entry: Optional[tuple[bool, float]] = None
    
    def set_node_probe_snapshot(self, hostname: str, snapshot: NodeProbeSnapshot) -> None:
        """Cache a node probe snapshot (called by NATS subscriber)."""
        self._node_cache[hostname] = snapshot
        self._cache_last_refresh = time.time()
    
    def set_hrv_digest(self, digest: HRVDigestSnapshot) -> None:
        """Cache HRV digest (called by NATS subscriber)."""
        self._digest = digest
    
    def load_node_probe_from_kv(self, kv_payload: Dict[str, Any]) -> NodeProbeSnapshot:
        """Deserialize probe snapshot from hrv_node_state KV value."""
        return NodeProbeSnapshot(
            hostname=kv_payload.get("hostname", "unknown"),
            swap_pct=kv_payload.get("swap_pct"),
            mem_gb_available=kv_payload.get("mem_gb_available"),
            load_1m=kv_payload.get("load_1m"),
            load_5m=kv_payload.get("load_5m"),
            disk_free_gb=kv_payload.get("disk_free_gb"),
            active_workers=kv_payload.get("active_workers"),
            max_workers=kv_payload.get("max_workers"),
            bedrock_tpm_remaining=kv_payload.get("bedrock_tpm_remaining"),
            ts=kv_payload.get("ts"),
        )
    
    def evaluate_node(
        self,
        task_id: str,
        node_hostname: str,
        model_name: str,
        task_priority: Optional[int] = None,
        task_min_resources: Optional[Dict[str, Any]] = None,
    ) -> Optional[str]:
        """
        Evaluate whether a node should be rejected for a task.
        
        Returns:
            None if node passes all gates.
            String reason if node is rejected (e.g., "memory_pressure" or 
            "bedrock_rate_limit_saturation[claude-haiku]").
        
        Args:
            task_id: kanban task ID (for logging).
            node_hostname: target node hostname.
            model_name: LLM model name (e.g. "claude-haiku", "gpt-4").
            task_priority: task priority level (P0=0, P1=1, ..., or None).
            task_min_resources: dict with mem_gb, cpu_cores, bedrock_tpm_reservation.
        """
        probe = self._node_cache.get(node_hostname)
        
        # Condition 1: memory_pressure (swap_pct >= 90% OR ...)
        if self._check_memory_pressure(probe):
            return "memory_pressure"
        
        # Condition 2: kanban_dispatcher_health
        # (Note: this typically comes from systemd status, not the KV snapshot.
        #  For now, we'll leave this as a hook for future integration.)
        if self._check_dispatcher_health(node_hostname):
            return "kanban_dispatcher_health"
        
        # Condition 3: bedrock_rate_limit_saturation
        if self._check_bedrock_rate_limit(model_name, probe):
            return f"bedrock_rate_limit_saturation[{model_name}]"
        
        # Condition 4: hrv.status.digest interval_class == 'urgent' AND priority < P0
        if self._check_hrv_urgency(task_priority):
            return "hrv_urgent_state"
        
        # Condition 5: min_resources overflow
        if self._check_min_resources_fit(task_min_resources, probe):
            return "min_resources_overflow"
        
        return None
    
    def _check_memory_pressure(self, probe: Optional[NodeProbeSnapshot]) -> bool:
        """Reject if probe is RED: swap_pct >= 90% or OOM kill in last 5min.

        Sub-conditions:
        1a) swap_pct >= 90.0 (requires a fresh probe snapshot).
        1b) OOM kill detected in kernel log within the last
            ``OOM_SCAN_WINDOW_SECONDS`` seconds — probed via journalctl and, on
            failure, dmesg. Independent of probe freshness because it queries
            local kernel state; still fails open on any error.
        """
        # 1a: swap pressure — only when the KV probe is fresh
        if probe is not None and not probe.is_stale(self.max_probe_age_seconds):
            if probe.swap_pct is not None and probe.swap_pct >= 90.0:
                return True

        # 1b: OOM kill in last 5min — local kernel probe, no dependency on KV
        if self._oom_kill_within_window():
            return True

        return False

    def _oom_kill_within_window(
        self, window_seconds: int = OOM_SCAN_WINDOW_SECONDS
    ) -> bool:
        """True iff a kernel OOM kill was recorded in the last ``window_seconds``.

        Node-independent (reads local kernel state), so results are TTL-cached
        via ``self._oom_cache_entry`` when the underlying probe returns a
        DEFINITIVE True/False. UNKNOWN answers (both probes error out) are
        NEVER cached — a subsequent call re-probes, preserving fail-open
        semantics without pinning "no evidence" as a truth for the TTL window.

        On any subprocess error / timeout / permission denied, returns False
        (UNKNOWN → fail-open, consistent with the gate's stale-probe policy).
        """
        return self._cached_bool_probe(
            slot_name="_oom_cache_entry",
            uncached_probe=lambda: self._uncached_oom_kill_within_window(window_seconds),
        )

    def _uncached_oom_kill_within_window(
        self, window_seconds: int
    ) -> Optional[bool]:
        """Tri-state OOM probe: True (OOM found), False (none found), None (UNKNOWN).

        Strategy (unchanged from historical behaviour):
        - Primary: ``journalctl -k --since '<N> seconds ago' --no-pager -q``
          filtered for OOM markers (``Out of memory``, ``oom-kill``,
          ``Killed process``, ``invoked oom-killer``).
        - Fallback: ``dmesg -T`` (or ``dmesg``) parsed the same way. The dmesg
          ring buffer isn't time-bounded, so we time-filter on the fly using
          the ``-T`` timestamp when available; when it isn't, dmesg is skipped
          rather than risk stale false-positives from an unrelated old OOM.

        Returns None only when BOTH probes couldn't produce a definitive
        answer — that's the UNKNOWN case that must NOT be cached.
        """
        # Primary path: journalctl (systemd-journal). Preferred because it has
        # a native time filter, so a very old OOM never leaks through.
        journal_result = self._probe_oom_journal(window_seconds)
        if journal_result is not None:
            return journal_result

        # Fallback: dmesg -T (human-readable timestamps). Skipped when -T isn't
        # supported, because untimed dmesg lines can be years old.
        dmesg_result = self._probe_oom_dmesg(window_seconds)
        if dmesg_result is not None:
            return dmesg_result

        # No probe returned a definitive answer → UNKNOWN.
        return None

    def _cached_bool_probe(
        self,
        slot_name: str,
        uncached_probe,
    ) -> bool:
        """Read-through TTL cache for tri-state (Optional[bool]) probes.

        Semantics — critical for correctness:
        * A DEFINITIVE True or False is cached for ``host_global_probe_ttl_seconds``.
        * UNKNOWN (None from the uncached probe) is NEVER cached; the next call
          re-probes so a transient probe error can't pin "fail-open" as the
          truth for the entire TTL window.
        * TTL <= 0 disables caching (each call re-probes).
        * Public return type is bool (None collapses to False → don't reject,
          matching the historical fail-open contract of the wrapping methods).

        Args:
            slot_name: attribute name on ``self`` holding the ``Optional[tuple[bool, float]]``
                cache entry. Passed as a string so a single helper can serve
                multiple independent probe caches without a shared dict lookup.
            uncached_probe: zero-arg callable returning ``Optional[bool]`` —
                True/False are definitive; None means UNKNOWN.
        """
        ttl = self.host_global_probe_ttl_seconds
        # TTL <= 0 → caching disabled entirely.
        if ttl > 0:
            entry = getattr(self, slot_name, None)
            if entry is not None:
                cached_value, expires_at = entry
                if time.monotonic() < expires_at:
                    return cached_value
                # Expired — clear so a re-probe with UNKNOWN doesn't leave a
                # stale expired tuple around confusing debug dumps.
                setattr(self, slot_name, None)

        result = uncached_probe()

        # Only cache DEFINITIVE answers. UNKNOWN (None) must never be cached
        # so the next call can re-probe (fail-open contract).
        if ttl > 0 and result is not None:
            setattr(
                self,
                slot_name,
                (result, time.monotonic() + ttl),
            )

        # Public contract: return bool. None (UNKNOWN) → False (don't reject).
        return bool(result) if result is not None else False

    def invalidate_host_global_probe_cache(self) -> None:
        """Clear both TTL cache slots for node-independent probes.

        Useful for tests, and for callers who observe a state change (e.g.
        systemd unit was just restarted) and want to skip the TTL window.
        No-op when nothing is cached.
        """
        self._oom_cache_entry = None
        self._dispatcher_cache_entry = None

    @staticmethod
    def _line_matches_oom(line: str) -> bool:
        """True if a kernel log line looks like an OOM-killer event.

        Matches (case-insensitive) any of the canonical Linux OOM markers:
        - "Out of memory" (kernel headline)
        - "oom-kill" (cgroup OOM killer)
        - "invoked oom-killer" (process invoking OOM)
        - "Killed process <pid>" (kernel kill notification)
        """
        low = line.lower()
        return (
            "out of memory" in low
            or "oom-kill" in low
            or "invoked oom-killer" in low
            or "killed process" in low
        )

    def _probe_oom_journal(self, window_seconds: int) -> Optional[bool]:
        """Query journalctl for OOM kills. Returns True/False on success, None on error."""
        if shutil.which("journalctl") is None:
            return None
        try:
            proc = subprocess.run(
                [
                    "journalctl",
                    "-k",
                    "--since",
                    f"{window_seconds} seconds ago",
                    "--no-pager",
                    "-q",
                ],
                capture_output=True,
                text=True,
                timeout=_SUBPROC_TIMEOUT_SECONDS,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.debug(f"[hrv-node-gate] journalctl OOM probe failed: {exc}")
            return None

        # journalctl returns 0 on success (even when no matches). Non-zero →
        # unusable (permission denied, journal corrupt, etc.).
        if proc.returncode != 0:
            logger.debug(
                f"[hrv-node-gate] journalctl OOM probe rc={proc.returncode}: "
                f"{proc.stderr.strip()[:200]}"
            )
            return None

        for line in proc.stdout.splitlines():
            if self._line_matches_oom(line):
                logger.info(f"[hrv-node-gate] OOM kill detected in journal: {line.strip()[:200]}")
                return True
        return False

    def _probe_oom_dmesg(self, window_seconds: int) -> Optional[bool]:
        """Query dmesg -T for OOM kills. Returns True/False on success, None on error.

        Requires ``-T`` (human-readable timestamps) so we can time-filter. When
        ``-T`` isn't available or dmesg is unreadable, returns None.
        """
        if shutil.which("dmesg") is None:
            return None
        try:
            proc = subprocess.run(
                ["dmesg", "-T", "--nopager"],
                capture_output=True,
                text=True,
                timeout=_SUBPROC_TIMEOUT_SECONDS,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.debug(f"[hrv-node-gate] dmesg OOM probe failed: {exc}")
            return None

        if proc.returncode != 0:
            # dmesg without CAP_SYSLOG returns EPERM; treat as UNKNOWN.
            logger.debug(
                f"[hrv-node-gate] dmesg OOM probe rc={proc.returncode}: "
                f"{proc.stderr.strip()[:200]}"
            )
            return None

        cutoff = time.time() - window_seconds
        # dmesg -T format: "[Mon Aug 31 12:34:56 2026] kernel: Out of memory: ..."
        ts_re = re.compile(r"^\[([A-Za-z]{3} [A-Za-z]{3} [ \d]\d [\d:]+ \d{4})\]\s*(.*)$")
        for raw in proc.stdout.splitlines():
            m = ts_re.match(raw)
            if not m:
                # Timestamp missing → can't safely time-bound; skip.
                continue
            ts_str, rest = m.group(1), m.group(2)
            try:
                # strptime with locale-independent %b/%a — Python's default locale
                # on Ubuntu is C, matching dmesg's English output.
                struct = time.strptime(ts_str, "%a %b %d %H:%M:%S %Y")
                event_ts = time.mktime(struct)
            except (ValueError, OverflowError):
                continue
            if event_ts < cutoff:
                continue
            if self._line_matches_oom(rest):
                logger.info(f"[hrv-node-gate] OOM kill detected in dmesg: {rest.strip()[:200]}")
                return True
        return False

    def _check_dispatcher_health(self, node_hostname: str) -> bool:
        """Reject if kanban dispatcher systemd unit is unhealthy on this host.

        Node-independent: the systemd check is local to the machine running
        the gate (the ``node_hostname`` arg is retained for the interface's
        symmetry with the per-node checks but the actual probe reads the
        local host's systemd — same answer for every ``node_hostname``).
        Results are TTL-cached via ``self._dispatcher_cache_entry`` when the
        underlying probe returns a DEFINITIVE True/False. UNKNOWN answers
        (systemctl missing, empty state, timeout) are NEVER cached — a
        subsequent call re-probes, preserving fail-open semantics.

        Queries ``systemctl is-active <unit>`` and rejects when the reported
        state is one of ``_DISPATCHER_UNHEALTHY_STATES`` (activating / failed /
        deactivating). When the unit doesn't exist on the host, systemctl
        can't be run, or the call times out, returns False (UNKNOWN →
        fail-open, consistent with the gate's overall policy).

        Unit name overridable via env ``HRV_DISPATCHER_UNIT_NAME``. Note that
        the check is local to the machine running the gate — cross-node
        systemd status would require SSH or a remote probe, out of scope for
        this pass. In practice the gate runs on the dispatcher host itself.
        """
        return self._cached_bool_probe(
            slot_name="_dispatcher_cache_entry",
            uncached_probe=self._uncached_check_dispatcher_health,
        )

    def _uncached_check_dispatcher_health(self) -> Optional[bool]:
        """Tri-state dispatcher-health probe.

        Returns:
            True  — unit is in an unhealthy state (activating/failed/deactivating).
            False — unit is definitively healthy (active/reloading).
            None  — UNKNOWN: no systemctl binary, empty output, subprocess
                    error, OR the state is in ``_DISPATCHER_UNKNOWN_STATES``
                    ('inactive'/'unknown' — unit not installed or unreachable).
                    The task's fail-open contract classifies these as UNKNOWN,
                    so they must NOT be cached: a follow-up call must re-probe.
        """
        unit_name = os.environ.get(
            "HRV_DISPATCHER_UNIT_NAME", _DEFAULT_DISPATCHER_UNIT_NAME
        )
        if shutil.which("systemctl") is None:
            return None  # No systemd binary → UNKNOWN → fail-open, don't cache

        try:
            proc = subprocess.run(
                ["systemctl", "is-active", unit_name],
                capture_output=True,
                text=True,
                timeout=_SUBPROC_TIMEOUT_SECONDS,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            logger.debug(
                f"[hrv-node-gate] systemctl is-active {unit_name} failed: {exc}"
            )
            return None  # UNKNOWN → don't cache

        # `systemctl is-active` exits 0 for active, non-zero otherwise. The
        # state string comes from stdout regardless of exit code.
        state = (proc.stdout or "").strip().lower()
        if not state:
            return None  # No answer → UNKNOWN → don't cache

        if state in _DISPATCHER_UNHEALTHY_STATES:
            logger.info(
                f"[hrv-node-gate] dispatcher unit {unit_name} unhealthy: state={state}"
            )
            return True

        if state in _DISPATCHER_UNKNOWN_STATES:
            # 'inactive' / 'unknown' — the gate's spec explicitly classifies
            # these as UNKNOWN (fail-open, don't reject, and don't cache so
            # we re-probe on the next call). This mirrors the task's caching
            # contract: UNKNOWN responses must never be pinned for the TTL
            # window, since the underlying cause (unit not yet installed,
            # transient reload) can clear at any moment.
            return None

        # Definitive healthy states — 'active', 'reloading', or any other
        # future state systemd invents — cacheable-False.
        return False
    
    def _check_bedrock_rate_limit(
        self, model_name: str, probe: Optional[NodeProbeSnapshot]
    ) -> bool:
        """Reject if bedrock_tpm_remaining is RED (< 1000 TPM remaining)."""
        if probe is None or probe.is_stale(self.max_probe_age_seconds):
            # Stale/missing → fail-open
            return False

        # Conservative threshold: reject if remaining below saturation floor.
        if (
            probe.bedrock_tpm_remaining is not None
            and probe.bedrock_tpm_remaining < BEDROCK_TPM_SATURATION_THRESHOLD
        ):
            return True

        return False
    
    def _check_hrv_urgency(self, task_priority: Optional[int]) -> bool:
        """Reject if hrv digest is 'urgent' AND task priority < P0 (0)."""
        if self._digest is None or self._digest.is_stale(self.max_probe_age_seconds):
            # Stale/missing → fail-open
            return False
        
        if self._digest.interval_class == "urgent":
            # P0 (priority 0) is the highest — only P0 tasks run during 'urgent'
            if task_priority is not None and task_priority > 0:
                return True
        
        return False
    
    def _check_min_resources_fit(
        self,
        task_min_resources: Optional[Dict[str, Any]],
        probe: Optional[NodeProbeSnapshot],
    ) -> bool:
        """Reject if node.available < task.min_resources."""
        if task_min_resources is None:
            # No requirement → pass
            return False
        
        if probe is None or probe.is_stale(self.max_probe_age_seconds):
            # Stale/missing → fail-open (don't reject)
            return False
        
        # Check each resource type
        required_mem_gb = task_min_resources.get("mem_gb", 0.5)
        if (probe.mem_gb_available is not None and
            probe.mem_gb_available < required_mem_gb):
            return True
        
        # CPU cores: estimate from load
        # (real CPU tracking would come from a separate field)
        required_cpu_cores = task_min_resources.get("cpu_cores", 1)
        if probe.load_1m is not None and probe.load_1m > required_cpu_cores:
            # Very rough: if load1 > required cores, assume overloaded
            # (this is conservative and should be refined)
            if probe.load_1m > required_cpu_cores + 0.5:
                return True
        
        # Bedrock TPM reservation: check against bedrock_tpm_remaining.
        #
        # Starvation-edge fix (t_d18420a6, review comment on t_55fb6cb6):
        # DEFAULT_MIN_RESOURCES.bedrock_tpm_reservation is 10000, but the
        # condition-3 saturation reject threshold is BEDROCK_TPM_SATURATION_THRESHOLD
        # (1000). A node publishing bedrock_tpm_remaining in
        # (SATURATION_THRESHOLD, DEFAULT_RESERVATION) would previously reject
        # every DEFAULT-declared task via condition-5 while passing condition-3
        # — dispatch starvation while the model is provably healthy.
        #
        # We resolve this by clamping the effective condition-5 threshold to
        # the saturation floor: condition-5 only rejects when the node's
        # remaining TPM is BELOW BOTH the task's reservation AND the
        # saturation floor. That is, condition-5 will never reject a node
        # that condition-3 has already accepted. A caller that genuinely
        # needs a stricter cutoff can raise BEDROCK_TPM_SATURATION_THRESHOLD
        # rather than smuggling it in via task-level reservations.
        required_tpm = task_min_resources.get("bedrock_tpm_reservation", 10000)
        effective_required_tpm = min(required_tpm, BEDROCK_TPM_SATURATION_THRESHOLD)
        if (
            probe.bedrock_tpm_remaining is not None
            and probe.bedrock_tpm_remaining < effective_required_tpm
        ):
            return True

        return False
    
    def emit_rejection_metric(
        self,
        task_id: str,
        node_hostname: str,
        reason: str,
        metrics_fn=None,
    ) -> None:
        """Emit dispatch.node_rejected metric.
        
        Args:
            metrics_fn: optional callable (reason: str) -> None for telemetry.
        """
        if metrics_fn is not None:
            try:
                metrics_fn(reason)
            except Exception as e:
                logger.warning(f"Failed to emit metric for node rejection: {e}")
        
        logger.info(
            f"[hrv-node-gate] node_rejected: task_id={task_id}, "
            f"node={node_hostname}, reason={reason}"
        )


# Singleton instance for use by kanban dispatcher
_default_gate: Optional[HRVNodeGate] = None


def get_default_gate() -> HRVNodeGate:
    """Get or create the default HRV node gate instance."""
    global _default_gate
    if _default_gate is None:
        _default_gate = HRVNodeGate()
    return _default_gate
