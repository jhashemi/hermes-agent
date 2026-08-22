"""Unit tests for gateway/cluster_dispatch.py — ClusterNodeRouter lifecycle,
remote_spawn_cmd argv, spawn_on_remote Popen, local_node_router, factory,
and the node-hosts loader. All subprocess/LLM/SSH effects are mocked.
"""
from __future__ import annotations

import os
import sys
import types
from unittest import mock

import pytest

# Prefer the repo containing THIS test file over any sibling clone that
# happens to be on sys.path (e.g. /home/ubuntu/hermes-agent, the shared
# deployment clone). This lets the tests run inside a git worktree.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from gateway import cluster_dispatch as cd


# ---------------------------------------------------------------------------
# local_node_router
# ---------------------------------------------------------------------------

class TestLocalNodeRouter:
    def test_always_returns_none(self):
        assert cd.local_node_router("t_1", "werner_vogels") is None

    def test_none_for_empty_inputs(self):
        assert cd.local_node_router("", "") is None


# ---------------------------------------------------------------------------
# ClusterNodeRouter.__call__ — routing table lookups + unknown-node guard
# ---------------------------------------------------------------------------

class TestRouterCall:
    def _router(self):
        return cd.ClusterNodeRouter(board="b")

    def test_unclaimed_task_returns_none(self):
        r = self._router()
        assert r("t_unknown", "agent") is None

    def test_known_task_returns_cached_node(self):
        r = self._router()
        r._routing["t_1"] = "hermes1"
        assert r("t_1", "agent") == "hermes1"

    def test_unknown_node_falls_back_to_local(self):
        r = self._router()
        r._routing["t_1"] = "hermes99"  # not in _KNOWN_NODES
        assert r("t_1", "agent") is None

    def test_local_node_returned_as_is(self):
        r = self._router()
        r._routing["t_1"] = r._local_node  # hermes2
        assert r("t_1", "agent") == r._local_node


# ---------------------------------------------------------------------------
# ClusterNodeRouter._try_init — lazy import, caching, failure latching
# ---------------------------------------------------------------------------

class TestTryInit:
    def test_success_imports_and_caches(self):
        r = cd.ClusterNodeRouter(board="b")
        fake_mod = types.ModuleType("llm_cluster_dispatcher")
        class _D:
            def __init__(self, board): self.board = board
        fake_mod.LLMClusterDispatcher = _D
        with mock.patch.dict(sys.modules, {"llm_cluster_dispatcher": fake_mod}):
            assert r._try_init() is True
            assert r._dispatcher is not None
            # second call short-circuits on cached dispatcher
            assert r._try_init() is True

    def test_import_failure_latches_error(self):
        r = cd.ClusterNodeRouter(board="b")
        # Force import to fail by pointing the script path at a bogus module
        with mock.patch.dict(sys.modules, {"llm_cluster_dispatcher": None}):
            ok = r._try_init()
        assert ok is False
        assert r._init_error is not None
        # Latched: subsequent calls return False without re-import
        assert r._try_init() is False

    def test_dispatcher_construction_failure_latches(self):
        r = cd.ClusterNodeRouter(board="b")
        fake_mod = types.ModuleType("llm_cluster_dispatcher")
        def _boom(board): raise RuntimeError("ctor fail")
        fake_mod.LLMClusterDispatcher = _boom
        with mock.patch.dict(sys.modules, {"llm_cluster_dispatcher": fake_mod}):
            assert r._try_init() is False
            assert "ctor fail" in r._init_error


# ---------------------------------------------------------------------------
# ClusterNodeRouter.refresh — populate routing table, fallbacks
# ---------------------------------------------------------------------------

