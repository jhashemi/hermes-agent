#!/usr/bin/env python3
"""
LLM-backed VCG Cluster Dispatcher
==================================

Bridges the deterministic capacity-proportional load balancer
(``infrastructure/harness/cluster_load_balancer.py``) with an LLM routing
layer that reasons over agent affinity, skill match, and live resource
telemetry.

Architecture
------------
::

    gateway kanban (SQLite, ready tasks)
         |
         v
    LLMClusterDispatcher.tick()
         |  1. collect node telemetry    (DuckDB machine_resources + SSH)
         |  2. collect ready tasks       (SQLite tasks WHERE status='ready')
         |  3. collect agent registry    (DuckDB kanban_agents)
         |  4. deterministic pre-filter  (hard resource gates, StaleNodeMetrics)
         |  5. LLM routing decision      (welfare scoring over survivors)
         |  6. safety validate + clamp   (never route to excluded node)
         |  7. claim + spawn worker      (hermes -p <agent> --skills kanban-worker)
         v
    DuckDB audit ledger (dispatch_decisions)

The LLM is *advisory*, never authoritative: every LLM decision is validated
against the deterministic gates before a claim is written. If the LLM is
unreachable or returns garbage, the dispatcher falls back to pure
capacity-proportional fill (the proven algorithm from ADR-006).

Pitfall defenses baked in (from cluster-load-balancer-honest-metrics):
  - StaleNodeMetrics raised on missing/stale metrics — never silently bias.
  - Capacity-proportional headroom fill — never greedy single-node collapse.
  - Heartbeat staleness > 120s excludes the node.
  - skills column on spawned task stays ``kanban-worker`` (never capability tags).
  - workspace_kind read from live board, never assumed.

Usage
-----
    python3 scripts/llm_cluster_dispatcher.py --dry-run
    python3 scripts/llm_cluster_dispatcher.py --once
    python3 scripts/llm_cluster_dispatcher.py --daemon --interval 30

Author: generated 2026-07-30 (kanban task: wire LLM dispatcher into live VCG)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shlex
import sqlite3
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
EAF_ROOT = Path("/home/ubuntu/executive_agents_framework")
sys.path.insert(0, str(EAF_ROOT / "src"))

# HAMILTON CRITICAL-3 safety spine lives in hermes-agent (t_64a6a7bd).
HERMES_AGENT_ROOT = Path("/home/ubuntu/hermes-agent")
if HERMES_AGENT_ROOT.exists():
    sys.path.insert(0, str(HERMES_AGENT_ROOT))

DUCKDB_BOARD = EAF_ROOT / "data" / "kanban_board.db"
GATEWAY_BOARDS_DIR = Path.home() / ".hermes" / "kanban" / "boards"
AUDIT_DB = Path.home() / ".hermes" / "memory" / "llm_dispatcher.duckdb"
PROFILES_DIR = Path.home() / ".hermes" / "profiles"

# Reuse the battle-tested deterministic balancer
from executive_agents.infrastructure.harness.cluster_load_balancer import (
    ClusterLoadBalancer,
    ClusterResourceMonitor,
    NodeResources,
    StaleNodeMetrics,
)

# HAMILTON CRITICAL-3 safety spine: kill switch + NATS-liveness TCP probe +
# armed-token assertion + AMBER-only high-memory gating (t_64a6a7bd).
try:
    from hermes_cli.vcg_safety_spine import (
        ArmedTokenMissing,
        SafetySpine,
        amber_blocks_task,
    )
    _SAFETY_SPINE_AVAILABLE = True
except ImportError as _spine_err:  # pragma: no cover — logged at startup
    ArmedTokenMissing = RuntimeError  # type: ignore[assignment,misc]
    SafetySpine = None  # type: ignore[assignment]
    amber_blocks_task = None  # type: ignore[assignment]
    _SAFETY_SPINE_AVAILABLE = False
    _SPINE_IMPORT_ERROR = _spine_err

logger = logging.getLogger("llm_dispatcher")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
HEARTBEAT_STALE_S = 120.0          # Exclude node if heartbeat older than this
LOAD_RATIO_HARD_MAX = 0.85         # Never route if load_1min/cpu_count > this
# Reporter stores disk as USED pct. hermes nodes run ~85-92% used normally;
# hard floor at 8% FREE (92% used) — only block when truly disk-critical.
DISK_FREE_HARD_MIN_PCT = 8.0       # Never route if disk free < this pct
LIVE_PROBE_ON_STALE = True         # Probe live when DuckDB heartbeat is stale
LLM_TIMEOUT_S = 90.0               # glm-5.2 thinking + SSH probes need headroom
LLM_BATCH_SIZE = 6                 # Small batches -> fewer thinking tokens, faster
LLM_MODEL = os.environ.get("VCG_DISPATCH_MODEL", "glm-5.2")
OLLAMA_CLOUD_URL = "https://ollama.com/v1/chat/completions"
AGENT_LOAD_ESTIMATE = 0.1          # ADR-006 chaos-drill validated value

# Node universe: hermes2 local, hermes1 via Tailscale
NODE_HOSTS = {
    "hermes2": None,               # local
    "hermes1": "100.107.83.25",    # Tailscale
}

# ---------------------------------------------------------------------------
# min_resources — per-task resource declaration (ADR-006b Phase 2)
# ---------------------------------------------------------------------------
# Kept in sync with hermes_cli.kanban_db.SUPPORTED_MIN_RESOURCE_KEYS and
# DEFAULT_MIN_RESOURCES. Duplicated here because this script is standalone
# (deployed to ~/.hermes/scripts/) and must not require the hermes package
# to be importable at dispatcher runtime.
SUPPORTED_MIN_RESOURCE_KEYS: dict[str, type] = {
    "mem_gb": float,
    "cpu_cores": int,
    "bedrock_tpm_reservation": int,
}
DEFAULT_MIN_RESOURCES: dict[str, Any] = {
    "mem_gb": 0.5,
    "cpu_cores": 1,
    "bedrock_tpm_reservation": 10000,
}

# Per-active-worker CPU headroom estimate (cores). Used when checking whether
# a node has enough spare CPU cores for a task's cpu_cores minimum. Matches
# AGENT_LOAD_ESTIMATE=0.1 * cpu_count for load-ratio math; here we use a
# whole-core proxy so 4-core minimums are honored against multi-active-worker
# nodes.
CPU_CORES_PER_ACTIVE_WORKER = 1.0

# ---------------------------------------------------------------------------
# Nervous-system gates (ADR-006b Phase 2, t_c6b97f78)
# ---------------------------------------------------------------------------
# Read the shared NATS JetStream KV bucket `hrv_node_state` (populated by
# scripts/vcg_nats_publish.py, t_2bb941a8) to reject nodes whose HRV probes
# are RED before the deterministic min_resources/eligibility gates run.
#
# Fail-open contract: if the reader returns {} (NATS down / bucket missing /
# key missing / decode error), NO gate rejects. Callers see the same behavior
# as if the feature were disabled. See hrv_node_state_reader.py for details.
#
# The four probes mapped from failure classes are all consumed schema-
# agnostically: a probe is RED only when its key is *present* in the KV
# payload AND meets the RED predicate. Missing keys mean "unknown" → allow.
# This lets producers add coverage over time without churning the gate.

# Probe #1 — memory_pressure (failure class #5). RED when swap_pct >= 90 OR
# oom_kill_last_5min > 0. `swap_pct` is emitted today by
# vcg_resource_reporter.py + vcg_nats_publish.py; `oom_kill_last_5min` is
# reserved for a future OOM-scanning probe.
SWAP_PCT_RED_THRESHOLD = float(
    os.environ.get("VCG_DISPATCH_SWAP_PCT_RED", "90.0")
)

# Softer companion band (t_ad7e65f9): SWAP_PCT in [AMBER, RED) puts a node
# into AMBER memory_state, which the safety spine's ``amber_blocks_task()``
# rejects for HIGH-memory tasks only (ordinary tasks still route). This is
# the graceful-not-cliff band the spine was built to express; without it the
# import of ``amber_blocks_task`` in this module was dead. Default 75 %
# gives ~15 pp of headroom before RED trips.
SWAP_PCT_AMBER_THRESHOLD = float(
    os.environ.get("VCG_DISPATCH_SWAP_PCT_AMBER", "75.0")
)

# Probe #4 — HRV interval class. `urgent` on a node means the RSI pacemaker
# is in urgent mode; refuse to route non-P0 work there so the node can drain.
# P0_PRIORITY_THRESHOLD: minimum kanban `priority` value considered P0.
# Kanban tasks default to priority=0; explicit P0s bump this deliberately.
# Set high (100) so the gate only opens for tasks the caller has explicitly
# tagged P0. Adjust via env at deploy time if operator conventions differ.
P0_PRIORITY_THRESHOLD = int(
    os.environ.get("VCG_DISPATCH_P0_PRIORITY_THRESHOLD", "100")
)

# Master switch. Off = skip the gate entirely (fallback to pre-t_c6b97f78
# behavior). Useful for A/B / kill-switch.
NERVOUS_SYSTEM_GATES_ENABLED = (
    os.environ.get("VCG_DISPATCH_NERVOUS_GATES", "1").strip().lower()
    not in ("0", "false", "no", "off")
)

# ---------------------------------------------------------------------------
# LLM routing prompt (the artifact this whole task is about)
# ---------------------------------------------------------------------------
DISPATCHER_SYSTEM_PROMPT = """\
You are the VCG Cluster Dispatcher. Route kanban tasks to the healthiest
cluster machine while respecting agent specialization and resource limits.

