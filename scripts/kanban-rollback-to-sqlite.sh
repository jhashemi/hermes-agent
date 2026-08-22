#!/usr/bin/env bash
# Kanban rollback to SQLite-only writes (ADR-012 P1 / G1a-FIX).
#
# When a dual-write shadow window (G1b) or a post-flip DuckDB authority
# regime (G1c) needs to be rolled back to pre-migration behaviour, this
# script:
#
#   1. Sets HERMES_KANBAN_WRITE_BACKEND=sqlite for the running dispatcher
#      environment (systemd unit override, if present).
#   2. Optionally removes DuckDB sidecar files so a subsequent re-enable
#      forces a fresh migration (--purge-duckdb).
#   3. Verifies via a smoke roundtrip that the SQLite path still works.
#
# It's an ANNOTATED script — it prints every mutation it plans to make
# and refuses to proceed unless --confirm is passed. Rollbacks touch
# production kanban state; the script is deliberately loud.
#
# Usage:
#   kanban-rollback-to-sqlite.sh --confirm
#   kanban-rollback-to-sqlite.sh --confirm --purge-duckdb
#   kanban-rollback-to-sqlite.sh --dry-run
#
# Env:
#   HERMES_HOME (default: $HOME/.hermes)
#   HERMES_KANBAN_WRITE_BACKEND (this script will unset/replace)

set -euo pipefail

CONFIRM=0
DRY_RUN=0
PURGE_DUCKDB=0

usage() {
    cat <<'EOF' >&2
kanban-rollback-to-sqlite.sh — set the kanban dispatcher back to SQLite-only writes.

Options:
  --confirm         Apply the rollback (required for any mutating action).
  --dry-run         Print the plan but touch nothing.
  --purge-duckdb    Remove kanban.duckdb files after rolling back.
  --help            Print this help.

Environment:
  HERMES_HOME       Kanban root (default: $HOME/.hermes)
EOF
    exit 1
}

for arg in "$@"; do
    case "$arg" in
        --confirm)      CONFIRM=1 ;;
        --dry-run)      DRY_RUN=1 ;;
        --purge-duckdb) PURGE_DUCKDB=1 ;;
        --help|-h)      usage ;;
        *)              echo "unknown arg: $arg" >&2; usage ;;
    esac
done

if [[ $CONFIRM -eq 0 && $DRY_RUN -eq 0 ]]; then
    echo "refusing to run without --confirm or --dry-run" >&2
    usage
fi

HOME_ROOT="${HERMES_HOME:-$HOME/.hermes}"
echo "[plan] HERMES_HOME     = $HOME_ROOT"
echo "[plan] dry_run         = $DRY_RUN"
echo "[plan] purge_duckdb    = $PURGE_DUCKDB"

# 1. Unset any process-scoped override so children inherit sqlite path
if [[ $DRY_RUN -eq 0 ]]; then
    unset HERMES_KANBAN_WRITE_BACKEND
    export HERMES_KANBAN_WRITE_BACKEND=sqlite
    echo "[done] HERMES_KANBAN_WRITE_BACKEND=sqlite (shell scope)"
else
    echo "[dry ] would set HERMES_KANBAN_WRITE_BACKEND=sqlite (shell scope)"
fi

# 2. Write systemd env drop-in if the dispatcher unit is present
SYSTEMD_UNIT="hermes-kanban-dispatcher.service"
DROP_IN_DIR="/etc/systemd/system/${SYSTEMD_UNIT}.d"
DROP_IN_FILE="$DROP_IN_DIR/10-rollback-to-sqlite.conf"
if systemctl list-unit-files "$SYSTEMD_UNIT" >/dev/null 2>&1; then
    if [[ $DRY_RUN -eq 0 ]]; then
        if [[ $EUID -ne 0 ]]; then
            echo "[warn] systemd drop-in requires root; skipping ($DROP_IN_FILE)"
        else
            mkdir -p "$DROP_IN_DIR"
            cat >"$DROP_IN_FILE" <<EOF
# ADR-012 rollback — restore SQLite-only writes on the kanban dispatcher.
[Service]
Environment=HERMES_KANBAN_WRITE_BACKEND=sqlite
EOF
            systemctl daemon-reload
            systemctl restart "$SYSTEMD_UNIT" || echo "[warn] $SYSTEMD_UNIT restart failed"
            echo "[done] wrote $DROP_IN_FILE and restarted $SYSTEMD_UNIT"
        fi
    else
        echo "[dry ] would write $DROP_IN_FILE + daemon-reload + restart $SYSTEMD_UNIT"
    fi
else
    echo "[skip] systemd unit $SYSTEMD_UNIT not installed"
fi

# 3. Optionally purge kanban.duckdb sidecars
if [[ $PURGE_DUCKDB -eq 1 ]]; then
    shopt -s nullglob
    DUCK_FILES=("$HOME_ROOT"/kanban.duckdb "$HOME_ROOT"/kanban/boards/*/kanban.duckdb)
    if [[ ${#DUCK_FILES[@]} -eq 0 ]]; then
        echo "[skip] no kanban.duckdb sidecars under $HOME_ROOT"
    else
        for f in "${DUCK_FILES[@]}"; do
            if [[ $DRY_RUN -eq 0 ]]; then
                rm -f -- "$f" "$f".wal "$f".tmp || true
                echo "[done] rm $f"
            else
                echo "[dry ] would rm $f"
            fi
        done
    fi
fi

# 4. Smoke check: SQLite path still works
if [[ $DRY_RUN -eq 0 ]]; then
    if command -v python3 >/dev/null 2>&1; then
        python3 - <<'PY' || echo "[warn] smoke roundtrip returned non-zero"
import os, sqlite3, sys, pathlib
home = pathlib.Path(os.environ.get("HERMES_HOME", str(pathlib.Path.home() / ".hermes")))
for candidate in [home / "kanban.db"] + sorted((home / "kanban/boards").glob("*/kanban.db")):
    if not candidate.exists():
        continue
    conn = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
    try:
        n = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        print(f"[smoke] {candidate}: tasks={n}")
    finally:
        conn.close()
PY
    fi
fi

echo "[done] kanban rollback complete (backend=sqlite)"
exit 0
