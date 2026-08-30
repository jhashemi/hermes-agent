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
import shlex
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

def _worker_api_env_for_remote(target_node: str) -> dict[str, str]:
    """Return the ``HERMES_KANBAN_API_*`` env pairs for a remote-spawn worker.

    Cross-host workers cannot safely open ``kanban.db`` files over NFS
    (see ``hermes_cli.kanban_db.KanbanDbOnNetworkFsError``). Instead,
    when the dispatcher SSH-spawns a worker onto a REMOTE node, we
    export the API endpoint + bearer token so the worker's kanban_*
    tools route over HTTP to the DB owner (hermes2) — never touching
    the DB file directly.

    Config precedence:
      * ``HERMES_KANBAN_REMOTE_API_URL_<UPPER_NODE>`` (per-target override)
      * ``HERMES_KANBAN_REMOTE_API_URL``              (default for all)
      * ``kanban.remote_api_url`` in config.yaml      (persistent default)

    Same three-tier lookup for the token:
      * ``HERMES_KANBAN_REMOTE_API_TOKEN_<UPPER_NODE>``
      * ``HERMES_KANBAN_REMOTE_API_TOKEN``
      * ``kanban.remote_api_token`` in config.yaml

    Returns an empty dict when neither URL nor token is configured — the
    worker then falls back to the legacy local-open path, which is what
    the NFS gate will refuse. That is the intended safety net: opt-in
    plumbing failure surfaces as a clean refusal instead of silent
    resumed corruption.

    Never returns partial state: if either URL or token is missing when
    the other is set, we log a warning and return empty. Half-configured
    remote workers must never appear to be working.
    """
    node_key = target_node.upper().replace("-", "_")

    def _pick(prefix: str, cfg_key: str) -> str:
        # 1. Per-target env override.
        per_target = os.environ.get(f"{prefix}_{node_key}", "").strip()
        if per_target:
            return per_target
        # 2. Global env default.
        glob = os.environ.get(prefix, "").strip()
        if glob:
            return glob
        # 3. Persistent config.
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:
            return ""
        kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg, dict) else {}
        val = kanban_cfg.get(cfg_key, "") or ""
        return str(val).strip()

    url = _pick("HERMES_KANBAN_REMOTE_API_URL", "remote_api_url")
    token = _pick("HERMES_KANBAN_REMOTE_API_TOKEN", "remote_api_token")

    if not url and not token:
        return {}  # legacy local-open behaviour (NFS gate will refuse — by design).
    if not (url and token):
        # Half-configured — refuse to produce env so the operator sees a
        # loud failure instead of a silent bypass of the auth token.
        logger.warning(
            "[cluster_dispatch] Remote kanban API env is half-configured for %s: "
            "url=%r token=%s. Refusing to export partial env; remote workers "
            "will fall back to the local-open path (which the NFS gate refuses "
            "when the workspace sits on an unsafe mount).",
            target_node, url, "<set>" if token else "<empty>",
        )
        return {}

    return {
        "HERMES_KANBAN_API_URL": url,
        "HERMES_KANBAN_API_TOKEN": token,
    }


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
        env_extra: Extra environment variables to forward via SSH. Any
            worker API env keys resolved from :func:`_worker_api_env_for_remote`
            are merged in AFTER these, so callers passing an explicit
            ``HERMES_KANBAN_API_URL``/``_TOKEN`` are honoured verbatim.

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
    # shlex.quote: workspace paths contain spaces; task/board ids and any
    # caller-influenced value must never break out of the quoting (the
    # t_dda5868a suite proves the payload with shell metacharacters).
    env_lines.append(f"export HERMES_KANBAN_TASK={shlex.quote(str(task_id))}")
    env_lines.append(f"export HERMES_KANBAN_WORKSPACE={shlex.quote(str(workspace))}")
    env_lines.append(f"export HERMES_KANBAN_BOARD={shlex.quote(str(board))}")

    # Merge caller-supplied env with the resolved API endpoint env. The
    # resolved values LAST so an explicit test/override in env_extra can
    # be strengthened (not overridden) — callers who want to force a
    # different endpoint can set the resolver's own inputs instead.
    #
    # The API token especially can contain shell metacharacters (base64
    # padding, JWT dots, random URL-safe alphabet) that WOULD be safe under
    # ``export FOO=bar`` in most cases but must not be relied on across all
    # token formats. Shell-quote every merged value so the SSH remote-shell
    # sees the literal string. (The three env vars above ship raw for
    # bug-for-bug parity with the original implementation; task_id/board/
    # workspace are already validated upstream.)
    merged_env: dict[str, str] = {}
    if env_extra:
        merged_env.update(env_extra)
    merged_env.update(_worker_api_env_for_remote(target_node))
    for k, v in merged_env.items():
        env_lines.append(f"export {k}={shlex.quote(str(v))}")

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

    If cluster dispatch is enabled in config **and** ``board`` appears in
    the ``kanban.cluster_dispatch_board`` whitelist, returns a
    ``ClusterNodeRouter`` backed by ``LLMClusterDispatcher``. Otherwise
    returns the local-only router (always None), meaning every task on
    that board spawns on the local node.

    Config keys:
      - ``kanban.cluster_dispatch``       (bool, default: False) — master gate
      - ``kanban.cluster_dispatch_board`` (list[str], default: []) — per-board
        whitelist. When empty or missing, NO board is cluster-routed even if
        ``cluster_dispatch`` is True — the whitelist is opt-in per board.

    Env override:
      - ``HERMES_CLUSTER_DISPATCH`` in {0, false, no, off} disables globally.
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

    # Per-board whitelist gate. The master switch is on, but each board must
    # be explicitly opted in via ``kanban.cluster_dispatch_board``. This is
    # the fix for the wiring bug where every board (default, campaignforge,
    # etc.) was routed through the LLM router regardless of the whitelist
    # config that already existed in config.yaml.
    raw_whitelist = kanban_cfg.get("cluster_dispatch_board", [])
    if not isinstance(raw_whitelist, (list, tuple)):
        logger.warning(
            "[ClusterNodeRouter] kanban.cluster_dispatch_board must be a list; "
            "got %r — treating as empty (no board cluster-routed)",
            type(raw_whitelist).__name__,
        )
        whitelist: set[str] = set()
    else:
        whitelist = {str(b) for b in raw_whitelist}

    if board not in whitelist:
        logger.info(
            "[ClusterNodeRouter] board=%r not in cluster_dispatch_board "
            "whitelist %r; routing locally",
            board, sorted(whitelist),
        )
        return local_node_router

    logger.info(
        "[ClusterNodeRouter] Cluster dispatch enabled and board=%r whitelisted; "
        "initializing LLM router", board,
    )
    return ClusterNodeRouter(board=board)

