"""Unit tests for AccessControlManager (gateway.access_control).

Task t_c30a3997 — TEST: Unit Tests for Access Control.

Nine required test functions covering AccessControlManager behavior:

1.  test_grant_access                       — normal + duplicate attempts
2.  test_revoke_access                      — normal + default user protection
3.  test_thread_safety_concurrent_grants    — race condition test
4.  test_thread_safety_concurrent_revokes   — race condition test
5.  test_user_id_validation                 — format validation
6.  test_user_id_length                     — max length enforcement
7.  test_whitelist_persistence              — file save/load roundtrip
8.  test_audit_logging                      — log entries created
9.  test_default_whitelist                  — cannot revoke defaults

Coverage target: 85%+ of gateway/access_control.py::AccessControlManager
(module-level validate_user_id() also covered by tests 5 & 6).

Isolation: every test that mutates state runs against a per-test temp
JSON file and temp audit log via the ``isolated_manager`` fixture, so
tests never touch ~/.hermes/access_control.json or ~/.hermes/audit.log.
"""

from __future__ import annotations

import json
import tempfile
import threading
from pathlib import Path
from unittest import mock

import pytest

from gateway.access_control import (
    AccessControlManager,
    DEFAULT_WHITELIST,
    MAX_USER_ID_LENGTH,
    validate_user_id,
)


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_files(tmp_path):
    """Provide isolated paths for the access-control JSON + audit log."""
    access_file = tmp_path / "access_control.json"
    audit_file = tmp_path / "audit.log"
    return access_file, audit_file


@pytest.fixture
def isolated_manager(temp_files):
    """Yield an AccessControlManager wired to per-test temp files."""
    access_file, audit_file = temp_files
    with mock.patch("gateway.access_control.ACCESS_CONTROL_FILE", access_file), \
         mock.patch("gateway.access_control.AUDIT_LOG_FILE", audit_file):
        yield AccessControlManager()


# ============================================================================
# 1. test_grant_access — normal + duplicate attempts
# ============================================================================

def test_grant_access(isolated_manager):
    """grant_access adds new user (True); duplicate grant is idempotent (False).

    Also exercises has_access(MessageEvent) — the intended public read path —
    plus the user_id-extraction fallbacks (event.user_id present, chat_id
    fallback, and the unknown_user default) and list_users() formatting.
    """
    from unittest.mock import Mock
    mgr = isolated_manager

    # Normal path: newly granted returns True and user appears in whitelist.
    assert mgr.grant_access("new_user") is True
    assert "new_user" in mgr.whitelist

    # Duplicate: second grant returns False, whitelist size unchanged.
    size_before = len(mgr.whitelist)
    assert mgr.grant_access("new_user") is False
    assert len(mgr.whitelist) == size_before
    assert "new_user" in mgr.whitelist

    # check_access reflects grant.
    assert mgr.check_access("new_user") is True
    assert mgr.check_access("stranger_never_added") is False

    # has_access with a MessageEvent-shaped mock (event.user_id path).
    event_with_user = Mock()
    event_with_user.user_id = "new_user"
    event_with_user.chat_id = "some_chat"
    assert mgr.has_access(event_with_user) is True

    # has_access when only chat_id is set — get_user_id falls back to chat_id.
    event_chat_only = Mock(spec=[])   # no attributes
    event_chat_only.chat_id = "new_user"   # dynamically set — hasattr picks it up
    assert mgr.get_user_id(event_chat_only) == "new_user"
    assert mgr.has_access(event_chat_only) is True

    # has_access when neither user_id nor chat_id is present — falls back to
    # the "unknown_user" sentinel (not in whitelist).
    event_none = Mock(spec=[])
    assert mgr.get_user_id(event_none) == "unknown_user"
    assert mgr.has_access(event_none) is False

    # list_users formats the whitelist as a human-readable string.
    listing = mgr.list_users()
    assert "new_user" in listing
    assert "Total:" in listing
    for default_user in DEFAULT_WHITELIST:
        assert default_user in listing

    # reset_to_defaults wipes grants and restores DEFAULT_WHITELIST.
    mgr.reset_to_defaults()
    assert "new_user" not in mgr.whitelist
    assert mgr.whitelist == set(DEFAULT_WHITELIST)


