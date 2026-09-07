"""Tests for t_6ca85bd2 — kanban worker cgroup detach via ``systemd-run --scope``.

Root cause: kanban workers are spawned as subprocess children of the
long-lived gateway process. On Linux with systemd, they inherit the
gateway's unit cgroup (``hermes-gateway.service``). When systemd stops
that unit — a routine restart, deploy, ``systemctl stop``, or OOM — it
tears down the whole cgroup with ``KillMode=mixed`` and SIGKILLs every
worker in flight. ``start_new_session=True`` doesn't help: session/process
groups are orthogonal to cgroup membership.

Fix: wrap the child argv with ``systemd-run --user --scope --slice=…``.
The scope moves the worker into a fresh transient scope under a dedicated
``hermes-workers.slice``, so a gateway stop only touches the gateway.
Because ``--scope`` execs into the target, ``Popen.pid`` still returns
the worker's real pid and every reap path continues to work.

Rollout is gated by ``HERMES_KANBAN_SPAWN_DETACH=1`` — an opt-in for the
initial ship. These tests cover the guard's decision surface, the argv
wrapper's shape, and the ``_default_spawn`` integration path.
"""
from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# _should_detach_worker_cgroup — guard predicates
# ---------------------------------------------------------------------------


class TestShouldDetachWorkerCgroup:
    """The guard fails safely on any host that can't satisfy the wrapper.

    A True return means ``_default_spawn`` will prepend ``systemd-run
    --user --scope …``. Every path that returns False must be a genuine
    reason the wrapper can't run, so a mis-configured host silently falls
    back to the legacy attached spawn instead of raising or refusing work.
    """

    @pytest.fixture(autouse=True)
    def _linux_capable_host(self, monkeypatch):
        """Preset the environment as a Linux host that CAN detach.

        Individual tests then knock out one precondition at a time to
        exercise the corresponding False branch.
        """
        monkeypatch.setattr(kb, "_IS_WINDOWS", False)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        monkeypatch.setattr(
            kb, "_find_executable",
            lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None,
        )
        # By default: env var opts in.
        monkeypatch.setenv("HERMES_KANBAN_SPAWN_DETACH", "1")

    def test_returns_true_when_all_preconditions_met(self):
        assert kb._should_detach_worker_cgroup() is True

    def test_env_var_unset_returns_false(self, monkeypatch):
        # Default is opt-in — must be explicitly on.
        monkeypatch.delenv("HERMES_KANBAN_SPAWN_DETACH", raising=False)
        assert kb._should_detach_worker_cgroup() is False

    @pytest.mark.parametrize("val", ["", "0", "false", "no", "off", "random"])
    def test_env_var_off_or_junk_returns_false(self, monkeypatch, val):
        monkeypatch.setenv("HERMES_KANBAN_SPAWN_DETACH", val)
        assert kb._should_detach_worker_cgroup() is False

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on", "  1  "])
    def test_env_var_truthy_variants_all_enable(self, monkeypatch, val):
        monkeypatch.setenv("HERMES_KANBAN_SPAWN_DETACH", val)
        assert kb._should_detach_worker_cgroup() is True

    def test_windows_host_returns_false_even_if_opted_in(self, monkeypatch):
        monkeypatch.setattr(kb, "_IS_WINDOWS", True)
        assert kb._should_detach_worker_cgroup() is False

    def test_non_linux_posix_returns_false(self, monkeypatch):
        # macOS: has systemd-run? no, but even if _find_executable lies
        # about it, the platform check should refuse.
        monkeypatch.setattr(sys, "platform", "darwin")
        assert kb._should_detach_worker_cgroup() is False

    def test_missing_xdg_runtime_dir_returns_false(self, monkeypatch):
        # No user manager reachable — the transient scope has nowhere to live.
        monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
        assert kb._should_detach_worker_cgroup() is False

    def test_missing_systemd_run_returns_false(self, monkeypatch):
        monkeypatch.setattr(kb, "_find_executable", lambda name: None)
        assert kb._should_detach_worker_cgroup() is False


# ---------------------------------------------------------------------------
# _wrap_argv_for_cgroup_detach — argv shape
# ---------------------------------------------------------------------------


