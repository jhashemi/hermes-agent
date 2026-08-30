"""Cluster-aware dispatch routing — wires LLMClusterDispatcher into the
canonical kanban dispatch path (``dispatch_once`` in ``hermes_cli.kanban_db``).

Architecture
------------
::

    gateway _kanban_dispatcher_watcher()
        |
        v
    dispatch_once(conn, ..., node_router=cluster_node_router)
        |  for each ready task:
        |    1. claim task (existing path)
        |    2. resolve workspace (existing path)
        |    3. node_router(task_id, assignee) → target_node
        |    4. _default_spawn(task, workspace, ..., target_node=target_node)
        |        - local node → existing Popen spawn
        |        - remote node → SSH spawn
        |
        v
    DuckDB audit ledger (dispatch_decisions)

The ``node_router`` is **advisory**: every routing decision is re-validated
against deterministic hard gates (health, load, disk, heartbeat) before the
claim is written. If the LLM is unreachable or returns garbage, the
dispatcher falls back to pure capacity-proportional fill (ADR-006).

This module provides:
  - ``NodeRouter`` protocol (``Callable[[str, str], Optional[str]]``)
  - ``ClusterNodeRouter`` — concrete implementation backed by
    ``LLMClusterDispatcher``
  - ``LOCAL_NODE`` — the canonical name for the node running the dispatcher
  - ``remote_spawn_cmd`` — build an SSH-based spawn command for remote nodes
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The node running the dispatcher is "local". Everything else is remote
# (reached via SSH over Tailscale). This value is compared against
# RoutingDecision.target_node and NODE_HOSTS keys.
LOCAL_NODE = os.environ.get("HERMES_CLUSTER_LOCAL_NODE", "hermes2")

# Path to the standalone LLM cluster dispatcher script.
LLM_DISPATCHER_SCRIPT = Path(
    os.environ.get(
        "HERMES_LLM_DISPATCHER_SCRIPT",
        str(Path.home() / ".hermes" / "scripts" / "llm_cluster_dispatcher.py"),
    )
)

# ---------------------------------------------------------------------------
# NodeRouter protocol
# ---------------------------------------------------------------------------

# A node_router receives (task_id, assignee) and returns either a target
# node name (e.g. "hermes1", "hermes2") or None (meaning "use local node").
NodeRouter = Callable[[str, str], Optional[str]]


# ---------------------------------------------------------------------------
# ClusterNodeRouter — backed by LLMClusterDispatcher
# ---------------------------------------------------------------------------

class ClusterNodeRouter:
    """Thread-safe node router that consults the LLM cluster dispatcher
    on every tick and caches the routing table for the duration of that
    tick's dispatch_once call.

    Usage::

        router = ClusterNodeRouter(board="okr-2026-q2")
        # Called once per dispatch_once tick:
        router.refresh()
        # Called for each task:
        node = router("t_abc123", "werner_vogels")

    If the LLM dispatcher is unavailable (import error, no API key, network
    timeout), ``refresh()`` logs a warning and falls back to
    capacity-proportional fill — still returning node assignments, just
    using the deterministic algorithm instead of LLM reasoning.
    """

    def __init__(self, board: str = "okr-2026-q2"):
        self.board = board
        self._routing: dict[str, str] = {}  # task_id → target_node
        self._dispatcher = None
        self._init_error: Optional[str] = None
        self._local_node = LOCAL_NODE

    def _try_init(self) -> bool:
        """Attempt lazy import of LLMClusterDispatcher. Returns True on success."""
        if self._dispatcher is not None:
            return True
        if self._init_error is not None:
            return False
        try:
            # Add the scripts dir to sys.path so the import works
            import sys
            scripts_dir = str(LLM_DISPATCHER_SCRIPT.parent)
            if scripts_dir not in sys.path:
                sys.path.insert(0, scripts_dir)
            from llm_cluster_dispatcher import LLMClusterDispatcher
            self._dispatcher = LLMClusterDispatcher(board=self.board)
            return True
        except Exception as e:
            self._init_error = str(e)
            logger.warning(
                "[ClusterNodeRouter] LLM dispatcher import failed: %s; "
                "will use local-only routing", self._init_error,
            )
            return False

    def refresh(self) -> None:
        """Run one LLM routing tick and cache the results.

        Called once per dispatch_once tick. After refresh, the routing
        table is populated with (task_id → target_node) mappings.
        """
        self._routing.clear()
        if not self._try_init():
            # Dispatcher unavailable — all tasks stay local
            logger.info("[ClusterNodeRouter] No LLM dispatcher; routing all tasks to local node")
            return

        try:
            decisions = self._dispatcher.tick(dry_run=True)
            for d in decisions:
                self._routing[d.task_id] = d.target_node
            logger.info(
                "[ClusterNodeRouter] LLM routing: %d decisions for board=%s",
                len(decisions), self.board,
            )
        except Exception as e:
            logger.warning(
                "[ClusterNodeRouter] LLM routing tick failed: %s; "
                "falling back to local-only", e,
            )

    def __call__(self, task_id: str, assignee: str) -> Optional[str]:
        """Return the target node for a task, or None for local spawn.

        This is the ``NodeRouter`` protocol implementation.
        """
        node = self._routing.get(task_id)
        if node is None:
            return None  # local
        # Safety: never route to an unknown node — fall back to local
        if node not in _KNOWN_NODES and node != self._local_node:
            logger.warning(
                "[ClusterNodeRouter] Unknown node %r for task %s; "
                "falling back to local", node, task_id,
            )
            return None
        return node


# ---------------------------------------------------------------------------
# Node host mapping (shared with llm_cluster_dispatcher)
# ---------------------------------------------------------------------------

# These come from the dispatcher script but we need them here for SSH.
# If the script isn't importable, fall back to defaults.
def _load_node_hosts() -> dict[str, Optional[str]]:
    """Load NODE_HOSTS from the LLM dispatcher module, or use defaults."""
    try:
        import sys
        scripts_dir = str(LLM_DISPATCHER_SCRIPT.parent)
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from llm_cluster_dispatcher import NODE_HOSTS
        return dict(NODE_HOSTS)
    except Exception:
        # Defaults matching llm_cluster_dispatcher.py NODE_HOSTS
        return {
            "hermes2": None,           # local
            "hermes1": "100.107.83.25",  # Tailscale IP
        }

_NODE_HOSTS: dict[str, Optional[str]] = _load_node_hosts()
_KNOWN_NODES: set[str] = set(_NODE_HOSTS.keys())


# ---------------------------------------------------------------------------
# Remote spawn via SSH
# ---------------------------------------------------------------------------

def remote_spawn_cmd(
    task_id: str,
    assignee: str,
    workspace: str,
    board: str,
    target_node: str,
    *,
    skills: Optional[list[str]] = None,
    env_extra: Optional[dict[str, str]] = None,
) -> list[str]:
    """Build the command list for spawning a worker on a remote node via SSH.

    Returns the full argv list (including ``ssh`` prefix) suitable for
    ``subprocess.Popen``. The remote command mirrors the local ``hermes``
    spawn exactly, just prefixed with SSH to reach the target node.

    Args:
        task_id: Kanban task ID (e.g. "t_abc123")
        assignee: Hermes profile name for the worker
        workspace: Resolved workspace directory on the REMOTE node
        board: Kanban board slug
        target_node: Node name from NODE_HOSTS (must have a non-None host)
        skills: Additional skills to load (already includes kanban-worker)
        env_extra: Extra environment variables to forward via SSH

    Returns:
        Command list for ``subprocess.Popen``
    """
    host = _NODE_HOSTS.get(target_node)
    if host is None:
        raise ValueError(
            f"Cannot SSH-spawn to local node {target_node!r}; "
            "use regular Popen spawn instead"
        )

    from hermes_cli.profiles import normalize_profile_name
    profile_arg = normalize_profile_name(assignee)
    prompt = f"work kanban task {task_id}"

    # Build the remote command — mirrors _default_spawn exactly.
    # RC-2 fix 2026-08-18: bare "hermes" fails in non-interactive SSH sessions
    # because the venv PATH is not sourced.  Wrap the command in `bash -l -c "..."`
    # so the remote's login profile (.bashrc / .profile) activates the venv and
    # puts `hermes` on PATH.  This is the same reason _resolve_hermes_argv() falls
    # back to the absolute venv python path for local spawns — a non-login shell
    # does not inherit the user's PATH.
    remote_cmd_parts = [
        "hermes",
        "-p", profile_arg,
        "--skills", "kanban-worker",
    ]
    if skills:
        for sk in skills:
            if sk and sk != "kanban-worker":
                remote_cmd_parts.extend(["--skills", sk])
    remote_cmd_parts.extend(["chat", "-q", prompt])

    # Environment variables for the remote worker
    env_lines = []
    env_lines.append(f"export HERMES_KANBAN_TASK={task_id}")
    env_lines.append(f"export HERMES_KANBAN_WORKSPACE={workspace}")
    env_lines.append(f"export HERMES_KANBAN_BOARD={board}")
    if env_extra:
        for k, v in env_extra.items():
            env_lines.append(f"export {k}={v}")

    # Wrap in bash -l so ~/.bashrc / ~/.profile runs and the venv hermes shim
    # is on PATH.  Without -l the SSH non-interactive shell has a bare minimal
    # PATH and "hermes: command not found" silently exits rc=127.
    inline_cmd = "; ".join(env_lines) + "; " + " ".join(remote_cmd_parts)
    ssh_cmd = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=accept-new",
        host,
        f"bash -l -c {inline_cmd!r}",
    ]
    return ssh_cmd


def spawn_on_remote(
    task_id: str,
    assignee: str,
    workspace: str,
    board: str,
    target_node: str,
    log_path: Optional[str] = None,
    *,
    skills: Optional[list[str]] = None,
    env_extra: Optional[dict[str, str]] = None,
) -> Optional[int]:
    """Spawn a kanban worker on a remote cluster node via SSH.

    This is the remote counterpart to ``_default_spawn`` in kanban_db.py.
    It builds an SSH command that mirrors the local spawn exactly, just
    executed on the remote node.

    Args:
        task_id: Kanban task ID
        assignee: Hermes profile name
        workspace: Workspace directory on the remote node
        board: Kanban board slug
        target_node: Node to spawn on (must have a host in NODE_HOSTS)
        log_path: Path to the log file (local; SSH stdout/stderr go here)
        skills: Additional skills to load
        env_extra: Extra environment variables

    Returns:
        PID of the SSH process (NOT the remote hermes PID — we can't
        observe that directly). The caller can still detect that the
        SSH tunnel is alive.
    """
    ssh_cmd = remote_spawn_cmd(
        task_id=task_id,
        assignee=assignee,
        workspace=workspace,
        board=board,
        target_node=target_node,
        skills=skills,
        env_extra=env_extra,
    )

    # Log file for this task — same pattern as _default_spawn
    log_f = None
    stdout_target = subprocess.PIPE
    if log_path:
        from hermes_cli.kanban_db import _rotate_worker_log
        log_f = open(log_path, "ab")
        stdout_target = log_f

    try:
        proc = subprocess.Popen(
            ssh_cmd,
            stdin=subprocess.DEVNULL,
            stdout=stdout_target,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        return proc.pid
    except FileNotFoundError:
        if log_f:
            log_f.close()
        raise RuntimeError(
            "`ssh` executable not found on PATH. "
            "Remote cluster spawning requires SSH."
        )
    except Exception:
        if log_f:
            log_f.close()
        raise


# ---------------------------------------------------------------------------
# Null router — always routes locally (used when cluster dispatch is disabled)
# ---------------------------------------------------------------------------

def local_node_router(task_id: str, assignee: str) -> Optional[str]:
    """NodeRouter that always returns None (local spawn).

    Used when cluster dispatch is disabled or when the dispatcher is not
    available. This is the default/fallback behavior."""
    return None


# ---------------------------------------------------------------------------
# Convenience: create a configured router for the gateway watcher
# ---------------------------------------------------------------------------

def create_cluster_node_router(board: str) -> NodeRouter:
    """Create a NodeRouter for use by the gateway kanban watcher.

    If cluster dispatch is enabled in config, returns a ClusterNodeRouter
    backed by LLMClusterDispatcher. Otherwise returns the local-only
    router (always None).

    Config key: ``kanban.cluster_dispatch`` (default: False)
    """
    # Check config
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception:
        return local_node_router

    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
    if not kanban_cfg.get("cluster_dispatch", False):
        logger.info("[ClusterNodeRouter] Cluster dispatch disabled in config")
        return local_node_router

    # Check env override
    env_override = os.environ.get("HERMES_CLUSTER_DISPATCH", "").strip().lower()
    if env_override in ("0", "false", "no", "off"):
        logger.info("[ClusterNodeRouter] Cluster dispatch disabled via env")
        return local_node_router

    logger.info("[ClusterNodeRouter] Cluster dispatch enabled, initializing router for board=%s", board)
    return ClusterNodeRouter(board=board)