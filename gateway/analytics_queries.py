"""Real-time analytics queries for the OKR Metrics DuckDB store.

Queries the DuckDB analytics DB populated by ``telemetry_collector.py``
(see ``IMPLEMENTATION_ROADMAP.md`` Steps 1.4 / 2.1 and
``OKR_METRICS_FRAMEWORK.md`` Phase 2/3).

Each function takes a ``duckdb.DuckDBPyConnection`` and returns a list of
``dict`` rows (column name -> value). Time windows default to values from the
canonical queries but are parameterised so callers can widen/narrow them.

Schema assumptions (see OKR_METRICS_FRAMEWORK.md lines 331-406):

- ``llm_metrics(task_id, model_used, input_tokens, output_tokens,
  reasoning_tokens, total_tokens, cost_usd, cost_per_token, timestamp)``
- ``tool_metrics(task_id, tool_name, tool_call_count, success_count,
  failure_count, avg_duration_ms, max_duration_ms, timestamp)``
- ``allocation_metrics(allocation_id, task_id, agent_id, welfare_score,
  adaptive_weight, success, regret, timestamp)``
- ``voice_metrics(session_id, utterance_id, stt_latency_ms, stt_accuracy,
  tts_latency_ms, end_to_end_latency_ms, timestamp)``
- ``okr_tracking(okr_id, objective, key_result_id, metric_value, metric_unit,
  status, created_at, completed_at)``
- ``learning_cycles(cycle_id, cycle_number, decisions_analyzed,
  success_rate_improvement, allocation_regret_reduction, new_constraints,
  start_time, end_time)``

All queries are read-only. Callers should open the DB with
``duckdb.connect(path, read_only=True)`` when running against the shared
production analytics DB (see hermes-agent memory: single-writer discipline
for DuckDB files with a systemd owner).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional
import duckdb


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rows_to_dicts(cursor) -> List[Dict[str, Any]]:
    """Return the last-executed cursor's rows as list-of-dicts.

    DuckDB cursors expose ``description`` after ``.execute()``.
    """
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


# ---------------------------------------------------------------------------
# 1. LLM cost analysis (hourly aggregates over a rolling window)
# ---------------------------------------------------------------------------

LLM_COST_SQL = """
SELECT
    DATE_TRUNC('hour', timestamp)                                       AS period,
    COUNT(*)                                                            AS task_count,
    AVG(cost_usd)                                                       AS avg_cost_per_task,
    SUM(cost_usd)                                                       AS total_cost_hour,
    AVG(total_tokens)                                                   AS avg_tokens_per_task,
    AVG(cost_per_token)                                                 AS avg_cost_per_token_m
FROM llm_metrics
WHERE timestamp > NOW() - INTERVAL {hours} HOUR
GROUP BY period
ORDER BY period DESC
"""


def llm_cost_by_hour(
    db: duckdb.DuckDBPyConnection, hours: int = 24
) -> List[Dict[str, Any]]:
    """Hourly LLM cost roll-up for the last ``hours`` hours.

    Columns: period, task_count, avg_cost_per_task, total_cost_hour,
    avg_tokens_per_task, avg_cost_per_token_m.
    """
    if hours <= 0:
        raise ValueError("hours must be positive")
    cur = db.execute(LLM_COST_SQL.format(hours=int(hours)))
    return _rows_to_dicts(cur)


# ---------------------------------------------------------------------------
# 2. Tool success rate (per-tool aggregates over a rolling window)
# ---------------------------------------------------------------------------

TOOL_SUCCESS_SQL = """
SELECT
    tool_name,
    SUM(success_count)                                                  AS successes,
    SUM(failure_count)                                                  AS failures,
    CASE
        WHEN SUM(success_count) + SUM(failure_count) = 0 THEN NULL
        ELSE CAST(SUM(success_count) AS DOUBLE)
             / (SUM(success_count) + SUM(failure_count))
    END                                                                 AS success_rate,
    AVG(avg_duration_ms)                                                AS avg_latency_ms