class TestRefresh:
    def test_refresh_populates_routing_table(self):
        r = cd.ClusterNodeRouter(board="b")
        r._dispatcher = mock.Mock()
        dec = mock.Mock(); dec.task_id = "t_1"; dec.target_node = "hermes1"
        r._dispatcher.tick.return_value = [dec]
        r.refresh()
        assert r._routing == {"t_1": "hermes1"}
        r._dispatcher.tick.assert_called_once_with(dry_run=True)

    def test_refresh_clears_stale_table(self):
        r = cd.ClusterNodeRouter(board="b")
        r._routing["t_old"] = "hermes1"
        r._dispatcher = mock.Mock()
        r._dispatcher.tick.return_value = []
        r.refresh()
        assert r._routing == {}

    def test_refresh_without_init_keeps_local(self):
        r = cd.ClusterNodeRouter(board="b")
        r._init_error = "no import"
        r.refresh()
        assert r._routing == {}

    def test_refresh_tick_exception_leaves_empty_and_does_not_raise(self):
        r = cd.ClusterNodeRouter(board="b")
        r._dispatcher = mock.Mock()
        r._dispatcher.tick.side_effect = RuntimeError("llm boom")
        r.refresh()  # must not raise
        assert r._routing == {}

    def test_refresh_multiple_decisions(self):
        r = cd.ClusterNodeRouter(board="b")
        r._dispatcher = mock.Mock()
        decs = []
        for i, node in enumerate(["hermes1", "hermes2", "hermes1"]):
            d = mock.Mock(); d.task_id = f"t_{i}"; d.target_node = node
            decs.append(d)
        r._dispatcher.tick.return_value = decs
        r.refresh()
        assert r._routing == {"t_0": "hermes1", "t_1": "hermes2", "t_2": "hermes1"}


# ---------------------------------------------------------------------------
# remote_spawn_cmd — argv construction
# ---------------------------------------------------------------------------

