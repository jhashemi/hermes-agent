#!/usr/bin/env bash
# kanban-rollback-to-sqlite.sh — ADR-012 rollback path
#
# Reverses the SQLite→DuckDB kanban migration. Sets the dispatcher's write
# backend back to SQLite and, if any board's SQLite file was renamed to
# `kanban.db.retired.<ts>` during Phase 4, restores it in place.
#
# See docs/adr/ADR-012-sqlite-kanban-sunset.md §5 for the full contract.
#
# Usage:
#   scripts/kanban-rollback-to-sqlite.sh --all-boards
#   scripts/kanban-rollback-to-sqlite.sh --board=<slug>
#   scripts/kanban-rollback-to-sqlite.sh --all-boards --dry-run
#
# Env:
#   HERMES_HOME     defaults to ~/.hermes
#   HERMES_CONFIG   defaults to $HERMES_HOME/config.yaml
#
# Exit codes:
#   0 = rollback complete
#   1 = usage error
#   2 = precondition failed (SQLite file missing on some board)
#   3 = post-rollback parity check found DuckDB-only events (data-loss risk)

set -euo pipefail

DRY_RUN=0
ALL_BOARDS=0
BOARD=""

for arg in "$@"; do
    case "$arg" in
        --all-boards) ALL_BOARDS=1 ;;
        --board=*)    BOARD="${arg#*=}" ;;
        --dry-run)    DRY_RUN=1 ;;
        -h|--help)
            grep -E "^# " "$0" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *) echo "unknown arg: $arg" >&2; exit 1 ;;
    esac
done

if [[ $ALL_BOARDS -eq 0 && -z "$BOARD" ]]; then
    echo "must specify --all-boards or --board=<slug>" >&2
    exit 1
fi

: "${HERMES_HOME:=$HOME/.hermes}"
: "${HERMES_CONFIG:=$HERMES_HOME/config.yaml}"
KANBAN_ROOT="$HERMES_HOME/kanban"
BOARDS_ROOT="$KANBAN_ROOT/boards"

log() { echo "[kanban-rollback] $*"; }
run() {
    if [[ $DRY_RUN -eq 1 ]]; then
        log "DRY-RUN: $*"
    else
        log "RUN: $*"
        eval "$@"
    fi
}

# --- Step 1: flip the env override -------------------------------------------

log "Step 1: set HERMES_KANBAN_WRITE_BACKEND=sqlite in config"
if [[ $DRY_RUN -eq 1 ]]; then
    log "DRY-RUN: would edit $HERMES_CONFIG env.kanban_write_backend=sqlite"
else
    python3 - <<PYEOF
import os, sys, pathlib
try:
    import yaml
except ImportError:
    print("PyYAML required. install with: pip install pyyaml", file=sys.stderr)
    sys.exit(4)
p = pathlib.Path(os.environ["HERMES_CONFIG"])
cfg = {}
if p.exists():
    cfg = yaml.safe_load(p.read_text()) or {}
cfg.setdefault("env", {})["kanban_write_backend"] = "sqlite"
p.write_text(yaml.safe_dump(cfg, sort_keys=False))
print(f"wrote {p}: env.kanban_write_backend=sqlite")
PYEOF
fi

# --- Step 2: pick boards ------------------------------------------------------

declare -a BOARD_DIRS=()
if [[ $ALL_BOARDS -eq 1 ]]; then
    while IFS= read -r -d '' d; do
        BOARD_DIRS+=("$d")
    done < <(find "$BOARDS_ROOT" -mindepth 1 -maxdepth 1 -type d -print0 2>/dev/null || true)
    # Also handle the legacy global file
    if [[ -e "$KANBAN_ROOT/kanban.db" || -e "$KANBAN_ROOT/kanban.db.retired."* ]]; then
        BOARD_DIRS+=("$KANBAN_ROOT")
    fi
else
    BOARD_DIRS+=("$BOARDS_ROOT/$BOARD")
fi

log "Step 2: rolling back ${#BOARD_DIRS[@]} board(s)"

# --- Step 3: restore .db files that were renamed .retired.* ------------------

MISSING_SQLITE=()
for d in "${BOARD_DIRS[@]}"; do
    if [[ ! -e "$d/kanban.db" ]]; then
        # look for a retired snapshot
        retired=$(ls -1 "$d"/kanban.db.retired.* 2>/dev/null | sort | tail -1 || true)
        if [[ -n "$retired" ]]; then
            log "Step 3: restore $retired -> $d/kanban.db"
            run "mv \"$retired\" \"$d/kanban.db\""
        else
            log "WARN: no kanban.db and no kanban.db.retired.* found in $d"
            MISSING_SQLITE+=("$d")
        fi
    else
        log "Step 3: $d/kanban.db already in place"
    fi
