"""
P4-002: Comprehensive Unit Tests for AccessControl

Tests the access control manager for:
- grant_access(), revoke_access(), check_access()
- JSON persistence (file I/O)
- Thread-safety of whitelist
- Audit logging

Minimum 20 test cases, all passing.
"""

import pytest
import threading
import time
import json
import tempfile
import os
import uuid
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from gateway.access_control import (
    AccessControlManager,
    get_access_manager,
    validate_user_id,
    DEFAULT_WHITELIST,
    MAX_USER_ID_LENGTH,
)
from gateway.platforms.base import MessageEvent


# ============================================================================
# Test Fixtures
# ============================================================================

@pytest.fixture
def temp_access_file():
    """Create a temporary access control file for testing."""
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
        temp_path = f.name
    yield temp_path
    if os.path.exists(temp_path):
        os.unlink(temp_path)


@pytest.fixture
def temp_audit_file():
    """Create a temporary audit log file for testing."""
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log') as f:
        temp_path = f.name
    yield temp_path
    if os.path.exists(temp_path):
        os.unlink(temp_path)


@pytest.fixture
def access_manager():
    """Create a fresh AccessControlManager for each test."""
    return AccessControlManager()


@pytest.fixture
def mock_message_event():
    """Create a mock MessageEvent."""
    event = Mock(spec=MessageEvent)
    event.user_id = "test_user"
    event.chat_id = "test_chat"
    return event


# ============================================================================
# Test: User ID validation
# ============================================================================

def test_validate_user_id_valid():
    """Test validation of valid user IDs."""
    valid_ids = ["user1", "test_user", "alice_123", "u", "a_b_c"]
    for uid in valid_ids:
        is_valid, error = validate_user_id(uid)
        assert is_valid, f"Should accept {uid}: {error}"


def test_validate_user_id_empty():
    """Test validation of empty user ID."""
    is_valid, error = validate_user_id("")
    assert not is_valid
    assert error is not None


def test_validate_user_id_not_string():
    """Test validation of non-string user ID."""
    is_valid, error = validate_user_id(123)
    assert not is_valid
    assert error is not None


def test_validate_user_id_too_long():
    """Test validation of user ID exceeding max length."""
    long_id = "x" * (MAX_USER_ID_LENGTH + 1)
    is_valid, error = validate_user_id(long_id)
    assert not is_valid
    assert "exceeds maximum" in error


def test_validate_user_id_max_length():
    """Test validation of user ID at max length."""
    max_id = "x" * MAX_USER_ID_LENGTH
    is_valid, error = validate_user_id(max_id)
    assert is_valid


def test_validate_user_id_invalid_characters():
    """Test validation rejects special characters."""
    invalid_ids = ["user@email", "user-name", "user.name", "user name", "user/name"]
    for uid in invalid_ids:
        is_valid, error = validate_user_id(uid)
        assert not is_valid, f"Should reject {uid}"


def test_validate_user_id_none():
    """Test validation of None."""
    is_valid, error = validate_user_id(None)
    assert not is_valid


# ============================================================================
# Test: Basic grant_access functionality
# ============================================================================

def test_grant_access_new_user():
    """Test granting access to a new user."""
    mgr = AccessControlManager()
    unique_user = f"new_user_{uuid.uuid4().hex[:8]}"
    result = mgr.grant_access(unique_user)
    assert result is True
    assert unique_user in mgr.whitelist


def test_grant_access_existing_user():
    """Test granting access to user who already has access."""
    mgr = AccessControlManager()
    # User already in defaults
    result = mgr.grant_access("taylor_swanson")
    assert result is False  # Already had access


def test_grant_access_with_grantor():
    """Test granting access with audit trail."""
    mgr = AccessControlManager()
    unique_user = f"new_user_{uuid.uuid4().hex[:8]}"
    result = mgr.grant_access(unique_user, grantor_id="admin")
    assert result is True
    assert unique_user in mgr.whitelist


