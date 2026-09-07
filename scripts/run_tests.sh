#!/usr/bin/env bash
# Canonical test runner for hermes-agent. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * Per-file isolation via scripts/run_tests_parallel.py — each test
#     file runs in its own freshly-spawned `python -m pytest <file>`
#     subprocess. No xdist, no shared workers, no module-level leakage
#     between files.
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running pytest outside our
#     conftest path — e.g. on a single file)
#   * Proper venv activation (probes .venv, venv, then ~/.hermes/...)
#   * Full-suite concurrency gate (flock): prevents multiple concurrent
#     full-suite runs from swamping the host (h1 blackout RCA,
#     2026-09-07). Second invocation WAITS (up to HERMES_PYTEST_LOCK_WAIT
#     seconds, default 7200) rather than silently skipping, so CI and
#     kanban workers still get a result. Set HERMES_PYTEST_NO_WAIT=1 to
#     no-op instead (exits 0 immediately, prints a notice to stderr).
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap parallelism
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -q            # path + bare pytest flag
#   scripts/run_tests.sh tests/foo.py -v --tb=long  # bare flags "just work"
#   scripts/run_tests.sh -k 'pattern'               # value flags pass through too
#   scripts/run_tests.sh tests/foo.py -- --tb=long  # explicit '--' still works
#
# Bare pytest flags (anything starting with '-' that isn't one of this
# runner's own options: -j/--jobs, --paths, --slice, --file-timeout, etc.)
# are forwarded to each per-file pytest invocation automatically — no '--'
# separator required. The explicit '--' form still works and stacks with
# bare flags. Positional path arguments override the default discovery
# root (tests/).

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Full-suite concurrency gate (flock) ─────────────────────────────────────
# Prevent multiple concurrent full hermes-agent pytest suites from
# killing the host (h1 blackout RCA, 2026-09-07: load >700, 45×
# oversubscribed, 55-minute SSH blackout).
#
# Gate only fires when the invocation looks like a full-suite run
# (no positional path arg that restricts to a subset, and flock is
# available). Targeted runs — `scripts/run_tests.sh tests/foo.py` or
# `scripts/run_tests.sh tests/agent/` — are NOT gated: they are cheap
# and safe to run concurrently.
#
# Lock file lives in ~/.hermes/ (always writable by the user running
# workers, even in a minimal container without /var/lock).
#
# Knobs:
#   HERMES_PYTEST_NO_WAIT=1   — exit 0 immediately if lock is held
#                               (kanban workers that want to no-op rather
#                               than wait a long time can set this)
#   HERMES_PYTEST_LOCK_WAIT   — seconds flock waits before giving up
#                               (default 7200 = 2 h; 0 = no-op immediately)
#
# Preflight load check:
#   If load-per-core >= HERMES_PYTEST_LOAD_LIMIT (default 2.0), the
#   script sleeps HERMES_PYTEST_LOAD_BACKOFF (default 60) seconds and
#   rechecks up to HERMES_PYTEST_LOAD_RETRIES (default 30) times before
#   proceeding. This protects against launching a new suite while another
#   already-running suite is still peaking. Set limit=0 to skip entirely.

_FULL_SUITE_LOCK="${HOME}/.hermes/pytest-full-suite.lock"
_LOCK_WAIT="${HERMES_PYTEST_LOCK_WAIT:-7200}"
_NO_WAIT="${HERMES_PYTEST_NO_WAIT:-0}"
_LOAD_LIMIT="${HERMES_PYTEST_LOAD_LIMIT:-2.0}"
_LOAD_BACKOFF="${HERMES_PYTEST_LOAD_BACKOFF:-60}"
_LOAD_RETRIES="${HERMES_PYTEST_LOAD_RETRIES:-30}"