# ============================================================================
# 2. test_revoke_access — normal + default user protection (at manager level)
# ============================================================================

def test_revoke_access(isolated_manager):
    """revoke_access removes granted user; on user not in whitelist returns False.

    (Default-user PROTECTION at the *handler* layer is exercised by
    test_default_whitelist below; at the manager layer revoke works on any id,
    so this test covers the manager-level normal-path + not-present path.)
    """
    mgr = isolated_manager

    # Grant then revoke — should succeed both times.
    mgr.grant_access("temp_user")
    assert "temp_user" in mgr.whitelist
    assert mgr.revoke_access("temp_user") is True
    assert "temp_user" not in mgr.whitelist

    # Revoking a user that isn't in the whitelist returns False (idempotent).
    assert mgr.revoke_access("never_existed") is False


# ============================================================================
# 3. test_thread_safety_concurrent_grants — race condition test
# ============================================================================

def test_thread_safety_concurrent_grants(isolated_manager):
    """Concurrent grants from many threads must not corrupt the whitelist.

    N threads each grant M distinct users. Post-condition:
    every one of N*M ids is present exactly once, plus the defaults.
    """
    mgr = isolated_manager
    N_THREADS = 10
    PER_THREAD = 20
    expected_new: set[str] = set()

    def worker(tid: int) -> None:
        for i in range(PER_THREAD):
            mgr.grant_access(f"user_t{tid}_i{i}")

    for tid in range(N_THREADS):
        for i in range(PER_THREAD):
            expected_new.add(f"user_t{tid}_i{i}")

    threads = [threading.Thread(target=worker, args=(t,)) for t in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Every newly-granted id present.
    assert expected_new.issubset(mgr.whitelist)
    # Defaults preserved.
    assert DEFAULT_WHITELIST.issubset(mgr.whitelist)
    # No duplicates / no unexpected drift.
    assert mgr.whitelist == DEFAULT_WHITELIST | expected_new


# ============================================================================
# 4. test_thread_safety_concurrent_revokes — race condition test
# ============================================================================

def test_thread_safety_concurrent_revokes(isolated_manager):
    """Concurrent revokes on the same pre-populated user set must be atomic.

    Pre-seed N*M users, then revoke them from N threads concurrently
    (each thread iterates every id). At the end, whitelist must be exactly
    the defaults — no id survives, no crash, no partial state.
    """
    mgr = isolated_manager
    N_THREADS = 10
    N_IDS = 50
    ids = [f"revoke_me_{i}" for i in range(N_IDS)]

    # Pre-seed
    for uid in ids:
        mgr.grant_access(uid)
    for uid in ids:
        assert uid in mgr.whitelist

    def worker() -> None:
        # Every thread tries to revoke every id — at most one wins per id,
        # the rest see it already gone and return False. Nothing must raise.
        for uid in ids:
            mgr.revoke_access(uid)

    threads = [threading.Thread(target=worker) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # All revocable users are gone; defaults untouched.
    for uid in ids:
        assert uid not in mgr.whitelist
    assert mgr.whitelist == set(DEFAULT_WHITELIST)


# ============================================================================
# 5. test_user_id_validation — format validation
# ============================================================================

def test_user_id_validation():
    """validate_user_id enforces alphanumeric + underscore, non-empty."""
    # Valid shapes.
    for good in ("john_doe", "abc123", "___", "a", "Taylor_Swanson_1"):
        ok, err = validate_user_id(good)
        assert ok is True, f"expected {good!r} to be valid, got {err!r}"
        assert err is None

    # Invalid: empty / None / non-string.
    for bad in ("", None, 12345, [], {}):
        ok, err = validate_user_id(bad)  # type: ignore[arg-type]
        assert ok is False
        assert err  # non-empty error message

    # Invalid: illegal characters (space, dash, dot, at, slash, unicode).
    for bad in ("john doe", "john-doe", "john.doe", "john@example", "john/doe", "café"):
        ok, err = validate_user_id(bad)
        assert ok is False, f"expected {bad!r} to be invalid"
        assert "alphanumeric" in err.lower() or "underscore" in err.lower()


# ============================================================================
# 6. test_user_id_length — max length enforcement
# ============================================================================

def test_user_id_length():
    """validate_user_id caps at MAX_USER_ID_LENGTH characters."""
    # At the boundary — exactly MAX_USER_ID_LENGTH is allowed.
    at_limit = "a" * MAX_USER_ID_LENGTH
    ok, err = validate_user_id(at_limit)
    assert ok is True, f"length={MAX_USER_ID_LENGTH} should be valid, got {err!r}"
    assert err is None

    # One over — rejected with a length-specific error.
    over = "a" * (MAX_USER_ID_LENGTH + 1)
    ok, err = validate_user_id(over)
    assert ok is False
    assert err is not None
    assert "length" in err.lower() or str(MAX_USER_ID_LENGTH) in err

    # Way over — still rejected.
    way_over = "b" * (MAX_USER_ID_LENGTH * 10)
    ok, err = validate_user_id(way_over)
    assert ok is False
    assert err is not None


# ============================================================================
# 7. test_whitelist_persistence — file save/load roundtrip
# ============================================================================

def test_whitelist_persistence(temp_files):
    """Grants are persisted to disk and reloaded on a fresh manager."""
    access_file, audit_file = temp_files

    with mock.patch("gateway.access_control.ACCESS_CONTROL_FILE", access_file), \
         mock.patch("gateway.access_control.AUDIT_LOG_FILE", audit_file):

        # Instance A: seed with two extra users.
        mgr_a = AccessControlManager()
        mgr_a.grant_access("persisted_user_one")
        mgr_a.grant_access("persisted_user_two")

        # File must exist and contain the whitelist as JSON.
        assert access_file.exists(), "access control file was not written"
        data = json.loads(access_file.read_text())
        assert "whitelist" in data
        assert isinstance(data["whitelist"], list)
        assert "persisted_user_one" in data["whitelist"]
        assert "persisted_user_two" in data["whitelist"]
        # Defaults also persisted.
        for default_user in DEFAULT_WHITELIST:
            assert default_user in data["whitelist"]

        # Instance B: fresh manager reads the same file and observes the grants.
        mgr_b = AccessControlManager()
        assert "persisted_user_one" in mgr_b.whitelist
        assert "persisted_user_two" in mgr_b.whitelist
        assert DEFAULT_WHITELIST.issubset(mgr_b.whitelist)

        # Revoke on B is also persisted — instance C sees the deletion.
        mgr_b.revoke_access("persisted_user_one")
        mgr_c = AccessControlManager()
        assert "persisted_user_one" not in mgr_c.whitelist
        assert "persisted_user_two" in mgr_c.whitelist

        # Corrupt-file recovery: garble the JSON and construct a fresh manager.
        # It must log-and-recover to DEFAULT_WHITELIST (exception path on
        # lines 133-135 of access_control.py), not raise.
        access_file.write_text("{ this is not valid json ][")
        mgr_recovered = AccessControlManager()
        assert mgr_recovered.whitelist == set(DEFAULT_WHITELIST)

        # And after recovery the file has been rewritten as valid JSON.
        recovered_data = json.loads(access_file.read_text())
        assert set(recovered_data["whitelist"]) == set(DEFAULT_WHITELIST)


# ============================================================================
# 8. test_audit_logging — log entries created
# ============================================================================

def test_audit_logging(isolated_manager, temp_files):
    """grant_access and revoke_access append JSON-line entries to the audit log."""
    _, audit_file = temp_files
    mgr = isolated_manager

    # Perform a grant then a revoke — each should produce one audit line.
    mgr.grant_access("audited_user", grantor_id="admin_person")
    mgr.revoke_access("audited_user", grantor_id="admin_person")

    assert audit_file.exists(), "audit log file was not created"
    lines = [ln for ln in audit_file.read_text().splitlines() if ln.strip()]
    assert len(lines) == 2, f"expected 2 audit lines, got {len(lines)}: {lines!r}"

    grant_entry = json.loads(lines[0])
    revoke_entry = json.loads(lines[1])

    # Required schema fields.
    for entry, expected_action in ((grant_entry, "grant"), (revoke_entry, "revoke")):
        assert entry["user_id"] == "audited_user"
        assert entry["action"] == expected_action
        assert entry["grantor_id"] == "admin_person"
        assert "timestamp" in entry
        assert entry["timestamp"].endswith("Z")  # ISO-Z formatted UTC stamp

    # Idempotent no-op grants (duplicate) must NOT emit a spurious audit line.
    mgr.grant_access("audited_user_2")   # +1 line
    mgr.grant_access("audited_user_2")   # duplicate — no audit line
    lines_after = [ln for ln in audit_file.read_text().splitlines() if ln.strip()]
    assert len(lines_after) == 3, (
        f"duplicate grant should not audit; got {len(lines_after)} lines"
    )

    # Same for revoke-of-nonexistent.
    mgr.revoke_access("never_here")
    lines_final = [ln for ln in audit_file.read_text().splitlines() if ln.strip()]
    assert len(lines_final) == 3

    # Direct call to audit_log() with an unrecognized action normalizes to
    # "unknown" (defensive branch on line 193 of access_control.py).
    mgr.audit_log("someone", "wat", "sys")
    lines_unknown = [ln for ln in audit_file.read_text().splitlines() if ln.strip()]
    assert len(lines_unknown) == 4
    normalized = json.loads(lines_unknown[-1])
    assert normalized["action"] == "unknown"
    assert normalized["user_id"] == "someone"

    # list_users returns a helpful message when the whitelist is empty
    # (branch coverage for the empty-list path).
    mgr.whitelist.clear()
    empty_listing = mgr.list_users()
    assert "empty" in empty_listing.lower()


# ============================================================================
# 9. test_default_whitelist — cannot revoke defaults (via handler layer)
# ============================================================================

def test_default_whitelist(isolated_manager):
    """The /access-revoke handler refuses to remove any DEFAULT_WHITELIST user.

    The manager-level revoke_access() is intentionally unopinionated (that's
    what the handler layer guards). The DoD-relevant protection lives in
    handle_access_revoke_command, so we test both:

      (a) DEFAULT_WHITELIST is non-empty and every entry is present after init.
      (b) handle_access_revoke_command returns an ACCESS_DENIED response when
          a default administrator id is passed, and the user stays in the
          whitelist.
    """
    import asyncio
    from unittest.mock import Mock

    from gateway.access_control import handle_access_revoke_command

    mgr = isolated_manager

    # (a) Defaults exist and are in the whitelist post-init.
    assert len(DEFAULT_WHITELIST) >= 1
    for default_user in DEFAULT_WHITELIST:
        assert default_user in mgr.whitelist

    # (b) Try to revoke a default user via the handler; must be refused,
    # and the user must remain in the whitelist.
    default_user = next(iter(DEFAULT_WHITELIST))

    # The handler pulls the singleton via get_access_manager(); patch it so
    # our isolated manager is what the handler sees.
    with mock.patch("gateway.access_control.get_access_manager", return_value=mgr):
        # Requester must itself be in DEFAULT_WHITELIST so we get past the
        # admin gate and reach the default-protection check.
        requester = next(iter(DEFAULT_WHITELIST))
        event = Mock()
        event.user_id = requester
        event.chat_id = requester

        response = asyncio.run(
            handle_access_revoke_command(gateway_runner=None, event=event, user_id=default_user)
        )

    # Response is an emoji-formatted error string mentioning the default user
    # (the handler renders ErrorResponse.to_emoji_response() which is a str).
    assert isinstance(response, str)
    assert default_user in response
    # And the default user is still in the whitelist.
    assert default_user in mgr.whitelist
