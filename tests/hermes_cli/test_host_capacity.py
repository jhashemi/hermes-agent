"""Tests for hermes_cli.host_capacity — worker-side admission control (t_2cff74c4).

Behavior contracts under test:
  1. probe_host_saturated() classifies saturation from PSI + load signals.
  2. Every probe failure path is fail-open (proceed, never block on a broken probe).
  3. check_and_defer_if_saturated() releases a running claim back to 'ready'
     WITHOUT incrementing failures, and appends a host_capacity_deferred audit event.
  4. The kanban_worker_pre_execute plugin hook can veto execution (policy gates).
  5. Thresholds come from kanban.host_capacity in config.yaml; garbage values
     fall back to defaults (never crash the worker).
"""
from __future__ import annotations

import contextlib
import sqlite3
import types

import pytest

from hermes_cli import host_capacity as hc
from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_kb(monkeypatch, tmp_path):
    """A minimal sqlite stand-in for the kanban DB with one running task."""
    db = sqlite3.connect(str(tmp_path / "kanban.db"))
    db.execute(
        """CREATE TABLE tasks (
               id TEXT PRIMARY KEY, status TEXT,
               claim_lock TEXT, claim_expires TEXT, worker_pid INTEGER)"""
    )
    db.execute("CREATE TABLE task_events (id INTEGER PRIMARY KEY AUTOINCREMENT, "
               "task_id TEXT, kind TEXT, payload TEXT)")
    db.execute("INSERT INTO tasks (id, status, claim_lock, worker_pid) "
               "VALUES ('t_test', 'running', 'hermes2:1', 1234)")
    db.commit()

    events: list[tuple] = []

    class _Conn:
        def execute(self, sql, params=()):
            return db.execute(sql, params)

        def commit(self):
            db.commit()

        def close(self):
            pass

    @contextlib.contextmanager
    def write_txn(conn):
        yield conn
        conn.commit()

    monkeypatch.setattr(kb, "connect", lambda: _Conn())
    monkeypatch.setattr(kb, "write_txn", write_txn)
    monkeypatch.setattr(
        kb, "_append_event",
        lambda conn, task_id, kind, payload: events.append((task_id, kind, payload)))
    return types.SimpleNamespace(db=db, events=events)


def _row(db):
    return db.execute("SELECT status, claim_lock, worker_pid FROM tasks "
                      "WHERE id='t_test'").fetchone()


def _patch_probes(monkeypatch, *, cpu=None, mem=None, load1=None, nproc: int | None = 16):
    monkeypatch.setattr(hc, "_read_psi_avg10",
                        lambda k: {"cpu": cpu, "memory": mem}.get(k))
    monkeypatch.setattr(hc, "_read_load1", lambda: load1)
    monkeypatch.setattr(hc, "_nproc", lambda: nproc)


# ---------------------------------------------------------------------------
# probe_host_saturated — classification contract
# ---------------------------------------------------------------------------

def test_memory_pressure_alone_saturates(monkeypatch):
    # mem.full > 20% is a stand-alone gate — no load condition required.
    _patch_probes(monkeypatch, cpu=0.0, mem=55.0, load1=1.0)
    sat, reason = hc.probe_host_saturated()
    assert sat and "memory.full" in reason


def test_cpu_pressure_with_oversubscription_saturates(monkeypatch):
    _patch_probes(monkeypatch, cpu=16.0, mem=0.0, load1=700.0, nproc=16)  # 700 >= 2*16
    sat, reason = hc.probe_host_saturated()
    assert sat and "cpu.full" in reason


def test_cpu_pressure_without_oversubscription_is_healthy(monkeypatch):
    # High cpu.full but load well under 2*nproc → NOT saturated (matches the
    # h1 incident discriminator: a PSI spike alone is not a veto).
    _patch_probes(monkeypatch, cpu=16.0, mem=0.0, load1=1.5, nproc=16)
    sat, reason = hc.probe_host_saturated()
    assert sat is False and reason == ""


def test_all_probes_failing_is_fail_open(monkeypatch):
    _patch_probes(monkeypatch, cpu=None, mem=None, load1=None, nproc=None)
    assert hc.probe_host_saturated() == (False, "")


def test_healthy_host_is_clean(monkeypatch):
    _patch_probes(monkeypatch, cpu=0.0, mem=0.0, load1=1.0, nproc=16)
    assert hc.probe_host_saturated() == (False, "")


# ---------------------------------------------------------------------------
# threshold loading — config knob + garbage tolerance
# ---------------------------------------------------------------------------

