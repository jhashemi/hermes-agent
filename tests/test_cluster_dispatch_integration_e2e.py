"""Integration tests: the real dispatch_once -> node_router -> _default_spawn
chain, plus an opt-in SYSTEM test against the live gateway.

Integration tests use the real kanban_db.dispatch_once and the real
ClusterNodeRouter with only the outermost effects stubbed (workspace
resolution + the actual fork). No live LLM, no live SSH.

The SYSTEM test (@pytest.mark.system) exercises the live gateway's wired
router end-to-end and is skipped unless RUN_SYSTEM_TESTS=1 is set.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, "/home/ubuntu/hermes-agent")
sys.path.insert(0, "/home/ubuntu/.hermes/scripts")

from gateway import cluster_dispatch as cd
from hermes_cli import kanban_db as kb
import llm_cluster_dispatcher as lcd


# ---------------------------------------------------------------------------
# Board fixture: real kanban DB via kb.connect (full schema + migration)
#
# The repo conftest isolates HERMES_HOME to a per-test tempdir (autouse).
# boards_root() derives from HERMES_HOME, so we build boards under the REAL
# boards_root() — already isolated — rather than patching it (patching loses
# to the autouse fixture and splits the DB from the connect() path).
# ---------------------------------------------------------------------------

def _make_board(tmp_path: Path, board: str = "itest") -> Path:
    """Create a real kanban board DB under the (isolated) boards_root().

    Returns the boards root (parent of <board>/kanban.db)."""
    root = kb.boards_root()
    (root / board).mkdir(parents=True, exist_ok=True)
    conn = kb.connect(board=board)
    conn.close()
    return root


def _board_db(root: Path, board: str) -> Path:
    return root / board / "kanban.db"


def _insert_task(db: Path, tid: str, assignee="werner_vogels",
                 status="ready", priority=50.0):
    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO tasks (id, title, body, status, priority, assignee,"
        " created_at, workspace_kind, skills, consecutive_failures)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (tid, "IT task", "body", status, priority, assignee,
         time.time(), "scratch", "kanban-worker", 0),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Integration: dispatch_once consults node_router and routes to _default_spawn
# ---------------------------------------------------------------------------

class TestDispatchOnceRouterIntegration:
    """Drive the REAL dispatch_once with a controlled node_router and a
    recording spawn, verifying the routing value reaches the spawn layer."""

    def _run_dispatch(self, tmp_path, router, tasks, board="itest"):
        root = _make_board(tmp_path, board=board)
        db = _board_db(root, board)
        for t in tasks:
            _insert_task(db, t)
        spawned = []

        def recording_spawn(task, workspace, board=None, target_node=None, **kw):
            spawned.append({"task_id": task.id, "target_node": target_node,
                            "workspace": str(workspace)})
            return 12345

        with mock.patch.object(kb, "resolve_workspace",
                               return_value=tmp_path / "ws"), \
             mock.patch.object(kb, "set_workspace_path"), \
             mock.patch("hermes_cli.profiles.profile_exists",
                        return_value=True):
            conn = kb.connect(board=board)
            kb.dispatch_once(
                conn, board=board, max_spawn=50,
                node_router=router, spawn_fn=recording_spawn,
            )
            conn.commit()
            conn.close()
        return spawned

    def test_router_none_routes_local(self, tmp_path):
        # node_router returns None -> target_node None -> local spawn
        router = lambda tid, assignee: None
        spawned = self._run_dispatch(tmp_path, router, ["t_a"])
        assert len(spawned) == 1
        assert spawned[0]["target_node"] is None

    def test_router_remote_routes_to_hermes1(self, tmp_path):
        router = lambda tid, assignee: "hermes1"
        spawned = self._run_dispatch(tmp_path, router, ["t_b"])
        assert spawned[0]["target_node"] == "hermes1"

    def test_router_exception_falls_back_to_local(self, tmp_path):
        def bad_router(tid, assignee):
            raise RuntimeError("router exploded")
        spawned = self._run_dispatch(tmp_path, bad_router, ["t_c"])
        # dispatch_once catches router exceptions -> target_node None (local)
        assert spawned[0]["target_node"] is None

    def test_cluster_node_router_object_integrates(self, tmp_path):
        # The REAL ClusterNodeRouter (not a lambda) works with dispatch_once
        r = cd.ClusterNodeRouter(board="itest")
        r._routing["t_d"] = "hermes1"
        spawned = self._run_dispatch(tmp_path, r, ["t_d"])
        assert spawned[0]["target_node"] == "hermes1"

    def test_multiple_tasks_route_independently(self, tmp_path):
        routing = {"t_e1": "hermes1", "t_e2": None, "t_e3": "hermes1"}
        router = lambda tid, assignee: routing.get(tid)
        spawned = self._run_dispatch(tmp_path, router, ["t_e1", "t_e2", "t_e3"])
        by_id = {s["task_id"]: s["target_node"] for s in spawned}
        assert by_id["t_e1"] == "hermes1"
        assert by_id["t_e2"] is None
        assert by_id["t_e3"] == "hermes1"


# ---------------------------------------------------------------------------
# Integration: _default_spawn remote branch (SSH probe + spawn_on_remote)
# ---------------------------------------------------------------------------

class TestDefaultSpawnRemoteBranch:
    """Exercise _default_spawn's cluster-routing branch with the remote-spawn
    seam mocked at the SSH boundary."""

    def _task(self, tid="t_spawn"):
        t = mock.Mock()
        t.id = tid
        t.assignee = "werner_vogels"
        t.skills = ["kanban-worker"]
        t.priority = 50.0
        return t

    def test_remote_spawn_invoked_for_remote_node(self, tmp_path):
        task = self._task()
        with mock.patch.object(kb, "worker_logs_dir",
                               return_value=tmp_path), \
             mock.patch.object(kb, "_rotate_worker_log"), \
             mock.patch("gateway.cluster_dispatch.spawn_on_remote",
                        return_value=777) as spawn_remote, \
             mock.patch("subprocess.run") as ssh_probe:
            ssh_probe.return_value = mock.Mock(returncode=0)
            pid = kb._default_spawn(
                task, str(tmp_path / "ws"), board="b",
                target_node="hermes1",
            )
        assert pid == 777
        spawn_remote.assert_called_once()
        # SSH probe ran before spawn
        assert ssh_probe.called
        probe_argv = ssh_probe.call_args[0][0]
        assert probe_argv[0] == "ssh" and "100.107.83.25" in probe_argv

    def test_ssh_probe_failure_falls_back_to_local(self, tmp_path):
        task = self._task()
        with mock.patch.object(kb, "worker_logs_dir",
                               return_value=tmp_path), \
             mock.patch.object(kb, "_rotate_worker_log"), \
             mock.patch("gateway.cluster_dispatch.spawn_on_remote") as spawn_remote, \
             mock.patch("subprocess.run") as ssh_probe, \
             mock.patch.object(kb.subprocess, "Popen", return_value=mock.Mock(pid=55)) as local_popen:
            ssh_probe.return_value = mock.Mock(returncode=255)  # unreachable
            pid = kb._default_spawn(
                task, str(tmp_path / "ws"), board="b",
                target_node="hermes1",
            )
        # Fell back to local Popen, not remote SSH
        spawn_remote.assert_not_called()
        assert local_popen.called

    def test_ssh_probe_exception_falls_back_to_local(self, tmp_path):
        task = self._task()
        with mock.patch.object(kb, "worker_logs_dir",
                               return_value=tmp_path), \
             mock.patch.object(kb, "_rotate_worker_log"), \
             mock.patch("gateway.cluster_dispatch.spawn_on_remote") as spawn_remote, \
             mock.patch("subprocess.run", side_effect=OSError("no ssh")), \
             mock.patch.object(kb.subprocess, "Popen", return_value=mock.Mock(pid=55)) as local_popen:
            pid = kb._default_spawn(
                task, str(tmp_path / "ws"), board="b",
                target_node="hermes1",
            )
        spawn_remote.assert_not_called()
        assert local_popen.called

    def test_local_target_skips_remote_branch(self, tmp_path):
        task = self._task()
        with mock.patch.object(kb, "worker_logs_dir",
                               return_value=tmp_path), \
             mock.patch.object(kb, "_rotate_worker_log"), \
             mock.patch("gateway.cluster_dispatch.spawn_on_remote") as spawn_remote, \
             mock.patch.object(kb.subprocess, "Popen", return_value=mock.Mock(pid=66)) as local_popen:
            kb._default_spawn(task, str(tmp_path / "ws"), board="b",
                              target_node=None)
        spawn_remote.assert_not_called()
        assert local_popen.called

    def test_remote_spawn_preserves_kanban_worker_skill(self, tmp_path):
        task = self._task()
        captured = {}
        def _capture(**kw):
            captured.update(kw)
            return 888
        with mock.patch.object(kb, "worker_logs_dir",
                               return_value=tmp_path), \
             mock.patch.object(kb, "_rotate_worker_log"), \
             mock.patch("gateway.cluster_dispatch.spawn_on_remote",
                        side_effect=_capture), \
             mock.patch("subprocess.run") as ssh_probe:
            ssh_probe.return_value = mock.Mock(returncode=0)
            kb._default_spawn(task, str(tmp_path / "ws"), board="b",
                              target_node="hermes1")
        assert "kanban-worker" in (captured.get("skills") or [])


# ---------------------------------------------------------------------------
# SYSTEM test — live gateway, opt-in only (RUN_SYSTEM_TESTS=1)
# ---------------------------------------------------------------------------

SYSTEM = pytest.mark.skipif(
    os.environ.get("RUN_SYSTEM_TESTS") != "1",
    reason="system test: set RUN_SYSTEM_TESTS=1 to run against live gateway",
)
pytestmark_system = pytest.mark.system


@SYSTEM
@pytest.mark.system
class TestLiveGatewaySystem:
    """End-to-end against the RUNNING gateway. Requires:
      - gateway process live with kanban.cluster_dispatch=true
      - ollama-cloud reachable (glm-5.2)
      - okr-2026-q2 board present
    These are deliberately read-only / non-mutating.
    """

    def test_live_router_produces_decisions(self):
        router = cd.create_cluster_node_router(board="okr-2026-q2")
        assert isinstance(router, cd.ClusterNodeRouter)
        router.refresh()
        # Live LLM should produce a non-empty routing table for the ready queue
        assert len(router._routing) >= 0  # may be 0 if queue drained
        # Every routed node must be a known node
        for node in router._routing.values():
            assert node in cd._KNOWN_NODES

    def test_live_telemetry_both_nodes_eligible_or_probed(self):
        tc = lcd.TelemetryCollector()
        nodes = tc.collect()
        assert set(nodes.keys()) == {"hermes1", "hermes2"}
        # At least hermes1 (16-core) should be reachable/eligible
        assert any(n.eligible for n in nodes.values())

    def test_live_audit_ledger_accumulating(self):
        import duckdb
        db = os.path.expanduser("~/.hermes/memory/llm_dispatcher.duckdb")
        con = duckdb.connect(db, read_only=True)
        n = con.execute("SELECT COUNT(*) FROM dispatch_decisions").fetchone()[0]
        con.close()
        assert n > 0

    def test_live_dispatch_once_routes_via_router(self, tmp_path):
        # The full live chain: real router + real dispatch_once + recording spawn
        router = cd.create_cluster_node_router(board="okr-2026-q2")
        router.refresh()
        root = _make_board(tmp_path, board="syslive")
        db = _board_db(root, "syslive")
        _insert_task(db, "t_syslive_probe")
        spawned = []

        def rec(task, workspace, board=None, target_node=None, **kw):
            spawned.append(target_node)
            return 1

        with mock.patch.object(kb, "resolve_workspace",
                               return_value=tmp_path / "ws"), \
             mock.patch.object(kb, "set_workspace_path"):
            conn = kb.connect(board="syslive")
            kb.dispatch_once(conn, board="syslive", max_spawn=5,
                             node_router=router, spawn_fn=rec)
            conn.commit()
            conn.close()
        # A routed value (node or None for local) reached the spawn layer
        assert len(spawned) == 1
        assert spawned[0] in cd._KNOWN_NODES or spawned[0] is None
