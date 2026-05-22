# RAFT Consensus Design: 2-Node + Arbiter Kanban Cluster

**Status**: Proposed
**Date**: 2026-05-21
**Authors**: Werner Vogels (distributed systems), Jeff Dean (infrastructure)

## Problem

Kanban boards on hermes1 (100.107.83.25) and hermes2 (100.79.15.66) are independent SQLite/DuckDB files with no coordination. A 24h outage resulted from 5 independent schema-migration failures — divergent schemas on the two machines caused incompatible reads/writes.

We need **strong consistency** for kanban writes: if a task is `completed` on hermes1, it must never appear as `running` on hermes2.

## Design Overview

Implement a minimal RAFT consensus layer for kanban writes between the two Hermes machines, using a **2-node cluster with a gateway-arbiter tiebreaker**.

```
┌─────────────┐    Tailscale    ┌─────────────┐
│   hermes1   │◄──────────────►│   hermes2   │
│ 100.107.83.25│               │ 100.79.15.66│
│  (RAFT peer) │               │  (RAFT peer) │
└──────┬──────┘               └──────┬──────┘
       │                              │
       │   HTTP/Tailscale             │
       ▼                              ▼
┌──────────────────────────────────────────┐
│          Gateway Arbiter                  │
│  (Runs on whichever gateway is leader)    │
│  Lightweight RAFT voter — no data, only   │
│  vote on leader election + log commit     │
└──────────────────────────────────────────┘
```

### Why Not 2-Node RAFT Alone?

A 2-node RAFT cluster cannot achieve majority on any vote when one node is down. With 2 peers, quorum = 2. If either node partitions, **no writes are possible** — the system degrades to read-only for *both* sides, defeating the purpose of having two machines.

The **arbiter** breaks the tie: it's a third voter that holds no data and participates only in elections and log-commit votes. Quorum = 2 of 3, so any single failure still allows progress.

## RAFT Components

### 1. Log Entries

Each kanban write operation is encoded as a RAFT log entry:

```python
@dataclass
class KanbanLogEntry:
    term: int
    index: int
    operation: str          # "create_task", "claim_task", "complete_task", etc.
    board: str              # board slug
    payload: dict           # operation-specific parameters
    timestamp: int          # Unix time
```

Operations are **fine-grained** — one log entry per logical kanban operation, not one per SQL statement. This matches the existing `write_txn` boundaries:

```
WRITE_TXN: create_task + _append_event("created")
  → LOG_ENTRY: {op: "create_task", board: "default", payload: {title, body, ...}}
```

### 2. State Machine

The state machine is simply the DuckDB database. Each node applies log entries to its **local** `kanban.duckdb`. The RAFT layer guarantees that all nodes apply entries in the same order.

```python
class KanbanStateEngine:
    """Applies committed RAFT log entries to local DuckDB."""
    
    def apply(self, entry: KanbanLogEntry):
        conn = _get_local_duckdb_conn()
        with write_txn(conn):
            if entry.operation == "create_task":
                _apply_create_task(conn, entry.board, entry.payload)
            elif entry.operation == "claim_task":
                _apply_claim_task(conn, entry.board, entry.payload)
            # ... all 32 write_txn call sites
            _append_event(conn, entry.board, {
                "kind": f"raft_applied:{entry.operation}",
                "created_at": entry.timestamp
            })
```

### 3. Leader Election

```
States: FOLLOWER → CANDIDATE → LEADER

Election timeout: 150-300ms (randomized, standard RAFT)
Heartbeat interval: 50ms (leader sends AppendEntries as heartbeat)

Election flow:
1. Follower starts election timer on heartbeat timeout
2. Increments term, votes for self, sends RequestVote to other peer + arbiter
3. Wins with 2 of 3 votes → becomes LEADER
4. Leader sends periodic AppendEntries (heartbeat)
5. Followers reset election timer on valid AppendEntries
```

**Tailscale transport**: All RAFT RPCs travel over Tailscale (`100.x.x.x` addresses). Tailscale provides:
- Encrypted WireGuard tunnels (no need for TLS on RAFT RPCs)
- Stable IP addresses despite network changes
- ACLs to restrict RAFT traffic to kanban ports only

### 4. Arbiter Design

The arbiter is the **critical addition** that makes 2-node RAFT viable. It runs as part of the gateway process:

```python
class KanbanArbiter:
    """Lightweight RAFT arbiter — votes but stores no data."""
    
    def __init__(self):
        self.current_term = 0
        self.voted_for = None
        self.commit_index = 0
        # NO log storage, NO state machine
    
    def handle_request_vote(self, req: RequestVoteRequest) -> RequestVoteResponse:
        """Vote according to standard RAFT rules."""
        if req.term < self.current_term:
            return RequestVoteResponse(term=self.current_term, vote_granted=False)
        if req.term > self.current_term:
            self.current_term = req.term
            self.voted_for = None
        if self.voted_for is None or self.voted_for == req.candidate_id:
            if req.last_log_index >= self._last_known_log_index:
                self.voted_for = req.candidate_id
                return RequestVoteResponse(term=self.current_term, vote_granted=True)
        return RequestVoteResponse(term=self.current_term, vote_granted=False)
    
    def handle_append_entries(self, req: AppendEntriesRequest) -> AppendEntriesResponse:
        """Acknowledge heartbeats; update commit_index."""
        if req.term < self.current_term:
            return AppendEntriesResponse(term=self.current_term, success=False)
        self.current_term = req.term
        self.commit_index = max(self.commit_index, req.leader_commit)
        self._last_known_log_index = req.prev_log_index + len(req.entries)
        return AppendEntriesResponse(term=self.current_term, success=True)
```

**Key properties**:
- **No database access**: The arbiter never touches `kanban.duckdb`.
- **No disk persistence**: State is in-memory only. On restart, it starts as a fresh voter (term=0, voted_for=None). This is safe — it just means it will vote for whichever candidate contacts it first with a valid term.
- **Low resource usage**: ~1KB memory, no CPU, no disk I/O.

#### Arbiter Deployment

The arbiter runs as a built-in component of the gateway:

```
gateway/run.py → _start_kanban_services():
    - _kanban_notifier_watcher()      (existing)
    - _kanban_dispatcher_watcher()    (existing)
    - _kanban_raft_arbiter_server()   (NEW: if kanban.raft.arbiter=true in config)
```