# ---------------------------------------------------------------------------
# Scope lint: detect boards with active work outside effective dispatch scope
# ---------------------------------------------------------------------------
#
# Two operator failure modes we want to surface:
#
# 1. ``cluster_dispatch=True`` but a board with active tickets is NOT in
#    ``cluster_dispatch_board``. Cluster routing will fall back to local for
#    those boards even though the operator opted into cluster dispatch. This
#    is a legit config-drift signal: the operator added a new board and
#    forgot to whitelist it.
#
# 2. ``cluster_dispatch=False`` (local-only) AND ``cluster_dispatch_board``
#    whitelist exists but omits boards with active tickets. Not currently a
#    functional issue (local dispatch touches every board), but it is
#    informational: flipping ``cluster_dispatch=True`` would leave those
#    boards routed locally by accident. Reported as INFO, not WARN.
#
# The dispatcher iterates every board via ``list_boards`` regardless of
# whitelist, so no board is ever fully "unreached" today. If that changes
# (a future ``kanban.dispatch_scope`` knob restricting board enumeration),
# extend this helper to reflect the new semantics — the doctor check and
# gateway warn read from a single source of truth.


def _active_ticket_count(db_path) -> int:
    """Return count of tasks in states that need dispatcher attention.

    Uses a direct sqlite3 read (no ORM, no kanban_db import churn) so this
    helper is safe to call from doctor before the CLI is fully wired.
    Returns 0 on any error — a missing/corrupt DB should never break the
    doctor pass; other checks already surface those.
    """
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM tasks "
                "WHERE status IN ('todo', 'ready', 'running')"
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            conn.close()
    except Exception:
        return 0


