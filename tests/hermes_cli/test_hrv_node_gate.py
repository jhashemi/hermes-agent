"""Tests for HRV node evaluation gate."""
import pytest
import time
import datetime
from hermes_cli.hrv_node_gate import (
    HRVNodeGate,
    NodeProbeSnapshot,
    HRVDigestSnapshot,
)


def get_now_ts():
    """Return current time as ISO8601 timestamp."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def get_old_ts(seconds_ago=90):
    """Return a timestamp from N seconds ago."""
    then = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=seconds_ago)
    return then.isoformat()


class TestMemoryPressure:
    """Test memory_pressure rejection condition (swap_pct >= 90%)."""
    
    def test_healthy_memory_passes(self):
        """Node with swap_pct < 90% should pass."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=45.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result is None
    
    def test_high_swap_rejected(self):
        """Node with swap_pct >= 90% should be rejected."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=95.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result == "memory_pressure"
    
    def test_stale_probe_fails_open(self):
        """Stale memory probe should NOT reject (fail-open)."""
        gate = HRVNodeGate(max_probe_age_seconds=60)
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=95.0,  # Would be rejected if fresh
            ts=get_old_ts(90),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        # Should NOT reject due to staleness
        assert result is None  # fail-open on stale probe
    
    def test_missing_probe_fails_open(self):
        """Missing probe should NOT reject (fail-open)."""
        gate = HRVNodeGate()
        # Don't set any probe
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result is None  # fail-open on missing probe


class TestBedrockRateLimit:
    """Test bedrock_rate_limit_saturation rejection condition."""
    
    def test_healthy_bedrock_tpm_passes(self):
        """Node with sufficient Bedrock TPM should pass."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            bedrock_tpm_remaining=50000,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result is None
    
    def test_low_bedrock_tpm_rejected(self):
        """Node with bedrock_tpm_remaining < 1000 should be rejected."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            bedrock_tpm_remaining=500,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result == "bedrock_rate_limit_saturation[claude-haiku]"
    
    def test_stale_bedrock_fails_open(self):
        """Stale Bedrock TPM probe should NOT reject."""
        gate = HRVNodeGate(max_probe_age_seconds=60)
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            bedrock_tpm_remaining=500,  # Would be rejected if fresh
            ts=get_old_ts(90),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result is None  # fail-open


class TestHRVUrgency:
    """Test hrv urgency gate (urgent + priority < P0)."""
    
    def test_urgent_p0_task_passes(self):
        """P0 task during urgent state should pass."""
        gate = HRVNodeGate()
        digest = HRVDigestSnapshot(
            interval_class="urgent",
            ts=get_now_ts(),
        )
        gate.set_hrv_digest(digest)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku", task_priority=0)
        assert result is None
    
    def test_urgent_p1_task_rejected(self):
        """P1 task during urgent state should be rejected."""
        gate = HRVNodeGate()
        digest = HRVDigestSnapshot(
            interval_class="urgent",
            ts=get_now_ts(),
        )
        gate.set_hrv_digest(digest)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku", task_priority=1)
        assert result == "hrv_urgent_state"
    
    def test_calm_state_allows_all_priorities(self):
        """Calm state should allow any priority."""
        gate = HRVNodeGate()
        digest = HRVDigestSnapshot(
            interval_class="calm",
            ts=get_now_ts(),
        )
        gate.set_hrv_digest(digest)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku", task_priority=5)
        assert result is None
    
    def test_stale_urgency_fails_open(self):
        """Stale urgency digest should NOT reject."""
        gate = HRVNodeGate(max_probe_age_seconds=60)
        digest = HRVDigestSnapshot(
            interval_class="urgent",
            ts=get_old_ts(90),
        )
        gate.set_hrv_digest(digest)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku", task_priority=1)
        assert result is None  # fail-open


class TestMinResources:
    """Test min_resources overflow rejection condition."""
    
    def test_sufficient_memory_passes(self):
        """Node with sufficient memory should pass."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            mem_gb_available=4.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        task_min_resources = {"mem_gb": 1.0, "cpu_cores": 1}
        result = gate.evaluate_node(
            "task1", "hermes2", "claude-haiku",
            task_min_resources=task_min_resources
        )
        assert result is None
    
    def test_insufficient_memory_rejected(self):
        """Node with insufficient memory should be rejected."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            mem_gb_available=0.5,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        task_min_resources = {"mem_gb": 1.0}
        result = gate.evaluate_node(
            "task1", "hermes2", "claude-haiku",
            task_min_resources=task_min_resources
        )
        assert result == "min_resources_overflow"
    
    def test_insufficient_bedrock_tpm_rejected(self):
        """Node with TPM remaining below the saturation floor is rejected.

        Starvation-edge fix (t_d18420a6): condition-5's TPM check is clamped
        to BEDROCK_TPM_SATURATION_THRESHOLD (1000). A node with TPM remaining
        below the saturation floor is rejected regardless of the task's
        declared reservation, because condition-3 would also reject.
        """
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            bedrock_tpm_remaining=500,  # below the 1000 saturation floor
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)

        task_min_resources = {"bedrock_tpm_reservation": 10000}
        result = gate.evaluate_node(
            "task1", "hermes2", "claude-haiku",
            task_min_resources=task_min_resources
        )
        # condition-3 rejects first with its own reason; either reason is a valid
        # "insufficient TPM" outcome and both are acceptable for this assertion.
        assert result in ("min_resources_overflow", "bedrock_rate_limit_saturation[claude-haiku]")

    def test_tpm_above_saturation_but_below_reservation_passes(self):
        """Starvation-edge fix: TPM in (saturation, reservation) must NOT be rejected.

        Before the fix, a node publishing bedrock_tpm_remaining in
        (BEDROCK_TPM_SATURATION_THRESHOLD, task.bedrock_tpm_reservation) would
        pass condition-3 but be rejected by condition-5 — starving DEFAULT
        tasks on a healthy node. This regression test locks in the clamp.
        """
        from hermes_cli.hrv_node_gate import BEDROCK_TPM_SATURATION_THRESHOLD

        gate = HRVNodeGate()
        # 8000 TPM: comfortably above the 1000 saturation floor, but well below
        # the 10000 default reservation. Pre-fix this was rejected; post-fix
        # it must pass.
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            bedrock_tpm_remaining=8000,
            mem_gb_available=8.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        assert 8000 > BEDROCK_TPM_SATURATION_THRESHOLD  # sanity check on constants

        task_min_resources = {"bedrock_tpm_reservation": 10000, "mem_gb": 0.5}
        result = gate.evaluate_node(
            "task1", "hermes2", "claude-haiku",
            task_min_resources=task_min_resources
        )
        assert result is None
    
    def test_no_min_resources_passes(self):
        """Node with no min_resources requirement should pass."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            mem_gb_available=0.1,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        result = gate.evaluate_node(
            "task1", "hermes2", "claude-haiku",
            task_min_resources=None
        )
        assert result is None
    
    def test_stale_resources_fails_open(self):
        """Stale resource probe should NOT reject."""
        gate = HRVNodeGate(max_probe_age_seconds=60)
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            mem_gb_available=0.1,
            ts=get_old_ts(90),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        task_min_resources = {"mem_gb": 1.0}
        result = gate.evaluate_node(
            "task1", "hermes2", "claude-haiku",
            task_min_resources=task_min_resources
        )
        assert result is None  # fail-open


