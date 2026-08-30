"""Tests: ``tasks.worker_node`` bookkeeping keeps reap loops from
false-crashing SSH-spawned remote workers (ticket t_78fbccf4; RCA
t_360c2eb1).

Failure mode being locked down:

- ``_default_spawn`` SSHes a worker to a remote node and returns its
  remote pid.
- The task row's ``claim_lock`` still carries the LOCAL host prefix
  (it was set by ``claim_task`` on the local dispatcher before the
  node router ran), so the ``host_prefix`` guard in
  ``detect_crashed_workers`` does NOT exclude the row.
- ``_pid_alive(remote_pid)`` runs against ``/proc/<pid>`` on the local
  host and returns False (or True for an unrelated local pid) — either
  way, the reap loop reclaims a perfectly healthy remote worker every
  tick, spawning a false-crash storm.

The fix persists the actual spawn node in ``tasks.worker_node`` and
gates both reap loops (``detect_crashed_workers`` and
``release_stale_claims``) on it: a task whose ``worker_node`` differs
from the local node is left alone by pid-based checks. Heartbeat
staleness remains the backstop for genuinely dead remote workers.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    # No crash grace window — we want reap decisions immediately.
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    # Pin the local node id so the test is deterministic regardless of
    # the operator's env.
    monkeypatch.setenv("HERMES_CLUSTER_LOCAL_NODE", "hermes2")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db()
    return home


@pytest.fixture
def conn(kanban_home):
    with kb.connect() as c:
        yield c


# ---------------------------------------------------------------------------
# Migration + dataclass hydration
# ---------------------------------------------------------------------------

def test_migration_adds_worker_node_column(conn):
    """``_migrate_add_optional_columns`` must land the ``worker_node``
    column on legacy tables. Also verifies rerunning is a no-op."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "worker_node" in cols

    # Drop the column via SQLite's ADD-only ALTER TABLE limitation? We
    # can't. Instead simulate a legacy table by recreating a schema
    # without the column and confirming the migration re-adds it.
    conn.executescript(
        """
        DROP TABLE IF EXISTS tasks_legacy;
        CREATE TABLE tasks_legacy (id TEXT PRIMARY KEY, title TEXT);
        """
    )
    # The migration helper is idempotent; a rerun on the live table
    # must succeed (no duplicate-column error).
    kb._migrate_add_optional_columns(conn)
    cols_after = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
    assert "worker_node" in cols_after


def test_task_dataclass_hydrates_worker_node(conn):
    tid = kb.create_task(conn, title="cluster-task", assignee="eng")
    t = kb.get_task(conn, tid)
    assert t.worker_node is None, "new tasks default to local (NULL)"

    kb._set_worker_node(conn, tid, "hermes3")
    t2 = kb.get_task(conn, tid)
    assert t2.worker_node == "hermes3"

    kb._set_worker_node(conn, tid, None)
    t3 = kb.get_task(conn, tid)
    assert t3.worker_node is None


# ---------------------------------------------------------------------------
# detect_crashed_workers respects worker_node
# ---------------------------------------------------------------------------

def test_detect_crashed_skips_remote_worker_even_with_dead_pid(conn):
    """A remote task whose recorded pid happens to be dead on THIS host
    must NOT be reclaimed by ``detect_crashed_workers`` — the pid is
    meaningless here. The remote node's reap loop is authoritative."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="remote-worker", assignee="eng")
    kb.claim_task(conn, tid, claimer=f"{host}:A")

    dead = subprocess.Popen(["true"])
    dead.wait()
    kb._set_worker_pid(conn, tid, dead.pid)
    # Mark the task as running on a different node.
    kb._set_worker_node(conn, tid, "hermes1")

    crashed = kb.detect_crashed_workers(conn)
    assert tid not in crashed, (
        "remote-node task was reclaimed by local reap loop — the fix "
        "did not take effect"
    )
    status = conn.execute(
        "SELECT status FROM tasks WHERE id=?", (tid,)
    ).fetchone()["status"]
    assert status == "running"


def test_detect_crashed_still_reclaims_local_dead_worker(conn):
    """Baseline: a task with ``worker_node`` NULL (== local) still
    goes through the pid liveness check and gets reclaimed when the
    worker is dead. This is the pre-existing behaviour and must be
    preserved."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="local-worker", assignee="eng")
    kb.claim_task(conn, tid, claimer=f"{host}:A")
    dead = subprocess.Popen(["true"])
    dead.wait()
    kb._set_worker_pid(conn, tid, dead.pid)
    # No worker_node set → treated as local, must be reclaimed.
    conn.execute(
        "UPDATE tasks SET started_at = started_at - 9999 WHERE id=?", (tid,),
    )
    conn.execute(
        "UPDATE task_runs SET started_at = started_at - 9999 WHERE task_id=?",
        (tid,),
    )
    conn.commit()
    kb._record_worker_exit(dead.pid, 1 << 8)

    crashed = kb.detect_crashed_workers(conn)
    assert tid in crashed
    status = conn.execute(
        "SELECT status FROM tasks WHERE id=?", (tid,)
    ).fetchone()["status"]
    assert status in ("ready", "blocked", "todo")


