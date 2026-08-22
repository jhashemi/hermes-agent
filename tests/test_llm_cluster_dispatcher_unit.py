"""Unit tests for llm_cluster_dispatcher.py — telemetry, gates, LLM client,
fallback fill, decision validation, audit, prompt building, and tick batching.

All external effects (DuckDB, SQLite, SSH subprocess, urllib LLM calls) are
mocked/monkeypatched. No live cluster, no live LLM, no real DB writes.
"""
from __future__ import annotations

import json
import os
import sys
import time
import types
from pathlib import Path
from unittest import mock

import pytest

# Make the dispatcher importable (it lives outside the package tree).
SCRIPTS_DIR = "/home/ubuntu/.hermes/scripts"
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import llm_cluster_dispatcher as lcd


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolated_breaker(tmp_path):
    """Isolate the circuit breaker from the live production state file.

    lcd._get_breaker() otherwise loads ~/.hermes/state/llm_dispatcher_breaker.json
    — if production currently has the circuit OPEN, every llm_route test
    short-circuits and fails. Each test gets a fresh CLOSED breaker in tmp.
    """
    lcd._BREAKER = lcd.CircuitBreaker(state_file=tmp_path / "breaker.json")
    yield
    lcd._BREAKER = None

def make_telemetry(node_id="hermes1", cpu_count=16, load_1min=0.5,
                   mem_avail_gb=22.0, disk_free_pct=57.0, active_workers=0,
                   max_workers=16, heartbeat_age_s=0.0, status="healthy"):
    return lcd.NodeTelemetry(
        node_id=node_id, cpu_count=cpu_count, load_1min=load_1min,
        mem_avail_gb=mem_avail_gb, disk_free_pct=disk_free_pct,
        active_workers=active_workers, max_workers=max_workers,
        heartbeat_age_s=heartbeat_age_s, status=status,
    )


def make_task(tid="t_1", title="T", assignee="werner_vogels", priority=50.0):
    return {"id": tid, "title": title, "assignee": assignee,
            "priority": priority, "body": "", "workspace_kind": "scratch"}


def make_registry(agent="werner_vogels", skills=None, capacity=5, reliability=0.91):
    return [{"id": agent, "skills": skills or ["distributed-systems"],
             "capacity": capacity, "reliability": reliability}]


# ---------------------------------------------------------------------------
# NodeTelemetry — load_ratio + eligible gate (boundary matrix)
# ---------------------------------------------------------------------------

class TestNodeTelemetry:
    def test_load_ratio_basic(self):
        n = make_telemetry(cpu_count=16, load_1min=8.0)
        assert n.load_ratio == pytest.approx(0.5)

    def test_load_ratio_zero_cores_clamps_to_one(self):
        # cpu_count=0 -> max(1,0)=1, no ZeroDivisionError
        n = make_telemetry(cpu_count=0, load_1min=0.5)
        assert n.load_ratio == pytest.approx(0.5)

    def test_load_ratio_negative_cores_clamps(self):
        n = make_telemetry(cpu_count=-4, load_1min=2.0)
        assert n.load_ratio == pytest.approx(2.0)

    # eligible gate: each condition independently, then combined
    def test_eligible_healthy_fresh_low_load(self):
        assert make_telemetry().eligible is True

    def test_ineligible_when_status_not_healthy(self):
        n = make_telemetry(status="overloaded")
        assert n.eligible is False

    def test_ineligible_when_status_unknown(self):
        assert make_telemetry(status="unknown").eligible is False

    def test_ineligible_when_heartbeat_stale(self):
        n = make_telemetry(heartbeat_age_s=lcd.HEARTBEAT_STALE_S + 1)
        assert n.eligible is False

    def test_eligible_at_exact_heartbeat_boundary(self):
        # Boundary: exactly at staleness threshold is still fresh (<=)
        n = make_telemetry(heartbeat_age_s=lcd.HEARTBEAT_STALE_S)
        assert n.eligible is True

    def test_ineligible_load_ratio_above_max(self):
        # load_ratio = 13.7/16 = 0.856 > 0.85
        n = make_telemetry(cpu_count=16, load_1min=13.7)
        assert n.load_ratio > lcd.LOAD_RATIO_HARD_MAX
        assert n.eligible is False

    def test_eligible_load_ratio_just_under_max(self):
        n = make_telemetry(cpu_count=16, load_1min=13.5)  # 0.84
        assert n.load_ratio <= lcd.LOAD_RATIO_HARD_MAX
        assert n.eligible is True

    def test_ineligible_disk_below_floor(self):
        n = make_telemetry(disk_free_pct=lcd.DISK_FREE_HARD_MIN_PCT - 0.5)
        assert n.eligible is False

    def test_eligible_disk_at_floor(self):
        # Boundary: exactly at floor is eligible (>=)
        n = make_telemetry(disk_free_pct=lcd.DISK_FREE_HARD_MIN_PCT)
        assert n.eligible is True

    def test_ineligible_workers_at_capacity(self):
        n = make_telemetry(active_workers=16, max_workers=16)
        assert n.eligible is False

    def test_ineligible_workers_over_capacity(self):
        n = make_telemetry(active_workers=20, max_workers=16)
        assert n.eligible is False

    def test_eligible_workers_one_below_capacity(self):
        n = make_telemetry(active_workers=15, max_workers=16)
        assert n.eligible is True


# ---------------------------------------------------------------------------
# RoutingDecision dataclass
# ---------------------------------------------------------------------------