class TestRemoteSpawnCmd:
    def _cmd(self, **kw):
        defaults = dict(
            task_id="t_abc", assignee="werner_vogels",
            workspace="/tmp/ws", board="okr-2026-q2", target_node="hermes1",
        )
        defaults.update(kw)
        return cd.remote_spawn_cmd(**defaults)

    def test_raises_for_local_node(self):
        with pytest.raises(ValueError, match="local node"):
            self._cmd(target_node="hermes2")  # hermes2 host is None

    def test_raises_for_unknown_node(self):
        with pytest.raises(ValueError):
            self._cmd(target_node="hermes99")

    def test_ssh_prefix_and_host(self):
        cmd = self._cmd()
        assert cmd[0] == "ssh"
        assert "100.107.83.25" in cmd
        assert "BatchMode=yes" in cmd
        assert "ConnectTimeout=10" in cmd

    def test_remote_cmd_mirrors_local_hermes_spawn(self):
        cmd = self._cmd()
        payload = cmd[-1]
        assert "hermes -p werner_vogels" in payload
        assert "--skills kanban-worker" in payload
        assert "chat -q work kanban task t_abc" in payload

    def test_env_exports_present(self):
        cmd = self._cmd()
        payload = cmd[-1]
        assert "export HERMES_KANBAN_TASK=t_abc" in payload
        assert "export HERMES_KANBAN_WORKSPACE=/tmp/ws" in payload
        assert "export HERMES_KANBAN_BOARD=okr-2026-q2" in payload

    def test_env_extra_forwarded(self):
        cmd = self._cmd(env_extra={"HERMES_FOO": "bar"})
        assert "export HERMES_FOO=bar" in cmd[-1]

    def test_env_extra_with_shell_metacharacters_is_quoted(self, monkeypatch):
        """env_extra values that contain shell metachars must be quoted.

        Without shlex.quote, a workspace path with spaces or a token
        containing ``$`` or ``;`` would be interpreted by the remote
        login shell — either splitting the value or worse, executing
        code. Values without metachars pass through untouched.
        """
        # Clear any resolver env so this test is deterministic.
        for k in list(os.environ):
            if k.startswith("HERMES_KANBAN_REMOTE_API_"):
                monkeypatch.delenv(k, raising=False)
        cmd = self._cmd(env_extra={"HERMES_FOO": "hello world; rm -rf /"})
        payload = cmd[-1]
        # shlex.quote wraps in single quotes and escapes embedded quotes.
        assert "export HERMES_FOO='hello world; rm -rf /'" in payload
        # Value never appears un-quoted (would be an injection vector).
        assert " rm -rf /" not in payload.replace("'hello world; rm -rf /'", "")

    def test_api_env_injected_when_both_configured(self, monkeypatch):
        """Both URL and token in env → both exported (this is the whole point)."""
        for k in list(os.environ):
            if k.startswith("HERMES_KANBAN_REMOTE_API_"):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("HERMES_KANBAN_REMOTE_API_URL", "http://100.79.15.66:9119")
        monkeypatch.setenv("HERMES_KANBAN_REMOTE_API_TOKEN", "s3cr3t-token")
        cmd = self._cmd()
        payload = cmd[-1]
        assert "export HERMES_KANBAN_API_URL=http://100.79.15.66:9119" in payload
        assert "export HERMES_KANBAN_API_TOKEN=s3cr3t-token" in payload

    def test_api_env_not_injected_when_absent(self, monkeypatch):
        """No env, no config → no exports. Legacy local-open behaviour."""
        for k in list(os.environ):
            if k.startswith("HERMES_KANBAN_REMOTE_API_"):
                monkeypatch.delenv(k, raising=False)
        # Also mask config so a laptop with kanban.remote_api_* set locally
        # doesn't false-positive this test.
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"kanban": {}},
        )
        cmd = self._cmd()
        payload = cmd[-1]
        assert "HERMES_KANBAN_API_URL" not in payload
        assert "HERMES_KANBAN_API_TOKEN" not in payload

    def test_api_env_half_configured_is_refused(self, monkeypatch, caplog):
        """URL set but token missing → nothing exported, warning logged."""
        for k in list(os.environ):
            if k.startswith("HERMES_KANBAN_REMOTE_API_"):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("HERMES_KANBAN_REMOTE_API_URL", "http://100.79.15.66:9119")
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"kanban": {}},
        )
        with caplog.at_level("WARNING", logger="gateway.cluster_dispatch"):
            cmd = self._cmd()
        payload = cmd[-1]
        # No half-configured exports.
        assert "HERMES_KANBAN_API_URL" not in payload
        assert "HERMES_KANBAN_API_TOKEN" not in payload
        # Warning surfaces to operator logs.
        assert any(
            "half-configured" in rec.message and "hermes1" in rec.message
            for rec in caplog.records
        ), f"expected half-configured warning; got {[r.message for r in caplog.records]!r}"

    def test_api_env_per_target_env_beats_global(self, monkeypatch):
        """HERMES_KANBAN_REMOTE_API_URL_HERMES1 wins over the unsuffixed one."""
        for k in list(os.environ):
            if k.startswith("HERMES_KANBAN_REMOTE_API_"):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("HERMES_KANBAN_REMOTE_API_URL", "http://global:9119")
        monkeypatch.setenv("HERMES_KANBAN_REMOTE_API_URL_HERMES1", "http://per-target:9119")
        monkeypatch.setenv("HERMES_KANBAN_REMOTE_API_TOKEN", "tok")
        cmd = self._cmd()
        payload = cmd[-1]
        assert "export HERMES_KANBAN_API_URL=http://per-target:9119" in payload
        # Global default was NOT used.
        assert "http://global:9119" not in payload

    def test_api_env_falls_back_to_config(self, monkeypatch):
        """Config.yaml kanban.remote_api_{url,token} is the last fallback."""
        for k in list(os.environ):
            if k.startswith("HERMES_KANBAN_REMOTE_API_"):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"kanban": {
                "remote_api_url": "http://from-config:9119",
                "remote_api_token": "config-token",
            }},
        )
        cmd = self._cmd()
        payload = cmd[-1]
        assert "export HERMES_KANBAN_API_URL=http://from-config:9119" in payload
        assert "export HERMES_KANBAN_API_TOKEN=config-token" in payload

    def test_api_env_token_with_shell_metachars_is_shell_quoted(self, monkeypatch):
        """A token containing $, ;, or spaces must survive the SSH shell.

        JWTs are dot-separated base64url so alnum + dashes + underscores,
        but a paranoid deploy might use a token generated by a system that
        emits ``+`` or ``/``. shlex.quote is unconditional insurance.
        """
        for k in list(os.environ):
            if k.startswith("HERMES_KANBAN_REMOTE_API_"):
                monkeypatch.delenv(k, raising=False)
        monkeypatch.setenv("HERMES_KANBAN_REMOTE_API_URL", "http://ok:9119")
        monkeypatch.setenv("HERMES_KANBAN_REMOTE_API_TOKEN", "abc; rm -rf /")
        cmd = self._cmd()
        payload = cmd[-1]
        # Present, quoted, and NOT executed.
        assert "export HERMES_KANBAN_API_TOKEN='abc; rm -rf /'" in payload

    def test_extra_skills_included(self):
        cmd = self._cmd(skills=["kanban-worker", "custom-skill"])
        payload = cmd[-1]
        assert "--skills custom-skill" in payload

    def test_kanban_worker_not_duplicated(self):
        cmd = self._cmd(skills=["kanban-worker"])
        payload = cmd[-1]
        # kanban-worker appears once (base), not appended again
        assert payload.count("--skills kanban-worker") == 1