class TestIntegration:
    """Integration tests: healthy node with all signals green."""
    
    def test_healthy_node_passes_all_gates(self):
        """Node with all signals green should pass."""
        gate = HRVNodeGate()
        
        # Healthy probe
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=30.0,
            mem_gb_available=8.0,
            bedrock_tpm_remaining=50000,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        # Calm HRV state
        digest = HRVDigestSnapshot(
            interval_class="calm",
            ts=get_now_ts(),
        )
        gate.set_hrv_digest(digest)
        
        # Task with moderate requirements
        task_min_resources = {"mem_gb": 1.0, "cpu_cores": 1, "bedrock_tpm_reservation": 10000}
        
        result = gate.evaluate_node(
            "task1", "hermes2", "claude-haiku",
            task_priority=5,
            task_min_resources=task_min_resources
        )
        assert result is None
    
    def test_rejection_priority_order(self):
        """When multiple conditions are true, the first one wins."""
        gate = HRVNodeGate()
        
        # Probe with multiple issues
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=95.0,  # Memory pressure
            bedrock_tpm_remaining=500,  # Rate limit
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        # Urgent state
        digest = HRVDigestSnapshot(
            interval_class="urgent",
            ts=get_now_ts(),
        )
        gate.set_hrv_digest(digest)
        
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku", task_priority=1)
        # Should reject on the first condition (memory_pressure)
        assert result == "memory_pressure"