class TestRoutingDecision:
    def test_defaults(self):
        d = lcd.RoutingDecision(task_id="t", assigned_agent="a", target_node="n")
        assert d.welfare_score == 0.0
        assert d.reasoning == ""
        assert d.source == "llm"
        assert d.validated is False

    def test_explicit_fields(self):
        d = lcd.RoutingDecision(task_id="t", assigned_agent="a", target_node="h1",
                                welfare_score=0.9, reasoning="r",
                                source="fallback-proportional", validated=True)
        assert d.source == "fallback-proportional"
        assert d.validated is True


# ---------------------------------------------------------------------------
# _probe_live — subprocess parsing, local vs SSH, failure paths
# ---------------------------------------------------------------------------

class TestProbeLive:
    def _fake_run(self, outputs):
        """Return a subprocess.run replacement mapping cmd->stdout.

        Matches on the *longest* key that is a substring of the command so
        specific keys (e.g. '/proc/loadavg') win over generic ones.
        """
        def _run(cmd, shell, capture_output, text, timeout):
            class R:
                stdout = ""
            r = R()
            best = None
            for key in outputs:
                if key in cmd and (best is None or len(key) > len(best)):
                    best = key
            if best is not None:
                r.stdout = outputs[best]
            return r
        return _run

    def test_local_probe_parses_all_metrics(self):
        # Fixture values are what the SHELL PIPELINE returns (the mock replaces
        # subprocess.run wholesale, so `cut`/`grep`/`tail` don't actually run).
        outputs = {
            "nproc": "16\n",
            "/proc/loadavg": "0.50\n",          # after `cut -d' ' -f1`
            "free -m": "Mem:  16384  6000  5000  10  5000  10088\n",
            "df -P": "/dev/root 100 43 57 43% /\n",
            "pgrep": "1234 hermes -p x\n1235 hermes -p y\n",
        }
        with mock.patch("subprocess.run", side_effect=self._fake_run(outputs)):
            n = lcd._probe_live("hermes2", None)
        assert n is not None
        assert n.cpu_count == 16
        assert n.load_1min == pytest.approx(0.50)
        assert n.mem_avail_gb == pytest.approx(10088 / 1024.0, rel=1e-3)
        assert n.disk_free_pct == pytest.approx(57.0)
        assert n.active_workers == 2
        assert n.max_workers == 16
        assert n.status == "healthy"
        assert n.heartbeat_age_s == 0.0

    def test_local_probe_no_shlex_quote(self):
        # Local (host=None) must NOT wrap cmd in shlex.quote — that broke
        # local exec. Verify the command passed has no surrounding quotes.
        seen = {}
        def _run(cmd, shell, capture_output, text, timeout):
            seen["cmd"] = cmd
            class R: stdout = "4\n"
            return R()
        with mock.patch("subprocess.run", side_effect=_run):
            lcd._probe_live("hermes2", None)
        assert not seen["cmd"].startswith("'")

    def test_ssh_probe_uses_shlex_quote_and_prefix(self):
        seen = []
        def _run(cmd, shell, capture_output, text, timeout):
            seen.append(cmd)
            class R: stdout = "4\n"
            return R()
        with mock.patch("subprocess.run", side_effect=_run):
            lcd._probe_live("hermes1", "100.107.83.25")
        assert any("ssh -o ConnectTimeout=3" in c for c in seen)
        # remote commands ARE quoted
        assert any("'" in c for c in seen)

    def test_probe_handles_empty_free_output(self):
        outputs = {
            "nproc": "8\n", "/proc/loadavg": "1.0\n",
            "free -m": "", "df -P": "", "pgrep": "",
        }
        with mock.patch("subprocess.run", side_effect=self._fake_run(outputs)):
            n = lcd._probe_live("hermes2", None)
        assert n.mem_avail_gb == 0.0
        assert n.disk_free_pct == 0.0  # df empty -> used=100 -> free=0

    def test_probe_handles_zero_pgrep(self):
        outputs = {
            "nproc": "4\n", "/proc/loadavg": "0.1\n",
            "free -m": "Mem: 100 0 0 0 0 50\n",
            "df -P": "/dev/root 100 10 90 10% /\n",
            "pgrep": "",
        }
        with mock.patch("subprocess.run", side_effect=self._fake_run(outputs)):
            n = lcd._probe_live("hermes2", None)
        assert n.active_workers == 0

    def test_probe_returns_none_on_exception(self):
        with mock.patch("subprocess.run", side_effect=OSError("boom")):
            n = lcd._probe_live("hermes1", "100.107.83.25")
        assert n is None

    def test_probe_handles_garbage_numbers(self):
        outputs = {
            "nproc": "not_a_number\n", "/proc/loadavg": "x y z\n",
            "free -m": "garbage", "df -P": "garbage", "pgrep": "",
        }
        with mock.patch("subprocess.run", side_effect=self._fake_run(outputs)):
            n = lcd._probe_live("hermes2", None)
        assert n is None  # int()/float() raise -> caught -> None

    def test_probe_disk_clamped_nonnegative(self):
        outputs = {
            "nproc": "4\n", "/proc/loadavg": "0.1\n",
            "free -m": "Mem: 100 0 0 0 0 50\n",
            "df -P": "/dev/root 100 5 95 105% /\n",  # >100% used -> free clamps 0
            "pgrep": "",
        }
        with mock.patch("subprocess.run", side_effect=self._fake_run(outputs)):
            n = lcd._probe_live("hermes2", None)
        assert n.disk_free_pct == 0.0