# ---------------------------------------------------------------------------
# spawn_on_remote — Popen, log file, failure paths
# ---------------------------------------------------------------------------

class TestSpawnOnRemote:
    def test_popen_returns_pid(self):
        fake_proc = mock.Mock(); fake_proc.pid = 4242
        with mock.patch.object(cd.subprocess, "Popen", return_value=fake_proc) as pop:
            pid = cd.spawn_on_remote(
                task_id="t", assignee="a", workspace="/tmp/w",
                board="b", target_node="hermes1",
            )
        assert pid == 4242
        assert pop.called
        argv = pop.call_args[0][0]
        assert argv[0] == "ssh"

    def test_popen_uses_devnull_stdin_and_new_session(self):
        fake_proc = mock.Mock(); fake_proc.pid = 1
        with mock.patch.object(cd.subprocess, "Popen", return_value=fake_proc) as pop:
            cd.spawn_on_remote(task_id="t", assignee="a", workspace="/tmp/w",
                               board="b", target_node="hermes1")
        kwargs = pop.call_args[1]
        import subprocess as sp
        assert kwargs["stdin"] == sp.DEVNULL
        assert kwargs["start_new_session"] is True

    def test_ssh_binary_missing_raises_runtime_error(self):
        with mock.patch.object(cd.subprocess, "Popen",
                               side_effect=FileNotFoundError("no ssh")):
            with pytest.raises(RuntimeError):
                cd.spawn_on_remote(task_id="t", assignee="a", workspace="/tmp/w",
                                   board="b", target_node="hermes1")

    def test_log_file_opened_when_provided(self, tmp_path):
        log = tmp_path / "task.log"
        fake_proc = mock.Mock(); fake_proc.pid = 7
        with mock.patch.object(cd.subprocess, "Popen", return_value=fake_proc):
            cd.spawn_on_remote(task_id="t", assignee="a", workspace="/tmp/w",
                               board="b", target_node="hermes1",
                               log_path=str(log))
        assert log.exists()


# ---------------------------------------------------------------------------
# _load_node_hosts / _KNOWN_NODES / _NODE_HOSTS
# ---------------------------------------------------------------------------

class TestNodeHosts:
    def test_defaults_present(self):
        assert "hermes1" in cd._NODE_HOSTS
        assert "hermes2" in cd._NODE_HOSTS
        assert cd._NODE_HOSTS["hermes2"] is None      # local
        assert cd._NODE_HOSTS["hermes1"] == "100.107.83.25"

    def test_known_nodes_matches_hosts(self):
        assert cd._KNOWN_NODES == set(cd._NODE_HOSTS.keys())

    def test_load_node_hosts_fallback_on_import_error(self):
        with mock.patch.dict(sys.modules, {"llm_cluster_dispatcher": None}):
            hosts = cd._load_node_hosts()
        assert hosts["hermes1"] == "100.107.83.25"
        assert hosts["hermes2"] is None


