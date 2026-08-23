# ADR-014: SQLite Runtime Upgrade Path for WAL-Reset Vulnerability

**Status:** Accepted
**Date:** 2026-08-23
**Author:** ops (kanban task t_c7edcdc6)
**Scope:** hermes2 (primary) and hermes1 (replica) — all hosts running kanban boards

---

## Context

Both hosts in the fleet run Ubuntu 24.04 and link SQLite 3.45.1, the Ubuntu Noble
system default. SQLite 3.45.1 carries the WAL-reset bug: on a full checkpoint the
WAL file is reset to frame 0, which can silently discard recent writes that have not
yet been flushed to the main database file. Any process holding a WAL-mode database
open while the checkpoint fires is at risk of data loss.

Fleet inventory (hermes2, confirmed 2026-08-23):

  System sqlite:   3.45.1 (libsqlite3-0 3.45.1-1ubuntu2.7)
  Venv python:     /home/ubuntu/hermes-agent/venv/bin/python
  Interpreter:     /usr/bin/python3.12 (base_prefix=/usr — system python)
  WAL-mode kanban.db files active on hermes2: 21
  Boards on DELETE mode (safe): 5 (legacy, inactive)

hermes1 (100.107.83.25) shows an identical profile: same OS, same SQLite 3.45.1,
same system-python venv layout.

Target: SQLite >= 3.46.0 (WAL-reset fix landed there). Acceptable alternatives per
task brief: >= 3.51.3, 3.50.7, or 3.44.6. python-build-standalone 3.12.13 (the
latest available via uv on this architecture) bundles SQLite 3.50.x+, which is
within the acceptable range.

---

## Decision

**Chosen path: Option (i) — `hermes update`**

Run `hermes update` on both hosts during a coordinated maintenance window. The
update pipeline invokes `repair_vulnerable_runtime` (managed_uv.py), which:

1. Detects that the live venv python reports `wal_reset_vulnerable = True`.
2. Downloads python-build-standalone 3.12.13 via the hermes-managed uv binary into
   `.hermes-runtime/python/` (checkout-scoped, not system-wide).
3. Builds a candidate venv against the new private Python.
4. Atomically renames the live venv aside, promotes the candidate.
5. Rolls back to the old venv if any post-cutover smoke test fails.

**Go/No-go on option (i) for system-python hosts: GO.**

The "system-python" label is misleading in this context. The venv at
`/home/ubuntu/hermes-agent/venv` has `base_prefix=/usr` but that just means uv
built it against the system interpreter. `repair_vulnerable_runtime` does NOT require
the current venv to be uv-managed. It only requires:

  (a) `pyproject.toml` exists at the hermes checkout root — CHECK
      (`/home/ubuntu/hermes-agent/pyproject.toml`)
  (b) `venv/bin/python` exists — CHECK

Once those preconditions pass, repair provisions a *private* Python (python-build-
standalone, fully self-contained) and replaces the venv interpreter. The system
Python is untouched. The upgrade is venv-scoped, not host-wide.

**Why not option (ii) — WAL->DELETE journal mode downgrade?**

Option (ii) is a workaround, not a fix. Switching to DELETE mode removes WAL but
also removes WAL's concurrency advantages (readers never block writers). With 21
active kanban boards across hundreds of dispatcher ticks per day, DELETE-mode
contention would materially degrade throughput. Additionally:

  - The downgrade has a known silent-revert trap: any process that opens a db while
    a -wal sidecar still exists re-enters WAL mode on next write. This requires a
    full stop-the-world maintenance window for every board anyway — so the downtime
    cost is equivalent to option (i) without any lasting benefit.
  - It does not fix /home/ubuntu/.hermes/kanban.db, the session store, or EAF data
    stores — only the kanban boards. Option (i) fixes all of them at once.
  - It is reversible only by re-enabling WAL, which reintroduces the vulnerability
    until the interpreter is updated.

---

## Per-Host Steps

The same steps apply to both hermes2 and hermes1. Perform on one host, verify,
then repeat on the second.

### Pre-flight (both hosts)

  # Confirm current vulnerable state
  python3 -c "import sqlite3; print(sqlite3.sqlite_version)"
  # Expected: 3.45.1

  # Snapshot active board count
  find /home/ubuntu/.hermes/kanban -name "kanban.db" -exec sqlite3 {} "PRAGMA journal_mode;" \; | sort | uniq -c

  # Ensure no stuck WAL files > 100 MB (would indicate an already-in-progress loss event)
  find /home/ubuntu/.hermes -name "*.db-wal" -size +100M 2>/dev/null

### Maintenance Window

1. Drain the dispatcher: set all boards to paused or wait for running tasks to
   complete. The update itself does not require zero in-flight tasks, but stopping
   new dispatch reduces the window of risk during restart.

2. Stop the gateway (if running):
     systemctl stop hermes-gateway 2>/dev/null || pkill -f "hermes.*gateway" || true

