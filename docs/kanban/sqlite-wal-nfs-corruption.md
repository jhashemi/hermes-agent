# Runbook: SQLite WAL corruption over NFS multi-writer

> **Audience:** an on-call agent with no prior context on this board.
> **Incident:** 2026-08-18 kanban.db corruption cascade on `okr-vfe-2026-q3`.
> **References:** diagnosis task `t_b8863099`, fix task `t_baff33c8`, this runbook task `t_d6e7a50b`, commit `42864e9486`.

---

## 1. Symptom

Watch for all of these signals together:

- `kanban.db.corrupt.*.bak` files accumulating in the board directory
  (content-addressed by SHA-256, so identical corruption deduplicates, but
  evolving corruption mints new fingerprints).
- `KanbanDbOnNetworkFsError` in gateway/worker logs.
- WAL file (`kanban.db-wal`) not checkpointing — size stays at 0 or grows
  without ever shrinking back.
- `sqlite3 kanban.db "PRAGMA integrity_check;"` returns errors like
  `invalid page number` or `database disk image is malformed`.
- Dispatcher appears to spawn fresh workers that each open the corrupt
  file, trigger another quarantine, and repeat — a corruption storm.

## 2. Root cause

SQLite WAL mode is incompatible with NFS multi-writer access. Two
hermes-gateway instances on different hosts writing to the same `kanban.db`
over an NFSv4 mount causes silent corruption because:

- SQLite's `fcntl(F_SETLK)` byte-range locks do not carry cache-coherence
  guarantees across NFS clients.
- WAL sidecar coordination (the `-wal` and `-shm` files) assumes a shared
  memory-mapped page cache that does not cohere across NFS clients.
- The concrete failure mode is a malformed b-tree page. `integrity_check`
  reports `invalid page number`; the dispatcher spawns a fresh worker on
  the next tick; that worker opens the corrupt file, the guard preserves
  another `.corrupt.<hash>.bak`, and the cycle repeats.

The codebase documents and guards against this in:

- `hermes_cli/kanban_db.py` — class `KanbanDbOnNetworkFsError` (added in
  commit `e3795bc039`), which refuses to open a DB whose resolved path sits
  on `nfs`/`nfs4`/`nfs3`/`cifs`/`smb`/`smb2`/`smb3`/`fuse` mounts.
- `hermes_cli/kanban_db.py` — function `_backup_corrupt_db` (hardened in
  commit `42864e9486`) with defense-in-depth guards for the corruption
  storm.
- `hermes_state.py` — `_WAL_INCOMPAT_MARKERS` and `apply_wal_with_fallback`
  for runtime WAL-to-DELETE fallback when the filesystem refuses WAL.

## 3. Diagnosis steps

Run these checks in order. All can be executed from any host with access
to the kanban directory or the NFS export.

### 3.1 Check the NFS export on the host owning the kanban directory

```bash
# On the host that owns the kanban directory (e.g. hermes2):
cat /etc/exports | grep -i kanban
```

If the kanban directory is exported via NFS, you will see a line like:

```
/srv/hermes/kanban  *(rw,sync,no_subtree_check)
```

### 3.2 Check mounts on all other hosts

```bash
# On every other host in the fleet:
mount | grep -i nfs
# Or more precisely:
mount | grep "$(dirname /path/to/kanban.db)"
```

Look for `type nfs` or `type nfs4` entries that overlap the kanban
directory path.

### 3.3 Check who has the DB file open

```bash
# On the host that owns the kanban directory:
lsof /path/to/kanban.db
fuser /path/to/kanban.db
```