# ---------------------------------------------------------------------------
# create_cluster_node_router factory — config gate + env override
# ---------------------------------------------------------------------------

class TestFactory:
    def _cfg(self, enabled, whitelist=("b",)):
        """Build a kanban config. The default whitelist=('b',) matches the
        board name used across these tests so pre-existing tests keep their
        original intent (cluster dispatch enabled → cluster router)."""
        cfg = {"kanban": {"cluster_dispatch": enabled}}
        if whitelist is not None:
            cfg["kanban"]["cluster_dispatch_board"] = list(whitelist)
        return cfg

    def test_disabled_returns_local_router(self, monkeypatch):
        monkeypatch.delenv("HERMES_CLUSTER_DISPATCH", raising=False)
        fake_cfg = mock.Mock()
        fake_cfg.get.return_value = self._cfg(False)
        with mock.patch("hermes_cli.config.load_config", return_value=self._cfg(False)):
            router = cd.create_cluster_node_router(board="b")
        assert router is cd.local_node_router

    def test_enabled_returns_cluster_router(self, monkeypatch):
        monkeypatch.delenv("HERMES_CLUSTER_DISPATCH", raising=False)
        with mock.patch("hermes_cli.config.load_config", return_value=self._cfg(True)):
            router = cd.create_cluster_node_router(board="b")
        assert isinstance(router, cd.ClusterNodeRouter)

    def test_env_override_off_forces_local(self, monkeypatch):
        monkeypatch.setenv("HERMES_CLUSTER_DISPATCH", "0")
        with mock.patch("hermes_cli.config.load_config", return_value=self._cfg(True)):
            router = cd.create_cluster_node_router(board="b")
        assert router is cd.local_node_router

    def test_env_override_variants(self, monkeypatch):
        for val in ("0", "false", "no", "off", "FALSE", "Off"):
            monkeypatch.setenv("HERMES_CLUSTER_DISPATCH", val)
            with mock.patch("hermes_cli.config.load_config", return_value=self._cfg(True)):
                router = cd.create_cluster_node_router(board="b")
            assert router is cd.local_node_router, f"env={val}"

    def test_config_load_failure_returns_local(self, monkeypatch):
        monkeypatch.delenv("HERMES_CLUSTER_DISPATCH", raising=False)
        with mock.patch("hermes_cli.config.load_config",
                        side_effect=RuntimeError("no config")):
            router = cd.create_cluster_node_router(board="b")
        assert router is cd.local_node_router


# ---------------------------------------------------------------------------
# create_cluster_node_router — board whitelist (t_595717ab)
#
# Regression: with cluster_dispatch=True, ONLY boards listed in
# kanban.cluster_dispatch_board should be routed through the LLM cluster
# router. Every other board must fall back to local spawn. The pre-fix
# code ignored the whitelist entirely and routed every board.
# ---------------------------------------------------------------------------