class TestProbeSnapshotDeserialization:
    """Test KV payload deserialization."""
    
    def test_deserialize_from_kv_payload(self):
        """Load probe snapshot from NATS KV payload."""
        gate = HRVNodeGate()
        
        kv_payload = {
            "hostname": "hermes2",
            "swap_pct": 45.0,
            "mem_gb_available": 8.5,
            "load_1m": 1.2,
            "load_5m": 0.9,
            "disk_free_gb": 100.0,
            "active_workers": 3,
            "max_workers": 8,
            "bedrock_tpm_remaining": 45000,
            "ts": get_now_ts(),
        }
        
        probe = gate.load_node_probe_from_kv(kv_payload)
        
        assert probe.hostname == "hermes2"
        assert probe.swap_pct == 45.0
        assert probe.mem_gb_available == 8.5
        assert probe.bedrock_tpm_remaining == 45000


class TestMetricEmission:
    """Test metric emission on rejection."""
    
    def test_emit_rejection_metric(self):
        """Metric callback should be called with reason."""
        gate = HRVNodeGate()
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=95.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)
        
        # Call evaluate to trigger rejection
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result == "memory_pressure"
        
        # Capture metric emissions
        metrics_called = []
        def capture_metric(reason):
            metrics_called.append(reason)
        
        gate.emit_rejection_metric("task1", "hermes2", result, metrics_fn=capture_metric)
        assert metrics_called == ["memory_pressure"]


class TestOOMDetection:
    """Test OOM-kill sub-condition of memory_pressure (condition 1b)."""

    def test_no_oom_kill_passes(self, monkeypatch):
        """No OOM markers in kernel log → don't reject."""
        gate = HRVNodeGate()
        # Both probes report clean output
        monkeypatch.setattr(
            gate, "_probe_oom_journal", lambda window: False
        )
        monkeypatch.setattr(
            gate, "_probe_oom_dmesg", lambda window: False
        )
        assert gate._oom_kill_within_window() is False

    def test_oom_kill_from_journal_rejects(self, monkeypatch):
        """journalctl reports an OOM kill → memory_pressure."""
        gate = HRVNodeGate()
        monkeypatch.setattr(
            gate, "_probe_oom_journal", lambda window: True
        )
        # Should short-circuit before dmesg; assertion sentinel:
        monkeypatch.setattr(
            gate, "_probe_oom_dmesg",
            lambda window: (_ for _ in ()).throw(AssertionError("dmesg should not run"))
        )

        # Fresh probe with healthy swap; OOM alone should trigger rejection.
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=10.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)

        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result == "memory_pressure"

    def test_oom_kill_falls_through_to_dmesg(self, monkeypatch):
        """journalctl returns None (unusable) → dmesg queried; dmesg True → reject."""
        gate = HRVNodeGate()
        monkeypatch.setattr(gate, "_probe_oom_journal", lambda window: None)
        monkeypatch.setattr(gate, "_probe_oom_dmesg", lambda window: True)

        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=10.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)

        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result == "memory_pressure"

    def test_both_probes_unusable_fails_open(self, monkeypatch):
        """Both journalctl and dmesg unusable → UNKNOWN → don't reject."""
        gate = HRVNodeGate()
        monkeypatch.setattr(gate, "_probe_oom_journal", lambda window: None)
        monkeypatch.setattr(gate, "_probe_oom_dmesg", lambda window: None)

        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=10.0,
            ts=get_now_ts(),
        )
        gate.set_node_probe_snapshot("hermes2", probe)

        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result is None

    def test_oom_rejects_even_with_stale_kv_probe(self, monkeypatch):
        """OOM kill probes local kernel state; not dependent on KV probe freshness."""
        gate = HRVNodeGate(max_probe_age_seconds=60)
        monkeypatch.setattr(gate, "_probe_oom_journal", lambda window: True)

        # Stale KV probe would fail-open on swap, but OOM sub-check is
        # independent and must still fire.
        probe = NodeProbeSnapshot(
            hostname="hermes2",
            swap_pct=10.0,
            ts=get_old_ts(90),
        )
        gate.set_node_probe_snapshot("hermes2", probe)

        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result == "memory_pressure"

    def test_line_matches_oom_recognizes_canonical_markers(self):
        """OOM line matcher recognizes the standard kernel markers."""
        gate = HRVNodeGate()
        # Positive cases (real kernel log samples, redacted)
        assert gate._line_matches_oom("Out of memory: Killed process 1234 (python)")
        assert gate._line_matches_oom("[oom-kill]:constraint=CONSTRAINT_MEMCG,...")
        assert gate._line_matches_oom("python invoked oom-killer: gfp_mask=0x...")
        assert gate._line_matches_oom("Killed process 5678 (bash) total-vm:12345kB")
        # Case-insensitive
        assert gate._line_matches_oom("OUT OF MEMORY: killed")
        # Negative cases
        assert not gate._line_matches_oom("systemd[1]: Started something.service")
        assert not gate._line_matches_oom("kernel: TCP: eth0: link is up")
        assert not gate._line_matches_oom("")