def test_thresholds_default_and_override(monkeypatch):
    import hermes_cli.config as hcfg

    # defaults when no kanban section
    monkeypatch.setattr(hcfg, "load_config_readonly", lambda: {})
    t = hc._load_thresholds()
    assert t == {"cpu_full_avg10_pct": 5.0, "mem_full_avg10_pct": 20.0,
                 "load_per_core": 2.0}

    # override with real values
    monkeypatch.setattr(
        hcfg, "load_config_readonly",
        lambda: {"kanban": {"host_capacity": {"cpu_full_avg10_pct": 9.5,
                                              "load_per_core": 3.0}}})
    t = hc._load_thresholds()
    assert t["cpu_full_avg10_pct"] == 9.5
    assert t["load_per_core"] == 3.0
    assert t["mem_full_avg10_pct"] == 20.0  # unspecified key keeps default

    # garbage values fall back per-key (fail-open)
    monkeypatch.setattr(
        hcfg, "load_config_readonly",
        lambda: {"kanban": {"host_capacity": {"cpu_full_avg10_pct": "banana",
                                              "load_per_core": -5}}})
    t = hc._load_thresholds()
    assert t["cpu_full_avg10_pct"] == 5.0 and t["load_per_core"] == 2.0

    # config loader explodes → defaults, never raises
    def _boom():
        raise RuntimeError("config unavailable")
    monkeypatch.setattr(hcfg, "load_config_readonly", _boom)
    assert hc._load_thresholds()["cpu_full_avg10_pct"] == 5.0


# ---------------------------------------------------------------------------
# check_and_defer_if_saturated — release + audit contract
# ---------------------------------------------------------------------------

def test_saturated_host_releases_claim_and_audits(monkeypatch, fake_kb):
    monkeypatch.setattr(hc, "probe_host_saturated", lambda: (True, "cpu.full 36%"))
    monkeypatch.setattr(hc, "_fire_plugin_veto", lambda *a, **k: (False, ""))

    assert hc.check_and_defer_if_saturated("t_test", run_id=42) is True
    status, lock, pid = fake_kb.db.execute(
        "SELECT status, claim_lock, worker_pid FROM tasks WHERE id='t_test'"
    ).fetchone()
    assert status == "ready" and lock is None and pid is None
    assert [k for _, k, _ in fake_kb.events] == ["host_capacity_deferred"]
    assert fake_kb.events[0][2]["run_id"] == 42
    assert "cpu.full 36%" in fake_kb.events[0][2]["reason"]


def test_healthy_host_does_not_touch_db(monkeypatch, fake_kb):
    monkeypatch.setattr(hc, "probe_host_saturated", lambda: (False, ""))
    monkeypatch.setattr(hc, "_fire_plugin_veto", lambda *a, **k: (False, ""))
    assert hc.check_and_defer_if_saturated("t_test") is False
    status = fake_kb.db.execute(
        "SELECT status FROM tasks WHERE id='t_test'").fetchone()[0]
    assert status == "running"
    assert fake_kb.events == []


def test_plugin_hook_vetoes_without_saturation(monkeypatch, fake_kb):
    monkeypatch.setattr(hc, "probe_host_saturated", lambda: (False, ""))
    monkeypatch.setattr(hc, "_fire_plugin_veto",
                        lambda *a, **k: (True, "policy gate: persona frozen"))
    assert hc.check_and_defer_if_saturated("t_test") is True
    status = fake_kb.db.execute(
        "SELECT status FROM tasks WHERE id='t_test'").fetchone()[0]
    assert status == "ready"
    assert "policy gate" in fake_kb.events[0][2]["reason"]


def test_release_failure_still_returns_true(monkeypatch, fake_kb):
    # Even if the DB release blows up, the worker must exit (return True) —
    # doing the work anyway would double-execute on an oversubscribed host.
    monkeypatch.setattr(hc, "probe_host_saturated", lambda: (True, "mem 55%"))
    monkeypatch.setattr(hc, "_fire_plugin_veto", lambda *a, **k: (False, ""))

    def _boom():
        raise RuntimeError("db locked")
    monkeypatch.setattr(kb, "connect", _boom)
    assert hc.check_and_defer_if_saturated("t_test") is True


# ---------------------------------------------------------------------------
# wiring seam — the admission check must be reachable from cli.main
# ---------------------------------------------------------------------------

def test_cli_wiring_present():
    """The seam must be wired into cli.py's kanban task path (source contract)."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "cli.py"
    if not src.exists():  # package-layout tolerance
        return
    text = src.read_text(errors="replace")
    assert "check_and_defer_if_saturated" in text
    assert "host_capacity admission check failed (fail-open)" in text