FROM tool_metrics
WHERE timestamp > NOW() - INTERVAL {hours} HOUR
GROUP BY tool_name
ORDER BY success_rate DESC NULLS LAST, tool_name
"""


def tool_success_rate(
    db: duckdb.DuckDBPyConnection, hours: int = 24
) -> List[Dict[str, Any]]:
    """Per-tool success-rate roll-up for the last ``hours`` hours.

    Columns: tool_name, successes, failures, success_rate, avg_latency_ms.
    ``success_rate`` is ``NULL`` when a tool recorded zero attempts.
    """
    if hours <= 0:
        raise ValueError("hours must be positive")
    cur = db.execute(TOOL_SUCCESS_SQL.format(hours=int(hours)))
    return _rows_to_dicts(cur)


# ---------------------------------------------------------------------------
# 3. Adaptive-weighting effectiveness (per-agent aggregates, learning-cycle
#    aware fallback to a fixed window when no cycles have been recorded)
# ---------------------------------------------------------------------------

ALLOCATION_LEARNING_SQL = """
SELECT
    a.agent_id                                                          AS agent_id,
    COUNT(*)                                                            AS allocation_count,
    SUM(CASE WHEN a.success THEN 1 ELSE 0 END)                          AS success_count,
    CAST(SUM(CASE WHEN a.success THEN 1 ELSE 0 END) AS DOUBLE)
        / COUNT(*)                                                      AS agent_success_rate,
    AVG(a.adaptive_weight)                                              AS avg_weight,
    AVG(a.regret)                                                       AS avg_regret,
    SUM(a.regret)                                                       AS total_regret
FROM allocation_metrics a
WHERE a.timestamp > COALESCE(
    (SELECT MAX(start_time) - INTERVAL {lookback_hours} HOUR FROM learning_cycles),
    NOW() - INTERVAL {fallback_hours} HOUR
)
GROUP BY a.agent_id
ORDER BY agent_success_rate DESC, agent_id
"""


def allocation_effectiveness(
    db: duckdb.DuckDBPyConnection,
    lookback_hours: int = 6,
    fallback_hours: int = 24,
) -> List[Dict[str, Any]]:
    """Per-agent allocation effectiveness relative to the last learning cycle.

    Uses the newest ``learning_cycles.start_time`` minus ``lookback_hours``
    as the lower bound; if ``learning_cycles`` is empty, falls back to
    ``NOW() - fallback_hours``.

    Columns: agent_id, allocation_count, success_count, agent_success_rate,
    avg_weight, avg_regret, total_regret.
    """
    if lookback_hours <= 0 or fallback_hours <= 0:
        raise ValueError("lookback_hours and fallback_hours must be positive")
    cur = db.execute(
        ALLOCATION_LEARNING_SQL.format(
            lookback_hours=int(lookback_hours),
            fallback_hours=int(fallback_hours),
        )
    )
    return _rows_to_dicts(cur)


# ---------------------------------------------------------------------------
# 4. Voice session quality (hourly aggregates with p95 e2e latency)
# ---------------------------------------------------------------------------

VOICE_QUALITY_SQL = """
SELECT
    DATE_TRUNC('hour', timestamp)                                       AS period,
    COUNT(DISTINCT session_id)                                          AS session_count,
    AVG(stt_latency_ms)                                                 AS avg_stt_latency,
    AVG(tts_latency_ms)                                                 AS avg_tts_latency,
    AVG(end_to_end_latency_ms)                                          AS avg_e2e_latency,
    AVG(stt_accuracy)                                                   AS avg_stt_accuracy,
    QUANTILE_CONT(end_to_end_latency_ms, 0.95)                          AS p95_e2e_latency
FROM voice_metrics
WHERE timestamp > NOW() - INTERVAL {hours} HOUR
GROUP BY period
ORDER BY period DESC
"""


def voice_session_quality(
    db: duckdb.DuckDBPyConnection, hours: int = 24
) -> List[Dict[str, Any]]:
    """Hourly voice-session quality roll-up for the last ``hours`` hours.

    Columns: period, session_count, avg_stt_latency, avg_tts_latency,
    avg_e2e_latency, avg_stt_accuracy, p95_e2e_latency.

    Note: uses DuckDB's ``QUANTILE_CONT`` for a continuous p95 rather than
    Postgres's ``PERCENTILE_CONT(...) WITHIN GROUP``, which is semantically
    equivalent but not DuckDB syntax.
    """
    if hours <= 0:
        raise ValueError("hours must be positive")
    cur = db.execute(VOICE_QUALITY_SQL.format(hours=int(hours)))
    return _rows_to_dicts(cur)


# ---------------------------------------------------------------------------
# 5. OKR progress tracking (per-KR aggregates over a rolling window)
# ---------------------------------------------------------------------------

OKR_PROGRESS_SQL = """
SELECT
    objective,
    key_result_id,
    COUNT(*)                                                            AS total_measurements,
    AVG(metric_value)                                                   AS current_value,
    MAX(metric_value)                                                   AS best_value,
    MIN(metric_value)                                                   AS worst_value,
    STDDEV(metric_value)                                                AS variability,
    status