class TestWrapArgvForCgroupDetach:
    """The wrapper must produce an argv that:

    * runs ``systemd-run`` as argv[0] so ``execvp`` finds it via PATH,
    * requests ``--user`` (per-user manager, matches the gateway unit),
    * requests ``--scope`` (exec-into-target so ``Popen.pid`` is the worker),
    * pins the ``hermes-workers.slice`` for observability + grouping,
    * uses ``--`` before the target argv so no future worker flag can
      collide with a systemd-run option.
    """

    def test_prepends_expected_systemd_run_prefix(self):
        original = ["/usr/bin/hermes", "-p", "backend-eng", "chat", "-q", "work kanban task t_x"]
        wrapped = kb._wrap_argv_for_cgroup_detach(original)

        assert wrapped[0] == "systemd-run"
        assert "--user" in wrapped
        assert "--scope" in wrapped
        assert f"--slice={kb._KANBAN_WORKER_SLICE}" in wrapped
        assert kb._KANBAN_WORKER_SLICE == "hermes-workers.slice"

    def test_uses_double_dash_separator_before_target(self):
        original = ["/usr/bin/hermes", "chat"]
        wrapped = kb._wrap_argv_for_cgroup_detach(original)
        assert "--" in wrapped
        # The target argv must appear intact AFTER the ``--`` separator.
        sep_idx = wrapped.index("--")
        assert wrapped[sep_idx + 1:] == original

    def test_original_argv_is_not_mutated(self):
        original = ["/usr/bin/hermes", "chat"]
        original_copy = list(original)
        _ = kb._wrap_argv_for_cgroup_detach(original)
        assert original == original_copy

    def test_target_argv_appears_last_in_order(self):
        original = ["/usr/bin/hermes", "-p", "backend-eng", "--cli", "chat", "-q", "prompt"]
        wrapped = kb._wrap_argv_for_cgroup_detach(original)
        # Every original element must appear, in order, at the tail.
        assert wrapped[-len(original):] == original


# ---------------------------------------------------------------------------
# _default_spawn integration — spawn argv respects the guard
# ---------------------------------------------------------------------------


class _CapturingFakePopen:
    """Popen shim that records argv without actually spawning anything."""

    instances: list = []

    def __init__(self, cmd, **kwargs):
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.pid = 424242
        _CapturingFakePopen.instances.append(self)


@pytest.fixture
def _reset_capturing_popen():
    _CapturingFakePopen.instances = []
    yield
    _CapturingFakePopen.instances = []


def _make_task(assignee: str = "backend-eng"):
    return kb.Task(
        id="t_detach_probe",
        title="probe",
        body=None,
        assignee=assignee,
        status="ready",
        priority=0,
        created_by=None,
        created_at=0,
        started_at=None,
        completed_at=None,
        workspace_kind="scratch",
        workspace_path=None,
        claim_lock=None,
        claim_expires=None,
        tenant=None,
    )