# ---------------------------------------------------------------------------
# TelemetryCollector.collect — DuckDB read, staleness, alias, probe fallback
# ---------------------------------------------------------------------------

class TestTelemetryCollector:
    def _duckdb_rows_module(self, rows):
        """Build a fake duckdb module whose connect() returns `rows`."""
        fake = types.ModuleType("duckdb")
        class _Con:
            def execute(self, q, params=None):
                class _Cur:
                    def fetchall(self): return rows
                return _Cur()
            def close(self): pass
        fake.connect = lambda *a, **k: _Con()
        return fake

    def test_collect_fresh_heartbeat_used_no_probe(self):
        now = time.time()
        rows = [("hermes2", 4, 0.5, 9.0, 43.0, 1, 8, now - 5, "healthy")]
        fake = self._duckdb_rows_module(rows)
        with mock.patch.dict(sys.modules, {"duckdb": fake}), \
             mock.patch.object(lcd, "_probe_live") as probe, \
             mock.patch.object(lcd, "NODE_HOSTS", {"hermes2": None}):
            out = lcd.TelemetryCollector().collect()
        probe.assert_not_called()
        assert out["hermes2"].heartbeat_age_s <= 120
        assert out["hermes2"].cpu_count == 4
        assert out["hermes2"].disk_free_pct == pytest.approx(57.0)  # 100-43

    def test_collect_stale_heartbeat_triggers_probe(self):
        now = time.time()
        rows = [("hermes2", 4, 0.5, 9.0, 43.0, 0, 8, now - 99999, "healthy")]
        fake = self._duckdb_rows_module(rows)
        probed = make_telemetry(node_id="hermes2")
        with mock.patch.dict(sys.modules, {"duckdb": fake}), \
             mock.patch.object(lcd, "_probe_live", return_value=probed) as probe, \
             mock.patch.object(lcd, "NODE_HOSTS", {"hermes2": None}):
            out = lcd.TelemetryCollector().collect()
        probe.assert_called_once()
        assert out["hermes2"] is probed

    def test_collect_missing_row_triggers_probe(self):
        fake = self._duckdb_rows_module([])
        probed = make_telemetry(node_id="hermes1")
        with mock.patch.dict(sys.modules, {"duckdb": fake}), \
             mock.patch.object(lcd, "_probe_live", return_value=probed), \
             mock.patch.object(lcd, "NODE_HOSTS", {"hermes1": "100.107.83.25"}):
            out = lcd.TelemetryCollector().collect()
        assert out["hermes1"] is probed

    def test_collect_hermes1_alias_ip_row(self):
        # hermes1 == ip-172-31-30-216 alias (pitfall #11)
        now = time.time()
        rows = [("ip-172-31-30-216", 16, 0.3, 22.0, 43.0, 0, 16, now - 3, "healthy")]
        fake = self._duckdb_rows_module(rows)
        with mock.patch.dict(sys.modules, {"duckdb": fake}), \
             mock.patch.object(lcd, "NODE_HOSTS", {"hermes1": "100.107.83.25"}):
            out = lcd.TelemetryCollector().collect()
        assert out["hermes1"].cpu_count == 16
        assert out["hermes1"].status == "healthy"

    def test_collect_probe_fails_marks_unreachable_ineligible(self):
        now = time.time()
        rows = [("hermes1", 16, 0.5, 9.0, 43.0, 0, 16, now - 99999, "healthy")]
        fake = self._duckdb_rows_module(rows)
        with mock.patch.dict(sys.modules, {"duckdb": fake}), \
             mock.patch.object(lcd, "_probe_live", return_value=None), \
             mock.patch.object(lcd, "NODE_HOSTS", {"hermes1": "100.107.83.25"}):
            out = lcd.TelemetryCollector().collect()
        n = out["hermes1"]
        assert n.status == "unknown"
        assert n.load_1min == 999.0
        assert n.active_workers == 999
        assert n.eligible is False

    def test_collect_duckdb_exception_falls_through_to_probe(self):
        fake = types.ModuleType("duckdb")
        def _connect(*a, **k): raise RuntimeError("db locked")
        fake.connect = _connect
        probed = make_telemetry(node_id="hermes2")
        with mock.patch.dict(sys.modules, {"duckdb": fake}), \
             mock.patch.object(lcd, "_probe_live", return_value=probed), \
             mock.patch.object(lcd, "NODE_HOSTS", {"hermes2": None}):
            out = lcd.TelemetryCollector().collect()
        assert out["hermes2"] is probed

    def test_collect_live_probe_disabled_marks_unreachable(self):
        now = time.time()
        rows = [("hermes2", 4, 0.5, 9.0, 43.0, 0, 8, now - 99999, "healthy")]
        fake = self._duckdb_rows_module(rows)
        with mock.patch.dict(sys.modules, {"duckdb": fake}), \
             mock.patch.object(lcd, "LIVE_PROBE_ON_STALE", False), \
             mock.patch.object(lcd, "NODE_HOSTS", {"hermes2": None}):
            out = lcd.TelemetryCollector().collect()
        assert out["hermes2"].status == "unknown"
        assert out["hermes2"].eligible is False

    def test_collect_disk_used_pct_converted_to_free(self):
        now = time.time()
        rows = [("hermes2", 4, 0.5, 9.0, 91.0, 0, 8, now - 3, "healthy")]
        fake = self._duckdb_rows_module(rows)
        with mock.patch.dict(sys.modules, {"duckdb": fake}), \
             mock.patch.object(lcd, "NODE_HOSTS", {"hermes2": None}):
            out = lcd.TelemetryCollector().collect()
        assert out["hermes2"].disk_free_pct == pytest.approx(9.0)  # 100-91