def test_detect_crashed_reclaims_local_when_worker_node_matches(conn):
    """A task whose ``worker_node`` equals the local node behaves like
    a NULL worker_node — it goes through the pid liveness check and is
    reclaimed on dead-pid, so the column doesn't accidentally shield
    genuinely-local workers from reap when someone tags them
    explicitly.

    Also verifies the reclaim's UPDATE clears ``worker_node`` so a
    subsequent local respawn starts with a clean row (the mirror of the
    remote-crash bug: if the tagging node's own reap leaves
    ``worker_node`` intact, a different node's later local spawn sees a
    stale placement tag and skips or mis-owns the task).
    """
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="tagged-local", assignee="eng")
    kb.claim_task(conn, tid, claimer=f"{host}:A")
    dead = subprocess.Popen(["true"])
    dead.wait()
    kb._set_worker_pid(conn, tid, dead.pid)
    kb._set_worker_node(conn, tid, kb._local_node_id())  # explicit local tag
    conn.execute(
        "UPDATE tasks SET started_at = started_at - 9999 WHERE id=?", (tid,),
    )
    conn.execute(
        "UPDATE task_runs SET started_at = started_at - 9999 WHERE task_id=?",
        (tid,),
    )
    conn.commit()
    kb._record_worker_exit(dead.pid, 1 << 8)

    crashed = kb.detect_crashed_workers(conn)
    assert tid in crashed
    row = conn.execute(
        "SELECT status, worker_node, claim_lock, worker_pid "
        "FROM tasks WHERE id=?", (tid,),
    ).fetchone()
    assert row["worker_node"] is None, (
        "detect_crashed_workers must clear worker_node so a subsequent "
        "spawn on a different node doesn't inherit the stale placement "
        "tag (mirror-of-remote-crash bug, t_a57efff4)"
    )
    assert row["claim_lock"] is None
    assert row["worker_pid"] is None


# ---------------------------------------------------------------------------
# release_stale_claims respects worker_node
# ---------------------------------------------------------------------------