# Detect whether this is a full-suite invocation by checking for path
# positional arguments. We parse just enough: any argument that does
# NOT start with '-' and IS NOT a value for a flag we know (-j, -k,
# --jobs, --paths, --slice, --file-timeout, --file-retries) is a path.
# False-positive (treating a value as a path) is safe: it just skips
# the gate, which is the conservative direction.
_IS_FULL_SUITE=1
_PREV_WAS_FLAG_WITH_VALUE=0
for _arg in "$@"; do
  if [ "$_PREV_WAS_FLAG_WITH_VALUE" = "1" ]; then
    _PREV_WAS_FLAG_WITH_VALUE=0
    continue  # skip the value; it is not a path
  fi
  case "$_arg" in
    -j|--jobs|-k|--paths|--slice|--file-timeout|--file-retries)
      _PREV_WAS_FLAG_WITH_VALUE=1 ;;
    --)
      break ;;  # rest are pytest pass-through; not our path args
    -*)
      : ;;  # bare flag, no value
    *)
      # Positional non-flag arg: this is a path restriction → NOT full suite
      _IS_FULL_SUITE=0
      break ;;
  esac
done

if [ "$_IS_FULL_SUITE" = "1" ] && command -v flock >/dev/null 2>&1; then
  # Ensure the lock directory exists (it always should, but be defensive).
  mkdir -p "$(dirname "$_FULL_SUITE_LOCK")"
  touch "$_FULL_SUITE_LOCK"

  # ── Preflight: check load before acquiring the lock ──────────────────
  if [ "${_LOAD_LIMIT%.*}" != "0" ] || [ "${_LOAD_LIMIT#*.}" != "0" ]; then
    _NPROC="$(nproc 2>/dev/null || echo 1)"
    _retry=0
    while [ "$_retry" -lt "$_LOAD_RETRIES" ]; do
      # /proc/loadavg field 1 is the 1-min load average (most reactive).
      _LOAD1="$(awk '{print $1}' /proc/loadavg 2>/dev/null || echo 0)"
      # Shell integer arithmetic — compare load×100 vs limit×100 to
      # avoid needing bc or awk for floating-point comparison.
      _LOAD1_INT="$(echo "$_LOAD1" | awk '{printf "%d", $1 * 100}')"
      _LIMIT_INT="$(echo "$_NPROC" "$_LOAD_LIMIT" | awk '{printf "%d", $1 * $2 * 100}')"
      if [ "$_LOAD1_INT" -lt "$_LIMIT_INT" ]; then
        break  # load is acceptable
      fi
      _retry=$(( _retry + 1 ))
      echo "▶ load preflight: 1-min load ${_LOAD1} >= ${_NPROC}×${_LOAD_LIMIT}; waiting ${_LOAD_BACKOFF}s (attempt ${_retry}/${_LOAD_RETRIES})" >&2
      sleep "$_LOAD_BACKOFF"
    done
    if [ "$_retry" -ge "$_LOAD_RETRIES" ]; then
      _LOAD1="$(awk '{print $1}' /proc/loadavg 2>/dev/null || echo '?')"
      echo "▶ load preflight: proceeding after ${_LOAD_RETRIES} retries (current load: ${_LOAD1})" >&2
    fi
  fi

  if [ "$_NO_WAIT" = "1" ] || [ "$_LOCK_WAIT" = "0" ]; then
    # Non-blocking: exit 0 if another suite is already running.
    if ! flock -n "$_FULL_SUITE_LOCK" true 2>/dev/null; then
      echo "▶ full-suite gate: another full pytest suite is already running on this host." >&2
      echo "  Skipping this run (HERMES_PYTEST_NO_WAIT=1 or HERMES_PYTEST_LOCK_WAIT=0)." >&2
      echo "  To wait for the lock, unset HERMES_PYTEST_NO_WAIT and set HERMES_PYTEST_LOCK_WAIT=7200." >&2
      exit 0
    fi
  fi

  # Re-exec this script under flock so the lock is held for the
  # entire run (including venv setup, bytecode compilation, and the
  # parallel run itself). flock --timeout N exits 1 if it times out.
  # We map that to a clear error message + exit 1 (not 0, so CI fails
  # visibly rather than silently skipping).
  #
  # Guard against re-entrant exec: if HERMES_PYTEST_FLOCK_ACTIVE is
  # already set, we are already inside the flock'd child — skip the
  # re-exec to avoid an infinite loop.
  if [ -z "${HERMES_PYTEST_FLOCK_ACTIVE:-}" ]; then
    export HERMES_PYTEST_FLOCK_ACTIVE=1
    echo "▶ full-suite gate: acquiring lock (timeout ${_LOCK_WAIT}s) ..." >&2
    # flock --timeout is a Linux-specific extension (util-linux);
    # macOS flock uses -w N. Try the Linux form first.
    if flock --timeout "$_LOCK_WAIT" "$_FULL_SUITE_LOCK" \
        bash "${BASH_SOURCE[0]}" "$@"; then
      exit $?
    else
      _FLOCK_RC=$?
      if [ "$_FLOCK_RC" = "1" ]; then
        echo "ERROR: full-suite gate: lock wait timed out after ${_LOCK_WAIT}s." >&2
        echo "  Another full pytest suite has been running for >${_LOCK_WAIT}s." >&2
        echo "  Check for hung workers: pgrep -a pytest" >&2
        exit 1
      fi
      exit "$_FLOCK_RC"
    fi
  fi
  # If we reach here, HERMES_PYTEST_FLOCK_ACTIVE is set — we ARE the
  # flock'd child. Fall through to the normal test runner logic below.
  echo "▶ full-suite gate: lock acquired — proceeding" >&2
