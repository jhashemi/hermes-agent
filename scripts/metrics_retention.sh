#!/usr/bin/env bash
# metrics_retention.sh — nightly DuckDB retention for metrics_dashboard.duckdb
# Keeps 14 days of raw/aggregate/anomaly rows; checkpoints after purge.
# Must run as the hermes-orchestrator user with the service STOPPED,
# or send a stop/start wrapper. Run via systemd timer or hermes cron.
#
# Cron: daily at 03:15 on hermes2
# Owner: backend-eng / disk-pressure card t_7e688b12

set -euo pipefail

DB="/home/ubuntu/.hermes/memory/metrics_dashboard.duckdb"
CUTOFF_DAYS=14
LOG_TAG="metrics-retention"

if [ ! -f "$DB" ]; then
  echo "[$LOG_TAG] DB not found: $DB — skipping" >&2
  exit 0
fi

# Brief stop of the orchestrator so we get exclusive DuckDB access
echo "[$LOG_TAG] stopping hermes-orchestrator"
sudo systemctl stop hermes-orchestrator

cleanup() {
  echo "[$LOG_TAG] restarting hermes-orchestrator"
  sudo systemctl start hermes-orchestrator
}
trap cleanup EXIT

python3 - << PYEOF
import duckdb, time, sys

DB = "$DB"
CUTOFF = time.time() - ${CUTOFF_DAYS} * 86400

c = duckdb.connect(DB)
tables = {r[0] for r in c.execute("SHOW TABLES").fetchall()}

deleted = {}
for t, col in [("metrics_raw", "ts"), ("metrics_aggregate", "window_end"), ("metrics_anomalies", "detected_at")]:
    if t not in tables:
        continue
    before = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    c.execute(f"DELETE FROM {t} WHERE {col} < {CUTOFF}")
    after = c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    deleted[t] = before - after
    print(f"[$LOG_TAG] {t}: {before} → {after} rows (deleted {before-after})")

c.execute("CHECKPOINT")
print(f"[$LOG_TAG] checkpoint done")
c.close()

import subprocess, shutil
# Report disk reclaim (DuckDB doesn't shrink file on delete — blocks reused)
result = subprocess.run(["du", "-sh", DB], capture_output=True, text=True)
print(f"[$LOG_TAG] file size after: {result.stdout.strip()}")
PYEOF

echo "[$LOG_TAG] done"