# ============================================================================
# Test: Basic revoke_access functionality
# ============================================================================

def test_revoke_access_existing_user():
    """Test revoking access from a user with access."""
    mgr = AccessControlManager()
    # Ensure user has access
    mgr.grant_access("user_to_revoke")
    
    result = mgr.revoke_access("user_to_revoke")
    assert result is True
    assert "user_to_revoke" not in mgr.whitelist


def test_revoke_access_nonexistent_user():
    """Test revoking access from user without access."""
    mgr = AccessControlManager()
    result = mgr.revoke_access("unknown_user")
    assert result is False


def test_revoke_access_default_user():
    """Test revoking access from default user."""
    mgr = AccessControlManager()
    result = mgr.revoke_access("taylor_swanson")
    assert result is True
    assert "taylor_swanson" not in mgr.whitelist


# ============================================================================
# Test: check_access functionality
# ============================================================================

def test_check_access_granted():
    """Test checking access for user with access."""
    mgr = AccessControlManager()
    # One of the default users should exist
    default_user = list(DEFAULT_WHITELIST)[0]
    result = mgr.check_access(default_user)
    assert result is True


def test_check_access_denied():
    """Test checking access for user without access."""
    mgr = AccessControlManager()
    result = mgr.check_access("unknown_user")
    assert result is False


def test_check_access_after_grant():
    """Test checking access after granting."""
    mgr = AccessControlManager()
    mgr.grant_access("new_user")
    result = mgr.check_access("new_user")
    assert result is True


def test_check_access_after_revoke():
    """Test checking access after revoking."""
    mgr = AccessControlManager()
    mgr.grant_access("temp_user")
    mgr.revoke_access("temp_user")
    result = mgr.check_access("temp_user")
    assert result is False


# ============================================================================
# Test: has_access with MessageEvent
# ============================================================================

def test_has_access_with_message_event_user_id():
    """Test checking access with MessageEvent via user_id."""
    mgr = AccessControlManager()
    event = Mock(spec=MessageEvent)
    # Use a default user that should exist
    default_user = list(DEFAULT_WHITELIST)[0]
    event.user_id = default_user
    event.chat_id = "some_chat"
    
    result = mgr.has_access(event)
    assert result is True


def test_has_access_with_message_event_no_user_id():
    """Test checking access with MessageEvent via chat_id."""
    mgr = AccessControlManager()
    event = Mock(spec=MessageEvent)
    # Use a default user ID as chat_id
    default_user = list(DEFAULT_WHITELIST)[0]
    event.user_id = None
    event.chat_id = default_user
    
    result = mgr.has_access(event)
    assert result is True


def test_has_access_with_message_event_unknown():
    """Test checking access for unknown user in event."""
    mgr = AccessControlManager()
    event = Mock(spec=MessageEvent)
    event.user_id = "unknown_user"
    event.chat_id = "unknown_chat"
    
    result = mgr.has_access(event)
    assert result is False

# ============================================================================
# P1-RCA: Telegram numeric ID access denied
# Tests that Telegram users (identified by numeric ID) can access
# restricted commands when their numeric ID is in the whitelist.
# ============================================================================

def test_telegram_numeric_id_has_access():
    """Test that a Telegram user identified by numeric ID has access
    when that numeric ID is in the whitelist.

    RCA: get_user_id() for Telegram returns str(numeric_id) like '445462521',
    but DEFAULT_WHITELIST contains username strings like 'taylor_swanson'.
    The types never match, so all Telegram users get Access Denied.
    """
    mgr = AccessControlManager()
    # Simulate Telegram: source.user_id is a numeric string
    mgr.grant_access("445462521")  # Telegram numeric user ID

    event = Mock(spec=MessageEvent)
    source = Mock()
    source.user_id = "445462521"
    source.chat_id = "445462521"
    event.source = source
    event.user_id = None  # Telegram uses source.user_id, not event.user_id
    event.chat_id = None

    result = mgr.has_access(event)
    assert result is True, "Telegram user with numeric ID in whitelist should have access"


