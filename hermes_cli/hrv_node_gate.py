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
import re
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


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
    
    def __init__(self, max_probe_age_seconds: float = 60.0):
        """
        Initialize the gate.
        
        Args:
            max_probe_age_seconds: reject on unknown/stale probes.
                When stale, treat as UNKNOWN → fail-open (do NOT reject).
        """
        self.max_probe_age_seconds = max_probe_age_seconds
        
        # In-memory cache: hostname -> NodeProbeSnapshot
        self._node_cache: Dict[str, NodeProbeSnapshot] = {}
        self._cache_last_refresh: Optional[float] = None
        
        # HRV digest cache
        self._digest: Optional[HRVDigestSnapshot] = None
    
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
        """Reject if probe is RED: swap_pct >= 90% or OOM kill in last 5min."""
        if probe is None or probe.is_stale(self.max_probe_age_seconds):
            # Stale/missing → fail-open (don't reject)
            return False
        
        # Check swap pressure
        if probe.swap_pct is not None and probe.swap_pct >= 90.0:
            return True
        
        # Note: OOM kill detection would typically come from a separate probe
        # (e.g., kernel log tail or systemd journal query). For now, this is
        # a placeholder for future integration.
        
        return False
    
    def _check_dispatcher_health(self, node_hostname: str) -> bool:
        """Reject if dispatcher is activating or crashed in last 10min.
        
        Typically checked via systemctl status kanban-dispatcher@<node>.service.
        For now, returns False (placeholder for systemd integration).
        """
        # TODO: integrate with systemd.dbus / systemctl query
        return False
    
    def _check_bedrock_rate_limit(
        self, model_name: str, probe: Optional[NodeProbeSnapshot]
    ) -> bool:
        """Reject if bedrock_tpm_remaining is RED (< 1000 TPM remaining)."""
        if probe is None or probe.is_stale(self.max_probe_age_seconds):
            # Stale/missing → fail-open
            return False
        
        # Conservative threshold: reject if < 1000 TPM remaining
        if probe.bedrock_tpm_remaining is not None and probe.bedrock_tpm_remaining < 1000:
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
        
        # Bedrock TPM reservation: check against bedrock_tpm_remaining
        required_tpm = task_min_resources.get("bedrock_tpm_reservation", 10000)
        if (probe.bedrock_tpm_remaining is not None and
            probe.bedrock_tpm_remaining < required_tpm):
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