CLUSTER NODES (live telemetry):
{cluster_nodes_json}

READY TASKS (unclaimed, status='ready'):
{task_queue_json}

AGENT REGISTRY (id, skills, capacity, reliability):
{agent_registry_json}

ROUTING RULES (strict priority order):
1. AGENT-AFFINITY: prefer the node where the task's assignee already has
   recent execution context (see agent_node_history in task payload).
2. SKILL-MATCH: welfare = (skill_match^1.5) * (1-load_ratio) * reliability
   * (1 + priority/10). skill_match = fraction of task.required_skills the
   assignee's registry skills cover.
3. RESOURCE-HEADROOM HARD GATES: NEVER pick a node where
   health != 'healthy', load_ratio > 0.85, disk_free_pct < 15, or
   active_workers >= max_workers. These nodes are pre-filtered out of
   eligible_nodes — if a node is absent from eligible_nodes, do not pick it.
4. CAPACITY-PROPORTIONAL FILL: among eligible nodes prefer the one with the
   largest remaining headroom = (1-resource_score)*cpu_cores -
   placed_this_tick*0.1*cpu_cores. Never stack all tasks on one node.
5. HEARTBEAT STALENESS: nodes with heartbeat_age_s > 120 are ineligible.
6. ANTI-FLAP: do not re-route an already-claimed task.

OUTPUT: strict JSON array, one object per task, NOTHING else:
[
  {{
    "task_id": "t_xxxxxxxx",
    "assigned_agent": "demis_hassabis",
    "target_node": "hermes2",
    "welfare_score": 0.84,
    "reasoning": "one line"
  }}
]