def test_telegram_numeric_id_denied_when_not_in_whitelist():
    """Test that a Telegram user NOT in the whitelist is denied access."""
    mgr = AccessControlManager()

    event = Mock(spec=MessageEvent)
    source = Mock()
    source.user_id = "999999999"  # Not in whitelist
    source.chat_id = "999999999"
    event.source = source
    event.user_id = None
    event.chat_id = None

    result = mgr.has_access(event)
    assert result is False, "Telegram user not in whitelist should be denied"


def test_whatsoever_numeric_id_in_default_whitelist():
    """Test that a numeric ID added to DEFAULT_WHITELIST is recognized.

    This is the REGRESSION GUARD: after the fix, if we add Telegram user IDs
    to the default whitelist, has_access must work for those users.
    """
    # Add to DEFAULT_WHITELIST temporarily for test
    from gateway.access_control import DEFAULT_WHITELIST
    original = set(DEFAULT_WHITELIST)
    try:
        DEFAULT_WHITELIST.add("445462521")
        mgr = AccessControlManager()  # Reloads from DEFAULT_WHITELIST

        event = Mock(spec=MessageEvent)
        source = Mock()
        source.user_id = "445462521"
        source.chat_id = "445462521"
        event.source = source
        event.user_id = None
        event.chat_id = None

        result = mgr.has_access(event)
        assert result is True, "Numeric ID in DEFAULT_WHITELIST should grant access"
    finally:
        DEFAULT_WHITELIST.clear()
        DEFAULT_WHITELIST.update(original)


# ============================================================================
# Test: get_user_id from MessageEvent
# ============================================================================

def test_get_user_id_prefers_user_id():
    """Test that get_user_id prefers user_id over chat_id."""
    mgr = AccessControlManager()
    event = Mock(spec=MessageEvent)
    event.user_id = "alice"
    event.chat_id = "chat_123"
    
    user_id = mgr.get_user_id(event)
    assert user_id == "alice"


def test_get_user_id_fallback_to_chat_id():
    """Test that get_user_id falls back to chat_id."""
    mgr = AccessControlManager()
    event = Mock(spec=MessageEvent)
    event.user_id = None
    event.chat_id = "chat_123"
    
    user_id = mgr.get_user_id(event)
    assert user_id == "chat_123"


def test_get_user_id_fallback_to_unknown():
    """Test that get_user_id falls back to 'unknown_user'."""
    mgr = AccessControlManager()
    event = Mock(spec=MessageEvent)
    event.user_id = None
    event.chat_id = None
    
    user_id = mgr.get_user_id(event)
    assert user_id == "unknown_user"


# ============================================================================
# Test: JSON persistence
# ============================================================================

def test_save_to_file():
    """Test persisting whitelist to JSON file."""
    mgr = AccessControlManager()
    mgr.grant_access("new_user")
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
        temp_path = f.name
    
    try:
        with patch('gateway.access_control.ACCESS_CONTROL_FILE', Path(temp_path)):
            mgr._save_to_file()
        
        # Verify file exists and is valid JSON
        data = json.loads(Path(temp_path).read_text())
        assert "whitelist" in data
        assert isinstance(data["whitelist"], list)
        assert "new_user" in data["whitelist"]
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def test_load_from_file():
    """Test loading whitelist from JSON file."""
    # Create a temporary JSON file with test data
    test_data = {
        "whitelist": ["user1", "user2", "user3"],
        "description": "Test access list"
    }
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
        json.dump(test_data, f)
        temp_path = f.name
    
    try:
        with patch('gateway.access_control.ACCESS_CONTROL_FILE', Path(temp_path)):
            mgr = AccessControlManager()
        
        assert "user1" in mgr.whitelist
        assert "user2" in mgr.whitelist
        assert "user3" in mgr.whitelist
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def test_load_from_missing_file_uses_defaults():
    """Test that missing file falls back to defaults."""
    nonexistent_path = Path("/tmp/nonexistent_access_control_file.json")
    
    with patch('gateway.access_control.ACCESS_CONTROL_FILE', nonexistent_path):
        mgr = AccessControlManager()
    
    # Should have default whitelist
    for user in DEFAULT_WHITELIST:
        assert user in mgr.whitelist