def compute_out_of_scope_boards(
    cfg: dict | None = None,
) -> list[dict]:
    """Enumerate boards with active tickets that fall outside effective dispatch scope.

    Returns a list of dicts sorted by (severity_desc, slug_asc). Each entry::

        {
            "slug": "campaignforge-phase5",
            "active_count": 9,
            "severity": "warn" | "info",
            "reason": "cluster_dispatch=true but board not in cluster_dispatch_board whitelist",
            "fix_hints": ["Add 'campaignforge-phase5' to kanban.cluster_dispatch_board", ...],
        }

    ``cfg`` is the loaded config dict. When None, loads via
    ``hermes_cli.config.load_config``. On config-load failure returns [].

    Behaviour:
    * ``cluster_dispatch=False``: reports boards with active tickets that
      have a non-empty whitelist that OMITS them, as ``severity='info'``.
      No warn — the local dispatcher still processes every board.
    * ``cluster_dispatch=True``: reports boards with active tickets NOT on
      the whitelist as ``severity='warn'`` — the LLM cluster router falls
      back to local for those boards, which is likely operator drift.
    """
    if cfg is None:
        try:
            from hermes_cli.config import load_config
            cfg = load_config()
        except Exception:
            return []
    if not isinstance(cfg, dict):
        return []

    kanban_cfg = cfg.get("kanban", {}) if isinstance(cfg.get("kanban"), dict) else {}
    cluster_enabled = bool(kanban_cfg.get("cluster_dispatch", False))
    raw_whitelist = kanban_cfg.get("cluster_dispatch_board")
    if isinstance(raw_whitelist, (list, tuple)):
        whitelist = {str(x).strip() for x in raw_whitelist if str(x).strip()}
    else:
        whitelist = set()

    try:
        from hermes_cli import kanban_db as _kb
    except Exception:
        return []
    try:
        boards = _kb.list_boards(include_archived=False)
    except Exception:
        return []

    out: list[dict] = []
    for meta in boards:
        slug = meta.get("slug") if isinstance(meta, dict) else None
        if not slug:
            continue
        # Compute the DB path from the board slug directly rather than via
        # ``kanban_db_path(slug)`` — that function honours HERMES_KANBAN_DB,
        # which the dispatcher pins to a single board's DB for its workers,
        # so calling it here from a worker would return the SAME path for
        # every slug. Use the disk layout directly.
        try:
            if slug == getattr(_kb, "DEFAULT_BOARD", "default"):
                db_path = _kb.kanban_home() / "kanban.db"
            else:
                db_path = _kb.boards_root() / slug / "kanban.db"
        except Exception:
            continue
        if not db_path.exists():
            continue
        active = _active_ticket_count(db_path)
        if active <= 0:
            continue

        in_whitelist = slug in whitelist

        if cluster_enabled:
            if whitelist and not in_whitelist:
                out.append({
                    "slug": slug,
                    "active_count": active,
                    "severity": "warn",
                    "reason": (
                        "cluster_dispatch=true but board not in "
                        "kanban.cluster_dispatch_board whitelist — LLM cluster "
                        "routing falls back to local for this board"
                    ),
                    "fix_hints": [
                        f"add '{slug}' to kanban.cluster_dispatch_board in config.yaml",
                        f"or remove active tickets from this board if it is retired",
                    ],
                })
        else:
            if whitelist and not in_whitelist:
                out.append({
                    "slug": slug,
                    "active_count": active,
                    "severity": "info",
                    "reason": (
                        "cluster_dispatch=false; whitelist exists but omits "
                        "this board — enabling cluster_dispatch would route "
                        "this board locally by fallback"
                    ),
                    "fix_hints": [
                        f"add '{slug}' to kanban.cluster_dispatch_board (pre-emptive)",
                        "or leave as-is if this board should stay local",
                    ],
                })

    _sev_order = {"warn": 0, "info": 1}
    out.sort(key=lambda e: (_sev_order.get(e.get("severity", "info"), 9), e["slug"]))
    return out


def log_out_of_scope_boards_at_startup() -> None:
    """Emit gateway startup log lines for each out-of-scope board.

    Called by ``_kanban_dispatcher_watcher`` after config load. Wrapped in a
    broad try/except so a corrupt board DB never blocks the dispatcher.
    """
    try:
        entries = compute_out_of_scope_boards()
    except Exception as exc:
        logger.debug("kanban dispatcher: scope lint failed (%s); skipping", exc)
        return
    for e in entries:
        level = logging.WARNING if e.get("severity") == "warn" else logging.INFO
        logger.log(
            level,
            "kanban dispatcher: board %s has %d active ticket(s) outside "
            "cluster dispatch scope — %s; fix: %s",
            e["slug"],
            e["active_count"],
            e["reason"],
            "; ".join(e.get("fix_hints", [])),
        )