class TestDispatcherHealth:
    """Test kanban_dispatcher_health condition (condition 2)."""

    def _fake_subprocess_run(self, returncode: int, stdout: str, stderr: str = ""):
        """Build a fake subprocess.run replacement returning fixed output."""
        class FakeCompletedProcess:
            def __init__(self):
                self.returncode = returncode
                self.stdout = stdout
                self.stderr = stderr

        def fake_run(cmd, **kwargs):
            return FakeCompletedProcess()
        return fake_run

    def test_dispatcher_active_passes(self, monkeypatch):
        """systemctl reports 'active' → don't reject."""
        import hermes_cli.hrv_node_gate as mod
        monkeypatch.setattr(
            mod.subprocess, "run",
            self._fake_subprocess_run(0, "active\n"),
        )
        # shutil.which must return truthy
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: "/usr/bin/systemctl")

        gate = HRVNodeGate()
        assert gate._check_dispatcher_health("hermes2") is False

    def test_dispatcher_activating_rejects(self, monkeypatch):
        """systemctl reports 'activating' → reject (kanban_dispatcher_health)."""
        import hermes_cli.hrv_node_gate as mod
        monkeypatch.setattr(
            mod.subprocess, "run",
            self._fake_subprocess_run(3, "activating\n"),
        )
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: "/usr/bin/systemctl")

        gate = HRVNodeGate()
        assert gate._check_dispatcher_health("hermes2") is True

        # And it flows through evaluate_node with the right reason
        result = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert result == "kanban_dispatcher_health"

    def test_dispatcher_failed_rejects(self, monkeypatch):
        """systemctl reports 'failed' → reject."""
        import hermes_cli.hrv_node_gate as mod
        monkeypatch.setattr(
            mod.subprocess, "run",
            self._fake_subprocess_run(3, "failed\n"),
        )
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: "/usr/bin/systemctl")

        gate = HRVNodeGate()
        assert gate._check_dispatcher_health("hermes2") is True

    def test_dispatcher_deactivating_rejects(self, monkeypatch):
        """systemctl reports 'deactivating' → reject (unit shutting down)."""
        import hermes_cli.hrv_node_gate as mod
        monkeypatch.setattr(
            mod.subprocess, "run",
            self._fake_subprocess_run(3, "deactivating\n"),
        )
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: "/usr/bin/systemctl")

        gate = HRVNodeGate()
        assert gate._check_dispatcher_health("hermes2") is True

    def test_dispatcher_inactive_fails_open(self, monkeypatch):
        """systemctl reports 'inactive' (unit not installed) → UNKNOWN → don't reject."""
        import hermes_cli.hrv_node_gate as mod
        monkeypatch.setattr(
            mod.subprocess, "run",
            self._fake_subprocess_run(3, "inactive\n"),
        )
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: "/usr/bin/systemctl")

        gate = HRVNodeGate()
        assert gate._check_dispatcher_health("hermes2") is False

    def test_dispatcher_no_systemctl_fails_open(self, monkeypatch):
        """No systemctl binary on host → UNKNOWN → don't reject."""
        import hermes_cli.hrv_node_gate as mod
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: None)

        gate = HRVNodeGate()
        assert gate._check_dispatcher_health("hermes2") is False

    def test_dispatcher_subprocess_timeout_fails_open(self, monkeypatch):
        """systemctl times out → UNKNOWN → don't reject (fail-open)."""
        import hermes_cli.hrv_node_gate as mod

        def raising_run(*args, **kwargs):
            raise mod.subprocess.TimeoutExpired(cmd="systemctl", timeout=3)

        monkeypatch.setattr(mod.subprocess, "run", raising_run)
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: "/usr/bin/systemctl")

        gate = HRVNodeGate()
        assert gate._check_dispatcher_health("hermes2") is False

    def test_dispatcher_env_override(self, monkeypatch):
        """HRV_DISPATCHER_UNIT_NAME env var overrides the unit name."""
        import hermes_cli.hrv_node_gate as mod

        captured_cmd = {}

        def capturing_run(cmd, **kwargs):
            captured_cmd["cmd"] = cmd

            class FakeCompletedProcess:
                returncode = 0
                stdout = "active\n"
                stderr = ""

            return FakeCompletedProcess()

        monkeypatch.setattr(mod.subprocess, "run", capturing_run)
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: "/usr/bin/systemctl")
        monkeypatch.setenv("HRV_DISPATCHER_UNIT_NAME", "my-custom-dispatcher.service")

        gate = HRVNodeGate()
        gate._check_dispatcher_health("hermes2")
        assert "my-custom-dispatcher.service" in captured_cmd["cmd"]


