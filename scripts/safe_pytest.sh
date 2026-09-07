#!/usr/bin/env bash
# safe_pytest.sh — flock-gated wrapper around any pytest invocation.
#
# PURPOSE: prevent multiple concurrent full hermes-agent pytest suites
# from oversubscribing the host (h1 blackout RCA, 2026-09-07: load >700,
# 45× oversubscribed, 55-min SSH blackout).
#
# USAGE:
#   # Instead of:
#       pytest tests/
#       python -m pytest tests/
#   # Use:
#       scripts/safe_pytest.sh tests/
#       scripts/safe_pytest.sh -x tests/agent/test_foo.py
#
# The gate only serializes invocations that look like FULL suites
# (no path argument, or path argument = "tests" / "tests/"). Targeted
# runs with a specific subpath are passed through without gating.
#
# Knobs (env vars):
#   HERMES_PYTEST_NO_WAIT=1        exit 0 immediately when lock is held
#   HERMES_PYTEST_LOCK_WAIT=N      flock timeout seconds (default 7200)
#   HERMES_PYTEST_LOAD_LIMIT=N.N   load-per-core ceiling (default 2.0)
#   HERMES_PYTEST_LOAD_BACKOFF=N   seconds between load checks (default 60)
#   HERMES_PYTEST_LOAD_RETRIES=N   max load check iterations (default 30)
#   HERMES_PYTEST_WORKERS=N        cap run_tests_parallel.py workers
#                                  (default: nproc/2 for full-suite runs,
#                                   unset for targeted runs)
#
# Exit codes:
#   0   tests passed (or gate no-op'd because HERMES_PYTEST_NO_WAIT=1)
#   1   tests failed, or lock timed out (HERMES_PYTEST_LOCK_WAIT expired)
#   *   whatever pytest returned for targeted/ungated runs

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

_FULL_SUITE_LOCK="${HOME}/.hermes/pytest-full-suite.lock"
_LOCK_WAIT="${HERMES_PYTEST_LOCK_WAIT:-7200}"
_NO_WAIT="${HERMES_PYTEST_NO_WAIT:-0}"
_LOAD_LIMIT="${HERMES_PYTEST_LOAD_LIMIT:-2.0}"
_LOAD_BACKOFF="${HERMES_PYTEST_LOAD_BACKOFF:-60}"
_LOAD_RETRIES="${HERMES_PYTEST_LOAD_RETRIES:-30}"

# ── Detect whether this is a full-suite invocation ────────────────────────
# A full-suite invocation is one where no path argument is given OR
# the only path argument is "tests", "tests/", "./tests", or similar
# (the default discovery root for hermes-agent).
#
# We parse positional args: anything that does not start with '-' and
# is not the value of a known flag is treated as a path.
_IS_FULL_SUITE=1
_PREV_FLAG_NEEDS_VALUE=0
for _arg in "$@"; do
  if [ "$_PREV_FLAG_NEEDS_VALUE" = "1" ]; then
    _PREV_FLAG_NEEDS_VALUE=0
    continue
  fi
  case "$_arg" in
    # pytest flags that consume the next token as a value
    -k|-m|--co|--config-file|--rootdir|--import-mode|--basetemp|\
    --timeout|--timeout-method|-n|--dist|--numprocesses|--workers|\
    -p|--override-ini|-W|--log-level|--log-format|--log-file|\
    --tb|--capture|-q|-v|-s|--maxfail|--lf|--ff|\
    -j|--jobs|--paths|--slice|--file-timeout|--file-retries)
      _PREV_FLAG_NEEDS_VALUE=1 ;;
    --)
      break ;;
    -*)
      : ;;  # bare flag, no value
    *)
      # Positional path arg — check if it's a full-suite root.
      _norm="${_arg%/}"  # strip trailing slash
      case "$_norm" in
        tests|./tests|"$REPO_ROOT/tests")
          : ;;  # these ARE the default root; still a full-suite run
        *)
          _IS_FULL_SUITE=0
          break ;;
      esac
      ;;
  esac
done

# ── Find the pytest executable ────────────────────────────────────────────
# Prefer the venv pytest so the right interpreter runs the tests.
PYTEST_CMD=""
for _candidate_dir in "$REPO_ROOT/.venv/bin" "$REPO_ROOT/venv/bin" \
    "$HOME/.hermes/hermes-agent/venv/bin"; do
  if [ -x "$_candidate_dir/pytest" ]; then
    if "$_candidate_dir/python" -c 'import pytest' 2>/dev/null; then
      PYTEST_CMD="$_candidate_dir/pytest"
      break
    fi
  fi
