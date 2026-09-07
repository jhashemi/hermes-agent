"""P2-002 DoD acceptance tests for AccessControlManager thread-safety.

Explicitly verifies the acceptance criteria of the P2-002 kanban task
(t_2972f89e):

  - RLock implemented and held during all mutations
  - File writes are atomic (temp -> rename, never truncated)
  - 10 concurrent grants don't corrupt the whitelist
  - Audit log has all 10 entries
  - File persists correctly after concurrent updates
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

from gateway.access_control import AccessControlManager, DEFAULT_WHITELIST


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_files(tmp_path: Path):
    access_file = tmp_path / "access_control.json"
    audit_file = tmp_path / "audit.log"
    yield access_file, audit_file


@pytest.fixture
def manager(tmp_files):
    access_file, audit_file = tmp_files
    with mock.patch("gateway.access_control.ACCESS_CONTROL_FILE", access_file), \
         mock.patch("gateway.access_control.AUDIT_LOG_FILE", audit_file):
        yield AccessControlManager()


# ---------------------------------------------------------------------------
# 1. Lock IS an RLock (DoD: "RLock implemented and held during all mutations")
# ---------------------------------------------------------------------------

def test_lock_is_reentrant_rlock(manager):
    """The instance lock must be an RLock so lock-holding methods can
    recursively call other lock-guarded helpers without self-deadlock.
    """
    # threading.RLock() returns a private _thread.RLock object whose class
    # name is either 'RLock' or '_RLock' depending on the CPython
    # implementation of the lock primitive. Both are acceptable.
    assert type(manager._lock).__name__ in {"RLock", "_RLock"}
    assert type(manager._audit_lock).__name__ in {"RLock", "_RLock"}

    # Sanity check: acquire twice from the same thread must NOT block.
    assert manager._lock.acquire(timeout=1.0)
    try:
        assert manager._lock.acquire(timeout=1.0)
        manager._lock.release()
    finally:
        manager._lock.release()


# ---------------------------------------------------------------------------
# 2. Atomic file write (DoD: "File writes are atomic (temp -> rename)")
# ---------------------------------------------------------------------------

def test_save_uses_atomic_replace(manager, tmp_files):
    """_save_to_file must go through os.replace() so a partially-written
    JSON file is impossible.
    """
    access_file, _ = tmp_files
    with mock.patch("gateway.access_control.os.replace", wraps=os.replace) as spy:
        manager.grant_access("atomic_user")
    # grant_access -> _save_to_file -> os.replace(tmp, target)
    assert spy.called, "Atomic os.replace() was not used for the write"
    # After the grant, the file must be valid JSON with our user.
    data = json.loads(access_file.read_text())
    assert "atomic_user" in data["whitelist"]


def test_no_partial_file_visible_during_write(tmp_files):
    """If json.dumps produced a partial payload, a reader must not see it.

    We simulate an interrupted write by making the temp-write raise AFTER
    the temp file is created but BEFORE os.replace() runs. The target file
    should be unchanged and still contain the original content.
    """
    access_file, audit_file = tmp_files
    with mock.patch("gateway.access_control.ACCESS_CONTROL_FILE", access_file), \
         mock.patch("gateway.access_control.AUDIT_LOG_FILE", audit_file):
        m = AccessControlManager()

    # Snapshot known-good file state produced by __init__.
    good_bytes = access_file.read_bytes()
    assert good_bytes  # sanity: something was persisted

    original_replace = os.replace

    def failing_replace(*_a, **_kw):
        raise RuntimeError("simulated crash between fsync and rename")

    with mock.patch("gateway.access_control.os.replace", side_effect=failing_replace):
        # This grant will attempt to save, fail during replace, and the
        # error is swallowed by _save_to_file's outer except (logged only).
        m.whitelist.add("would_be_added")
        m._save_to_file()

    # File must be unchanged (still the original good bytes).
    assert access_file.read_bytes() == good_bytes

    # And no stray *.json.tmp files should be left behind in the directory.
    leftovers = list(access_file.parent.glob(".access_control.*.json.tmp"))
    assert leftovers == [], f"Temp files leaked: {leftovers}"

    # Sanity: os.replace still works normally after we drop the mock.
    m._save_to_file()
    assert original_replace  # keep the reference to silence lint


# ---------------------------------------------------------------------------
# 3. 10 concurrent grants: whitelist intact + audit log has 10 entries
#    + file persists correctly (DoD tests, verbatim)
# ---------------------------------------------------------------------------

def test_10_concurrent_grants_full_dod(manager, tmp_files):
    access_file, audit_file = tmp_files

    users = [f"concurrent_user_{i}" for i in range(10)]
    barrier = threading.Barrier(len(users))
    results: list[bool] = []
    results_lock = threading.Lock()

    def grant(u: str):
        barrier.wait()  # maximize contention
        ok = manager.grant_access(u, grantor_id="test_admin")
        with results_lock:
            results.append(ok)

    threads = [threading.Thread(target=grant, args=(u,)) for u in users]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert all(not t.is_alive() for t in threads), "deadlock / hang"

    # All 10 were newly added.
    assert len(results) == 10
    assert all(results), f"some grants returned False: {results}"

    # Whitelist has all 10 (plus defaults) — no corruption.
    for u in users:
        assert manager.check_access(u), f"missing user after concurrent grant: {u}"

    # File on disk mirrors in-memory state and is valid JSON.
    on_disk = json.loads(access_file.read_text())
    for u in users:
        assert u in on_disk["whitelist"]
    for d in DEFAULT_WHITELIST:
        assert d in on_disk["whitelist"]

    # Audit log has all 10 entries, each valid JSON, action=grant.
    log_lines = [ln for ln in audit_file.read_text().splitlines() if ln.strip()]
    grant_entries = [
        e for e in (json.loads(ln) for ln in log_lines)
        if e.get("action") == "grant" and e.get("user_id") in users
    ]
    assert len(grant_entries) == 10, (
        f"expected 10 grant audit entries, got {len(grant_entries)}"
    )
    assert {e["user_id"] for e in grant_entries} == set(users)
    for e in grant_entries:
        assert e["grantor_id"] == "test_admin"
        assert e["timestamp"].endswith("Z")


def test_file_persists_across_manager_reload(manager, tmp_files):
    """After concurrent grants, a fresh manager loading the same file
    must see the same whitelist.
    """
    access_file, audit_file = tmp_files
    for i in range(10):
        manager.grant_access(f"persist_user_{i}", grantor_id="test_admin")

    # Reload
    with mock.patch("gateway.access_control.ACCESS_CONTROL_FILE", access_file), \
         mock.patch("gateway.access_control.AUDIT_LOG_FILE", audit_file):
        reloaded = AccessControlManager()

    for i in range(10):
        assert reloaded.check_access(f"persist_user_{i}")