class TestHostGlobalProbeCache:
    """TTL cache for node-independent probes (OOM-in-window, dispatcher-health).

    Contract (from t_2f0cff4f):
    1) Two consecutive evaluate_node calls within TTL issue only one
       journalctl / systemctl subprocess.
    2) After TTL expiry the probe re-fires.
    3) UNKNOWN responses are NOT cached — a next call must re-probe.

    Every test constructs a fresh HRVNodeGate so cache slots start empty,
    matching the isolation used by the other test classes in this file.
    """

    def _fake_subprocess_run(self, returncode: int, stdout: str, counter: dict):
        """subprocess.run stub that counts calls in ``counter['n']``."""
        counter["n"] = 0

        class FakeCompletedProcess:
            def __init__(self, rc, so, se=""):
                self.returncode = rc
                self.stdout = so
                self.stderr = se

        def fake_run(cmd, **kwargs):
            counter["n"] += 1
            return FakeCompletedProcess(returncode, stdout)

        return fake_run

    # ------------------------------------------------------------------ #
    # (1) Cached within TTL: one subprocess for two evaluations
    # ------------------------------------------------------------------ #

    def test_dispatcher_probe_cached_within_ttl_across_evaluate_node(self, monkeypatch):
        """Two evaluate_node calls within TTL → exactly one systemctl call."""
        import hermes_cli.hrv_node_gate as mod

        counter = {"n": 0}
        monkeypatch.setattr(
            mod.subprocess, "run",
            self._fake_subprocess_run(0, "active\n", counter),
        )
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: "/usr/bin/systemctl")

        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        # Stub the OOM tri-state probes so evaluate_node's OOM branch doesn't
        # itself call subprocess.run — we're counting systemctl calls only.
        monkeypatch.setattr(gate, "_probe_oom_journal", lambda w: False)
        monkeypatch.setattr(gate, "_probe_oom_dmesg", lambda w: False)

        # A fresh probe snapshot so per-node checks pass cleanly.
        probe = NodeProbeSnapshot(hostname="hermes2", swap_pct=10.0, ts=get_now_ts())
        gate.set_node_probe_snapshot("hermes2", probe)

        # First evaluate_node → probe fires.
        r1 = gate.evaluate_node("task1", "hermes2", "claude-haiku")
        assert r1 is None
        assert counter["n"] == 1

        # Second evaluate_node (different task_id, same node) → served from
        # cache; no additional systemctl subprocess.
        r2 = gate.evaluate_node("task2", "hermes2", "claude-haiku")
        assert r2 is None
        assert counter["n"] == 1, (
            f"Expected 1 systemctl call within TTL, got {counter['n']}"
        )

    def test_dispatcher_probe_cached_within_ttl_direct(self, monkeypatch):
        """Direct _check_dispatcher_health calls within TTL hit cache."""
        import hermes_cli.hrv_node_gate as mod

        counter = {"n": 0}
        monkeypatch.setattr(
            mod.subprocess, "run",
            self._fake_subprocess_run(0, "active\n", counter),
        )
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: "/usr/bin/systemctl")

        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        assert gate._check_dispatcher_health("hermes2") is False
        assert gate._check_dispatcher_health("hermes2") is False
        assert gate._check_dispatcher_health("hermes3") is False  # node-indep
        assert counter["n"] == 1

    def test_oom_probe_cached_within_ttl(self, monkeypatch):
        """Two _oom_kill_within_window calls within TTL hit cache.

        Stubs the tri-state journal probe seam (not subprocess directly) to
        count exactly how many times we consult the underlying probe, so the
        assertion is unambiguous.
        """
        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        counter = {"n": 0}

        def counting_journal_probe(window):
            counter["n"] += 1
            return False  # definitive: no OOM in window

        monkeypatch.setattr(gate, "_probe_oom_journal", counting_journal_probe)
        monkeypatch.setattr(gate, "_probe_oom_dmesg", lambda window: False)

        assert gate._oom_kill_within_window() is False
        assert gate._oom_kill_within_window() is False
        assert counter["n"] == 1, (
            f"Expected 1 journal probe within TTL, got {counter['n']}"
        )

    def test_oom_probe_cached_through_evaluate_node(self, monkeypatch):
        """evaluate_node's memory-pressure branch uses the OOM cache."""
        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        counter = {"n": 0}

        def counting_journal_probe(window):
            counter["n"] += 1
            return False

        monkeypatch.setattr(gate, "_probe_oom_journal", counting_journal_probe)
        monkeypatch.setattr(gate, "_probe_oom_dmesg", lambda window: False)
        # Stub systemctl too so it doesn't fan out real subprocesses.
        import hermes_cli.hrv_node_gate as mod
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: None)  # systemctl absent

        probe = NodeProbeSnapshot(hostname="hermes2", swap_pct=10.0, ts=get_now_ts())
        gate.set_node_probe_snapshot("hermes2", probe)

        gate.evaluate_node("task1", "hermes2", "claude-haiku")
        gate.evaluate_node("task2", "hermes2", "claude-haiku")
        gate.evaluate_node("task3", "hermes2", "claude-haiku")
        assert counter["n"] == 1

    # ------------------------------------------------------------------ #
    # (2) After TTL expiry, probe re-fires
    # ------------------------------------------------------------------ #

    def test_dispatcher_probe_refires_after_ttl_expiry(self, monkeypatch):
        """A cache entry older than the TTL forces a re-probe."""
        import hermes_cli.hrv_node_gate as mod

        counter = {"n": 0}
        monkeypatch.setattr(
            mod.subprocess, "run",
            self._fake_subprocess_run(0, "active\n", counter),
        )
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: "/usr/bin/systemctl")

        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        assert gate._check_dispatcher_health("hermes2") is False
        assert counter["n"] == 1

        # Simulate TTL expiry by rewinding the cached entry's expires_at.
        # (Avoids sleeping for the real TTL in tests.)
        assert gate._dispatcher_cache_entry is not None
        cached_value, _expires_at = gate._dispatcher_cache_entry
        gate._dispatcher_cache_entry = (cached_value, time.monotonic() - 1.0)

        assert gate._check_dispatcher_health("hermes2") is False
        assert counter["n"] == 2, (
            f"Expected re-probe after TTL expiry, still {counter['n']} calls"
        )

    def test_oom_probe_refires_after_ttl_expiry(self, monkeypatch):
        """OOM cache entry beyond TTL forces a re-probe."""
        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        counter = {"n": 0}

        def counting_journal_probe(window):
            counter["n"] += 1
            return False

        monkeypatch.setattr(gate, "_probe_oom_journal", counting_journal_probe)
        monkeypatch.setattr(gate, "_probe_oom_dmesg", lambda window: False)

        assert gate._oom_kill_within_window() is False
        assert counter["n"] == 1

        assert gate._oom_cache_entry is not None
        cached_value, _ = gate._oom_cache_entry
        gate._oom_cache_entry = (cached_value, time.monotonic() - 1.0)

        assert gate._oom_kill_within_window() is False
        assert counter["n"] == 2

    def test_expired_entry_is_cleared_on_read(self, monkeypatch):
        """An expired tuple is proactively cleared so debug dumps stay clean."""
        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        monkeypatch.setattr(gate, "_probe_oom_journal", lambda window: False)
        monkeypatch.setattr(gate, "_probe_oom_dmesg", lambda window: False)

        gate._oom_cache_entry = (True, time.monotonic() - 1.0)  # already expired
        gate._oom_kill_within_window()
        # After the call: cache should hold the fresh probe result (False),
        # NOT the stale True.
        assert gate._oom_cache_entry is not None
        assert gate._oom_cache_entry[0] is False

    # ------------------------------------------------------------------ #
    # (3) UNKNOWN responses are NOT cached
    # ------------------------------------------------------------------ #

    def test_oom_unknown_not_cached(self, monkeypatch):
        """Both probes returning None → UNKNOWN, next call re-probes."""
        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        counter = {"j": 0, "d": 0}

        def journal_unknown(window):
            counter["j"] += 1
            return None

        def dmesg_unknown(window):
            counter["d"] += 1
            return None

        monkeypatch.setattr(gate, "_probe_oom_journal", journal_unknown)
        monkeypatch.setattr(gate, "_probe_oom_dmesg", dmesg_unknown)

        # First call: UNKNOWN → returns False (fail-open), does NOT cache.
        assert gate._oom_kill_within_window() is False
        assert gate._oom_cache_entry is None, "UNKNOWN result was cached!"
        assert counter["j"] == 1 and counter["d"] == 1

        # Second call: must re-probe both seams (no cached UNKNOWN sits around).
        assert gate._oom_kill_within_window() is False
        assert counter["j"] == 2 and counter["d"] == 2

    def test_dispatcher_unknown_not_cached_no_systemctl(self, monkeypatch):
        """systemctl missing → UNKNOWN, next call re-probes (which may find it)."""
        import hermes_cli.hrv_node_gate as mod

        # First call: no systemctl binary → UNKNOWN, no cache.
        which_calls = {"n": 0}

        def flipping_which(cmd):
            which_calls["n"] += 1
            return None  # systemctl absent

        monkeypatch.setattr(mod.shutil, "which", flipping_which)

        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        assert gate._check_dispatcher_health("hermes2") is False
        assert gate._dispatcher_cache_entry is None
        # Re-probe: still fires shutil.which.
        assert gate._check_dispatcher_health("hermes2") is False
        assert which_calls["n"] == 2, (
            f"UNKNOWN cached — expected re-probe, only {which_calls['n']} which() calls"
        )

    def test_dispatcher_unknown_not_cached_timeout(self, monkeypatch):
        """systemctl timeout → UNKNOWN, next call re-probes."""
        import hermes_cli.hrv_node_gate as mod

        run_calls = {"n": 0}

        def raising_run(*args, **kwargs):
            run_calls["n"] += 1
            raise mod.subprocess.TimeoutExpired(cmd="systemctl", timeout=3)

        monkeypatch.setattr(mod.subprocess, "run", raising_run)
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: "/usr/bin/systemctl")

        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        assert gate._check_dispatcher_health("hermes2") is False
        assert gate._check_dispatcher_health("hermes2") is False
        assert run_calls["n"] == 2, (
            f"UNKNOWN (timeout) cached — expected re-probe, only {run_calls['n']} subprocess calls"
        )
        assert gate._dispatcher_cache_entry is None

    def test_dispatcher_inactive_treated_as_unknown_not_cached(self, monkeypatch):
        """'inactive'/'unknown' systemd states are UNKNOWN per gate spec → don't cache."""
        import hermes_cli.hrv_node_gate as mod

        counter = {"n": 0}
        monkeypatch.setattr(
            mod.subprocess, "run",
            self._fake_subprocess_run(3, "inactive\n", counter),
        )
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: "/usr/bin/systemctl")

        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        assert gate._check_dispatcher_health("hermes2") is False
        assert gate._check_dispatcher_health("hermes2") is False
        assert counter["n"] == 2, "inactive treated as cacheable — must re-probe"
        assert gate._dispatcher_cache_entry is None

    # ------------------------------------------------------------------ #
    # Definitive True (reject) IS cached — verify caching is symmetric
    # ------------------------------------------------------------------ #

    def test_oom_definitive_true_is_cached(self, monkeypatch):
        """A definitive True (OOM detected) is cached like False."""
        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        counter = {"n": 0}

        def journal_hit(window):
            counter["n"] += 1
            return True

        monkeypatch.setattr(gate, "_probe_oom_journal", journal_hit)
        monkeypatch.setattr(gate, "_probe_oom_dmesg", lambda window: True)

        assert gate._oom_kill_within_window() is True
        assert gate._oom_kill_within_window() is True
        assert counter["n"] == 1

    def test_dispatcher_definitive_true_is_cached(self, monkeypatch):
        """A definitive True (dispatcher failed) is cached like False."""
        import hermes_cli.hrv_node_gate as mod

        counter = {"n": 0}
        monkeypatch.setattr(
            mod.subprocess, "run",
            self._fake_subprocess_run(3, "failed\n", counter),
        )
        monkeypatch.setattr(mod.shutil, "which", lambda cmd: "/usr/bin/systemctl")

        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        assert gate._check_dispatcher_health("hermes2") is True
        assert gate._check_dispatcher_health("hermes2") is True
        assert counter["n"] == 1

    # ------------------------------------------------------------------ #
    # Cache-disable + manual invalidation
    # ------------------------------------------------------------------ #

    def test_ttl_zero_disables_cache(self, monkeypatch):
        """host_global_probe_ttl_seconds=0 → every call re-probes."""
        gate = HRVNodeGate(host_global_probe_ttl_seconds=0.0)
        counter = {"n": 0}

        def counting_journal_probe(window):
            counter["n"] += 1
            return False

        monkeypatch.setattr(gate, "_probe_oom_journal", counting_journal_probe)
        monkeypatch.setattr(gate, "_probe_oom_dmesg", lambda window: False)

        for _ in range(3):
            gate._oom_kill_within_window()
        assert counter["n"] == 3
        assert gate._oom_cache_entry is None

    def test_invalidate_host_global_probe_cache_forces_reprobe(self, monkeypatch):
        """invalidate_host_global_probe_cache() clears both slots."""
        gate = HRVNodeGate(host_global_probe_ttl_seconds=30.0)
        counter = {"j": 0}

        def counting_journal_probe(window):
            counter["j"] += 1
            return False

        monkeypatch.setattr(gate, "_probe_oom_journal", counting_journal_probe)
        monkeypatch.setattr(gate, "_probe_oom_dmesg", lambda window: False)

        gate._oom_kill_within_window()
        assert counter["j"] == 1
        gate.invalidate_host_global_probe_cache()
        assert gate._oom_cache_entry is None
        assert gate._dispatcher_cache_entry is None
        gate._oom_kill_within_window()
        assert counter["j"] == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