# ---------------------------------------------------------------------------
# collect_ready_tasks — missing db, row mapping
# ---------------------------------------------------------------------------

class TestCollectReadyTasks:
    def test_missing_board_db_returns_empty(self, tmp_path):
        with mock.patch.object(lcd, "GATEWAY_BOARDS_DIR", tmp_path):
            assert lcd.collect_ready_tasks("no_such_board") == []

    def test_reads_ready_unclaimed_only(self, tmp_path):
        import sqlite3
        bdir = tmp_path / "b1"
        bdir.mkdir()
        db = bdir / "kanban.db"
        con = sqlite3.connect(str(db))
        con.execute("CREATE TABLE tasks (id TEXT, title TEXT, assignee TEXT, "
                    "priority REAL, body TEXT, workspace_kind TEXT, status TEXT, claim_lock TEXT)")
        con.execute("INSERT INTO tasks VALUES ('t_a','A','x',50,'','scratch','ready',NULL)")
        con.execute("INSERT INTO tasks VALUES ('t_b','B','y',60,'','scratch','done',NULL)")
        con.execute("INSERT INTO tasks VALUES ('t_c','C','z',70,'','scratch','ready','LOCK')")
        con.commit(); con.close()
        with mock.patch.object(lcd, "GATEWAY_BOARDS_DIR", tmp_path):
            rows = lcd.collect_ready_tasks("b1")
        ids = [r["id"] for r in rows]
        assert ids == ["t_a"]  # done + claimed excluded


# ---------------------------------------------------------------------------
# collect_agent_registry / agent_node_history — DuckDB + error paths
# ---------------------------------------------------------------------------

class TestAgentRegistryAndHistory:
    def test_registry_parses_skills_json(self):
        fake = types.ModuleType("duckdb")
        class _Con:
            def execute(self, q, params=None):
                class _Cur:
                    def fetchall(self):
                        return [("demis", '["architecture","rl"]', 10, 0.95)]
                return _Cur()
            def close(self): pass
        fake.connect = lambda *a, **k: _Con()
        with mock.patch.dict(sys.modules, {"duckdb": fake}):
            reg = lcd.collect_agent_registry()
        assert reg == [{"id": "demis", "skills": ["architecture", "rl"],
                        "capacity": 10, "reliability": 0.95}]

    def test_registry_empty_skills_json(self):
        fake = types.ModuleType("duckdb")
        class _Con:
            def execute(self, q, params=None):
                class _Cur:
                    def fetchall(self): return [("x", None, 5, 0.9)]
                return _Cur()
            def close(self): pass
        fake.connect = lambda *a, **k: _Con()
        with mock.patch.dict(sys.modules, {"duckdb": fake}):
            reg = lcd.collect_agent_registry()
        assert reg[0]["skills"] == []

    def test_registry_error_returns_empty(self):
        fake = types.ModuleType("duckdb")
        def _connect(*a, **k): raise RuntimeError("nope")
        fake.connect = _connect
        with mock.patch.dict(sys.modules, {"duckdb": fake}):
            assert lcd.collect_agent_registry() == []

    def test_history_missing_audit_db_returns_empty(self, tmp_path):
        with mock.patch.object(lcd, "AUDIT_DB", tmp_path / "nope.duckdb"):
            assert lcd.agent_node_history("demis") == []

    def test_history_query_error_returns_empty(self, tmp_path):
        with mock.patch.object(lcd, "AUDIT_DB", tmp_path / "x.duckdb"), \
             mock.patch.dict(sys.modules, {"duckdb": None}):
            # AUDIT_DB.exists() False short-circuits; force exists then error
            pass
        # Direct: patch exists True + duckdb raises
        fake = types.ModuleType("duckdb")
        def _connect(*a, **k): raise RuntimeError("corrupt")
        fake.connect = _connect
        p = tmp_path / "real.duckdb"
        p.write_bytes(b"\x00")
        with mock.patch.object(lcd, "AUDIT_DB", p), \
             mock.patch.dict(sys.modules, {"duckdb": fake}):
            assert lcd.agent_node_history("demis") == []


# ---------------------------------------------------------------------------
# _ollama_key — env var, .env file, quoting, precedence
# ---------------------------------------------------------------------------

