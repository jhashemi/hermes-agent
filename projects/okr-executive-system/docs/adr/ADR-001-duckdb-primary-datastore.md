# ADR-001: Use DuckDB for Primary Data Store

**Date**: 2026-05-22  
**Status**: Accepted  
**Author**: Hermes Agent  

## Decision

Use DuckDB (columnar OLAP database) as the primary data store for the OKR-driven executive system.

## Context

The OKR-driven executive system needs to:
1. Store OKRs, goals, tasks, agents, metrics persistently
2. Execute SQL analytics queries (KPI calculations, progress tracking)
3. Work with systems already deployed on hermes2
4. Maintain ACID compliance for state consistency
5. Support both transactional operations and analytical queries

Current kanban system uses SQLite, which is:
- Row-oriented (not optimized for analytics)
- Not suitable for columnar analytics (metrics aggregation)
- Already used for kanban, but not for metrics

DuckDB is already:
- Installed in the Python venv (`duckdb-1.5.2`)
- Designed for analytics workloads
- ACID compliant
- Supports SQL just like PostgreSQL

## Alternatives

### 1. SQLite (kanban_db)
**Rationale for rejection**: 
- Row-oriented storage not optimized for metrics queries
- No aggregation optimization
- Performance degrades on large metric datasets
- Already used for kanban, potential conflicts

### 2. PostgreSQL
**Rationale for rejection**:
- Requires external service (not deployed)
- Adds operational complexity (separate infra)
- Overkill for single-machine deployment
- More resources needed than DuckDB

### 3. In-memory Python structures
**Rationale for rejection**:
- No persistence across restarts
- Not suitable for production
- Can't handle large datasets

### 4. DuckDB ✅ **CHOSEN**

**Rationale for selection**:
- Already deployed (in venv)
- Columnar storage perfect for metrics
- ACID compliant
- SQL queryable (standard interface)
- Self-contained (no external service)
- Designed for analytics + transactions

## Rationale

DuckDB provides the best balance for our needs:

1. **Analytics-ready**: Columnar storage means KPI calculations are fast
   ```sql
   SELECT okr_id, AVG(actual_value) as avg_progress
   FROM metrics 
   WHERE metric_type = 'key_result'
   GROUP BY okr_id
   ```

2. **Already deployed**: No additional infrastructure needed

3. **Production-grade**: ACID guarantees, transactions, multi-threaded access

4. **Standard SQL**: Any tool can query it (Excel, Grafana, Python, etc.)

5. **Minimal ops**: Single file database, no background services

## Consequences

### Positive
- All OKR data stored in DuckDB
- Efficient metric aggregations
- SQL interface enables BI integrations later
- No migration needed if scaling to PostgreSQL later (same SQL)
- Better performance for analytics queries

### Negative
- Need to design schema (tables, columns)
- SQL knowledge required for queries
- Not suitable if we scale beyond single machine (but DuckDB can write to S3)

### Mitigation
- Use Repository pattern (adapter) so we can swap implementations
- Keep SQL queries in dedicated MetricsEngine
- Document schema clearly

## Related Decisions

- **ADR-002**: Use NATS for events (DuckDB will store event snapshots)
- **ADR-003**: Hexagonal architecture (DuckDB access through adapter)

## Implementation Notes

### Schema Design Required
```sql
-- OKRs
CREATE TABLE okrs (
    id VARCHAR PRIMARY KEY,
    title VARCHAR NOT NULL,
    status VARCHAR,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

-- Goals
CREATE TABLE goals (
    id VARCHAR PRIMARY KEY,
    okr_id VARCHAR NOT NULL REFERENCES okrs(id),
    title VARCHAR NOT NULL,
    effort_hours FLOAT
);

-- Metrics
CREATE TABLE metrics (
    id VARCHAR PRIMARY KEY,
    okr_id VARCHAR NOT NULL REFERENCES okrs(id),
    name VARCHAR NOT NULL,
    target_value FLOAT,
    actual_value FLOAT,
    calculated_at TIMESTAMP
);

-- Create indices for common queries
CREATE INDEX idx_okr_status ON okrs(status);
CREATE INDEX idx_metric_okr ON metrics(okr_id);
CREATE INDEX idx_metric_calculated ON metrics(calculated_at);
```

### Access Pattern
```python
class DuckDBRepository(Repository):
    def __init__(self, db_path: str):
        self.conn = duckdb.connect(db_path)
    
    async def calculate_kpis(self) -> Dict:
        result = self.conn.execute("""
            SELECT okr_id, 
                   COUNT(*) as metric_count,
                   AVG(actual_value / target_value) as progress
            FROM metrics
            WHERE actual_value IS NOT NULL
            GROUP BY okr_id
        """).fetch_df()  # Returns pandas DataFrame
        return result.to_dict()
```

## References

- [DuckDB Documentation](https://duckdb.org/docs/)
- [DuckDB Python API](https://duckdb.org/docs/api/python/overview)
- Existing kanban_db usage: `~/hermes-agent/hermes_cli/kanban_db.py`