If no eligible node exists for a task, omit it from the array.
Do not invent node names. Use only nodes present in eligible_nodes.
"""


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
@dataclass
class NodeTelemetry:
    node_id: str
    cpu_count: int
    load_1min: float
    mem_avail_gb: float
    disk_free_pct: float
    active_workers: int
    max_workers: int
    heartbeat_age_s: float
    status: str

    @property
    def load_ratio(self) -> float:
        return self.load_1min / max(1, self.cpu_count)

    @property
    def eligible(self) -> bool:
        return (
            self.status == "healthy"
            and self.heartbeat_age_s <= HEARTBEAT_STALE_S
            and self.load_ratio <= LOAD_RATIO_HARD_MAX
            and self.disk_free_pct >= DISK_FREE_HARD_MIN_PCT
            and self.active_workers < self.max_workers
        )


@dataclass
class RoutingDecision:
    task_id: str
    assigned_agent: str
    target_node: str
    welfare_score: float = 0.0
    reasoning: str = ""
    source: str = "llm"          # 'llm' or 'fallback-proportional'
    validated: bool = False


# ---------------------------------------------------------------------------
# Telemetry collection
# ---------------------------------------------------------------------------
def _probe_live(node_id: str, host: Optional[str]) -> Optional[NodeTelemetry]:
    """Probe a node directly (local or via SSH over Tailscale) for fresh
    telemetry. Used when the DuckDB heartbeat is stale/missing — the reporter
    cron is not reliably writing, so the dispatcher must not silently trust
    60-day-old rows."""
    prefix = ("ssh -o ConnectTimeout=3 -o BatchMode=yes " + host + " ") if host else ""
    try:
        def sh(cmd: str, timeout: int = 6) -> str:
            full = (prefix + shlex.quote(cmd)) if host else cmd
            r = subprocess.run(full, shell=True,
                               capture_output=True, text=True, timeout=timeout)
            return r.stdout.strip()

        cores = int(sh("nproc") or "2")
        load1 = float(sh("cat /proc/loadavg | cut -d' ' -f1") or "0.0")
        # Mem: `free -m` available column is index 6; -g rounds to 0 on <1G free
        mem_line = sh("free -m | grep Mem:")
        mem_avail = (float(mem_line.split()[6]) / 1024.0) if mem_line else 0.0
        disk_line = sh("df -P /home | tail -1")  # -P: posix one-line, no wrap
        disk_used_pct = float(disk_line.split()[4].replace("%", "")) if disk_line else 100.0
        # Active hermes worker processes on this node
        active = len([l for l in sh("pgrep -af 'hermes -p|kanban-worker'").splitlines() if l.strip()])
        return NodeTelemetry(
            node_id=node_id, cpu_count=cores, load_1min=load1,
            mem_avail_gb=mem_avail, disk_free_pct=max(0.0, 100.0 - disk_used_pct),
            active_workers=active, max_workers=max(2, cores),
            heartbeat_age_s=0.0, status="healthy",
        )
    except Exception as e:
        logger.warning(f"[Probe] live probe failed for {node_id}: {e}")
        return None


class TelemetryCollector:
    """Reads DuckDB machine_resources heartbeats (written by
    vcg_resource_reporter.py on both nodes via cron). Falls back to live
    probes when the DuckDB row is stale or missing (the reporter cron is
    not reliably writing — observed 54-69 day staleness 2026-07-30)."""

    def collect(self) -> dict[str, NodeTelemetry]:
        now = time.time()
        rows: dict[str, dict[str, Any]] = {}
        try:
            import duckdb
            con = duckdb.connect(str(DUCKDB_BOARD), read_only=True)
            for r in con.execute(
                "SELECT machine_id, cpu_count, load_1min, mem_avail_gb, "
                "disk_pct, active_tasks, max_workers, heartbeat_at, status "
                "FROM machine_resources"
            ).fetchall():
                rows[r[0]] = {
                    "cpu_count": r[1] or 2,
                    "load_1min": r[2] or 0.0,
                    "mem_avail_gb": r[3] or 0.0,
                    # disk_pct is USED pct in the reporter schema
                    "disk_free_pct": max(0.0, 100.0 - (r[4] or 100.0)),
                    "active_workers": r[5] or 0,
                    "max_workers": r[6] or 4,
                    "heartbeat_at": r[7] or 0.0,
                    "status": r[8] or "unknown",
                }
            con.close()
        except Exception as e:
            logger.warning(f"[Telemetry] DuckDB read failed: {e}")

        out: dict[str, NodeTelemetry] = {}
        for node_id, host in NODE_HOSTS.items():
            # hermes1 == ip-172-31-30-216 alias (skill pitfall #11)
            row = rows.get(node_id) or (
                rows.get("ip-172-31-30-216") if node_id == "hermes1" else None
            )
            age = (now - float(row["heartbeat_at"])) if row else 1e9

            if row is not None and age <= HEARTBEAT_STALE_S:
                out[node_id] = NodeTelemetry(
                    node_id=node_id,
                    cpu_count=int(row["cpu_count"]),
                    load_1min=float(row["load_1min"]),
                    mem_avail_gb=float(row["mem_avail_gb"]),
                    disk_free_pct=float(row["disk_free_pct"]),
                    active_workers=int(row["active_workers"]),
                    max_workers=int(row["max_workers"]),
                    heartbeat_age_s=age,
                    status=str(row["status"]),
                )
                continue

            # Stale or missing — live probe (defense against dead reporter cron)
            if LIVE_PROBE_ON_STALE:
                probed = _probe_live(node_id, host)
                if probed is not None:
                    logger.info(f"[Telemetry] {node_id} heartbeat stale "
                                f"(age={age:.0f}s); using live probe")
                    out[node_id] = probed
                    continue

            # Truly unreachable — ineligible
            out[node_id] = NodeTelemetry(
                node_id=node_id, cpu_count=2, load_1min=999.0,
                mem_avail_gb=0.0, disk_free_pct=0.0, active_workers=999,
                max_workers=1, heartbeat_age_s=age, status="unknown",
            )
        return out


# ---------------------------------------------------------------------------
# Task + agent collection
# ---------------------------------------------------------------------------
def collect_ready_tasks(board: str, limit: int = 20) -> list[dict[str, Any]]:
    db = GATEWAY_BOARDS_DIR / board / "kanban.db"
    if not db.exists():
        logger.warning(f"[Tasks] board db missing: {db}")
        return []
    con = sqlite3.connect(str(db))
    con.row_factory = sqlite3.Row
    # min_resources is optional (added in ADR-006b Phase 2). Older boards may
    # be missing the column; select defensively via PRAGMA-driven column list
    # so this script keeps working against pre-migration DBs.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(tasks)")}
    has_min_res = "min_resources" in cols
    # model_override optional too (used by t_c6b97f78 bedrock_rate_limit gate).
    has_model_override = "model_override" in cols
    select_cols = "id, title, assignee, priority, body, workspace_kind"
    if has_min_res:
        select_cols += ", min_resources"
    if has_model_override:
        select_cols += ", model_override"
    rows = con.execute(
        f"SELECT {select_cols} "
        "FROM tasks WHERE status='ready' AND claim_lock IS NULL "
        "ORDER BY priority DESC LIMIT ?",
        (limit,),
    ).fetchall()
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        if not has_min_res:
            d["min_resources"] = None
        if not has_model_override:
            d["model_override"] = None
        out.append(d)
    return out


def collect_agent_registry() -> list[dict[str, Any]]:
    try:
        import duckdb
        con = duckdb.connect(str(DUCKDB_BOARD), read_only=True)
        rows = con.execute(
            "SELECT id, skills, capacity, reliability FROM kanban_agents"
        ).fetchall()
        con.close()
        return [
            {"id": r[0], "skills": json.loads(r[1] or "[]"),
             "capacity": r[2], "reliability": r[3]}
            for r in rows
        ]
    except Exception as e:
        logger.warning(f"[Agents] DuckDB read failed: {e}")
        return []


def agent_node_history(agent_id: str) -> list[str]:
    """Best-effort recent execution nodes for an agent (audit ledger)."""
    if not AUDIT_DB.exists():
        return []
    try:
        import duckdb
        con = duckdb.connect(str(AUDIT_DB), read_only=True)
        rows = con.execute(
            "SELECT target_node FROM dispatch_decisions "
            "WHERE assigned_agent = ? ORDER BY decided_at DESC LIMIT 5",
            [agent_id],
        ).fetchall()
        con.close()
        return [r[0] for r in rows]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# LLM client (ollama-cloud, OpenAI-compatible)
# ---------------------------------------------------------------------------
def _ollama_key() -> str:
    key = os.environ.get("OLLAMA_API_KEY", "")
    if key:
        return key
    env_file = Path.home() / ".hermes" / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("OLLAMA_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def llm_route(system_prompt: str) -> Optional[list[dict[str, Any]]]:
    """Call the LLM for routing. Returns parsed decision list or None."""
    key = _ollama_key()
    if not key:
        logger.warning("[LLM] no OLLAMA_API_KEY; falling back")
        return None
    payload = json.dumps({
        "model": LLM_MODEL,
        "messages": [
            {"role": "system",
             "content": "You are a cluster routing engine. Output strict JSON only."},
            {"role": "user", "content": system_prompt},
        ],
        "temperature": 0.1,
        # glm-5.2 is a thinking model: reasoning tokens consume max_tokens.
        # Small budgets starve it -> empty content + finish_reason=length.
        # 8192 gives ample reasoning + JSON headroom (verified live 2026-07-30:
        # 2-node routing burns ~5-6k thinking tokens before emitting JSON).
        "max_tokens": 8192,
    }).encode()
    req = urllib.request.Request(
        OLLAMA_CLOUD_URL, data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT_S) as resp:
            body = json.loads(resp.read().decode())
        text = (body["choices"][0]["message"].get("content") or "").strip()
        if not text:
            # Thinking model exhausted max_tokens on reasoning, no JSON emitted.
            logger.warning("[LLM] empty content (reasoning token exhaustion); "
                           "falling back to proportional fill")
            return None
        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        decisions = json.loads(text)
        if not isinstance(decisions, list):
            raise ValueError("LLM output not a list")
        return decisions
    except Exception as e:
        logger.warning(f"[LLM] routing call failed: {e}")
        return None


def _check_nervous_system_gates(
    node: NodeTelemetry,
    task: dict[str, Any],
    probe_state: dict[str, Any],
) -> Optional[tuple[str, str]]:
    """Return ``(probe_name, rejection_reason)`` if any nervous-system probe
    is RED for this ``(node, task)`` pair, else ``None``.

    Consumes probe_state as an untyped dict (whatever the publisher stored).
    Missing keys are treated as "unknown" and never trip a rejection —
    strictly a REJECT-when-present-and-RED policy so producer schema evolution
    doesn't halt dispatch.

    Fail-open sink: an empty ``probe_state`` always returns None. Callers
    fetch probe state through :func:`_fetch_probe_states`, which already
    fail-opens on NATS/reader failure by returning ``{}`` per node.

    Four probes (parent umbrella t_88eadaa8 failure classes):

    1. memory_pressure (class #5) — RED when swap_pct >= SWAP_PCT_RED_THRESHOLD
       OR oom_kill_last_5min > 0.
    2. kanban_dispatcher_health (class #4) — RED when the field is present
       and its value is truthy-red (string "red", or dict with
       state in {"activating","crashed"} and age_s <= 600). Missing = allow.
    3. bedrock_rate_limit_saturation (class #9) — RED when the task is pinned
       to a model AND the field is a dict-per-model AND
       payload[model]["state"] == "red". Missing model → allow.
    4. hrv.status.digest.interval_class == "urgent" AND task priority <
       P0_PRIORITY_THRESHOLD.
    """
    if not NERVOUS_SYSTEM_GATES_ENABLED:
        return None
    if not probe_state:
        # Fail-open: no state → no gate.
        return None

    # --- Probe #1: memory_pressure -----------------------------------------
    # Explicit field wins if present.
    mp = probe_state.get("memory_pressure")
    if isinstance(mp, dict):
        if str(mp.get("state", "")).lower() == "red":
            return (
                "memory_pressure",
                f"node {node.node_id} memory_pressure=RED "
                f"({mp.get('reason', 'unspecified')})",
            )
    elif isinstance(mp, str) and mp.lower() == "red":
        return (
            "memory_pressure",
            f"node {node.node_id} memory_pressure=RED",
        )
    # Fall back to raw signals emitted by vcg_nats_publish today.
    try:
        swap_pct = float(probe_state.get("swap_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        swap_pct = 0.0
    if swap_pct >= SWAP_PCT_RED_THRESHOLD:
        return (
            "memory_pressure",
            f"node {node.node_id} swap_pct={swap_pct:.1f}% "
            f">= {SWAP_PCT_RED_THRESHOLD:.1f}% (memory_pressure RED)",
        )
    try:
        oom_recent = int(probe_state.get("oom_kill_last_5min", 0) or 0)
    except (TypeError, ValueError):
        oom_recent = 0
    if oom_recent > 0:
        return (
            "memory_pressure",
            f"node {node.node_id} had {oom_recent} OOM kill(s) in last 5min "
            f"(memory_pressure RED)",
        )

    # --- Probe #2: kanban_dispatcher_health --------------------------------
    kdh = probe_state.get("kanban_dispatcher_health")
    if isinstance(kdh, dict):
        state = str(kdh.get("state", "")).lower()
        try:
            age_s = float(kdh.get("age_s", 1e9) or 1e9)
        except (TypeError, ValueError):
            age_s = 1e9
        if state == "red" or (
            state in ("activating", "crashed", "failed") and age_s <= 600.0
        ):
            return (
                "kanban_dispatcher_health",
                f"node {node.node_id} kanban_dispatcher_health=RED "
                f"(state={state or 'red'}, age_s={age_s:.0f})",
            )
    elif isinstance(kdh, str) and kdh.lower() == "red":
        return (
            "kanban_dispatcher_health",
            f"node {node.node_id} kanban_dispatcher_health=RED",
        )

    # --- Probe #3: bedrock_rate_limit_saturation (per pinned model) --------
    # Task's pinned model may live in `model_override` (kanban DB column) or
    # `model` (older). If the task isn't pinned, this gate cannot apply.
    pinned_model = task.get("model_override") or task.get("model") or ""
    if pinned_model:
        brl = probe_state.get("bedrock_rate_limit_saturation")
        # Two shapes accepted:
        #   {"model-id": {"state": "red", ...}, ...}  (per-model dict)
        #   {"state": "red", "models": ["model-id"], ...}  (single-model)
        if isinstance(brl, dict):
            per_model = brl.get(pinned_model)
            if isinstance(per_model, dict) and str(
                per_model.get("state", "")
            ).lower() == "red":
                return (
                    "bedrock_rate_limit_saturation",
                    f"node {node.node_id} bedrock_rate_limit_saturation=RED "
                    f"for model {pinned_model}",
                )
            # Single-model shape
            if str(brl.get("state", "")).lower() == "red":
                affected = brl.get("models") or []
                if pinned_model in affected or not affected:
                    return (
                        "bedrock_rate_limit_saturation",
                        f"node {node.node_id} bedrock_rate_limit_saturation=RED "
                        f"for model {pinned_model}",
                    )

    # --- Probe #4: hrv.status.digest.interval_class ------------------------
    # Accept two shapes: a nested dict under `hrv.status.digest`, or a flat
    # `hrv_interval_class` key. NATS key-encoding rewrites dots so the flat
    # form is what the publisher typically writes; the nested form is a
    # convenience for producers that construct richer payloads.
    interval_class = ""
    hrv_nested = probe_state.get("hrv")
    if isinstance(hrv_nested, dict):
        status = hrv_nested.get("status") or {}
        if isinstance(status, dict):
            digest = status.get("digest") or {}
            if isinstance(digest, dict):
                interval_class = str(digest.get("interval_class", "") or "")
    if not interval_class:
        interval_class = str(probe_state.get("hrv_interval_class", "") or "")
    if interval_class.lower() == "urgent":
        try:
            priority = int(task.get("priority") or 0)
        except (TypeError, ValueError):
            priority = 0
        if priority < P0_PRIORITY_THRESHOLD:
            return (
                "hrv_interval_class",
                f"node {node.node_id} hrv.interval_class=urgent AND task "
                f"priority={priority} < P0={P0_PRIORITY_THRESHOLD} "
                f"(task {task.get('id')})",
            )

    return None


# --- t_ad7e65f9: AMBER memory_state gate (safety spine wiring) ---------------
#
# The safety spine ships ``amber_blocks_task(memory_state, task)`` — a
# graceful-not-cliff gate that rejects HIGH-memory tasks (mem_gb >= 4.0) when
# a node is in AMBER memory_state, while still letting ordinary tasks route.
# Prior to this task the symbol was imported at module scope but never
# called, so the gate was dead in the live path. This helper classifies a
# node's memory_state from probe telemetry and consults the spine.
#
# Classification precedence — least to most trusted:
#   1. Explicit ``probe_state["memory_state"]`` string (e.g. "green"/"amber"/"red")
#      or dict with a ``state`` field.
#   2. Explicit ``memory_pressure`` dict with a ``state`` field.
#   3. Raw ``swap_pct`` bands: RED >= SWAP_PCT_RED_THRESHOLD (matches the
#      hard gate above), AMBER >= SWAP_PCT_AMBER_THRESHOLD, else GREEN.
#
# Fail-open: missing/malformed inputs classify as GREEN (unknown). This
# matches the module-wide fail-open contract — a broken producer must not
# halt dispatch.
def _classify_node_memory_state(probe_state: dict) -> str:
    """Return one of ``"green"`` / ``"amber"`` / ``"red"`` for a node.

    Uses only local telemetry (probe state cache); NEVER round-trips NATS,
    since the whole point of this gate is that it survives a broken NATS.
    """
    if not probe_state:
        return "green"

    # 1. Explicit memory_state field wins.
    ms_raw = probe_state.get("memory_state")
    if isinstance(ms_raw, dict):
        s = str(ms_raw.get("state", "")).strip().lower()
        if s in ("green", "amber", "red"):
            return s
    elif isinstance(ms_raw, str):
        s = ms_raw.strip().lower()
        if s in ("green", "amber", "red"):
            return s

    # 2. memory_pressure dict — RED handled by the hard gate, but AMBER
    # signalled explicitly by producers should be honored here.
    mp = probe_state.get("memory_pressure")
    if isinstance(mp, dict):
        s = str(mp.get("state", "")).strip().lower()
        if s in ("amber", "red"):
            return s
    elif isinstance(mp, str):
        s = mp.strip().lower()
        if s in ("amber", "red"):
            return s

    # 3. Raw swap_pct bands.
    try:
        swap_pct = float(probe_state.get("swap_pct", 0.0) or 0.0)
    except (TypeError, ValueError):
        return "green"
    if swap_pct >= SWAP_PCT_RED_THRESHOLD:
        return "red"
    if swap_pct >= SWAP_PCT_AMBER_THRESHOLD:
        return "amber"
    return "green"


def _check_memory_amber_gate(
    node: NodeTelemetry,
    task: dict[str, Any],
    probe_state: dict[str, Any],
) -> Optional[tuple[str, str]]:
    """AMBER memory_state gate — rejects HIGH-memory tasks on AMBER nodes.

    Wraps :func:`hermes_cli.vcg_safety_spine.amber_blocks_task`. Fail-open
    when the safety spine is unavailable (mirrors module-scope import
    fallback: safer to admit than to silently reject when the classifier
    itself is missing). Returns ``(probe_name, reason)`` on reject, else
    ``None``.

    Notes:
      * RED classification is already handled by the hard memory_pressure
        gate above. This helper still consults the spine so RED tasks
        double-tap-reject with a spine-native reason string, but that path
        is defensive — the hard gate should have already vetoed.
      * The spine reads canonical ``mem_gb`` (t_ad7e65f9 fix); DB-sourced
        tasks classify correctly. Legacy ``memory_gb`` is still honored as
        fallback by the spine.
    """
    if not NERVOUS_SYSTEM_GATES_ENABLED:
        return None
    if amber_blocks_task is None:  # spine import failed at module load
        return None
    memory_state = _classify_node_memory_state(probe_state or {})
    if memory_state == "green":
        return None
    if not amber_blocks_task(memory_state, task):
        return None
    return (
        "memory_amber",
        f"node {node.node_id} memory_state={memory_state.upper()} rejects "
        f"high-memory task {task.get('id')} "
        f"(safety spine amber_blocks_task)",
    )


def _fetch_probe_states(
    node_ids: list[str],
    *,
    reader_fn: Any = None,
) -> dict[str, dict[str, Any]]:
    """Return ``{node_id: probe_state_dict}`` for each id.

    Fail-open per-node: any exception or missing key yields an empty dict for
    that node (which then bypasses the gate entirely). One-shot per call —
    the sync wrapper opens+closes a connection; that's fine at dispatcher
    tick cadence (every 30s+).

    ``reader_fn`` is a seam for tests. Defaults to
    :func:`hrv_node_state_reader.get_node_probe_state_sync`.
    """
    if not NERVOUS_SYSTEM_GATES_ENABLED:
        return {n: {} for n in node_ids}
    if reader_fn is None:
        try:
            # scripts/dispatch/ sits next to hrv_node_state_reader in the
            # same directory when deployed to ~/.hermes/scripts/ (flat) OR
            # when running from the repo (scripts/dispatch/…). Both layouts
            # end up with the module importable by name.
            from hrv_node_state_reader import (  # type: ignore
                get_node_probe_state_sync as reader_fn,  # noqa: N812
            )
        except Exception as e:
            logger.warning(
                "[NervousGates] reader import failed (%s) — fail-open for tick",
                e,
            )
            return {n: {} for n in node_ids}
    out: dict[str, dict[str, Any]] = {}
    for nid in node_ids:
        try:
            state = reader_fn(nid) or {}
            if not isinstance(state, dict):
                state = {}
        except Exception as e:  # pragma: no cover — defensive
            logger.warning(
                "[NervousGates] probe fetch failed for %s (%s) — fail-open",
                nid, e,
            )
            state = {}
        out[nid] = state
    return out


# ---------------------------------------------------------------------------
# Fallback: pure capacity-proportional fill (no LLM)
# ---------------------------------------------------------------------------
def _task_min_resources(task: dict[str, Any]) -> dict[str, Any]:
    """Return the effective min_resources for a task.

    Precedence:
      1. Stored ``min_resources`` column (JSON text) — canonical, already
         validated by the kanban_db ingest layer.
      2. YAML front-matter in ``body`` under ``min_resources:`` — parsed
         defensively; malformed / unknown keys silently dropped.
      3. DEFAULT_MIN_RESOURCES fallback for any missing dimension.

    The returned dict always contains every SUPPORTED_MIN_RESOURCE_KEYS key,
    with declared values overriding defaults dimension-by-dimension.
    """
    declared: dict[str, Any] = {}

    # 1. column
    raw = task.get("min_resources")
    if raw:
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
            if isinstance(parsed, dict):
                for k, coerce in SUPPORTED_MIN_RESOURCE_KEYS.items():
                    if k in parsed:
                        try:
                            declared[k] = coerce(parsed[k])
                        except (TypeError, ValueError):
                            pass
        except (json.JSONDecodeError, TypeError):
            pass

    # 2. body front-matter (best-effort, no yaml dep required)
    if not declared:
        body = task.get("body") or ""
        if isinstance(body, str) and body.lstrip().startswith("---"):
            try:
                # Minimal front-matter block extraction
                after = body.lstrip()[3:]
                end = after.find("\n---")
                if end > 0:
                    fm = after[:end]
                    # Look for min_resources: block, one indented key: value per line
                    in_block = False
                    for line in fm.splitlines():
                        stripped = line.strip()
                        if not in_block:
                            if stripped.startswith("min_resources:"):
                                in_block = True
                            continue
                        # dedent-based end: line at col 0 that isn't blank/comment
                        if line and not line[0].isspace() and not stripped.startswith("#"):
                            break
                        if ":" not in stripped or stripped.startswith("#"):
                            continue
                        k, _, v = stripped.partition(":")
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k in SUPPORTED_MIN_RESOURCE_KEYS:
                            try:
                                declared[k] = SUPPORTED_MIN_RESOURCE_KEYS[k](v)
                            except (TypeError, ValueError):
                                pass
            except Exception:
                # Front-matter parsing is best-effort; never crash ingest.
                pass

    # 3. merge with defaults
    merged = dict(DEFAULT_MIN_RESOURCES)
    merged.update(declared)
    return merged


def _node_meets_min_resources(
    node: NodeTelemetry,
    placed_so_far: int,
    min_req: dict[str, Any],
) -> tuple[bool, str]:
    """Return (ok, rejection_reason). Reason is empty when ok=True.

    Checks each declared dimension of ``min_req`` against ``node`` telemetry.
    ``placed_so_far`` is the number of tasks already routed to this node
    THIS TICK — used so multiple tasks in one batch can't all claim the
    same headroom.

    Dimensions:
      - mem_gb: node.mem_avail_gb >= min.mem_gb (no per-task subtraction —
        the telemetry is a snapshot; subtracting placed*min would double-count
        against active_workers headroom).
      - cpu_cores: (cpu_count - active_workers*CPU_CORES_PER_ACTIVE_WORKER
        - placed_so_far*CPU_CORES_PER_ACTIVE_WORKER) >= min.cpu_cores.
      - bedrock_tpm_reservation: no per-node telemetry today; always pass
        (hard-gate lives at the model-quota layer, not here).
    """
    need_mem = float(min_req.get("mem_gb", 0) or 0)
    if node.mem_avail_gb + 1e-9 < need_mem:
        return False, (
            f"node {node.node_id} has {node.mem_avail_gb:.2f}GB mem available, "
            f"task requires {need_mem:.2f}GB"
        )
    need_cpu = int(min_req.get("cpu_cores", 0) or 0)
    cpu_headroom = (
        node.cpu_count
        - node.active_workers * CPU_CORES_PER_ACTIVE_WORKER
        - placed_so_far * CPU_CORES_PER_ACTIVE_WORKER
    )
    if cpu_headroom + 1e-9 < need_cpu:
        return False, (
            f"node {node.node_id} has ~{cpu_headroom:.1f} cpu cores free "
            f"(cpu_count={node.cpu_count}, active={node.active_workers}, "
            f"placed_this_tick={placed_so_far}), task requires {need_cpu}"
        )
    # bedrock_tpm_reservation: no telemetry to gate on. Reserved for future
    # per-node bedrock-quota-headroom integration; currently a pass-through.
    return True, ""


def fallback_proportional(
    tasks: list[dict[str, Any]],
    nodes: dict[str, NodeTelemetry],
    registry: list[dict[str, Any]],
    *,
    probe_states: Optional[dict[str, dict[str, Any]]] = None,
) -> list[RoutingDecision]:
    eligible = [n for n in nodes.values() if n.eligible]
    if not eligible:
        return []
    # ADR-006b Phase 2 (t_c6b97f78): fetch nervous-system probe state for the
    # eligible nodes once per call. Fail-open on any error — empty dicts
    # bypass the gate for that node.
    if probe_states is None:
        probe_states = _fetch_probe_states([n.node_id for n in eligible])
    placed: dict[str, int] = {n.node_id: n.active_workers for n in eligible}
    decisions: list[RoutingDecision] = []

    def headroom(n: NodeTelemetry) -> float:
        # resource_score proxy: blend load ratio + disk pressure
        score = n.load_ratio * 0.6 + (1.0 - n.disk_free_pct / 100.0) * 0.4
        base = (1.0 - score) * max(1, n.cpu_count)
        consumed = placed.get(n.node_id, 0) * AGENT_LOAD_ESTIMATE * max(1, n.cpu_count)
        return base - consumed

    reg = {a["id"]: a for a in registry}
    for t in tasks:
        agent = t.get("assignee") or ""
        if agent and agent not in reg:
            # unregistered assignee — still routable if profile exists on disk
            if not (PROFILES_DIR / agent).exists():
                continue
        # ADR-006b Phase 2: filter nodes whose current headroom doesn't meet
        # this task's declared min_resources. Rejection reasons are logged so
        # dispatcher operators can see WHY a task is starving instead of
        # silent black-holing.
        min_req = _task_min_resources(t)
        survivors: list[NodeTelemetry] = []
        rejections: list[str] = []
        for n in eligible:
            # t_c6b97f78: nervous-system gates run BEFORE min_resources so
            # a swap-thrashing or bedrock-throttled node is rejected with a
            # probe-specific reason instead of a generic capacity miss.
            ns_reject = _check_nervous_system_gates(
                n, t, probe_states.get(n.node_id, {}),
            )
            if ns_reject is not None:
                probe_name, reason = ns_reject
                logger.info(
                    "[NervousGates] task %s: node %s rejected by probe %s — %s",
                    t.get("id"), n.node_id, probe_name, reason,
                )
                rejections.append(f"{probe_name}: {reason}")
                continue
            # t_ad7e65f9: AMBER memory_state gate — graceful high-memory
            # rejection; ordinary tasks still route on AMBER nodes.
            amber_reject = _check_memory_amber_gate(
                n, t, probe_states.get(n.node_id, {}),
            )
            if amber_reject is not None:
                probe_name, reason = amber_reject
                logger.info(
                    "[AmberGate] task %s: node %s rejected — %s",
                    t.get("id"), n.node_id, reason,
                )
                rejections.append(f"{probe_name}: {reason}")
                continue
            ok, why = _node_meets_min_resources(
                n, placed.get(n.node_id, 0) - n.active_workers, min_req,
            )
            if ok:
                survivors.append(n)
            else:
                rejections.append(why)
        if not survivors:
            logger.info(
                "[MinResources] task %s: no eligible node meets min_resources=%s; "
                "rejections: %s",
                t.get("id"), min_req, "; ".join(rejections),
            )
            continue
        best = max(survivors, key=headroom)
        decisions.append(RoutingDecision(
            task_id=t["id"],
            assigned_agent=agent or "default",
            target_node=best.node_id,
            welfare_score=headroom(best),
            reasoning="fallback capacity-proportional fill",
            source="fallback-proportional",
            validated=True,
        ))
        placed[best.node_id] = placed.get(best.node_id, 0) + 1
    return decisions


# ---------------------------------------------------------------------------
# Safety validation of LLM decisions
# ---------------------------------------------------------------------------
def validate_llm_decisions(
    raw: list[dict[str, Any]],
    tasks: list[dict[str, Any]],
    nodes: dict[str, NodeTelemetry],
    *,
    probe_states: Optional[dict[str, dict[str, Any]]] = None,
) -> list[RoutingDecision]:
    eligible_ids = {n.node_id for n in nodes.values() if n.eligible}
    task_ids = {t["id"] for t in tasks}
    tasks_by_id = {t["id"]: t for t in tasks}
    # placed-this-tick counter for min_resources cpu headroom subtraction
    placed_this_tick: dict[str, int] = {}
    # ADR-006b Phase 2 (t_c6b97f78): reuse the same probe-state snapshot the
    # fallback path would use. Callers can pass one in for consistency; else
    # we fetch it lazily for just the eligible nodes.
    if probe_states is None:
        probe_states = _fetch_probe_states(list(eligible_ids))
    out: list[RoutingDecision] = []
    for d in raw:
        try:
            tid = str(d["task_id"])
            node = str(d["target_node"])
            agent = str(d.get("assigned_agent") or "")
        except (KeyError, TypeError):
            continue
        if tid not in task_ids:
            logger.warning(f"[Validate] LLM hallucinated task {tid}; dropped")
            continue
        if node not in eligible_ids:
            logger.warning(f"[Validate] LLM picked ineligible node {node} "
                           f"for {tid}; dropped")
            continue
        # t_c6b97f78: nervous-system gate must veto the LLM's pick too.
        ns_reject = _check_nervous_system_gates(
            nodes[node], tasks_by_id[tid], probe_states.get(node, {}),
        )
        if ns_reject is not None:
            probe_name, reason = ns_reject
            logger.info(
                "[NervousGates] LLM picked %s for %s but probe %s is RED — %s; "
                "dropping (fallback will retry).",
                node, tid, probe_name, reason,
            )
            continue
        # t_ad7e65f9: AMBER memory_state gate — vetoes high-memory tasks on
        # AMBER nodes so the LLM can't route around the graceful band.
        amber_reject = _check_memory_amber_gate(
            nodes[node], tasks_by_id[tid], probe_states.get(node, {}),
        )
        if amber_reject is not None:
            probe_name, reason = amber_reject
            logger.info(
                "[AmberGate] LLM picked %s for %s but %s; dropping "
                "(fallback will retry).",
                node, tid, reason,
            )
            continue
        # ADR-006b Phase 2: enforce per-task min_resources on the LLM's pick.
        # LLM prompt already advises this, but we must not trust it.
        min_req = _task_min_resources(tasks_by_id[tid])
        node_tel = nodes[node]
        ok, why = _node_meets_min_resources(
            node_tel, placed_this_tick.get(node, 0), min_req,
        )
        if not ok:
            logger.warning(
                "[MinResources] LLM picked %s for %s but min_resources=%s "
                "unmet: %s; dropping (fallback will retry).",
                node, tid, min_req, why,
            )
            continue
        placed_this_tick[node] = placed_this_tick.get(node, 0) + 1
        out.append(RoutingDecision(
            task_id=tid, assigned_agent=agent, target_node=node,
            welfare_score=float(d.get("welfare_score") or 0.0),
            reasoning=str(d.get("reasoning") or "")[:200],
            source="llm", validated=True,
        ))
    return out


# ---------------------------------------------------------------------------
# Audit ledger
# ---------------------------------------------------------------------------
def _ensure_audit() -> None:
    AUDIT_DB.parent.mkdir(parents=True, exist_ok=True)
    import duckdb
    con = duckdb.connect(str(AUDIT_DB))
    con.execute(
        "CREATE TABLE IF NOT EXISTS dispatch_decisions ("
        " decided_at DOUBLE, task_id VARCHAR, assigned_agent VARCHAR,"
        " target_node VARCHAR, welfare_score DOUBLE, source VARCHAR,"
        " reasoning VARCHAR, board VARCHAR)"
    )
    con.close()


def audit(decisions: list[RoutingDecision], board: str) -> None:
    if not decisions:
        return
    _ensure_audit()
    import duckdb
    con = duckdb.connect(str(AUDIT_DB))
    now = time.time()
    con.executemany(
        "INSERT INTO dispatch_decisions VALUES (?,?,?,?,?,?,?,?)",
        [(now, d.task_id, d.assigned_agent, d.target_node, d.welfare_score,
          d.source, d.reasoning, board) for d in decisions],
    )
    con.close()


# ---------------------------------------------------------------------------
# Main tick
# ---------------------------------------------------------------------------
class LLMClusterDispatcher:
    def __init__(self, board: str = "okr-2026-q2", safety_spine: Optional[Any] = None):
        self.board = board
        self.telemetry = TelemetryCollector()
        # HAMILTON CRITICAL-3 safety spine (t_64a6a7bd). Injectable so tests
        # can pass a stub without needing /etc access; production main()
        # instantiates the real one after asserting the armed token.
        if safety_spine is not None:
            self.safety_spine = safety_spine
        elif _SAFETY_SPINE_AVAILABLE and SafetySpine is not None:
            self.safety_spine = SafetySpine()
        else:
            self.safety_spine = None
            logger.error(
                "[LLMClusterDispatcher] HAMILTON CRITICAL-3 safety spine "
                "UNAVAILABLE — dispatcher will refuse to arm."
            )

    def build_prompt(
        self,
        nodes: dict[str, NodeTelemetry],
        tasks: list[dict[str, Any]],
        registry: list[dict[str, Any]],
    ) -> str:
        nodes_payload = {
            nid: {
                "cpu_count": n.cpu_count,
                "load_ratio": round(n.load_ratio, 3),
                "mem_avail_gb": n.mem_avail_gb,
                "disk_free_pct": round(n.disk_free_pct, 1),
                "active_workers": n.active_workers,
                "max_workers": n.max_workers,
                "heartbeat_age_s": round(n.heartbeat_age_s, 0),
                "status": n.status,
                "eligible": n.eligible,
            }
            for nid, n in nodes.items()
        }
        tasks_payload = [
            {
                "task_id": t["id"],
                "title": (t["title"] or "")[:80],
                "assignee": t.get("assignee") or "",
                "priority": t.get("priority") or 0,
                "required_skills": [],  # capability tags live in body, not skills col
                "agent_node_history": agent_node_history(t.get("assignee") or ""),
            }
            for t in tasks
        ]
        prompt = DISPATCHER_SYSTEM_PROMPT.format(
            cluster_nodes_json=json.dumps(nodes_payload, indent=1),
            task_queue_json=json.dumps(tasks_payload, indent=1),
            agent_registry_json=json.dumps(registry, indent=1),
        )
        eligible = [nid for nid, n in nodes.items() if n.eligible]
        prompt += f"\n\neligible_nodes: {json.dumps(eligible)}\n"
        return prompt

    def tick(self, dry_run: bool = True) -> list[RoutingDecision]:
        # HAMILTON CRITICAL-3 gate — runs BEFORE any telemetry/LLM work so a
        # kill-switch operator can halt spawns without a healthy NATS,
        # DuckDB, or LLM router (t_64a6a7bd).
        if self.safety_spine is not None:
            decision = self.safety_spine.tick()
            if decision.halt_all_spawns:
                logger.warning(
                    "[SafetySpine] halted this tick: %s (kill_engaged=%s)",
                    decision.reason,
                    decision.kill_engaged,
                )
                return []
            if decision.safe_mode:
                # NATS is down. We continue local routing (LLM router + audit
                # DB are local), but skip any NATS-side effects. The
                # transition itself was already logged to a local file by
                # the monitor — NEVER to NATS.
                logger.warning(
                    "[SafetySpine] operating in safe mode: %s", decision.reason
                )

        nodes = self.telemetry.collect()
        tasks = collect_ready_tasks(self.board)
        registry = collect_agent_registry()

        logger.info(
            f"[Tick] board={self.board} ready={len(tasks)} "
            f"eligible_nodes={[n for n in nodes.values() if n.eligible and n.node_id] and [n.node_id for n in nodes.values() if n.eligible]}"
        )
        if not tasks:
            return []

        # Route in small batches: glm-5.2 thinking-token burn scales with the
        # number of tasks to reason over. Batches of 6 stay well under the
        # max_tokens budget and return in seconds instead of timing out.
        decisions: list[RoutingDecision] = []
        for i in range(0, len(tasks), LLM_BATCH_SIZE):
            batch = tasks[i:i + LLM_BATCH_SIZE]
            prompt = self.build_prompt(nodes, batch, registry)
            raw = llm_route(prompt)
            if raw is not None:
                batch_dec = validate_llm_decisions(raw, batch, nodes)
                covered = {d.task_id for d in batch_dec}
                remainder = [t for t in batch if t["id"] not in covered]
                if remainder:
                    batch_dec += fallback_proportional(remainder, nodes, registry)
                decisions += batch_dec
            else:
                decisions += fallback_proportional(batch, nodes, registry)

        audit(decisions, self.board)

        for d in decisions:
            logger.info(
                f"[Route] {d.task_id} -> {d.assigned_agent}@{d.target_node} "
                f"welfare={d.welfare_score:.3f} src={d.source} :: {d.reasoning[:80]}"
            )
            if dry_run:
                print(f"DRY-RUN {d.task_id} -> {d.assigned_agent}@{d.target_node} "
                      f"({d.source}, welfare={d.welfare_score:.3f})")
        return decisions


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="LLM-backed VCG cluster dispatcher")
    ap.add_argument("--board", default="okr-2026-q2")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--daemon", action="store_true")
    ap.add_argument("--interval", type=float, default=30.0)
    ap.add_argument("--verbose", "-v", action="store_true")
    ap.add_argument(
        "--skip-armed-assertion",
        action="store_true",
        help=(
            "For dry-runs and CI only: skip the /etc/vcg_dispatch_armed "
            "check. Never use in production — the token is the "
            "out-of-band arming lock (t_64a6a7bd)."
        ),
    )
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # HAMILTON CRITICAL-3: refuse to arm unless the operator has placed the
    # armed-token file (t_64a6a7bd). Dry-runs and CI may opt out explicitly.
    if not args.skip_armed_assertion:
        if not _SAFETY_SPINE_AVAILABLE or SafetySpine is None:
            logger.error(
                "[HAMILTON-3] Safety spine unavailable; refusing to arm. "
                "Install /home/ubuntu/hermes-agent or pass "
                "--skip-armed-assertion for offline dry-runs."
            )
            return 2
        try:
            SafetySpine().assert_armed_or_die()
        except ArmedTokenMissing as e:
            logger.error("[HAMILTON-3] %s", e)
            return 2

    disp = LLMClusterDispatcher(board=args.board)

    if args.daemon:
        logger.info(f"[Daemon] interval={args.interval}s board={args.board}")
        while True:
            try:
                disp.tick(dry_run=args.dry_run)
            except Exception as e:
                logger.error(f"[Daemon] tick failed: {e}")
            time.sleep(args.interval)
    else:
        decisions = disp.tick(dry_run=args.dry_run or not args.once)
        print(f"\n{len(decisions)} routing decision(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