done
if [ -z "$PYTEST_CMD" ]; then
  PYTEST_CMD="$(command -v pytest 2>/dev/null || true)"
fi
if [ -z "$PYTEST_CMD" ]; then
  echo "ERROR: safe_pytest.sh: no pytest found. Install dev extras or activate venv." >&2
  exit 1
fi

# ── Worker-count cap for full-suite runs ──────────────────────────────────
# run_tests_parallel.py reads HERMES_TEST_WORKERS. When running a full
# suite, cap at nproc/2 (round up, min 1) unless the caller already set it.
# This ensures even a single full-suite run can't monopolise every core.
if [ "$_IS_FULL_SUITE" = "1" ] && [ -z "${HERMES_TEST_WORKERS:-}" ]; then
  _NPROC="$(nproc 2>/dev/null || echo 2)"
  _CAP=$(( (_NPROC + 1) / 2 ))  # ceil(nproc/2)
  [ "$_CAP" -lt 1 ] && _CAP=1
  export HERMES_TEST_WORKERS="$_CAP"
  echo "▶ safe_pytest: capping workers at ${_CAP} (nproc/2 = ${_NPROC}/2)" >&2
fi

if [ "$_IS_FULL_SUITE" != "1" ] || ! command -v flock >/dev/null 2>&1; then
  # Targeted run or no flock available — run directly without gating.
  exec "$PYTEST_CMD" "$@"
fi

# ── Full-suite gate (flock) ───────────────────────────────────────────────
mkdir -p "$(dirname "$_FULL_SUITE_LOCK")"
touch "$_FULL_SUITE_LOCK"

# Preflight load check.
if [ "$(echo "$_LOAD_LIMIT" | awk '{printf "%d", $1 * 100}')" -gt "0" ]; then
  _NPROC="$(nproc 2>/dev/null || echo 1)"
  _retry=0
  while [ "$_retry" -lt "$_LOAD_RETRIES" ]; do
    _LOAD1="$(awk '{print $1}' /proc/loadavg 2>/dev/null || echo 0)"
    _LOAD1_INT="$(echo "$_LOAD1" | awk '{printf "%d", $1 * 100}')"
    _LIMIT_INT="$(echo "$_NPROC" "$_LOAD_LIMIT" | awk '{printf "%d", $1 * $2 * 100}')"
    if [ "$_LOAD1_INT" -lt "$_LIMIT_INT" ]; then
      break
    fi
    _retry=$(( _retry + 1 ))
    echo "▶ safe_pytest load preflight: 1-min load ${_LOAD1} >= ${_NPROC}×${_LOAD_LIMIT}; waiting ${_LOAD_BACKOFF}s (${_retry}/${_LOAD_RETRIES})" >&2
    sleep "$_LOAD_BACKOFF"
  done
fi

if [ "$_NO_WAIT" = "1" ] || [ "$_LOCK_WAIT" = "0" ]; then
  if ! flock -n "$_FULL_SUITE_LOCK" true 2>/dev/null; then
    echo "▶ safe_pytest gate: another full pytest suite is already running. Skipping (HERMES_PYTEST_NO_WAIT=1)." >&2
    exit 0
  fi
fi

# Guard against re-entrant flock exec.
if [ -n "${HERMES_PYTEST_FLOCK_ACTIVE:-}" ]; then
  exec "$PYTEST_CMD" "$@"
fi

export HERMES_PYTEST_FLOCK_ACTIVE=1
echo "▶ safe_pytest gate: acquiring full-suite lock (timeout ${_LOCK_WAIT}s) ..." >&2

if flock --timeout "$_LOCK_WAIT" "$_FULL_SUITE_LOCK" \
    env HERMES_PYTEST_FLOCK_ACTIVE=1 \
    HERMES_TEST_WORKERS="${HERMES_TEST_WORKERS:-}" \
    "$PYTEST_CMD" "$@"; then
  exit $?
else
  _RC=$?
  if [ "$_RC" = "1" ]; then
    echo "ERROR: safe_pytest gate: lock wait timed out after ${_LOCK_WAIT}s." >&2
    echo "  Another full pytest suite has been running for >${_LOCK_WAIT}s." >&2
    echo "  Check for hung workers: pgrep -la pytest" >&2
    exit 1
  fi
  exit "$_RC"
fi