FROM okr_tracking
WHERE created_at > NOW() - INTERVAL {days} DAY
GROUP BY objective, key_result_id, status
ORDER BY objective, key_result_id
"""


def okr_progress(
    db: duckdb.DuckDBPyConnection, days: int = 7
) -> List[Dict[str, Any]]:
    """Per-(objective, key_result_id, status) OKR aggregates.

    Columns: objective, key_result_id, total_measurements, current_value,
    best_value, worst_value, variability, status.
    """
    if days <= 0:
        raise ValueError("days must be positive")
    cur = db.execute(OKR_PROGRESS_SQL.format(days=int(days)))
    return _rows_to_dicts(cur)


# ---------------------------------------------------------------------------
# Schema bootstrap (mirrors OKR_METRICS_FRAMEWORK.md Phase 2 for tests and
# for callers who want CREATE-IF-NOT-EXISTS convenience against a fresh DB)
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS llm_metrics (
    task_id           STRING,
    model_used        STRING,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    reasoning_tokens  INTEGER,
    total_tokens      INTEGER,
    cost_usd          DOUBLE,
    cost_per_token    DOUBLE,
    timestamp         TIMESTAMP,
    PRIMARY KEY (task_id, timestamp)
);

CREATE TABLE IF NOT EXISTS tool_metrics (
    task_id           STRING,
    tool_name         STRING,
    tool_call_count   INTEGER,
    success_count     INTEGER,
    failure_count     INTEGER,
    avg_duration_ms   DOUBLE,
    max_duration_ms   DOUBLE,
    timestamp         TIMESTAMP
);

CREATE TABLE IF NOT EXISTS allocation_metrics (
    allocation_id     STRING,
    task_id           STRING,
    agent_id          STRING,
    welfare_score     DOUBLE,
    adaptive_weight   DOUBLE,
    success           BOOLEAN,
    regret            DOUBLE,
    timestamp         TIMESTAMP,
    PRIMARY KEY (allocation_id)
);

CREATE TABLE IF NOT EXISTS voice_metrics (
    session_id            STRING,
    utterance_id          STRING,
    stt_latency_ms        DOUBLE,
    stt_accuracy          DOUBLE,
    tts_latency_ms        DOUBLE,
    end_to_end_latency_ms DOUBLE,
    timestamp             TIMESTAMP
);

CREATE TABLE IF NOT EXISTS okr_tracking (
    okr_id         STRING,
    objective      STRING,
    key_result_id  INTEGER,
    metric_value   DOUBLE,
    metric_unit    STRING,
    status         STRING,
    created_at     TIMESTAMP,
    completed_at   TIMESTAMP,
    PRIMARY KEY (okr_id, key_result_id)
);

CREATE TABLE IF NOT EXISTS learning_cycles (
    cycle_id                     STRING,
    cycle_number                 INTEGER,
    decisions_analyzed           INTEGER,
    success_rate_improvement     DOUBLE,
    allocation_regret_reduction  DOUBLE,
    new_constraints              INTEGER,
    start_time                   TIMESTAMP,
    end_time                     TIMESTAMP,
    PRIMARY KEY (cycle_id)
);
"""


def init_schema(db: duckdb.DuckDBPyConnection) -> None:
    """Create the analytics schema if it doesn't already exist.

    Mirrors ``OKR_METRICS_FRAMEWORK.md`` Phase 2. Idempotent.
    """
    db.execute(SCHEMA_SQL)


# ---------------------------------------------------------------------------
# CLI entry point (optional convenience for smoke-testing against a live DB)
# ---------------------------------------------------------------------------

def _summarize(rows: List[Dict[str, Any]], name: str) -> str:
    if not rows:
        return f"{name}: 0 rows"
    return f"{name}: {len(rows)} rows, first={rows[0]!r}"


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover
    import argparse
    import sys

    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("db_path", help="Path to the analytics DuckDB file")
    p.add_argument("--read-only", action="store_true", default=True)
    args = p.parse_args(argv)

    db = duckdb.connect(args.db_path, read_only=args.read_only)
    try:
        print(_summarize(llm_cost_by_hour(db), "llm_cost_by_hour"))
        print(_summarize(tool_success_rate(db), "tool_success_rate"))
        print(_summarize(allocation_effectiveness(db), "allocation_effectiveness"))
        print(_summarize(voice_session_quality(db), "voice_session_quality"))
        print(_summarize(okr_progress(db), "okr_progress"))
    finally:
        db.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