fi
# ── (end full-suite concurrency gate) ───────────────────────────────────────

# ── Locate python ───────────────────────────────────────────────────────────
# Probe local venvs first; fall back to the Nix devShell's editable venv
# (HERMES_PYTHON is exported by the devShell hook and ships [dev] extras:
# pytest, pytest-asyncio, pytest-timeout, ruff, ty).
#
# A candidate must have pytest INSTALLED, not merely exist. The release venv
# at ~/.hermes/hermes-agent/venv has bin/activate but no pytest, so an
# existence-only probe selected it in checkouts/worktrees without a local
# .venv — every file then died with "No module named pytest" and the run
# reported "0 tests passed" (which reads green at a glance even though the
# exit code is 1). Skip such a venv and keep probing instead.
VENV=""
VENV_PYTHON=""
SKIPPED_VENVS=""
for candidate in "$REPO_ROOT/.venv" "$REPO_ROOT/venv" "$HOME/.hermes/hermes-agent/venv"; do
  if [ -f "$candidate/bin/activate" ]; then
    if "$candidate/bin/python" -c 'import pytest' 2>/dev/null; then
      VENV="$candidate"
      VENV_PYTHON="$candidate/bin/python"
      break
    fi
    SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
  fi
  # Native Windows venv layout: python.exe and activate live under
  # Scripts/, and there is no bin/. Anyone running this script from
  # Git Bash / MSYS with a `python -m venv`- or uv-created venv hits
  # this branch — without it the canonical runner refuses to start.
  if [ -f "$candidate/Scripts/activate" ]; then
    if "$candidate/Scripts/python.exe" -c 'import pytest' 2>/dev/null; then
      VENV="$candidate"
      VENV_PYTHON="$candidate/Scripts/python.exe"
      break
    fi
    SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
  fi
done

if [ -n "$SKIPPED_VENVS" ]; then
  for skipped in $SKIPPED_VENVS; do
    echo "▶ skipping venv without pytest: $skipped" >&2
  done
fi

if [ -n "$VENV" ]; then
  PYTHON="$VENV_PYTHON"
elif [ -n "${HERMES_PYTHON:-}" ] && [ -x "$HERMES_PYTHON" ] \
    && "$HERMES_PYTHON" -c 'import pytest' 2>/dev/null; then
  # Guard with an import check: HERMES_PYTHON may point at the RELEASE
  # venv (no pytest) when inherited from a wrapped `hermes` binary rather
  # than the devShell hook.
  PYTHON="$HERMES_PYTHON"
  echo "▶ no local venv — using Nix dev venv via HERMES_PYTHON: $PYTHON"
else
  echo "error: no virtualenv with pytest found in $REPO_ROOT/.venv or $REPO_ROOT/venv," >&2
  echo "       and HERMES_PYTHON is not a python with pytest (enter the Nix devShell or create a venv)" >&2
  if [ -n "$SKIPPED_VENVS" ]; then
    echo "       (skipped for missing pytest:$SKIPPED_VENVS — install dev extras there, or create $REPO_ROOT/.venv)" >&2
  fi
  exit 1
fi


# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH=""
EXTRA_PYTEST_PLUGINS=""
if [ -f "$HOME/.hermes/pytest_live_guard.py" ]; then
  EXTRA_PYTHONPATH="$HOME/.hermes"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi


# ── Windows location variables (computed before we drop env) ───────────────
# `env -i` forwards HOME, which is enough on POSIX. Native Windows CPython
# resolves Path.home() from USERPROFILE (or HOMEDRIVE+HOMEPATH), stdlib
# platform paths come from LOCALAPPDATA/APPDATA, ssl/sockets need SYSTEMROOT,
# and tempfile needs TEMP/TMP. Dropping them breaks collection on native
# Windows (issues #67385, #70813). These are location variables, not
# credentials, so forwarding them keeps the isolation intent intact. Each is
# only forwarded when actually set, so POSIX runs are byte-for-byte unchanged.
WIN_ENV=()
for _win_var in USERPROFILE HOMEDRIVE HOMEPATH LOCALAPPDATA APPDATA SYSTEMROOT TEMP TMP; do
  if [ -n "${!_win_var:-}" ]; then
    WIN_ENV+=("$_win_var=${!_win_var}")
  fi
done

# ── Test-runner knobs (computed before we drop env) ────────────────────────
# The runner's own documented environment knobs must survive the hermetic
# `env -i` below, or they are silent no-ops for anyone invoking this script:
#
#   * HERMES_TEST_WORKERS / PATHS / FILE_TIMEOUT / FILE_RETRIES / SLICE are
#     read by run_tests_parallel.py at argparse-default time — inside the
#     stripped environment.
#   * HERMES_TEST_IMAGE is read by tests/docker/conftest.py to skip its
#     session-scoped `docker build`. CI's docker.yml sets it to the image
#     the build step just loaded; stripping it made every per-file pytest
#     subprocess rebuild the 5GB image from a cold builder cache instead
#     (~4 min per worker per run, and the rebuilt image lacked the
#     HERMES_GIT_SHA build-arg the workflow bakes in).
#
# These are test-infrastructure knobs, not credentials — same class as the
# HERMES_RUN_SLOW_PET_TESTS / HERMES_E2E_BROWSER opt-ins already forwarded.
# Keep this an explicit allowlist (no HERMES_TEST_* glob) so the "no
# credential can leak" property stays auditable at a glance.
TEST_ENV=()
for _test_var in HERMES_TEST_IMAGE HERMES_TEST_WORKERS HERMES_TEST_PATHS \
  HERMES_TEST_FILE_TIMEOUT HERMES_TEST_FILE_RETRIES HERMES_TEST_SLICE; do
  if [ -n "${!_test_var:-}" ]; then
    TEST_ENV+=("$_test_var=${!_test_var}")
  fi
done

# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
echo "▶ running per-file parallel test suite via run_tests_parallel.py"
echo "  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"

cd "$REPO_ROOT"

# ── Pre-compile .pyc bytecode cache ─────────────────────────────────────────
# Each test file runs in its own subprocess via run_tests_parallel.py.
# Pre-building the bytecode cache once here (instead of each subprocess
# compiling on first import) avoids redundant work across ~2000 processes.
# Uses git to list tracked .py files (skips venv, node_modules, etc).
echo "▶ pre-compiling bytecode cache"
"$PYTHON" -m compileall -q -j 0 -- $(git ls-files '*.py') >/dev/null 2>&1 || true

echo "▶ launching test runner"
exec env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  ${WIN_ENV[@]+"${WIN_ENV[@]}"} \
  ${TEST_ENV[@]+"${TEST_ENV[@]}"} \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  PYTHONUTF8=1 \
  ${HERMES_RUN_SLOW_PET_TESTS:+HERMES_RUN_SLOW_PET_TESTS="$HERMES_RUN_SLOW_PET_TESTS"} \
  ${HERMES_E2E_BROWSER:+HERMES_E2E_BROWSER="$HERMES_E2E_BROWSER"} \
  ${EXTRA_PYTHONPATH:+PYTHONPATH="$EXTRA_PYTHONPATH"} \
  ${EXTRA_PYTEST_PLUGINS:+PYTEST_PLUGINS="$EXTRA_PYTEST_PLUGINS"} \
  "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"