class TestFactoryWhitelist:
    """Board whitelist gate on ``kanban.cluster_dispatch_board``."""

    def _cfg(self, *, enabled=True, whitelist=None):
        cfg = {"kanban": {"cluster_dispatch": enabled}}
        if whitelist is not None:
            cfg["kanban"]["cluster_dispatch_board"] = list(whitelist)
        return cfg

    def test_non_whitelisted_board_returns_local(self, monkeypatch):
        """A board NOT in the whitelist gets local_node_router even when
        cluster_dispatch=True. This is the primary DoD assertion."""
        monkeypatch.delenv("HERMES_CLUSTER_DISPATCH", raising=False)
        cfg = self._cfg(whitelist=["adr-006b-phase-2", "okr-vfe-2026-q3"])
        with mock.patch("hermes_cli.config.load_config", return_value=cfg):
            router = cd.create_cluster_node_router(board="default")
        assert router is cd.local_node_router

    def test_whitelisted_board_returns_cluster_router(self, monkeypatch):
        """A board IN the whitelist gets a real ClusterNodeRouter when
        cluster_dispatch=True."""
        monkeypatch.delenv("HERMES_CLUSTER_DISPATCH", raising=False)
        cfg = self._cfg(whitelist=["adr-006b-phase-2", "okr-vfe-2026-q3"])
        with mock.patch("hermes_cli.config.load_config", return_value=cfg):
            router = cd.create_cluster_node_router(board="adr-006b-phase-2")
        assert isinstance(router, cd.ClusterNodeRouter)
        assert router.board == "adr-006b-phase-2"

    def test_missing_whitelist_key_defaults_to_local(self, monkeypatch):
        """If cluster_dispatch_board is missing entirely (key absent),
        no board is cluster-routed — the whitelist is opt-in per board."""
        monkeypatch.delenv("HERMES_CLUSTER_DISPATCH", raising=False)
        cfg = {"kanban": {"cluster_dispatch": True}}  # no whitelist key
        with mock.patch("hermes_cli.config.load_config", return_value=cfg):
            router = cd.create_cluster_node_router(board="adr-006b-phase-2")
        assert router is cd.local_node_router

    def test_empty_whitelist_returns_local(self, monkeypatch):
        """cluster_dispatch_board=[] behaves the same as missing — nothing
        is whitelisted, nothing is cluster-routed."""
        monkeypatch.delenv("HERMES_CLUSTER_DISPATCH", raising=False)
        cfg = self._cfg(whitelist=[])
        with mock.patch("hermes_cli.config.load_config", return_value=cfg):
            router = cd.create_cluster_node_router(board="anything")
        assert router is cd.local_node_router

    def test_malformed_whitelist_treated_as_empty(self, monkeypatch):
        """If cluster_dispatch_board is not a list (e.g. accidentally a
        string), fall back to empty (all-local) instead of crashing."""
        monkeypatch.delenv("HERMES_CLUSTER_DISPATCH", raising=False)
        cfg = {
            "kanban": {
                "cluster_dispatch": True,
                "cluster_dispatch_board": "adr-006b-phase-2",  # str, not list
            },
        }
        with mock.patch("hermes_cli.config.load_config", return_value=cfg):
            router = cd.create_cluster_node_router(board="adr-006b-phase-2")
        assert router is cd.local_node_router

    def test_whitelist_disabled_master_still_disables(self, monkeypatch):
        """cluster_dispatch=False takes priority over whitelist membership."""
        monkeypatch.delenv("HERMES_CLUSTER_DISPATCH", raising=False)
        cfg = self._cfg(enabled=False, whitelist=["adr-006b-phase-2"])
        with mock.patch("hermes_cli.config.load_config", return_value=cfg):
            router = cd.create_cluster_node_router(board="adr-006b-phase-2")
        assert router is cd.local_node_router

    def test_env_override_beats_whitelist(self, monkeypatch):
        """HERMES_CLUSTER_DISPATCH=0 forces local even for whitelisted boards."""
        monkeypatch.setenv("HERMES_CLUSTER_DISPATCH", "0")
        cfg = self._cfg(whitelist=["adr-006b-phase-2"])
        with mock.patch("hermes_cli.config.load_config", return_value=cfg):
            router = cd.create_cluster_node_router(board="adr-006b-phase-2")
        assert router is cd.local_node_router

    def test_all_production_whitelisted_boards(self, monkeypatch):
        """Smoke test: every board in config.yaml's real whitelist gets a
        cluster router; every non-whitelisted board gets local. Uses the
        current production whitelist from config.yaml as of t_595717ab."""
        monkeypatch.delenv("HERMES_CLUSTER_DISPATCH", raising=False)
        production_whitelist = [
            "adr-006b-phase-2",
            "executive-board-plugin",
            "rsi-council-audit",
            "okr-vfe-2026-q3",
        ]
        cfg = self._cfg(whitelist=production_whitelist)
        with mock.patch("hermes_cli.config.load_config", return_value=cfg):
            for slug in production_whitelist:
                r = cd.create_cluster_node_router(board=slug)
                assert isinstance(r, cd.ClusterNodeRouter), \
                    f"whitelisted board {slug} should get ClusterNodeRouter"
            for slug in ("default", "campaignforge", "voice-review", "random-board"):
                r = cd.create_cluster_node_router(board=slug)
                assert r is cd.local_node_router, \
                    f"non-whitelisted board {slug} should get local_node_router"


