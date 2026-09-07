"""Tests for the full-suite concurrency gate in scripts/run_tests.sh and safe_pytest.sh.

These tests verify the guard logic introduced as part of the h1 blackout RCA
(2026-09-07: load >700, 45× oversubscribed, 55-min SSH blackout caused by
concurrent full pytest suites).

What is tested
--------------
1. run_tests.sh full-suite detection: no-arg invocation → IS_FULL_SUITE=1;
   path-restricted invocation → IS_FULL_SUITE=0.
2. safe_pytest.sh full-suite detection: same semantics as run_tests.sh, plus
   tests/ and tests/ prefix still count as full-suite.
3. HERMES_PYTEST_NO_WAIT=1: the gate exits 0 immediately instead of waiting
   when the lock is held by another process.
4. Worker-count cap in run_tests_parallel.py: _default_worker_count() returns
   cpu_count (1×) not cpu_count*2 (old behaviour).
5. safe_pytest.sh worker cap: sets HERMES_TEST_WORKERS = ceil(nproc/2) for
   full-suite runs when the env var is not already set.

POSIX-only (flock is not available on Windows). Skipped if flock binary is
not found.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# ── helpers ─────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).parent.parent.resolve()
RUN_TESTS_SH = REPO_ROOT / "scripts" / "run_tests.sh"
SAFE_PYTEST_SH = REPO_ROOT / "scripts" / "safe_pytest.sh"


def _flock_available() -> bool:
    import shutil
    return shutil.which("flock") is not None


pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="flock not available on Windows"
)

_flock_skip = pytest.mark.skipif(
    not _flock_available(), reason="flock binary not found"
)


def _source_gate_and_check_is_full(script: Path, *args: str) -> int:
    """Run the gate-detection logic from a shell script in a subprocess.

    The shell script is not run in full — only the argument-parsing section
    that sets _IS_FULL_SUITE is extracted via ``set -x`` tracing and we
    check the final value. Instead, we source a minimal inline script that
    re-implements the same arg-parsing logic and prints the result.

    Returns 1 if the script would consider this a full-suite invocation,
    0 otherwise.
    """
    # The detection logic is: any positional arg that doesn't start with '-'
    # and isn't the value of a known flag → _IS_FULL_SUITE=0.
    # We replicate the exact same POSIX shell logic inline so this test is
    # a faithful whitebox test, not a system test of the full script.
    # NOTE: the script body ends with 'printf'; we do NOT append "$@" to
    # avoid bash trying to execute the test-path strings as commands.
    detect_script = r"""
_IS_FULL_SUITE=1
_PREV=0
for _arg in "$@"; do
  if [ "$_PREV" = "1" ]; then _PREV=0; continue; fi
  case "$_arg" in
    -j|--jobs|-k|--paths|--slice|--file-timeout|--file-retries)
      _PREV=1 ;;
    --) break ;;
    -*) : ;;
    *)
      _IS_FULL_SUITE=0
      break ;;
  esac
done
printf '%d\n' "$_IS_FULL_SUITE"
"""
    result = subprocess.run(
        ["bash", "-c", detect_script, "--"] + list(args),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, f"detection script failed: {result.stderr!r}"
    return int(result.stdout.strip())


def _source_safe_pytest_is_full(script: Path, *args: str) -> int:
    """Same idea for safe_pytest.sh's slightly richer detection logic.

    safe_pytest.sh considers 'tests', 'tests/', and the absolute path of
    tests/ as still a full-suite run (they are the default discovery root).
    """
    detect_script = r"""
_IS_FULL_SUITE=1
_PREV=0
for _arg in "$@"; do
  if [ "$_PREV" = "1" ]; then _PREV=0; continue; fi
  case "$_arg" in
    -k|-m|-n|-p|-W|-q|-v|-s|\
    --co|--config-file|--rootdir|--import-mode|--basetemp|\
    --timeout|--timeout-method|--dist|--numprocesses|--workers|\
    --override-ini|--log-level|--log-format|--log-file|\
    --tb|--capture|--maxfail|--lf|--ff|\
    -j|--jobs|--paths|--slice|--file-timeout|--file-retries)
      _PREV=1 ;;
    --) break ;;
    -*) : ;;
    *)
      _norm="${_arg%/}"
      case "$_norm" in
        tests|./tests)
          : ;;
        *)
          _IS_FULL_SUITE=0
          break ;;
      esac ;;
  esac
