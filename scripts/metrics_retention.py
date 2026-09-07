#!/usr/bin/env python3
"""
DuckDB metrics retention script for hermes2.
Deletes old metrics records (older than 14 days) to prevent disk bloat.

Run via: python3 metrics_retention.py
Typical: systemd timer calling this nightly.
"""

import duckdb
import os
import sys
import logging
from datetime import datetime, timedelta
from pathlib import Path

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
log = logging.getLogger(__name__)

DB_PATH = Path.home() / '.hermes' / 'memory' / 'metrics_dashboard.duckdb'
RETENTION_DAYS = 14

def retention_cycle():
    """Delete old metrics, checkpoint, and optionally vacuum."""
    if not DB_PATH.exists():
        log.error(f"Database not found: {DB_PATH}")
        return False
    
    try:
        log.info(f"Connecting to {DB_PATH}")
        conn = duckdb.connect(str(DB_PATH))
        
        # Calculate cutoff epoch (14 days ago)
        cutoff_epoch = int((datetime.now() - timedelta(days=RETENTION_DAYS)).timestamp())
        log.info(f"Retention cutoff: {RETENTION_DAYS} days ago (epoch {cutoff_epoch})")
        
        # Get pre-deletion counts
        for table in ['metrics_raw', 'metrics_aggregate', 'metrics_anomalies']:
            try:
                before = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                log.info(f"  {table} before: {before:,} rows")
            except Exception as e:
                log.warning(f"  Could not count {table}: {e}")
        
        # Delete old records from each table (keep 14 days retention)
        for table in ['metrics_raw', 'metrics_aggregate', 'metrics_anomalies']:
            try:
                # Delete records older than cutoff_epoch
                stmt = f"DELETE FROM {table} WHERE ts < {cutoff_epoch}"
                result = conn.execute(stmt)
                deleted = result.rows_affected if hasattr(result, 'rows_affected') else '?'
                log.info(f"  Deleted {deleted} rows from {table}")
            except Exception as e:
                log.error(f"  Failed to delete from {table}: {e}")
                conn.close()
                return False
        
        # Checkpoint to flush writes
        log.info("Running CHECKPOINT...")
        conn.execute("CHECKPOINT")
        
        # Get post-deletion counts
        for table in ['metrics_raw', 'metrics_aggregate', 'metrics_anomalies']:
            try:
                after = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                log.info(f"  {table} after: {after:,} rows")
            except Exception as e:
                log.warning(f"  Could not count {table}: {e}")
        
        conn.close()
        log.info("Retention cycle complete")
        return True
        
    except Exception as e:
        log.error(f"Retention cycle failed: {e}", exc_info=True)
        return False

def monthly_vacuum():
    """Full VACUUM + rebuild (monthly, costly but reclaims disk space)."""
    if not DB_PATH.exists():
        log.error(f"Database not found: {DB_PATH}")
        return False
    
    try:
        log.info(f"Starting VACUUM on {DB_PATH}")
        conn = duckdb.connect(str(DB_PATH))
        conn.execute("VACUUM")
        conn.close()
        log.info("VACUUM complete")
        return True
    except Exception as e:
        log.error(f"VACUUM failed: {e}", exc_info=True)
        return False

if __name__ == '__main__':
    mode = sys.argv[1] if len(sys.argv) > 1 else 'retention'
    
    if mode == 'retention':
        success = retention_cycle()
    elif mode == 'vacuum':
        success = monthly_vacuum()
    else:
        log.error(f"Unknown mode: {mode}. Use 'retention' or 'vacuum'")
        success = False
    
    sys.exit(0 if success else 1)