done

if [[ ${#MISSING_SQLITE[@]} -gt 0 ]]; then
    log "ERROR: SQLite file missing for boards:"
    printf "  %s\n" "${MISSING_SQLITE[@]}"
    log "cannot roll back these boards without their SQLite snapshot"
    exit 2
fi

# --- Step 4: restart dispatchers ---------------------------------------------

log "Step 4: restart hermes-dispatcher on this host"
if [[ $DRY_RUN -eq 1 ]]; then
    log "DRY-RUN: would run 'systemctl --user restart hermes-dispatcher' (or equivalent)"
else
    if systemctl --user list-unit-files 2>/dev/null | grep -q hermes-dispatcher; then
        systemctl --user restart hermes-dispatcher || log "WARN: dispatcher restart failed; check journalctl"
    else
        log "note: no hermes-dispatcher systemd unit — restart the dispatcher manually"
    fi
fi

# For multi-host clusters we cannot ssh from here without config; caller must
# repeat step 4 on the other host.
log "Step 4 note: rerun this script on every host in the dispatcher fleet"

# --- Step 5: parity check DuckDB → SQLite (last 1000 events) -----------------

log "Step 5: parity check DuckDB → SQLite on each rolled-back board"
PARITY_FAILURES=()
for d in "${BOARD_DIRS[@]}"; do
    if [[ ! -e "$d/kanban.duckdb" ]]; then
        log "  $d: no kanban.duckdb — nothing to check"
        continue
    fi
    if [[ $DRY_RUN -eq 1 ]]; then
        log "DRY-RUN: would run parity check on $d"
        continue
    fi
    result=$(python3 - "$d/kanban.duckdb" "$d/kanban.db" <<'PYEOF'
import sys, sqlite3
try:
    import duckdb
except ImportError:
    print("MISSING duckdb — parity check skipped", flush=True)
    sys.exit(0)
duckdb_path, sqlite_path = sys.argv[1], sys.argv[2]
try:
    dcon = duckdb.connect(duckdb_path, read_only=True)
    scon = sqlite3.connect(sqlite_path)
    dcon.execute("SELECT id FROM task_events ORDER BY id DESC LIMIT 1000")
    d_ids = {r[0] for r in dcon.fetchall()}
    s_ids = {r[0] for r in scon.execute("SELECT id FROM task_events ORDER BY id DESC LIMIT 1000")}
    duckdb_only = sorted(d_ids - s_ids)
    if duckdb_only:
        print(f"DIVERGENCE {len(duckdb_only)}: {duckdb_only[:10]}...")
        sys.exit(3)
    print("OK")
    sys.exit(0)
except Exception as e:
    print(f"PARITY-ERROR {e}")
    sys.exit(3)
PYEOF
    ) || true
    log "  $d: $result"
    if [[ "$result" == *DIVERGENCE* || "$result" == *PARITY-ERROR* ]]; then
        PARITY_FAILURES+=("$d")
    fi
done

# --- Step 6: emit NATS event --------------------------------------------------

log "Step 6: emit kanban.rollback.completed event"
if [[ $DRY_RUN -eq 1 ]]; then
    log "DRY-RUN: would publish rollback.completed to hrv.kanban.rollback.completed"
else
    if command -v nats >/dev/null 2>&1; then
        payload=$(printf '{"boards": ["%s"], "parity_failures": %d, "dry_run": false}' \
            "$(IFS='","'; echo "${BOARD_DIRS[*]}")" "${#PARITY_FAILURES[@]}")
        nats pub hrv.kanban.rollback.completed "$payload" 2>/dev/null || log "note: nats CLI publish failed; not fatal"
    else
        log "note: nats CLI not installed — event not emitted"
    fi
fi

# --- Final ---

if [[ ${#PARITY_FAILURES[@]} -gt 0 ]]; then
    log "PARTIAL ROLLBACK: parity failures on:"
    printf "  %s\n" "${PARITY_FAILURES[@]}"
    log "these boards may have DuckDB-only writes that were lost by rollback"
    log "review kanban.duckdb → kanban.db drift before completing rollback"
    exit 3
fi

log "ROLLBACK COMPLETE on ${#BOARD_DIRS[@]} board(s)"
exit 0