# ---------------------------------------------------------------------------
# NodeRouter protocol conformance
# ---------------------------------------------------------------------------

class TestProtocol:
    def test_local_router_is_callable_with_two_args(self):
        fn: cd.NodeRouter = cd.local_node_router
        assert callable(fn)
        assert fn("t", "a") is None

    def test_cluster_router_is_callable_with_two_args(self):
        r = cd.ClusterNodeRouter(board="b")
        assert callable(r)
        assert r("t", "a") is None


# ---------------------------------------------------------------------------
# Gap-fill: sys.path insertion, _load_node_hosts success, spawn exceptions
# ---------------------------------------------------------------------------

class TestTryInitPathInsertion:
    def test_scripts_dir_inserted_into_syspath(self):
        # Covers line 114: scripts_dir not already in sys.path -> inserted
        r = cd.ClusterNodeRouter(board="b")
        scripts_dir = str(cd.LLM_DISPATCHER_SCRIPT.parent)
        fake_mod = types.ModuleType("llm_cluster_dispatcher")
        class _D:
            def __init__(self, board): self.board = board
        fake_mod.LLMClusterDispatcher = _D
        with mock.patch.dict(sys.modules, {"llm_cluster_dispatcher": fake_mod}), \
             mock.patch.object(sys, "path", [p for p in sys.path if p != scripts_dir]):
            assert scripts_dir not in sys.path
            assert r._try_init() is True
            assert scripts_dir in sys.path


class TestLoadNodeHostsSuccess:
    def test_loads_from_real_module(self):
        # Covers line 182: successful import returns module's NODE_HOSTS
        fake_mod = types.ModuleType("llm_cluster_dispatcher")
        fake_mod.NODE_HOSTS = {"n1": "1.2.3.4", "n2": None}
        scripts_dir = str(cd.LLM_DISPATCHER_SCRIPT.parent)
        with mock.patch.dict(sys.modules, {"llm_cluster_dispatcher": fake_mod}), \
             mock.patch.object(sys, "path", [p for p in sys.path if p != scripts_dir]):
            assert scripts_dir not in sys.path
            hosts = cd._load_node_hosts()
            assert scripts_dir in sys.path  # line 182: inserted when absent
        assert hosts == {"n1": "1.2.3.4", "n2": None}
        # returns a copy, not the module's dict
        assert hosts is not fake_mod.NODE_HOSTS


class TestSpawnOnRemoteExceptions:
    def test_generic_popen_exception_reraises_and_closes_log(self, tmp_path):
        # Covers lines 339-342: non-FileNotFoundError closes log + re-raises
        log = tmp_path / "t.log"
        with mock.patch.object(cd.subprocess, "Popen",
                               side_effect=OSError("spawn exploded")):
            with pytest.raises(OSError, match="spawn exploded"):
                cd.spawn_on_remote(task_id="t", assignee="a", workspace="/tmp/w",
                                   board="b", target_node="hermes1",
                                   log_path=str(log))

    def test_generic_exception_without_log_path_reraises(self):
        with mock.patch.object(cd.subprocess, "Popen",
                               side_effect=OSError("boom")):
            with pytest.raises(OSError):
                cd.spawn_on_remote(task_id="t", assignee="a", workspace="/tmp/w",
                                   board="b", target_node="hermes1")

    def test_ssh_missing_with_log_closes_and_raises(self, tmp_path):
        # FileNotFoundError branch with a log file open (line 334-338)
        log = tmp_path / "t2.log"
        with mock.patch.object(cd.subprocess, "Popen",
                               side_effect=FileNotFoundError("no ssh")):
            with pytest.raises(RuntimeError, match="ssh"):
                cd.spawn_on_remote(task_id="t", assignee="a", workspace="/tmp/w",
                                   board="b", target_node="hermes1",
                                   log_path=str(log))