def test_load_from_corrupted_file_uses_defaults():
    """Test that corrupted JSON falls back to defaults."""
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
        f.write("{ invalid json }")
        temp_path = f.name
    
    try:
        with patch('gateway.access_control.ACCESS_CONTROL_FILE', Path(temp_path)):
            mgr = AccessControlManager()
        
        # Should have default whitelist
        for user in DEFAULT_WHITELIST:
            assert user in mgr.whitelist
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


# ============================================================================
# Test: Thread-safety
# ============================================================================

def test_thread_safety_concurrent_grants():
    """Test concurrent grant operations are thread-safe."""
    mgr = AccessControlManager()
    errors = []
    results = []
    
    def grant_user(user_id):
        try:
            result = mgr.grant_access(user_id)
            results.append((user_id, result))
        except Exception as e:
            errors.append(e)
    
    threads = []
    for i in range(50):
        user_id = f"user_{i}"
        t = threading.Thread(target=grant_user, args=(user_id,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    assert len(errors) == 0, f"Errors occurred: {errors}"
    assert len(results) == 50
    
    # Verify all users were added
    for i in range(50):
        assert mgr.check_access(f"user_{i}")


def test_thread_safety_concurrent_revokes():
    """Test concurrent revoke operations are thread-safe."""
    mgr = AccessControlManager()
    
    # First grant all users
    for i in range(50):
        mgr.grant_access(f"user_{i}")
    
    errors = []
    results = []
    
    def revoke_user(user_id):
        try:
            result = mgr.revoke_access(user_id)
            results.append((user_id, result))
        except Exception as e:
            errors.append(e)
    
    threads = []
    for i in range(50):
        user_id = f"user_{i}"
        t = threading.Thread(target=revoke_user, args=(user_id,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    assert len(errors) == 0
    assert len(results) == 50
    
    # Verify all users were removed
    for i in range(50):
        assert not mgr.check_access(f"user_{i}")


def test_thread_safety_mixed_operations():
    """Test mixed grant/revoke operations are thread-safe."""
    mgr = AccessControlManager()
    errors = []
    
    def mixed_operations(thread_id):
        try:
            for i in range(20):
                user_id = f"user_{thread_id}_{i}"
                mgr.grant_access(user_id)
                if i % 2 == 0:
                    mgr.revoke_access(user_id)
        except Exception as e:
            errors.append(e)
    
    threads = []
    for t_id in range(10):
        t = threading.Thread(target=mixed_operations, args=(t_id,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    assert len(errors) == 0


def test_thread_safety_concurrent_checks():
    """Test concurrent access checks are thread-safe."""
    mgr = AccessControlManager()
    
    # Grant some users
    for i in range(100):
        if i % 2 == 0:
            mgr.grant_access(f"user_{i}")
    
    errors = []
    results = []
    
    def check_access(user_id):
        try:
            result = mgr.check_access(user_id)
            results.append((user_id, result))
        except Exception as e:
            errors.append(e)
    
    threads = []
    for i in range(100):
        user_id = f"user_{i}"
        t = threading.Thread(target=check_access, args=(user_id,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    assert len(errors) == 0
    assert len(results) == 100


def test_thread_safety_concurrent_file_operations():
    """Test concurrent file I/O is thread-safe."""
    mgr = AccessControlManager()
    errors = []
    
    def update_whitelist(user_id):
        try:
            if user_id.endswith("_grant"):
                mgr.grant_access(user_id)
            else:
                mgr.revoke_access(user_id)
        except Exception as e:
            errors.append(e)
    
    threads = []
    for i in range(50):
        suffix = "_grant" if i % 2 == 0 else "_revoke"
        user_id = f"user_{i}{suffix}"
        t = threading.Thread(target=update_whitelist, args=(user_id,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    assert len(errors) == 0


# ============================================================================
# Test: Audit logging
# ============================================================================

def test_audit_log_grant():
    """Test audit logging for grant operations."""
    mgr = AccessControlManager()
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log') as f:
        temp_path = f.name
    
    try:
        with patch('gateway.access_control.AUDIT_LOG_FILE', Path(temp_path)):
            mgr.audit_log("test_user", "grant", "admin")
        
        # Verify audit log entry
        log_content = Path(temp_path).read_text()
        log_entry = json.loads(log_content.strip())
        assert log_entry["user_id"] == "test_user"
        assert log_entry["action"] == "grant"
        assert log_entry["grantor_id"] == "admin"
        assert "timestamp" in log_entry
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def test_audit_log_revoke():
    """Test audit logging for revoke operations."""
    mgr = AccessControlManager()
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log') as f:
        temp_path = f.name
    
    try:
        with patch('gateway.access_control.AUDIT_LOG_FILE', Path(temp_path)):
            mgr.audit_log("test_user", "revoke", "admin")
        
        log_content = Path(temp_path).read_text()
        log_entry = json.loads(log_content.strip())
        assert log_entry["action"] == "revoke"
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def test_audit_log_multiple_entries():
    """Test that multiple audit log entries are appended."""
    mgr = AccessControlManager()
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log') as f:
        temp_path = f.name
    
    try:
        with patch('gateway.access_control.AUDIT_LOG_FILE', Path(temp_path)):
            mgr.audit_log("user1", "grant", "admin")
            mgr.audit_log("user2", "revoke", "admin")
        
        log_content = Path(temp_path).read_text()
        lines = log_content.strip().split('\n')
        assert len(lines) == 2
        
        entry1 = json.loads(lines[0])
        entry2 = json.loads(lines[1])
        assert entry1["user_id"] == "user1"
        assert entry2["user_id"] == "user2"
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def test_audit_log_invalid_action():
    """Test audit logging handles invalid actions."""
    mgr = AccessControlManager()
    
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log') as f:
        temp_path = f.name
    
    try:
        with patch('gateway.access_control.AUDIT_LOG_FILE', Path(temp_path)):
            mgr.audit_log("test_user", "invalid_action", "admin")
        
        log_content = Path(temp_path).read_text()
        log_entry = json.loads(log_content.strip())
        assert log_entry["action"] == "unknown"
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


# ============================================================================
# Test: list_users formatting
# ============================================================================

def test_list_users_formatting():
    """Test that list_users returns formatted string."""
    mgr = AccessControlManager()
    result = mgr.list_users()
    assert isinstance(result, str)
    assert "Whitelisted Users" in result or "access" in result.lower()


def test_list_users_empty():
    """Test list_users with empty whitelist."""
    mgr = AccessControlManager()
    mgr.reset_to_defaults()
    mgr.whitelist.clear()
    result = mgr.list_users()
    assert "empty" in result.lower() or "no users" in result.lower()


# ============================================================================
# Test: reset_to_defaults
# ============================================================================

def test_reset_to_defaults():
    """Test resetting whitelist to defaults."""
    mgr = AccessControlManager()
    mgr.grant_access("new_user")
    
    mgr.reset_to_defaults()
    
    # Should have default users
    for user in DEFAULT_WHITELIST:
        assert user in mgr.whitelist
    
    # New user should be gone
    assert "new_user" not in mgr.whitelist


# ============================================================================
# Test: Singleton get_access_manager
# ============================================================================

def test_get_access_manager_singleton():
    """Test that get_access_manager returns same instance."""
    mgr1 = get_access_manager()
    mgr2 = get_access_manager()
    assert mgr1 is mgr2


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