3. Run the update:
     cd /home/ubuntu/hermes-agent
     hermes update

   Expected output includes:
     "Provisioning a private Python 3.12.x runtime with fixed SQLite..."
     "Managed Python runtime repaired (SQLite 3.45.1 -> 3.50.x)"
     "Restart required to finish the managed Python runtime repair."

   If the catalog is stale (no newer patch found), the updater automatically
   refreshes the managed uv catalog and retries once.

4. Restart all Hermes processes (gateway, dispatcher, any background agents):
     systemctl start hermes-gateway 2>/dev/null || true

### Verification

  # Confirm new SQLite version in the hermes venv
  /home/ubuntu/hermes-agent/venv/bin/python -c "import sqlite3; print(sqlite3.sqlite_version)"
  # Must be >= 3.46.0 (ideally 3.50.x)

  # Confirm WAL-mode boards still readable
  sqlite3 /home/ubuntu/.hermes/kanban/boards/okr-vfe-2026-q3/kanban.db "PRAGMA journal_mode; SELECT count(*) FROM tasks;"

  # Confirm the system Python is unchanged (should still show 3.45.1)
  /usr/bin/python3.12 -c "import sqlite3; print(sqlite3.sqlite_version)"
  # Expected: 3.45.1 — system is not affected

  # Run a kanban dispatch smoke test
  hermes kanban list --limit 3

---

## Expected Downtime

  Gateway pause:   ~2-5 minutes (stop + restart)
  Repair itself:   ~3-8 minutes (python-build-standalone download + venv rebuild)
  Total per host:  ~10-15 minutes

No database files are modified. WAL mode is preserved. Data is not migrated.

---

## Blast Radius

**Scope:** Contained to the hermes venv at `/home/ubuntu/hermes-agent/venv`.

  - System Python (/usr/bin/python3.12):          NOT touched
  - System SQLite (libsqlite3-0):                 NOT touched
  - Other Python venvs on the host:               NOT touched
  - Kanban database files:                        NOT touched (no schema changes)
  - EAF / executive agent databases:              NOT touched (only interpreter changes)
  - Active kanban sessions during update:         Gateway paused = no active sessions

**Rollback scope:** repair_vulnerable_runtime parks the old venv at
`venv.stale.runtime-<timestamp>/` before cutover. If post-cutover smoke tests fail,
it synchronously restores the parked venv. The backup is cleaned up automatically
after a successful repair (swept at next hermes update or startup).

**Risk factors:**
  - Network unavailability during python-build-standalone download: repair is
    non-fatal, old venv stays in place, retry at next hermes update.
  - Stale uv catalog (no newer patch indexed): updater auto-refreshes catalog and
    retries once. If still blocked, escalate to manual uv catalog refresh.

---

## Rollback Plan

If after restart the venv is broken (import failures, dep mismatches):

  # Locate the parked backup (created atomically before cutover)
  ls /home/ubuntu/hermes-agent/venv.stale.runtime-*/

  # If present, restore manually:
  mv /home/ubuntu/hermes-agent/venv /home/ubuntu/hermes-agent/venv.broken-$(date +%s)
  mv /home/ubuntu/hermes-agent/venv.stale.runtime-*/ /home/ubuntu/hermes-agent/venv

  # Restart gateway
  systemctl start hermes-gateway

  # Verify old sqlite is back
  /home/ubuntu/hermes-agent/venv/bin/python -c "import sqlite3; print(sqlite3.sqlite_version)"

The rollback returns the host to 3.45.1 (still vulnerable) but operational.
The root cause (stale catalog or dep incompatibility) can then be investigated
offline before re-attempting.

---

## Fleet Applicability

This path applies identically to:

  - hermes2 (primary, this host, 100.x.x.x) — confirmed system-python layout
  - hermes1 (100.107.83.25 / everett-dash) — confirmed system-python layout, SQLite 3.45.1

Any additional host running a hermes-agent checkout with `venv/bin/python` pointing
to system python will follow the same procedure. No host-specific modifications are
required.

---

## Alternatives Rejected

**Option (ii) — Coordinated WAL->DELETE downgrade:** rejected. See Decision section.

**apt backport of libsqlite3 >= 3.46.0:** No backport exists for Ubuntu 24.04 Noble
as of 2026-08-23. Candidate is still 3.45.1-1ubuntu2.7. A manual PPA or compile-
from-source approach introduces a persistent maintenance burden and modifies the
system Python's sqlite — wider blast radius than option (i) for no operational gain.

---

## References

  - managed_uv.py: repair_vulnerable_runtime() lines 1063-1218
  - update_cmd.py: update pipeline lines 3422-3430 (calls update_managed_uv + ensure_uv)
  - ADR-012: SQLite kanban sunset context
  - Ubuntu Noble libsqlite3-0 candidate: 3.45.1-1ubuntu2.7 (no backport available)