**Which machine runs the arbiter?** By default, **both** gateways start the arbiter endpoint, but only the one that can accept connections from both peers matters. Since both gateways are always running (they're systemd user services), the arbiter is always available.

In practice, either gateway can serve as arbiter. The RAFT peers are configured with two arbiter endpoints; they try both. The first one that responds is used.

### 5. Read-Only Fallback Under Partition

When a node cannot contact the leader (or the leader has stepped down), the node enters **read-only mode**:

```python
class KanbanRaftNode:
    def is_writable(self) -> bool:
        return self.state == "leader" or (
            self.state == "follower" and self._leader_lease_valid()
        )
    
    def check_writable(self):
        if not self.is_writable():
            raise KanbanReadOnlyError(
                "Cannot write: partitioned from RAFT leader. "
                "Read-only mode — no split-brain possible."
            )
```

The `write_txn` function is wrapped:

```python
@contextlib.contextmanager
def write_txn(conn):
    _raft_node.check_writable()  # RAFT guard
    # ... existing transaction logic
```

**Read path**: Reads always go to the local DuckDB. Under normal operation, the local DB is up-to-date (RAFT guarantees). Under partition, reads still work from the (possibly stale) local copy — no consistency guarantee needed for reads in our use case.

**Leader lease**: The leader maintains a lease via heartbeats. If a follower hasn't received a heartbeat within 2× election timeout (~600ms), it knows the leader may have changed and stops accepting writes locally.

### 6. RAFT RPC Protocol

```python
# Transport: HTTP over Tailscale (simple, debuggable, firewall-friendly)
# Content-Type: application/json
# Port: 17891 (configurable via kanban.raft.port)

# Endpoints:
POST /raft/v1/request_vote       → RequestVoteResponse
POST /raft/v1/append_entries     → AppendEntriesResponse
POST /raft/v1/install_snapshot    → InstallSnapshotResponse  (future, for new-node catchup)
GET  /raft/v1/status             → {state, term, leader, log_length, commit_index}

# Authentication: Tailscale identity (no additional auth needed on 100.x.x.x)
# Timeout: 500ms for all RPCs (must be < election timeout)
```

### 7. Log Persistence

RAFT logs are stored alongside the DuckDB database:

```
~/.hermes/kanban/
├── kanban.duckdb          # state machine
├── raft/
│   ├── log.bin            # RAFT log entries (append-only binary format)
│   ├── state.json         # {current_term, voted_for, commit_index}
│   └── snapshot/          # (future: periodic snapshots for log compaction)
```

Log format (simple, append-only):

```
[4 bytes: entry length][entry length bytes: msgpack-encoded KanbanLogEntry]
```

On startup, the node replays the log from the last snapshot point (or from the beginning if no snapshot) to reconstruct the in-memory log index. For our expected write rate (~10-100 entries/minute), the log will grow slowly (~50KB/day uncompressed), so compaction isn't urgent.

### 8. Node Startup and Recovery

```python
async def start_raft_node():
    """Called at gateway startup."""
    # 1. Load persisted RAFT state
    state = _load_raft_state()
    
    # 2. Open local DuckDB
    conn = connect(engine='duckdb')
    
    # 3. If log is non-empty, replay uncommitted entries
    log_entries = _load_raft_log()
    for entry in log_entries:
        if entry.index <= state.commit_index:
            # Already committed and applied; skip
            continue
        # Apply to state machine
        state_engine.apply(entry)
    
    # 4. Start RAFT protocol (follower initially)
    node = KanbanRaftNode(state, log_entries, conn)
    
    # 5. Start election timer
    node.start_election_timer()
    
    # 6. Start RPC server
    await start_raft_rpc_server(node)
```

### 9. Integration with Existing Code Paths

The key insight: **only `write_txn` needs RAFT integration**. All read operations continue to hit the local DuckDB directly.

Call sites that need changes:

| Function | Current | RAFT-Era |
|---|---|---|
| `connect()` | Returns sqlite3 conn | Returns KanbanConnection (DuckDB or SQLite) |
| `write_txn(conn)` | `BEGIN IMMEDIATE` | RAFT entry proposal + `BEGIN TRANSACTION` |
| `create_task()` | Direct `write_txn` | Propose RAFT entry → await commit → apply |
| `claim_task()` | Direct `write_txn` | Propose RAFT entry → await commit → apply |
| `complete_task()` | Direct `write_txn` | Propose RAFT entry → await commit → apply |
| `list_tasks()` | Direct read | No change (local read) |
| `get_task()` | Direct read | No change (local read) |
| `board_stats()` | Direct read | No change (local read) |
| All other reads | Direct read | No change |

**Write path flow (leader)**:
```
User calls create_task(title="Fix schema")
  → write_txn(conn, check_raft=True)
    → _raft_node.propose(KanbanLogEntry(op="create_task", ...))
      → AppendEntries to follower + arbiter
      → Wait for majority acknowledgment
      → Commit index advances
    → Apply entry to local DuckDB
    → Return result to caller
```

**Write path flow (follower)**:
```
User calls create_task(title="Fix schema")
  → write_txn(conn, check_raft=True)
    → _raft_node.check_writable()
    → Raise KanbanReadOnlyError or forward to leader
```

**Leader forwarding option**: Instead of rejecting writes on followers, we can forward them to the leader:

```python
def propose_or_forward(entry):
    if _raft_node.state == "leader":
        return _raft_node.propose(entry)
    elif _raft_node.leader_address:
        # Forward to leader via HTTP
        return http_post(f"http://{_raft_node.leader_address}/raft/v1/propose", entry)
    else:
        raise KanbanReadOnlyError("No leader available")
```

This makes the RAFT layer transparent to callers — they always call `create_task()` the same way regardless of which machine they're on.

## Configuration

```yaml
kanban:
  raft:
    enabled: true
    node_id: "hermes1"           # or "hermes2"
    peers:
      - id: "hermes1"
        address: "100.107.83.25:17891"
      - id: "hermes2"
        address: "100.79.15.66:17891"
    arbiters:
      - address: "100.107.83.25:17892"  # arbiter port on hermes1
      - address: "100.79.15.66:17892"   # arbiter port on hermes2
    election_timeout_ms: 200
    heartbeat_interval_ms: 50
    log_dir: "~/.hermes/kanban/raft"
    arbiter:
      enabled: true              # start arbiter on this node
      port: 17892
```

## Safety Analysis

### No Split-Brain Guarantee

With 3 voters (2 data nodes + arbiter), quorum = 2. In any partition:

- **Both nodes up, arbiter up**: Normal operation. Leader elected, writes proceed.
- **One node down**: Other node + arbiter = quorum = 2. Leader elected on surviving node.
- **Arbiter down**: Both nodes up = 2 of 3 voters. They can elect a leader among themselves.
- **One node + arbiter down**: Only 1 voter remaining. No quorum → read-only mode. **This is correct behavior** — we sacrifice availability over consistency.

### Comparison with Alternatives

| Approach | Split-Brain? | Latency | Complexity |
|---|---|---|---|
| No coordination (current) | Yes (schema drift) | 0ms | Low |
| Primary-replica (async) | Yes (replication lag) | 1ms | Low |
| 2-node RAFT (no arbiter) | No writes on any failure | 5ms | Medium |
| **2-node + arbiter RAFT** | No | ~10ms | Medium |
| 3-node full RAFT | No | ~10ms | High |
| CRDTs | Eventually consistent | 0ms | Very High |

The 2-node + arbiter design hits the sweet spot: strong consistency, acceptable latency for our write patterns (kanban writes are human-initiated, 50-200ms is fine), and moderate complexity.

## Implementation Phases

### Phase 1: Foundation (Week 1)
- `KanbanRaftNode` class with election timer and state transitions
- `KanbanArbiter` class
- RAFT log persistence (append-only binary file)
- HTTP RPC server for `/raft/v1/*` endpoints

### Phase 2: Write Path Integration (Week 2)
- `write_txn` RAFT guard + entry proposal
- Leader forwarding for follower writes
- `KanbanReadOnlyError` and gateway error handling

### Phase 3: Migration Coordination (Week 3)
- DuckDB migration (from ADR-001) on both machines
- RAFT log replay at startup
- Smoke testing: create task on hermes1 → verify on hermes2

### Phase 4: Hardening (Week 4)
- Log compaction (snapshot-based)
- Metrics: election count, commit latency, leader changes
- Alerting: leader not elected within 5s → PagerDuty