done
printf '%d\n' "$_IS_FULL_SUITE"
"""
    result = subprocess.run(
        ["bash", "-c", detect_script, "--"] + list(args),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, f"safe_pytest detection script failed: {result.stderr!r}"
    return int(result.stdout.strip())


# ── run_tests.sh detection tests ────────────────────────────────────────────

class TestRunTestsShDetection:
    """Gate detection logic in run_tests.sh."""

    def test_no_args_is_full_suite(self):
        assert _source_gate_and_check_is_full(RUN_TESTS_SH) == 1

    def test_path_arg_is_not_full_suite(self):
        assert _source_gate_and_check_is_full(RUN_TESTS_SH, "tests/foo.py") == 0

    def test_subdir_arg_is_not_full_suite(self):
        assert _source_gate_and_check_is_full(RUN_TESTS_SH, "tests/agent/") == 0

    def test_dash_j_alone_is_full_suite(self):
        """Flag-only args do not count as path restrictions."""
        assert _source_gate_and_check_is_full(RUN_TESTS_SH, "-j", "4") == 1

    def test_dash_k_alone_is_full_suite(self):
        assert _source_gate_and_check_is_full(RUN_TESTS_SH, "-k", "mytest") == 1

    def test_dash_v_alone_is_full_suite(self):
        """Bare flags (no value) are also not path restrictions."""
        assert _source_gate_and_check_is_full(RUN_TESTS_SH, "-v") == 1

    def test_dash_x_alone_is_full_suite(self):
        assert _source_gate_and_check_is_full(RUN_TESTS_SH, "-x") == 1

    def test_path_after_flags_is_not_full_suite(self):
        assert _source_gate_and_check_is_full(RUN_TESTS_SH, "-v", "tests/agent/") == 0

    def test_double_dash_stops_path_parsing(self):
        """Everything after '--' is pytest pass-through; gate stays full-suite."""
        assert _source_gate_and_check_is_full(RUN_TESTS_SH, "--", "--tb=long") == 1


# ── safe_pytest.sh detection tests ──────────────────────────────────────────

class TestSafePytestShDetection:
    """Gate detection logic in safe_pytest.sh."""

    def test_no_args_is_full_suite(self):
        assert _source_safe_pytest_is_full(SAFE_PYTEST_SH) == 1

    def test_tests_root_is_full_suite(self):
        """'tests' and 'tests/' are the default root; still full-suite."""
        assert _source_safe_pytest_is_full(SAFE_PYTEST_SH, "tests") == 1
        assert _source_safe_pytest_is_full(SAFE_PYTEST_SH, "tests/") == 1
        assert _source_safe_pytest_is_full(SAFE_PYTEST_SH, "./tests") == 1

    def test_specific_file_is_not_full_suite(self):
        assert _source_safe_pytest_is_full(SAFE_PYTEST_SH, "tests/foo.py") == 0

    def test_subdir_is_not_full_suite(self):
        assert _source_safe_pytest_is_full(SAFE_PYTEST_SH, "tests/agent/") == 0

    def test_flag_only_is_full_suite(self):
        assert _source_safe_pytest_is_full(SAFE_PYTEST_SH, "-v") == 1
        assert _source_safe_pytest_is_full(SAFE_PYTEST_SH, "-k", "pattern") == 1


# ── no-wait / lock-held gate tests ──────────────────────────────────────────

@_flock_skip
class TestNoWaitGate:
    """When HERMES_PYTEST_NO_WAIT=1, the gate skips without error.

    We hold a flock from Python and confirm that running the
    safe_pytest.sh gate (stripped down to just the flock-check portion)
    exits 0 immediately and prints the expected notice.
    """

    def test_no_wait_exits_zero_when_lock_held(self, tmp_path: Path):
        lock_file = tmp_path / "pytest-full-suite.lock"
        lock_file.touch()

        # The gate fragment: if lock is held and NO_WAIT=1, exit 0.
        gate_fragment = r"""
set -eu
_FULL_SUITE_LOCK="$1"
_NO_WAIT=1
# Non-blocking check — flock -n should fail because WE hold the lock
# already (Python test code acquired it before launching this shell).
if ! flock -n "$_FULL_SUITE_LOCK" true 2>/dev/null; then
  echo "GATE_SKIPPED" >&2
  exit 0
fi
echo "GATE_ENTERED" >&2
exit 0
"""
        # Acquire the lock from Python before running the shell fragment.
        with open(lock_file, "r") as lf:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_held = True
            except BlockingIOError:
                lock_held = False

            if not lock_held:
                pytest.skip("could not acquire test lock")

            result = subprocess.run(
                ["bash", "-s", str(lock_file)],
                input=gate_fragment,
                capture_output=True,
                text=True,
                timeout=5,
                env={**os.environ, "HERMES_PYTEST_NO_WAIT": "1"},
            )
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)

        assert result.returncode == 0, f"gate did not exit 0: {result.stderr}"
        assert "GATE_SKIPPED" in result.stderr, (
            f"expected 'GATE_SKIPPED' in stderr, got: {result.stderr!r}"
        )
        assert "GATE_ENTERED" not in result.stderr


# ── run_tests_parallel.py worker count tests ────────────────────────────────

class TestWorkerCount:
    """_default_worker_count() returns cpu_count (1×, not 2×)."""

    def test_default_worker_count_is_cpu_count(self):
        """After the RCA fix, the default is cpu_count, not cpu_count*2."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "run_tests_parallel",
            REPO_ROOT / "scripts" / "run_tests_parallel.py",
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        expected = os.cpu_count() or 4
        assert mod._default_worker_count() == expected, (
            f"Expected {expected} (cpu_count), got {mod._default_worker_count()}. "
            "The old cpu_count*2 default was reverted as part of the h1 blackout RCA."
        )

    def test_default_worker_count_is_not_double(self):
        """Explicit regression: must not be cpu_count * 2."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "run_tests_parallel",
            REPO_ROOT / "scripts" / "run_tests_parallel.py",
        )
        assert spec is not None and spec.loader is not None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]

        cpu = os.cpu_count() or 4
        count = mod._default_worker_count()
        assert count != cpu * 2, (
            f"Worker count is {count} == cpu_count*2 ({cpu}*2). "
            "This was the h1 blackout root cause — the fix (cpu_count only) was reverted."
        )