class TestOllamaKey:
    def test_env_var_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OLLAMA_API_KEY", "env_key_123")
        with mock.patch.object(Path, "home", return_value=tmp_path):
            assert lcd._ollama_key() == "env_key_123"

    def test_env_file_fallback(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        d = tmp_path / ".hermes"
        d.mkdir()
        (d / ".env").write_text("OTHER=x\nOLLAMA_API_KEY=file_key_456\n")
        with mock.patch.object(Path, "home", return_value=tmp_path):
            assert lcd._ollama_key() == "file_key_456"

    def test_env_file_strips_quotes(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        d = tmp_path / ".hermes"
        d.mkdir()
        (d / ".env").write_text('OLLAMA_API_KEY="quoted_key"\n')
        with mock.patch.object(Path, "home", return_value=tmp_path):
            assert lcd._ollama_key() == "quoted_key"

    def test_missing_both_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        with mock.patch.object(Path, "home", return_value=tmp_path):
            assert lcd._ollama_key() == ""


# ---------------------------------------------------------------------------
# llm_route — no key, HTTP, JSON parse, fences, empty content, non-list
# ---------------------------------------------------------------------------

class TestLlmRoute:
    def _http_response(self, payload: dict):
        """Build a fake urlopen context manager returning payload bytes."""
        class _Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return json.dumps(payload).encode()
        return _Resp()

    def _llm_body(self, content):
        return {"choices": [{"message": {"content": content}}]}

    def test_no_api_key_returns_none(self, monkeypatch):
        monkeypatch.setattr(lcd, "_ollama_key", lambda: "")
        assert lcd.llm_route("prompt") is None

    def test_success_plain_json(self, monkeypatch):
        monkeypatch.setattr(lcd, "_ollama_key", lambda: "k")
        body = self._llm_body('[{"task_id":"t1","target_node":"hermes1"}]')
        with mock.patch("urllib.request.urlopen", return_value=self._http_response(body)):
            out = lcd.llm_route("prompt")
        assert out == [{"task_id": "t1", "target_node": "hermes1"}]

    def test_strips_json_code_fence(self, monkeypatch):
        monkeypatch.setattr(lcd, "_ollama_key", lambda: "k")
        body = self._llm_body('```json\n[{"task_id":"t1","target_node":"hermes2"}]\n```')
        with mock.patch("urllib.request.urlopen", return_value=self._http_response(body)):
            out = lcd.llm_route("prompt")
        assert out == [{"task_id": "t1", "target_node": "hermes2"}]

    def test_strips_plain_code_fence(self, monkeypatch):
        monkeypatch.setattr(lcd, "_ollama_key", lambda: "k")
        body = self._llm_body('```\n[{"task_id":"t1","target_node":"hermes2"}]\n```')
        with mock.patch("urllib.request.urlopen", return_value=self._http_response(body)):
            out = lcd.llm_route("prompt")
        assert out == [{"task_id": "t1", "target_node": "hermes2"}]

    def test_empty_content_returns_none(self, monkeypatch):
        # glm-5.2 thinking-token exhaustion -> empty content
        monkeypatch.setattr(lcd, "_ollama_key", lambda: "k")
        body = self._llm_body("")
        with mock.patch("urllib.request.urlopen", return_value=self._http_response(body)):
            assert lcd.llm_route("prompt") is None

    def test_none_content_returns_none(self, monkeypatch):
        monkeypatch.setattr(lcd, "_ollama_key", lambda: "k")
        body = {"choices": [{"message": {"content": None}}]}
        with mock.patch("urllib.request.urlopen", return_value=self._http_response(body)):
            assert lcd.llm_route("prompt") is None

    def test_non_list_json_returns_none(self, monkeypatch):
        monkeypatch.setattr(lcd, "_ollama_key", lambda: "k")
        body = self._llm_body('{"not":"a list"}')
        with mock.patch("urllib.request.urlopen", return_value=self._http_response(body)):
            assert lcd.llm_route("prompt") is None

    def test_malformed_json_returns_none(self, monkeypatch):
        monkeypatch.setattr(lcd, "_ollama_key", lambda: "k")
        body = self._llm_body("this is not json at all")
        with mock.patch("urllib.request.urlopen", return_value=self._http_response(body)):
            assert lcd.llm_route("prompt") is None

    def test_timeout_returns_none(self, monkeypatch):
        monkeypatch.setattr(lcd, "_ollama_key", lambda: "k")
        with mock.patch("urllib.request.urlopen",
                        side_effect=TimeoutError("read timed out")):
            assert lcd.llm_route("prompt") is None

    def test_http_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(lcd, "_ollama_key", lambda: "k")
        with mock.patch("urllib.request.urlopen",
                        side_effect=Exception("HTTP 429")):
            assert lcd.llm_route("prompt") is None

    def test_request_uses_model_and_auth_header(self, monkeypatch):
        monkeypatch.setattr(lcd, "_ollama_key", lambda: "secret_key")
        captured = {}
        real_req = lcd.urllib.request.Request
        def _req(url, data=None, headers=None):
            captured["url"] = url
            captured["headers"] = headers
            captured["payload"] = json.loads(data.decode())
            return real_req(url, data=data, headers=headers)
        body = self._llm_body("[]")
        with mock.patch("urllib.request.Request", side_effect=_req), \
             mock.patch("urllib.request.urlopen", return_value=self._http_response(body)):
            lcd.llm_route("the prompt")
        assert captured["headers"]["Authorization"] == "Bearer secret_key"
        assert captured["payload"]["model"] == lcd.LLM_MODEL
        assert captured["payload"]["messages"][1]["content"] == "the prompt"
        assert captured["payload"]["max_tokens"] == 8192  # thinking-model headroom


# ---------------------------------------------------------------------------
# validate_llm_decisions — the safety net (never trust the LLM)
# ---------------------------------------------------------------------------

class TestValidateLlmDecisions:
    def setup_method(self):
        self.nodes = {
            "hermes1": make_telemetry("hermes1"),
            "hermes2": make_telemetry("hermes2", cpu_count=4, max_workers=4,
                                      disk_free_pct=50.0),
        }
        self.tasks = [make_task("t_1"), make_task("t_2")]

    def test_valid_decision_passes(self):
        raw = [{"task_id": "t_1", "target_node": "hermes1",
                "assigned_agent": "w", "welfare_score": 0.9, "reasoning": "ok"}]
        out = lcd.validate_llm_decisions(raw, self.tasks, self.nodes)
        assert len(out) == 1
        assert out[0].task_id == "t_1"
        assert out[0].target_node == "hermes1"
        assert out[0].source == "llm"
        assert out[0].validated is True

    def test_hallucinated_task_dropped(self):
        raw = [{"task_id": "t_ghost", "target_node": "hermes1"}]
        out = lcd.validate_llm_decisions(raw, self.tasks, self.nodes)
        assert out == []

    def test_ineligible_node_dropped(self):
        self.nodes["hermes2"] = make_telemetry("hermes2", status="overloaded")
        raw = [{"task_id": "t_1", "target_node": "hermes2"}]
        out = lcd.validate_llm_decisions(raw, self.tasks, self.nodes)
        assert out == []

    def test_unknown_node_dropped(self):
        raw = [{"task_id": "t_1", "target_node": "hermes99"}]
        out = lcd.validate_llm_decisions(raw, self.tasks, self.nodes)
        assert out == []

    def test_missing_task_id_key_dropped(self):
        raw = [{"target_node": "hermes1"}]
        out = lcd.validate_llm_decisions(raw, self.tasks, self.nodes)
        assert out == []

    def test_missing_target_node_key_dropped(self):
        raw = [{"task_id": "t_1"}]
        out = lcd.validate_llm_decisions(raw, self.tasks, self.nodes)
        assert out == []

    def test_non_dict_entry_dropped(self):
        raw = ["not_a_dict", {"task_id": "t_1", "target_node": "hermes1"}]
        out = lcd.validate_llm_decisions(raw, self.tasks, self.nodes)
        assert len(out) == 1

    def test_reasoning_truncated_to_200(self):
        raw = [{"task_id": "t_1", "target_node": "hermes1",
                "reasoning": "x" * 500}]
        out = lcd.validate_llm_decisions(raw, self.tasks, self.nodes)
        assert len(out[0].reasoning) == 200

    def test_missing_optional_fields_default(self):
        raw = [{"task_id": "t_1", "target_node": "hermes1"}]
        out = lcd.validate_llm_decisions(raw, self.tasks, self.nodes)
        assert out[0].assigned_agent == ""
        assert out[0].welfare_score == 0.0
        assert out[0].reasoning == ""


# ---------------------------------------------------------------------------
# fallback_proportional — capacity-proportional fill without the LLM
# ---------------------------------------------------------------------------

class TestFallbackProportional:
    def test_no_eligible_returns_empty(self):
        nodes = {"h1": make_telemetry("h1", status="overloaded")}
        out = lcd.fallback_proportional([make_task()], nodes, [])
        assert out == []

    def test_empty_tasks_returns_empty(self):
        nodes = {"h1": make_telemetry("h1")}
        assert lcd.fallback_proportional([], nodes, []) == []

    def test_single_node_all_tasks_land_there(self):
        nodes = {"h1": make_telemetry("h1")}
        tasks = [make_task(f"t_{i}") for i in range(3)]
        out = lcd.fallback_proportional(tasks, nodes, make_registry())
        assert len(out) == 3
        assert all(d.target_node == "h1" for d in out)
        assert all(d.source == "fallback-proportional" for d in out)
        assert all(d.validated for d in out)

    def test_capacity_proportional_spread_not_greedy_collapse(self):
        # Two similar nodes -> should spread, not stack all on one.
        nodes = {
            "h1": make_telemetry("h1", cpu_count=4, max_workers=4),
            "h2": make_telemetry("h2", cpu_count=4, max_workers=4),
        }
        tasks = [make_task(f"t_{i}") for i in range(8)]
        out = lcd.fallback_proportional(tasks, nodes, make_registry())
        from collections import Counter
        dist = Counter(d.target_node for d in out)
        # No single-node collapse: each node gets >= 1/4 of placements
        assert dist["h1"] >= 2 and dist["h2"] >= 2

    def test_bigger_node_absorbs_more(self):
        nodes = {
            "big": make_telemetry("big", cpu_count=16, max_workers=16),
            "small": make_telemetry("small", cpu_count=2, max_workers=2),
        }
        tasks = [make_task(f"t_{i}") for i in range(9)]
        out = lcd.fallback_proportional(tasks, nodes, make_registry())
        from collections import Counter
        dist = Counter(d.target_node for d in out)
        assert dist["big"] > dist["small"]  # monotonicity: capacity wins

    def test_unregistered_assignee_without_profile_skipped(self, tmp_path):
        with mock.patch.object(lcd, "PROFILES_DIR", tmp_path):
            nodes = {"h1": make_telemetry("h1")}
            tasks = [make_task("t_1", assignee="ghost_agent")]
            out = lcd.fallback_proportional(tasks, nodes, [])  # not in registry
        assert out == []

    def test_unregistered_assignee_with_profile_routed(self, tmp_path):
        (tmp_path / "real_agent").mkdir()
        with mock.patch.object(lcd, "PROFILES_DIR", tmp_path):
            nodes = {"h1": make_telemetry("h1")}
            tasks = [make_task("t_1", assignee="real_agent")]
            out = lcd.fallback_proportional(tasks, nodes, [])
        assert len(out) == 1

    def test_empty_assignee_defaults(self):
        nodes = {"h1": make_telemetry("h1")}
        tasks = [make_task("t_1", assignee="")]
        out = lcd.fallback_proportional(tasks, nodes, [])
        assert out[0].assigned_agent == "default"


# ---------------------------------------------------------------------------
# audit / _ensure_audit — DuckDB write, empty no-op
# ---------------------------------------------------------------------------

class TestAudit:
    def test_empty_decisions_noop(self, tmp_path):
        with mock.patch.object(lcd, "AUDIT_DB", tmp_path / "a.duckdb"), \
             mock.patch.object(lcd, "_ensure_audit") as ensure:
            lcd.audit([], "board")
        ensure.assert_not_called()

    def test_writes_decisions(self, tmp_path):
        real_duckdb = pytest.importorskip("duckdb")
        db = tmp_path / "audit.duckdb"
        with mock.patch.object(lcd, "AUDIT_DB", db):
            decs = [lcd.RoutingDecision(task_id="t1", assigned_agent="w",
                                        target_node="h1", welfare_score=0.9,
                                        source="llm", reasoning="r")]
            lcd.audit(decs, "b1")
        con = real_duckdb.connect(str(db), read_only=True)
        rows = con.execute("SELECT task_id, target_node, source, board "
                           "FROM dispatch_decisions").fetchall()
        con.close()
        assert rows == [("t1", "h1", "llm", "b1")]

    def test_ensure_audit_idempotent(self, tmp_path):
        real_duckdb = pytest.importorskip("duckdb")
        db = tmp_path / "audit2.duckdb"
        with mock.patch.object(lcd, "AUDIT_DB", db):
            lcd._ensure_audit()
            lcd._ensure_audit()  # CREATE TABLE IF NOT EXISTS — no error


# ---------------------------------------------------------------------------
# LLMClusterDispatcher.build_prompt — payload shape + eligible list
# ---------------------------------------------------------------------------

class TestBuildPrompt:
    def test_prompt_contains_node_task_agent_payloads(self):
        disp = lcd.LLMClusterDispatcher(board="b")
        nodes = {"hermes1": make_telemetry("hermes1")}
        tasks = [make_task("t_1")]
        reg = make_registry()
        with mock.patch.object(lcd, "agent_node_history", return_value=["hermes1"]):
            prompt = disp.build_prompt(nodes, tasks, reg)
        assert '"hermes1"' in prompt
        assert "t_1" in prompt
        assert "eligible_nodes" in prompt
        assert "werner_vogels" in prompt

    def test_prompt_eligible_list_excludes_ineligible(self):
        disp = lcd.LLMClusterDispatcher(board="b")
        nodes = {
            "good": make_telemetry("good"),
            "bad": make_telemetry("bad", status="overloaded"),
        }
        with mock.patch.object(lcd, "agent_node_history", return_value=[]):
            prompt = disp.build_prompt(nodes, [make_task()], [])
        # The eligible_nodes JSON line lists only 'good'
        line = [l for l in prompt.splitlines() if l.startswith("eligible_nodes")][0]
        assert "good" in line and "bad" not in line

    def test_prompt_title_truncated_to_80(self):
        disp = lcd.LLMClusterDispatcher(board="b")
        nodes = {"h1": make_telemetry("h1")}
        tasks = [make_task("t_1", title="x" * 200)]
        with mock.patch.object(lcd, "agent_node_history", return_value=[]):
            prompt = disp.build_prompt(nodes, tasks, [])
        assert ("x" * 200) not in prompt
        assert ("x" * 80) in prompt


# ---------------------------------------------------------------------------
# LLMClusterDispatcher.tick — batching, LLM+fallback merge, empty queue
# ---------------------------------------------------------------------------

class TestTick:
    def _dispatcher(self, tasks, nodes, registry=None):
        disp = lcd.LLMClusterDispatcher(board="b")
        disp.telemetry = mock.Mock()
        disp.telemetry.collect.return_value = nodes
        return disp

    def test_empty_ready_queue_returns_empty(self):
        nodes = {"h1": make_telemetry("h1")}
        disp = self._dispatcher([], nodes)
        with mock.patch.object(lcd, "collect_ready_tasks", return_value=[]), \
             mock.patch.object(lcd, "collect_agent_registry", return_value=[]), \
             mock.patch.object(lcd, "llm_route") as llm:
            out = disp.tick(dry_run=False)
        assert out == []
        llm.assert_not_called()

    def test_llm_none_falls_back_to_proportional(self):
        nodes = {"h1": make_telemetry("h1")}
        tasks = [make_task("t_1")]
        disp = self._dispatcher(tasks, nodes)
        with mock.patch.object(lcd, "collect_ready_tasks", return_value=tasks), \
             mock.patch.object(lcd, "collect_agent_registry", return_value=make_registry()), \
             mock.patch.object(lcd, "llm_route", return_value=None), \
             mock.patch.object(lcd, "audit"):
            out = disp.tick(dry_run=False)
        assert len(out) == 1
        assert out[0].source == "fallback-proportional"

    def test_llm_success_uses_llm_source(self):
        nodes = {"h1": make_telemetry("h1")}
        tasks = [make_task("t_1")]
        disp = self._dispatcher(tasks, nodes)
        raw = [{"task_id": "t_1", "target_node": "h1", "assigned_agent": "w"}]
        with mock.patch.object(lcd, "collect_ready_tasks", return_value=tasks), \
             mock.patch.object(lcd, "collect_agent_registry", return_value=make_registry()), \
             mock.patch.object(lcd, "llm_route", return_value=raw), \
             mock.patch.object(lcd, "audit"):
            out = disp.tick(dry_run=False)
        assert out[0].source == "llm"

    def test_llm_partial_coverage_fills_remainder_with_fallback(self):
        nodes = {"h1": make_telemetry("h1")}
        tasks = [make_task("t_1"), make_task("t_2")]
        disp = self._dispatcher(tasks, nodes)
        # LLM only routes t_1; t_2 must be filled by fallback
        raw = [{"task_id": "t_1", "target_node": "h1", "assigned_agent": "w"}]
        with mock.patch.object(lcd, "collect_ready_tasks", return_value=tasks), \
             mock.patch.object(lcd, "collect_agent_registry", return_value=make_registry()), \
             mock.patch.object(lcd, "llm_route", return_value=raw), \
             mock.patch.object(lcd, "audit"):
            out = disp.tick(dry_run=False)
        assert len(out) == 2
        by_id = {d.task_id: d for d in out}
        assert by_id["t_1"].source == "llm"
        assert by_id["t_2"].source == "fallback-proportional"

    def test_batching_splits_large_queue(self):
        nodes = {"h1": make_telemetry("h1")}
        tasks = [make_task(f"t_{i}") for i in range(13)]  # > 2 batches of 6
        disp = self._dispatcher(tasks, nodes)
        calls = []
        def _llm(prompt):
            calls.append(prompt)
            return None  # force fallback each batch
        with mock.patch.object(lcd, "collect_ready_tasks", return_value=tasks), \
             mock.patch.object(lcd, "collect_agent_registry", return_value=make_registry()), \
             mock.patch.object(lcd, "llm_route", side_effect=_llm), \
             mock.patch.object(lcd, "audit"):
            out = disp.tick(dry_run=False)
        # 13 tasks / batch 6 -> 3 LLM calls
        assert len(calls) == 3
        assert len(out) == 13

    def test_audit_called_with_all_decisions(self):
        nodes = {"h1": make_telemetry("h1")}
        tasks = [make_task("t_1")]
        disp = self._dispatcher(tasks, nodes)
        with mock.patch.object(lcd, "collect_ready_tasks", return_value=tasks), \
             mock.patch.object(lcd, "collect_agent_registry", return_value=make_registry()), \
             mock.patch.object(lcd, "llm_route", return_value=None), \
             mock.patch.object(lcd, "audit") as audit_mock:
            out = disp.tick(dry_run=False)
        audit_mock.assert_called_once()
        assert audit_mock.call_args[0][1] == "b"  # board


# ---------------------------------------------------------------------------
# main() — CLI smoke (arg parsing, one-shot path)
# ---------------------------------------------------------------------------

class TestMain:
    def test_main_once_runs_tick(self, capsys):
        with mock.patch.object(lcd.LLMClusterDispatcher, "tick", return_value=[]) as tick, \
             mock.patch.object(sys, "argv", ["prog", "--once"]):
            rc = lcd.main()
        assert rc == 0
        tick.assert_called_once()

    def test_main_dry_run_prints_decisions(self, capsys):
        # Covers tick dry_run print branch (line 617)
        nodes = {"h1": make_telemetry("h1")}
        tasks = [make_task("t_1")]
        disp = lcd.LLMClusterDispatcher(board="b")
        disp.telemetry = mock.Mock()
        disp.telemetry.collect.return_value = nodes
        with mock.patch.object(lcd, "collect_ready_tasks", return_value=tasks), \
             mock.patch.object(lcd, "collect_agent_registry", return_value=make_registry()), \
             mock.patch.object(lcd, "llm_route", return_value=None), \
             mock.patch.object(lcd, "audit"):
            out = disp.tick(dry_run=True)
        captured = capsys.readouterr()
        assert "DRY-RUN" in captured.out

    def test_main_daemon_loops_until_interrupt(self):
        # Covers daemon loop body (lines 643-649) — run 2 ticks then stop.
        tick_count = {"n": 0}
        def _tick(dry_run=False):
            tick_count["n"] += 1
            if tick_count["n"] >= 2:
                raise KeyboardInterrupt
            return []
        with mock.patch.object(lcd.LLMClusterDispatcher, "tick", side_effect=_tick), \
             mock.patch.object(lcd.time, "sleep"), \
             mock.patch.object(sys, "argv", ["prog", "--daemon", "--interval", "0.01"]):
            try:
                lcd.main()
            except KeyboardInterrupt:
                pass
        assert tick_count["n"] >= 2

    def test_main_daemon_tick_exception_logged_and_continues(self):
        # Covers the except branch inside the daemon loop (line 647-648)
        calls = {"n": 0}
        def _tick(dry_run=False):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("tick blew up")
            raise KeyboardInterrupt  # second tick stops the loop
        with mock.patch.object(lcd.LLMClusterDispatcher, "tick", side_effect=_tick), \
             mock.patch.object(lcd.time, "sleep"), \
             mock.patch.object(sys, "argv", ["prog", "--daemon", "--interval", "0.01"]):
            try:
                lcd.main()
            except KeyboardInterrupt:
                pass
        assert calls["n"] >= 2  # survived the first exception


class TestOllamaKeyEnvFileBranch:
    def test_env_file_line_without_prefix_skipped(self, monkeypatch, tmp_path):
        # Covers 370->373 branch: a non-matching line in the .env loop
        monkeypatch.delenv("OLLAMA_API_KEY", raising=False)
        d = tmp_path / ".hermes"
        d.mkdir()
        (d / ".env").write_text("FOO=bar\nOLLAMA_API_KEY=real_key\nBAZ=qux\n")
        with mock.patch.object(Path, "home", return_value=tmp_path):
            assert lcd._ollama_key() == "real_key"
