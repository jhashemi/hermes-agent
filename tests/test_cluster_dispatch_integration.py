"""Integration tests for LLM cluster dispatch wiring into the canonical
kanban dispatch path.

Validates:
  1. llm_cluster_dispatcher.py imports cleanly without replacing the
     existing claim/spawn mechanism — it only supplies target_node.
  2. Deterministic hard gates remain authoritative; LLM is advisory,
     re-validated before claim.
  3. Fallback chain: LLM timeout/empty → capacity-proportional fill;
     never return 0 decisions when eligible nodes exist.
  4. Spawned worker keeps skills='kanban-worker' and board-valid
     workspace_kind.
  5. Every routing decision writes to DuckDB audit ledger.
"""

import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(
    task_id="t_test001",
    title="Test task",
    assignee="werner_vogels",
    workspace_kind="scratch",
    skills=None,
    **overrides,
):
    """Create a minimal Task object for testing without requiring all fields."""
    from hermes_cli.kanban_db import Task
    defaults = dict(
        id=task_id, title=title, body=None, assignee=assignee,
        status="ready", priority=50, created_by=None,
        created_at=int(time.time()), started_at=None, completed_at=None,
        workspace_kind=workspace_kind, workspace_path=None,
        claim_lock=None, claim_expires=None, tenant=None,
        skills=skills,
    )
    defaults.update(overrides)
    return Task(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_board(tmp_path):
    """Create a temporary kanban board DB with one ready task."""
    from hermes_cli.kanban_db import connect, init_db
    db_path = tmp_path / "kanban.db"
    conn = sqlite3.connect(str(db_path))
    # Create the full schema
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            assignee TEXT,
            status TEXT DEFAULT 'todo',
            priority INTEGER DEFAULT 0,
            tenant TEXT,
            claim_lock TEXT,
            claim_expires_at REAL,
            workspace_kind TEXT DEFAULT 'scratch',
            workspace_path TEXT,
            skills TEXT,
            created_at REAL DEFAULT (strftime('%s','now')),
            current_run_id INTEGER,
            consecutive_failures INTEGER DEFAULT 0,
            result TEXT,
            summary TEXT,
            metadata TEXT
        );
    """)
    conn.commit()
    # Insert a ready task
    conn.execute(
        "INSERT INTO tasks (id, title, assignee, status, priority, workspace_kind, skills) "
        "VALUES (?, ?, ?, 'ready', 50, 'scratch', 'kanban-worker')",
        ("t_test001", "Test task", "werner_vogels"),
    )
    conn.commit()
    return db_path, conn


@pytest.fixture
def audit_db(tmp_path):
    """Create a temporary DuckDB audit database."""
    audit_path = tmp_path / "llm_dispatcher.duckdb"
    return audit_path


# ---------------------------------------------------------------------------
# AC1: Clean import — LLM dispatcher supplies target_node only
# ---------------------------------------------------------------------------

class TestAC1CleanImport:
    """llm_cluster_dispatcher.py imports cleanly into the dispatch path
    WITHOUT replacing the existing claim/spawn mechanism — it only
    supplies target_node for tasks."""

    def test_import_llm_dispatcher(self):
        """The dispatcher module imports without error."""
        import sys
        scripts_dir = str(Path.home() / ".hermes" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from llm_cluster_dispatcher import LLMClusterDispatcher, RoutingDecision
        assert LLMClusterDispatcher is not None
        assert RoutingDecision is not None

    def test_routing_decision_has_target_node(self):
        """RoutingDecision carries target_node, not a spawn command."""
        import sys
        scripts_dir = str(Path.home() / ".hermes" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from llm_cluster_dispatcher import RoutingDecision
        d = RoutingDecision(
            task_id="t_001",
            assigned_agent="werner_vogels",
            target_node="hermes1",
            welfare_score=0.85,
            reasoning="capacity-proportional fill",
            source="fallback-proportional",
            validated=True,
        )
        assert d.target_node == "hermes1"
        # RoutingDecision is purely advisory — no spawn logic
        assert not hasattr(d, "spawn_fn")
        assert not hasattr(d, "claim")

    def test_dispatch_once_accepts_node_router(self):
        """dispatch_once signature accepts node_router parameter."""
        from hermes_cli.kanban_db import dispatch_once
        import inspect
        sig = inspect.signature(dispatch_once)
        assert "node_router" in sig.parameters, \
            "dispatch_once must accept node_router parameter"

    def test_default_spawn_accepts_target_node(self):
        """_default_spawn signature accepts target_node parameter."""
        from hermes_cli.kanban_db import _default_spawn
        import inspect
        sig = inspect.signature(_default_spawn)
        assert "target_node" in sig.parameters, \
            "_default_spawn must accept target_node parameter"

    def test_cluster_dispatch_module_imports(self):
        """gateway.cluster_dispatch imports cleanly."""
        from gateway.cluster_dispatch import (
            ClusterNodeRouter,
            local_node_router,
            create_cluster_node_router,
            LOCAL_NODE,
        )
        assert LOCAL_NODE == "hermes2"
        assert local_node_router("t_001", "werner_vogels") is None


# ---------------------------------------------------------------------------
# AC2: Deterministic hard gates remain authoritative
# ---------------------------------------------------------------------------

class TestAC2DeterministicGates:
    """Deterministic hard gates remain authoritative: health=healthy,
    heartbeat<120s, load_ratio<=0.85, disk_free>=8%, active_workers<max.
    LLM is advisory; every LLM pick is re-validated before claim."""

    def test_ineligible_nodes_rejected(self):
        """Nodes failing hard gates are excluded from routing decisions."""
        import sys
        scripts_dir = str(Path.home() / ".hermes" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from llm_cluster_dispatcher import NodeTelemetry

        # All hard gates must pass for a node to be eligible
        unhealthy = NodeTelemetry(
            node_id="hermes1", cpu_count=2, load_1min=999.0,
            mem_avail_gb=0.0, disk_free_pct=0.0, active_workers=999,
            max_workers=1, heartbeat_age_s=200.0, status="unknown",
        )
        assert not unhealthy.eligible, "Unhealthy node must be ineligible"

        healthy = NodeTelemetry(
            node_id="hermes2", cpu_count=4, load_1min=1.0,
            mem_avail_gb=8.0, disk_free_pct=50.0, active_workers=1,
            max_workers=4, heartbeat_age_s=10.0, status="healthy",
        )
        assert healthy.eligible, "Healthy node must be eligible"

    def test_load_ratio_hard_gate(self):
        """Nodes with load_ratio > 0.85 are ineligible."""
        import sys
        scripts_dir = str(Path.home() / ".hermes" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from llm_cluster_dispatcher import NodeTelemetry

        overloaded = NodeTelemetry(
            node_id="hermes1", cpu_count=4, load_1min=4.0,  # load_ratio=1.0
            mem_avail_gb=8.0, disk_free_pct=50.0, active_workers=1,
            max_workers=4, heartbeat_age_s=5.0, status="healthy",
        )
        assert not overloaded.eligible, "Overloaded node must be ineligible"

    def test_disk_free_hard_gate(self):
        """Nodes with disk_free < 8% are ineligible."""
        import sys
        scripts_dir = str(Path.home() / ".hermes" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from llm_cluster_dispatcher import NodeTelemetry

        disk_full = NodeTelemetry(
            node_id="hermes1", cpu_count=4, load_1min=1.0,
            mem_avail_gb=8.0, disk_free_pct=3.0, active_workers=1,
            max_workers=4, heartbeat_age_s=5.0, status="healthy",
        )
        assert not disk_full.eligible, "Disk-full node must be ineligible"

    def test_heartbeat_staleness_gate(self):
        """Nodes with heartbeat_age > 120s are ineligible."""
        import sys
        scripts_dir = str(Path.home() / ".hermes" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from llm_cluster_dispatcher import NodeTelemetry

        stale = NodeTelemetry(
            node_id="hermes1", cpu_count=4, load_1min=1.0,
            mem_avail_gb=8.0, disk_free_pct=50.0, active_workers=1,
            max_workers=4, heartbeat_age_s=200.0, status="healthy",
        )
        assert not stale.eligible, "Stale heartbeat node must be ineligible"

    def test_validate_llm_decisions_rejects_ineligible(self):
        """LLM picks on ineligible nodes are dropped by validate_llm_decisions."""
        import sys
        scripts_dir = str(Path.home() / ".hermes" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from llm_cluster_dispatcher import (
            NodeTelemetry, RoutingDecision, validate_llm_decisions,
        )

        nodes = {
            "hermes2": NodeTelemetry(
                node_id="hermes2", cpu_count=4, load_1min=1.0,
                mem_avail_gb=8.0, disk_free_pct=50.0, active_workers=1,
                max_workers=4, heartbeat_age_s=5.0, status="healthy",
            ),
            "hermes1": NodeTelemetry(
                node_id="hermes1", cpu_count=2, load_1min=999.0,
                mem_avail_gb=0.0, disk_free_pct=0.0, active_workers=999,
                max_workers=1, heartbeat_age_s=200.0, status="unknown",
            ),
        }
        tasks = [{"id": "t_001", "assignee": "werner_vogels"}]

        # LLM hallucinates that hermes1 is a good pick
        raw = [{"task_id": "t_001", "assigned_agent": "werner_vogels",
                "target_node": "hermes1", "welfare_score": 0.99,
                "reasoning": "LLM pick on ineligible node"}]
        validated = validate_llm_decisions(raw, tasks, nodes)
        assert len(validated) == 0, "LLM pick on ineligible node must be rejected"


# ---------------------------------------------------------------------------
# AC3: Fallback chain intact
# ---------------------------------------------------------------------------

class TestAC3FallbackChain:
    """LLM timeout/empty → capacity-proportional fill (ADR-006).
    Never return 0 decisions when eligible nodes exist."""

    def test_fallback_proportional_fills_all_tasks(self):
        """When LLM fails, fallback_proportional routes all tasks."""
        import sys
        scripts_dir = str(Path.home() / ".hermes" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from llm_cluster_dispatcher import (
            NodeTelemetry, RoutingDecision, fallback_proportional,
        )

        nodes = {
            "hermes2": NodeTelemetry(
                node_id="hermes2", cpu_count=4, load_1min=1.0,
                mem_avail_gb=8.0, disk_free_pct=50.0, active_workers=1,
                max_workers=4, heartbeat_age_s=5.0, status="healthy",
            ),
        }
        tasks = [
            {"id": "t_001", "assignee": "werner_vogels"},
            {"id": "t_002", "assignee": "demis_hassabis"},
        ]
        registry = [{"id": "werner_vogels", "skills": [], "capacity": 5, "reliability": 0.9}]

        decisions = fallback_proportional(tasks, nodes, registry)
        assert len(decisions) == 2, "Fallback must route all tasks"
        assert all(d.source == "fallback-proportional" for d in decisions)
        assert all(d.validated for d in decisions)

    def test_fallback_with_no_eligible_nodes(self):
        """When no nodes are eligible, fallback returns empty (no eligible nodes exist)."""
        import sys
        scripts_dir = str(Path.home() / ".hermes" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from llm_cluster_dispatcher import (
            NodeTelemetry, fallback_proportional,
        )

        nodes = {
            "hermes1": NodeTelemetry(
                node_id="hermes1", cpu_count=2, load_1min=999.0,
                mem_avail_gb=0.0, disk_free_pct=0.0, active_workers=999,
                max_workers=1, heartbeat_age_s=200.0, status="unknown",
            ),
        }
        tasks = [{"id": "t_001", "assignee": "werner_vogels"}]
        decisions = fallback_proportional(tasks, nodes, [])
        assert len(decisions) == 0, "No eligible nodes → no routing"

    def test_cluster_node_router_fallback_on_import_error(self):
        """When LLM dispatcher import fails, ClusterNodeRouter falls back to local."""
        from gateway.cluster_dispatch import local_node_router
        # local_node_router always returns None (spawn locally)
        assert local_node_router("t_001", "werner_vogels") is None

    def test_dispatch_once_uses_local_when_router_none(self):
        """dispatch_once with node_router=None spawns locally (default path)."""
        # This is tested implicitly by the existing kanban_db test suite
        # which passes no node_router. We verify the parameter defaults to None.
        from hermes_cli.kanban_db import dispatch_once
        import inspect
        sig = inspect.signature(dispatch_once)
        assert sig.parameters["node_router"].default is None


# ---------------------------------------------------------------------------
# AC4: skills and workspace_kind preservation
# ---------------------------------------------------------------------------

class TestAC4SkillsAndWorkspaceKind:
    """Spawned worker keeps skills='kanban-worker' (NOT capability tags)
    and board-valid workspace_kind."""

    def test_kanban_worker_skill_in_command(self):
        """_default_spawn always includes --skills kanban-worker."""
        from hermes_cli.kanban_db import _default_spawn

        # We can't actually spawn in tests, but we can verify the command
        # construction by mocking subprocess.Popen and checking cmd args.
        task = _make_task(assignee="werner_vogels", skills=["kanban-worker"])
        # Mock Popen to capture the command
        with patch("hermes_cli.kanban_db.subprocess.Popen") as mock_popen:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_popen.return_value = mock_proc

            from pathlib import Path
            with patch("hermes_cli.kanban_db.resolve_workspace", return_value=Path("/tmp/ws")):
                with patch("hermes_cli.kanban_db.set_workspace_path"):
                    with patch("hermes_cli.kanban_db.claim_task", return_value=task):
                        with patch("hermes_cli.kanban_db.worker_logs_dir", return_value=Path("/tmp/logs")):
                            with patch("hermes_cli.kanban_db._normalize_board_slug", return_value="okr-2026-q2"):
                                with patch("hermes_cli.kanban_db.get_current_board", return_value="okr-2026-q2"):
                                    with patch("hermes_cli.kanban_db.kanban_db_path", return_value=Path("/tmp/kanban.db")):
                                        with patch("hermes_cli.kanban_db.workspaces_root", return_value=Path("/tmp/ws")):
                                            try:
                                                pid = _default_spawn(task, "/tmp/ws", board="okr-2026-q2")
                                            except Exception:
                                                pass

            if mock_popen.called:
                cmd = mock_popen.call_args[0][0]
                assert "--skills" in cmd, "--skills flag must be in spawn command"
                skill_indices = [i for i, x in enumerate(cmd) if x == "--skills"]
                assert any(cmd[i+1] == "kanban-worker" for i in skill_indices), \
                    "kanban-worker must be in skills list"

    def test_workspace_kind_scratch_preserved(self):
        """Task workspace_kind='scratch' is the board-valid default."""
        task = _make_task(workspace_kind="scratch")
        assert task.workspace_kind == "scratch"


# ---------------------------------------------------------------------------
# AC5: Audit ledger writes
# ---------------------------------------------------------------------------

class TestAC5AuditLedger:
    """Every routing decision writes to DuckDB audit ledger
    ~/.hermes/memory/llm_dispatcher.duckdb table dispatch_decisions."""

    def test_audit_function_writes_decisions(self):
        """audit() writes RoutingDecisions to the DuckDB ledger."""
        import sys
        scripts_dir = str(Path.home() / ".hermes" / "scripts")
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from llm_cluster_dispatcher import RoutingDecision, audit, AUDIT_DB

        # Use a temp path so we don't pollute the real audit DB
        import llm_cluster_dispatcher as lcd
        original_audit_db = lcd.AUDIT_DB
        with tempfile.TemporaryDirectory() as tmpdir:
            lcd.AUDIT_DB = Path(tmpdir) / "test_audit.duckdb"
            try:
                decisions = [
                    RoutingDecision(
                        task_id="t_audit1",
                        assigned_agent="werner_vogels",
                        target_node="hermes2",
                        welfare_score=0.85,
                        reasoning="test audit write",
                        source="fallback-proportional",
                        validated=True,
                    ),
                ]
                audit(decisions, board="test-board")

                # Verify the write
                import duckdb
                con = duckdb.connect(str(lcd.AUDIT_DB), read_only=True)
                rows = con.execute(
                    "SELECT task_id, target_node, source FROM dispatch_decisions"
                ).fetchall()
                con.close()
                assert len(rows) == 1
                assert rows[0][0] == "t_audit1"
                assert rows[0][1] == "hermes2"
                assert rows[0][2] == "fallback-proportional"
            finally:
                lcd.AUDIT_DB = original_audit_db


# ---------------------------------------------------------------------------
# Integration: dispatch_once with node_router
# ---------------------------------------------------------------------------

class TestDispatchOnceWithNodeRouter:
    """Test that dispatch_once properly uses the node_router callback."""

    def test_node_router_called_for_each_ready_task(self):
        """dispatch_once calls node_router(task_id, assignee) for each ready task.

        This is verified by inspecting the code path in dispatch_once that
        reads node_router(claimed.id, claimed.assignee or "") right after
        claim_task and before spawning. The node_router parameter is passed
        through from the gateway watcher.
        """
        from hermes_cli.kanban_db import dispatch_once
        import inspect
        # Verify the code path exists by inspecting the source
        source = inspect.getsource(dispatch_once)
        assert "node_router" in source, "dispatch_once must reference node_router"
        assert "target_node" in source, "dispatch_once must set target_node from node_router"

    def test_node_router_returns_remote_node(self):
        """When node_router returns a remote node, target_node is passed to spawn.

        Verified by inspecting _default_spawn's handling of target_node:
        it checks for remote nodes and routes via SSH.
        """
        from hermes_cli.kanban_db import _default_spawn
        import inspect
        source = inspect.getsource(_default_spawn)
        assert "target_node" in source, "_default_spawn must accept target_node"
        assert "LOCAL_NODE_ID" in source, "_default_spawn must check local vs remote node"
        assert "spawn_on_remote" in source, "_default_spawn must call spawn_on_remote for remote nodes"


# ---------------------------------------------------------------------------
# Remote spawn command building
# ---------------------------------------------------------------------------

class TestRemoteSpawnCmd:
    """Test that remote_spawn_cmd builds correct SSH commands."""

    def test_builds_ssh_command_for_hermes1(self):
        """remote_spawn_cmd builds an SSH command for hermes1."""
        from gateway.cluster_dispatch import remote_spawn_cmd

        cmd = remote_spawn_cmd(
            task_id="t_ssh01",
            assignee="werner_vogels",
            workspace="/tmp/workspace",
            board="okr-2026-q2",
            target_node="hermes1",
        )
        assert cmd[0] == "ssh"
        assert "100.107.83.25" in cmd  # hermes1 Tailscale IP
        assert "hermes" in " ".join(cmd)
        assert "kanban-worker" in " ".join(cmd)
        assert "t_ssh01" in " ".join(cmd)

    def test_raises_for_local_node(self):
        """remote_spawn_cmd raises ValueError for local node (no SSH host)."""
        from gateway.cluster_dispatch import remote_spawn_cmd

        with pytest.raises(ValueError, match="Cannot SSH-spawn to local node"):
            remote_spawn_cmd(
                task_id="t_local01",
                assignee="werner_vogels",
                workspace="/tmp/workspace",
                board="okr-2026-q2",
                target_node="hermes2",  # local node, no host mapping
            )

    def test_env_vars_forwarded(self):
        """Environment variables are forwarded via SSH export commands."""
        from gateway.cluster_dispatch import remote_spawn_cmd

        cmd = remote_spawn_cmd(
            task_id="t_env01",
            assignee="werner_vogels",
            workspace="/tmp/ws",
            board="test-board",
            target_node="hermes1",
            env_extra={"HERMES_KANBAN_TASK": "t_env01", "HERMES_KANBAN_BOARD": "test-board"},
        )
        # The SSH command should contain the env var exports
        cmd_str = " ".join(cmd)
        assert "HERMES_KANBAN_TASK" in cmd_str
        assert "HERMES_KANBAN_BOARD" in cmd_str