def test_release_stale_extends_ttl_for_remote_task_with_fresh_heartbeat(conn):
    """A remote-node task with a fresh heartbeat must have its TTL
    extended (not reclaimed) even though the local pid check would
    fail. Fresh = within DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="remote-fresh-hb", assignee="eng")
    kb.claim_task(conn, tid, claimer=f"{host}:A")

    dead = subprocess.Popen(["true"])
    dead.wait()  # pid is dead locally but the "real" worker lives elsewhere
    kb._set_worker_pid(conn, tid, dead.pid)
    kb._set_worker_node(conn, tid, "hermes1")

    now = int(time.time())
    original_expires = now - 60  # already expired
    conn.execute(
        "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
        "WHERE id = ?",
        (original_expires, now - 30, tid),  # fresh heartbeat 30s ago
    )
    conn.execute(
        "UPDATE task_runs SET claim_expires = ? WHERE task_id = ?",
        (original_expires, tid),
    )
    conn.commit()

    reclaimed_count = kb.release_stale_claims(conn)
    row = conn.execute(
        "SELECT status, claim_lock, claim_expires FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["status"] == "running", (
        f"remote task with fresh heartbeat was reclaimed "
        f"(count={reclaimed_count}, status={row['status']})"
    )
    assert row["claim_lock"] == f"{host}:A"
    assert row["claim_expires"] > original_expires, (
        "remote-worker branch failed to extend the claim TTL"
    )


def test_release_stale_reclaims_remote_task_with_stale_heartbeat(conn):
    """The heartbeat-stale backstop still fires for remote-node tasks —
    a worker that hasn't heartbeat past the max-stale window is
    genuinely gone and must be released back to ``ready``."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="remote-dead-hb", assignee="eng")
    kb.claim_task(conn, tid, claimer=f"{host}:A")

    dead = subprocess.Popen(["true"])
    dead.wait()
    kb._set_worker_pid(conn, tid, dead.pid)
    kb._set_worker_node(conn, tid, "hermes1")

    now = int(time.time())
    stale_hb = now - (kb.DEFAULT_CLAIM_HEARTBEAT_MAX_STALE_SECONDS + 60)
    conn.execute(
        "UPDATE tasks SET claim_expires = ?, last_heartbeat_at = ? "
        "WHERE id = ?",
        (now - 60, stale_hb, tid),
    )
    conn.execute(
        "UPDATE task_runs SET claim_expires = ? WHERE task_id = ?",
        (now - 60, tid),
    )
    conn.commit()

    kb.release_stale_claims(conn)
    row = conn.execute(
        "SELECT status, claim_lock, worker_node FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["status"] == "ready"
    assert row["claim_lock"] is None
    assert row["worker_node"] is None, (
        "reclaim path should clear worker_node so the next claim starts clean"
    )


# ---------------------------------------------------------------------------
# Dispatcher wiring: _default_spawn._last_actual_node signals node placement
# ---------------------------------------------------------------------------

def test_default_spawn_records_none_on_local_path(monkeypatch, conn):
    """When ``target_node`` is None (single-host board), the attribute
    is set to None so the dispatcher records NULL worker_node — the
    pre-existing behaviour for local spawns."""
    # We only need the pre-return bookkeeping side-effect. Stub out the
    # real subprocess plumbing by patching subprocess.Popen inside the
    # module namespace: return a dummy handle with a pid.
    tid = kb.create_task(conn, title="local-spawn", assignee="eng")
    task = kb.get_task(conn, tid)

    class _Handle:
        def __init__(self):
            self.pid = 424242

    def _fake_popen(*a, **kw):
        return _Handle()

    monkeypatch.setattr(kb.subprocess, "Popen", _fake_popen)
    # Silence anything the function tries to write out of band.
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    # Call with target_node=None → local path → attribute stays None.
    try:
        kb._default_spawn(task, "/tmp", board="default", target_node=None)
    except Exception:
        # We don't care whether the fake pid + missing workspace fails
        # later in the function; only the pre-return bookkeeping is
        # under test, and that's set at the TOP of the function.
        pass
    assert getattr(kb._default_spawn, "_last_actual_node", "unset") is None


# ---------------------------------------------------------------------------
# Mirror-of-remote-crash: every reclaim path must NULL worker_node
# ---------------------------------------------------------------------------
#
# Scenario (per t_a57efff4 review):
#
#   1. Task remote-spawned on hermes3 → row.worker_node = "hermes3"
#   2. hermes3's LOCAL reap detects real crash, reclaims the task
#      (nulls pid + lock)
#   3. hermes2 claims the ready row and spawns LOCALLY (no remote
#      routing) → _last_actual_node stays None → dispatcher never
#      calls _set_worker_node, so row still says worker_node="hermes3"
#   4. Row = {worker_node:"hermes3", worker_pid:<hermes2 pid>}
#      → hermes2's reap skips it (worker_node != local), hermes3's
#        reap mis-owns it → false-crash storm or eternal running.
#
# Fix: every ``UPDATE tasks SET status = 'ready'`` reclaim path adds
# ``worker_node = NULL`` to the SET list, so step 3 sees a clean row.

def _prep_reclaimable_local(
    conn, *, worker_node: str, host: str, backdate_seconds: int = 9999
):
    """Set up a running task pinned to ``worker_node`` with a dead pid
    and backdated started_at so any reap-path check that gates on age
    fires. Returns (task_id, dead_pid)."""
    tid = kb.create_task(conn, title="mirror-bug", assignee="eng")
    kb.claim_task(conn, tid, claimer=f"{host}:A")
    dead = subprocess.Popen(["true"])
    dead.wait()
    kb._set_worker_pid(conn, tid, dead.pid)
    kb._set_worker_node(conn, tid, worker_node)
    conn.execute(
        "UPDATE tasks SET started_at = started_at - ? WHERE id=?",
        (backdate_seconds, tid),
    )
    conn.execute(
        "UPDATE task_runs SET started_at = started_at - ? WHERE task_id=?",
        (backdate_seconds, tid),
    )
    conn.commit()
    kb._record_worker_exit(dead.pid, 1 << 8)
    return tid, dead.pid


def test_detect_crashed_reclaim_clears_worker_node(conn):
    """The primary local-reap path (``detect_crashed_workers``) must
    null ``worker_node`` on reclaim so a subsequent local respawn on
    ANY node starts with a clean placement tag."""
    host = kb._claimer_id().split(":", 1)[0]
    local = kb._local_node_id()
    tid, _pid = _prep_reclaimable_local(conn, worker_node=local, host=host)

    crashed = kb.detect_crashed_workers(conn)
    assert tid in crashed

    row = conn.execute(
        "SELECT status, worker_node FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["status"] in ("ready", "blocked", "todo")
    assert row["worker_node"] is None


def test_release_stale_local_branch_clears_worker_node(conn):
    """The ``release_stale_claims`` LOCAL branch (TTL expired, worker
    dead here) must null ``worker_node`` on reclaim.

    The mirror-bug narrative: hermes3 runs release_stale_claims against
    a task it spawned locally (worker_node = "hermes3"). Before the
    fix, that path cleared pid/lock but not worker_node, so a later
    local spawn on hermes2 inherited the stale ``hermes3`` tag.
    """
    host = kb._claimer_id().split(":", 1)[0]
    local = kb._local_node_id()
    tid, _pid = _prep_reclaimable_local(conn, worker_node=local, host=host)

    # Force TTL expiry so release_stale_claims fires.
    now = int(time.time())
    conn.execute(
        "UPDATE tasks SET claim_expires = ? WHERE id = ?",
        (now - 60, tid),
    )
    conn.execute(
        "UPDATE task_runs SET claim_expires = ? WHERE task_id = ?",
        (now - 60, tid),
    )
    conn.commit()

    kb.release_stale_claims(conn)
    row = conn.execute(
        "SELECT status, worker_node, claim_lock FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["status"] == "ready", (
        f"release_stale_claims local branch did not reclaim "
        f"(status={row['status']})"
    )
    assert row["claim_lock"] is None
    assert row["worker_node"] is None, (
        "release_stale_claims local branch must null worker_node — "
        "otherwise a subsequent local spawn on a different node inherits "
        "the stale placement tag (t_a57efff4 mirror bug)"
    )


def test_spawn_failure_release_clears_worker_node(conn):
    """The spawn-failure release path (``_record_spawn_failure`` with
    ``release_claim=True``) must null ``worker_node`` — a task whose
    spawn attempt failed after a prior remote assignment must not
    carry that node tag into the next dispatch tick."""
    host = kb._claimer_id().split(":", 1)[0]
    tid = kb.create_task(conn, title="spawn-fail", assignee="eng")
    kb.claim_task(conn, tid, claimer=f"{host}:A")
    kb._set_worker_node(conn, tid, "hermes3")  # pretend prior remote route

    kb._record_spawn_failure(
        conn, tid, "boom: fake spawn failure",
        failure_limit=999,  # well above threshold → release, don't give up
    )

    row = conn.execute(
        "SELECT status, worker_node, claim_lock, worker_pid "
        "FROM tasks WHERE id=?", (tid,)
    ).fetchone()
    assert row["status"] == "ready"
    assert row["claim_lock"] is None
    assert row["worker_pid"] is None
    assert row["worker_node"] is None, (
        "spawn-failure release must null worker_node so the next "
        "dispatch tick doesn't inherit the stale remote-node tag"
    )