If you see processes from multiple hosts (or multiple gateway PIDs from
different hosts' NFS clients), you have confirmed multi-writer access.

### 3.4 Check for multiple hermes-gateway processes writing the same path

```bash
# On every host:
ps aux | grep hermes-gateway | grep -v grep
```

Cross-reference the working directories and config paths. If two or more
gateways on different hosts are configured to use the same `kanban.db`
file path, and that path resolves to an NFS mount, you have found the
root cause.

### 3.5 Verify corruption

```bash
sqlite3 /path/to/kanban.db "PRAGMA integrity_check;"
```

If this returns anything other than `ok`, the DB is corrupt. The
`.corrupt.*.bak` files are content-addressed backups of the corrupt
state(s) — do NOT delete them yet (see section 6).

### 3.6 Confirm the guard is active

```bash
# Check whether the NFS guard would fire for this path:
python3 -c "
from pathlib import Path
from hermes_cli.kanban_db import _resolve_mount_for_path
m = _resolve_mount_for_path(Path('/path/to/kanban.db'))
print(m)  # Should print (mount_point, 'nfs4') if on NFS
"
```

If this returns `None`, either the path is on local disk (good) or
`/proc/mounts` is unreadable (non-Linux, sandboxed container).

## 4. Fix (preferred): retire the NFS export

This is the permanent fix. The kanban DB must have exactly one writer,
and that writer must be on local disk.

1. **Identify the DB owner.** This is the host whose local disk holds the
   real `kanban.db` — typically hermes2.

2. **Remove the NFS export.** On the DB-owning host:
   ```bash
   # Edit /etc/exports and remove or comment the line exporting the kanban dir
   sudo nano /etc/exports
   sudo exportfs -ra   # re-export everything (drops the removed entry)
   ```

3. **Point remote hosts at the HTTP kanban API.** On every other host,
   configure the kanban client to use the HTTP API instead of direct file
   access. Set in `~/.hermes/config.yaml`:
   ```yaml
   kanban:
     api_url: "http://hermes2.flounder-snake.ts.net:<port>"
   ```
   Or set the env var: `HERMES_KANBAN_API_URL=http://hermes2...`

4. **Verify single-writer.** Confirm that only hermes2's gateway has
   `kanban.dispatch_in_gateway: true` and that no other host has a direct
   file path to `kanban.db`. See `docs/kanban/multi-gateway.md` for the
   single-dispatcher posture.

5. **Monitor.** Watch for a 30-minute quiet window:
   ```bash
   # Count .bak files before and after a 30-minute wait:
   ls /path/to/kanban.db.corrupt.*.bak | wc -l
   sleep 1800
   ls /path/to/kanban.db.corrupt.*.bak | wc -l
   ```
   The count should not increase. The WAL file should checkpoint (size
   fluctuates, not stuck at 0 or growing monotonically). `PRAGMA
   integrity_check` should return `ok`.

   Reference verification: parent task `t_afa0c2d2` confirmed a 30-minute
   quiet window PASS with 0 new `.bak` files, WAL stable at 32992 bytes,
   and `integrity_check` returning `ok`.

## 5. Fix (fallback): single-writer + defense-in-depth guards

If retiring the NFS export is not immediately possible, enforce a
single-writer posture and rely on the codebase guards.

1. **Ensure only ONE hermes-gateway is active cluster-wide.** Disable the
   gateway on all hosts except one (the DB owner). On non-owner hosts:
   ```bash
   systemctl stop hermes-gateway
   systemctl disable hermes-gateway
   ```

2. **Apply the `_backup_corrupt_db` defense-in-depth guards** from commit
   `42864e9486`. These guards are already in the codebase if you are on a
   build that includes that commit. Verify:
   ```bash
   cd /path/to/hermes-agent
   git log --oneline 42864e9486 -1
   # Should show: VFE-INFRA-05: Implement 3 defense-in-depth fixes...
   ```

   The three guards are:
   - **Zero-byte quarantine cleanup** — removes interrupted `shutil.copy2`
     artifacts so retries can complete.
   - **Stable fingerprinting** — copies to a temp file before hashing,
     preventing N different hashes for the same logical corrupt state.
   - **Storm freeze** — when >=3 backups land in <60 seconds, pruning is
     frozen to preserve the forensic corpus.

3. **Set the escape hatch only if you have verified single-writer.** If
   the fail-closed guard (`KanbanDbOnNetworkFsError`) is blocking a
   one-off diagnostic or a verified single-writer setup:
   ```bash
   export HERMES_KANBAN_ALLOW_NFS=1
   ```
   Do NOT set this as a permanent configuration. It bypasses the only
   automated guard against the corruption class.

## 6. Do not delete .bak files until the quiet window passes

The `kanban.db.corrupt.*.bak` files are evidence. They are
content-addressed by SHA-256 of the corrupt DB state, so:

- Identical corruption deduplicates to one backup (no disk waste from
  repeats).
- Evolving corruption (partial repairs, further damage) mints new
  fingerprints — each is a distinct forensic snapshot.
- The retention cap (`_CORRUPT_BACKUP_RETENTION = 10`) prunes oldest
  backups beyond the cap, but pruning is FROZEN during an active storm
  (>=3 in <60s) specifically to preserve the forensic corpus.

**Wait until the 30-minute quiet window passes** (no new `.bak` files,
WAL checkpointing normally, `integrity_check` returns `ok`) before
archiving or deleting any `.bak` files. Until then, they are your
evidence trail for root-cause analysis.

## 7. Reference

| Item | Value |
|---|---|
| Diagnosis task | `t_b8863099` |
| Fix task | `t_baff33c8` |
| This runbook task | `t_d6e7a50b` |
| Defense-in-depth commit | `42864e9486` |
| NFS guard commit | `e3795bc039` |
| Guard class | `KanbanDbOnNetworkFsError` in `hermes_cli/kanban_db.py` |
| Guard function | `_guard_kanban_db_not_on_network_fs` in `hermes_cli/kanban_db.py` |
| Backup function | `_backup_corrupt_db` in `hermes_cli/kanban_db.py` |
| WAL fallback | `apply_wal_with_fallback` in `hermes_state.py` |
| WAL incompat markers | `_WAL_INCOMPAT_MARKERS` in `hermes_state.py:290` |
| Escape hatch env var | `HERMES_KANBAN_ALLOW_NFS=1` |
| Unsafe fstypes | `_KANBAN_UNSAFE_FSTYPES` in `hermes_cli/kanban_db.py` |
| Multi-gateway config | `docs/kanban/multi-gateway.md` |
| Quiet-window verification | Parent task `t_afa0c2d2` (PASS, 2026-08-24) |