class TestDefaultSpawnCgroupDetachIntegration:
    """End-to-end: when the guard says yes, the argv sent to Popen carries
    the ``systemd-run --scope`` prefix. When the guard says no, the argv is
    the raw ``hermes …`` command as before.
    """

    def test_detach_off_by_default_spawn_uses_raw_hermes_argv(
        self, tmp_path, monkeypatch, _reset_capturing_popen
    ):
        # Env var unset ⇒ guard returns False ⇒ no wrapper.
        monkeypatch.delenv("HERMES_KANBAN_SPAWN_DETACH", raising=False)
        monkeypatch.setattr("subprocess.Popen", _CapturingFakePopen)

        task = _make_task()
        kb._default_spawn(task, str(tmp_path))

        assert len(_CapturingFakePopen.instances) == 1
        cmd = _CapturingFakePopen.instances[0].cmd
        assert cmd[0] != "systemd-run", (
            f"detach must be off by default; got wrapper prefix in argv: {cmd}"
        )
        assert "chat" in cmd, f"chat subcommand missing from argv: {cmd}"

    def test_detach_on_and_capable_host_prepends_systemd_run(
        self, tmp_path, monkeypatch, _reset_capturing_popen
    ):
        # Opt in.
        monkeypatch.setenv("HERMES_KANBAN_SPAWN_DETACH", "1")
        # Pretend we're on a Linux host with systemd-run available.
        monkeypatch.setattr(kb, "_IS_WINDOWS", False)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        monkeypatch.setattr(
            kb, "_find_executable",
            lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None,
        )
        monkeypatch.setattr("subprocess.Popen", _CapturingFakePopen)

        task = _make_task()
        kb._default_spawn(task, str(tmp_path))

        cmd = _CapturingFakePopen.instances[0].cmd
        assert cmd[0] == "systemd-run", (
            f"detach on should prefix systemd-run; got: {cmd}"
        )
        assert "--scope" in cmd, f"--scope flag missing: {cmd}"
        assert f"--slice={kb._KANBAN_WORKER_SLICE}" in cmd, (
            f"--slice missing: {cmd}"
        )
        # Sanity: chat is still there — we only wrapped, not replaced.
        assert "chat" in cmd, f"chat subcommand missing from wrapped argv: {cmd}"

    def test_detach_on_but_host_incapable_falls_back_to_raw(
        self, tmp_path, monkeypatch, _reset_capturing_popen
    ):
        """When opt-in is on but ``systemd-run`` isn't on PATH (e.g.
        container that doesn't ship systemd), the guard MUST refuse the
        wrapper. Refusing to spawn would be worse than degrading to the
        legacy path — this ticket is about resilience, not correctness.
        """
        monkeypatch.setenv("HERMES_KANBAN_SPAWN_DETACH", "1")
        monkeypatch.setattr(kb, "_IS_WINDOWS", False)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        # systemd-run absent from PATH:
        monkeypatch.setattr(kb, "_find_executable", lambda name: None)
        monkeypatch.setattr("subprocess.Popen", _CapturingFakePopen)

        task = _make_task()
        kb._default_spawn(task, str(tmp_path))

        cmd = _CapturingFakePopen.instances[0].cmd
        assert cmd[0] != "systemd-run", (
            f"host without systemd-run must fall back to raw spawn: {cmd}"
        )

    def test_returned_pid_is_popen_pid_regardless_of_wrapping(
        self, tmp_path, monkeypatch, _reset_capturing_popen
    ):
        """The whole reap architecture (``_pid_alive``, ``_popen_retention``,
        ``_classify_worker_exit``) assumes ``_default_spawn`` returns the
        pid tracked by ``subprocess._Popen``. That contract must hold in
        both spawn paths — this is what makes ``systemd-run --scope`` the
        right primitive: it execs into the target, so ``Popen.pid`` IS
        the worker's real pid.
        """
        monkeypatch.setenv("HERMES_KANBAN_SPAWN_DETACH", "1")
        monkeypatch.setattr(kb, "_IS_WINDOWS", False)
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
        monkeypatch.setattr(
            kb, "_find_executable",
            lambda name: "/usr/bin/systemd-run" if name == "systemd-run" else None,
        )
        monkeypatch.setattr("subprocess.Popen", _CapturingFakePopen)

        task = _make_task()
        pid = kb._default_spawn(task, str(tmp_path))
        assert pid == _CapturingFakePopen.instances[0].pid == 424242


# ---------------------------------------------------------------------------
# Popen retention still works with the wrapper
# ---------------------------------------------------------------------------


class TestPopenRetentionCompatibility:
    """``_popen_retention`` is keyed by the pid returned from Popen. The
    wrapper design relies on ``systemd-run --scope`` exec-ing into the
    target, so this key stays valid. Verify integration explicitly so a
    future refactor of the wrapper (e.g. switching to ``--service`` which
    forks) can't silently break exit classification.
    """

    def test_spawned_pid_is_recorded_in_popen_retention(
        self, tmp_path, monkeypatch
    ):
        # Wrapper off — simplest path.
        monkeypatch.delenv("HERMES_KANBAN_SPAWN_DETACH", raising=False)

        class _RetentionProbeFake:
            def __init__(self, cmd, **kwargs):
                self.pid = 777001
                self.cmd = list(cmd)

            def poll(self):
                return None  # still running

        monkeypatch.setattr("subprocess.Popen", _RetentionProbeFake)

        # Snapshot the retention keys before / after.
        keys_before = set(kb._popen_retention.keys())
        try:
            task = _make_task()
            pid = kb._default_spawn(task, str(tmp_path))
            assert pid == 777001
            assert 777001 in kb._popen_retention, (
                "worker pid must be captured in _popen_retention so "
                "_sweep_popen_retention/_classify_worker_exit can record its exit"
            )
        finally:
            # Cleanup — never leak fake pids into the module-level dict.
            for k in set(kb._popen_retention.keys()) - keys_before:
                kb._popen_retention.pop(k, None)
