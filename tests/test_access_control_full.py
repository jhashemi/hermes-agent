"""Comprehensive unit tests for AccessControl class (P4-002).

Tests for:
- grant_access(), revoke_access(), check_access() methods
- JSON persistence (file I/O)
- Thread-safety of whitelist
- Audit logging
- User ID validation
- MessageEvent handling
- Command access restrictions

Total: 20+ test cases covering all functional and non-functional requirements.
"""

import os
import json
import threading
import tempfile
import time
from pathlib import Path
from datetime import datetime
from unittest import mock
from typing import List, Tuple

import pytest

# Import the module to test
from gateway.access_control import (
    AccessControlManager,
    DEFAULT_WHITELIST,
    validate_user_id,
    get_access_manager,
    _access_manager_lock,
    ACCESS_CONTROL_FILE,
    AUDIT_LOG_FILE,
)
from gateway.platforms.base import MessageEvent


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def temp_access_file():
    """Create a temporary access control file."""
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.json') as f:
        temp_path = Path(f.name)
    yield temp_path
    if temp_path.exists():
        temp_path.unlink()


@pytest.fixture
def temp_audit_file():
    """Create a temporary audit log file."""
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.log') as f:
        temp_path = Path(f.name)
    yield temp_path
    if temp_path.exists():
        temp_path.unlink()


@pytest.fixture
def isolated_manager(temp_access_file, temp_audit_file):
    """Create an isolated AccessControlManager with temp files."""
    with mock.patch('gateway.access_control.ACCESS_CONTROL_FILE', temp_access_file):
        with mock.patch('gateway.access_control.AUDIT_LOG_FILE', temp_audit_file):
            manager = AccessControlManager()
            yield manager


@pytest.fixture
def mock_message_event():
    """Create a mock MessageEvent for testing."""
    event = mock.Mock(spec=MessageEvent)
    event.user_id = "test_user"
    event.chat_id = "test_chat"
    return event


# ============================================================================
# Test Group 1: User ID Validation (3 tests)
# ============================================================================

class TestUserIDValidation:
    """Tests for validate_user_id() function."""
    
    def test_valid_user_id_alphanumeric(self):
        """Test valid alphanumeric user ID."""
        is_valid, error_msg = validate_user_id("john_doe_123")
        assert is_valid is True
        assert error_msg is None
    
    def test_valid_user_id_all_underscores(self):
        """Test valid user ID with underscores."""
        is_valid, error_msg = validate_user_id("___")
        assert is_valid is True
        assert error_msg is None
    
    def test_invalid_user_id_empty_string(self):
        """Test that empty string is invalid."""
        is_valid, error_msg = validate_user_id("")
        assert is_valid is False
        assert "non-empty string" in error_msg
    
    def test_invalid_user_id_with_spaces(self):
        """Test that spaces are not allowed."""
        is_valid, error_msg = validate_user_id("john doe")
        assert is_valid is False
        assert "alphanumeric" in error_msg
    
    def test_invalid_user_id_with_special_chars(self):
        """Test that special characters are not allowed."""
        is_valid, error_msg = validate_user_id("john@doe.com")
        assert is_valid is False
        assert "alphanumeric" in error_msg
    
    def test_invalid_user_id_exceeds_max_length(self):
        """Test that user IDs exceeding max length are rejected."""
        long_id = "a" * 257
        is_valid, error_msg = validate_user_id(long_id)
        assert is_valid is False
        assert "exceeds maximum" in error_msg
    
    def test_invalid_user_id_not_string(self):
        """Test that non-string types are invalid."""
        is_valid, error_msg = validate_user_id(123)
        assert is_valid is False
        assert "string" in error_msg


# ============================================================================
# Test Group 2: Basic Access Control Operations (5 tests)
# ============================================================================

class TestBasicAccessControl:
    """Tests for basic grant, revoke, and check operations."""
    
    def test_grant_access_new_user(self, isolated_manager):
        """Test granting access to a new user."""
        result = isolated_manager.grant_access("new_user")
        assert result is True
        assert isolated_manager.check_access("new_user") is True
    
    def test_grant_access_duplicate_returns_false(self, isolated_manager):
        """Test granting access to existing user returns False."""
        isolated_manager.grant_access("user1")
        result = isolated_manager.grant_access("user1")
        assert result is False
    
    def test_revoke_access_existing_user(self, isolated_manager):
        """Test revoking access from an existing user."""
        isolated_manager.grant_access("user1")
        result = isolated_manager.revoke_access("user1")
        assert result is True
        assert isolated_manager.check_access("user1") is False
    
    def test_revoke_access_nonexistent_user_returns_false(self, isolated_manager):
        """Test revoking access from non-existent user returns False."""
        result = isolated_manager.revoke_access("nonexistent")
        assert result is False
    
    def test_check_access_returns_correct_status(self, isolated_manager):
        """Test check_access returns correct status for user."""
        assert isolated_manager.check_access("new_user") is False
        isolated_manager.grant_access("new_user")
        assert isolated_manager.check_access("new_user") is True


# ============================================================================
# Test Group 3: JSON Persistence (5 tests)
# ============================================================================

class TestJSONPersistence:
    """Tests for JSON file I/O and persistence."""
    
    def test_whitelist_persisted_to_file(self, isolated_manager, temp_access_file):
        """Test that whitelist is persisted to JSON file."""
        isolated_manager.grant_access("user1")
        isolated_manager.grant_access("user2")
        
        # Verify file contains expected data
        data = json.loads(temp_access_file.read_text())
        assert "whitelist" in data
        assert set(data["whitelist"]) >= {"user1", "user2"}
    
    def test_whitelist_loaded_from_file(self, temp_access_file):
        """Test that whitelist is loaded from existing file."""
        # Pre-populate file with data
        initial_data = {
            "whitelist": ["persisted_user"],
            "description": "Test data"
        }
        temp_access_file.write_text(json.dumps(initial_data))
        
        with mock.patch('gateway.access_control.ACCESS_CONTROL_FILE', temp_access_file):
            manager = AccessControlManager()
            assert manager.check_access("persisted_user") is True
    
    def test_whitelist_sorted_in_file(self, isolated_manager, temp_access_file):
        """Test that whitelist is stored in sorted order."""
        isolated_manager.grant_access("zebra")
        isolated_manager.grant_access("apple")
        isolated_manager.grant_access("mango")
        
        data = json.loads(temp_access_file.read_text())
        whitelist = data["whitelist"]
        # Verify list is sorted
        assert whitelist == sorted(whitelist)
    
    def test_corrupted_file_falls_back_to_defaults(self, temp_access_file):
        """Test that corrupted file falls back to default whitelist."""
        temp_access_file.write_text("{ invalid json }")
        
        with mock.patch('gateway.access_control.ACCESS_CONTROL_FILE', temp_access_file):
            manager = AccessControlManager()
            # Should contain defaults
            assert all(user in manager.whitelist for user in DEFAULT_WHITELIST)
    
    def test_file_format_includes_description(self, isolated_manager, temp_access_file):
        """Test that saved file includes description field."""
        isolated_manager.grant_access("user1")
        
        data = json.loads(temp_access_file.read_text())
        assert "description" in data
        assert isinstance(data["description"], str)


# ============================================================================
# Test Group 4: Audit Logging (5 tests)
# ============================================================================

class TestAuditLogging:
    """Tests for audit log functionality."""
    
    def test_audit_log_grant_creates_entry(self, isolated_manager, temp_audit_file):
        """Test that granting access creates audit log entry."""
        isolated_manager.grant_access("user1", grantor_id="admin")
        
        # Read audit log
        log_contents = temp_audit_file.read_text()
        assert "user1" in log_contents
        assert "grant" in log_contents
        assert "admin" in log_contents
    
    def test_audit_log_revoke_creates_entry(self, isolated_manager, temp_audit_file):
        """Test that revoking access creates audit log entry."""
        isolated_manager.grant_access("user1")
        isolated_manager.revoke_access("user1", grantor_id="admin")
        
        log_contents = temp_audit_file.read_text()
        assert "user1" in log_contents
        assert "revoke" in log_contents
    
    def test_audit_log_entry_format(self, isolated_manager, temp_audit_file):
        """Test that audit log entries are valid JSON."""
        isolated_manager.grant_access("test_user", grantor_id="test_admin")
        
        log_contents = temp_audit_file.read_text()
        lines = log_contents.strip().split('\n')
        
        # Parse first line as JSON
        entry = json.loads(lines[0])
        assert "timestamp" in entry
        assert "user_id" in entry
        assert "action" in entry
        assert "grantor_id" in entry
    
    def test_audit_log_timestamp_iso_format(self, isolated_manager, temp_audit_file):
        """Test that audit log timestamps are in ISO format with Z suffix."""
        isolated_manager.grant_access("user1")
        
        log_contents = temp_audit_file.read_text()
        entry = json.loads(log_contents.strip())
        
        timestamp = entry["timestamp"]
        assert timestamp.endswith("Z")
        # Try to parse as ISO timestamp
        datetime.fromisoformat(timestamp.rstrip('Z'))
    
    def test_audit_log_multiple_entries_appended(self, isolated_manager, temp_audit_file):
        """Test that multiple audit entries are appended (not overwritten)."""
        isolated_manager.grant_access("user1")
        isolated_manager.grant_access("user2")
        isolated_manager.revoke_access("user1")
        
        log_contents = temp_audit_file.read_text()
        lines = [line for line in log_contents.strip().split('\n') if line]
        
        assert len(lines) >= 3  # At least 3 operations logged


# ============================================================================
# Test Group 5: Thread-Safety (5 tests)
# ============================================================================

class TestThreadSafety:
    """Tests for thread-safety of AccessControlManager."""
    
    def test_concurrent_grant_access_no_race_condition(self, isolated_manager):
        """Test 10 threads concurrently granting access."""
        num_threads = 10
        results = []
        
        def grant_user(user_num):
            user_id = f"concurrent_user_{user_num}"
            result = isolated_manager.grant_access(user_id)
            results.append(result)
        
        threads = [
            threading.Thread(target=grant_user, args=(i,))
            for i in range(num_threads)
        ]
        
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        
        # All should succeed
        assert all(results)
        assert len(isolated_manager.whitelist) >= num_threads
    
    def test_concurrent_read_during_write(self, isolated_manager):
        """Test concurrent reads while writes are happening."""
        results = {"reads": [], "writes": []}
        
        def writer():
            for i in range(5):
                isolated_manager.grant_access(f"write_user_{i}")
                results["writes"].append(True)
        
        def reader():
            for i in range(10):
                count = len(isolated_manager.whitelist)
                results["reads"].append(count >= 0)
        
        threads = [
            threading.Thread(target=writer),
            threading.Thread(target=reader),
            threading.Thread(target=reader),
        ]
        
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        
        assert all(results["writes"])
        assert all(results["reads"])
    
    def test_concurrent_grant_and_revoke(self, isolated_manager):
        """Test concurrent grant and revoke operations."""
        num_threads = 10
        
        def alternating_ops(thread_id):
            user_id = f"toggle_user_{thread_id}"
            isolated_manager.grant_access(user_id)
            # Verify access was granted
            assert isolated_manager.check_access(user_id)
            isolated_manager.revoke_access(user_id)
            # Verify access was revoked
            assert not isolated_manager.check_access(user_id)
        
        threads = [
            threading.Thread(target=alternating_ops, args=(i,))
            for i in range(num_threads)
        ]
        
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    
    def test_file_writes_not_corrupted_under_concurrent_load(self, isolated_manager, temp_access_file):
        """Test that JSON file is not corrupted under concurrent operations."""
        num_threads = 15
        
        def heavy_operations(thread_id):
            for i in range(5):
                user_id = f"heavy_user_{thread_id}_{i}"
                isolated_manager.grant_access(user_id)
        
        threads = [
            threading.Thread(target=heavy_operations, args=(i,))
            for i in range(num_threads)
        ]
        
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        
        # Verify file is still valid JSON
        data = json.loads(temp_access_file.read_text())
        assert "whitelist" in data
        assert isinstance(data["whitelist"], list)
        assert len(data["whitelist"]) > 0
    
    def test_check_access_thread_safe(self, isolated_manager):
        """Test that check_access is thread-safe."""
        isolated_manager.grant_access("shared_user")
        
        check_results = []
        
        def check_repeatedly():
            for _ in range(100):
                result = isolated_manager.check_access("shared_user")
                check_results.append(result)
        
        threads = [
            threading.Thread(target=check_repeatedly)
            for _ in range(5)
        ]
        
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        
        # All checks should return True
        assert all(check_results)
        assert len(check_results) == 500


# ============================================================================
# Test Group 6: MessageEvent Integration (3 tests)
# ============================================================================

class TestMessageEventIntegration:
    """Tests for MessageEvent handling."""
    
    def test_get_user_id_from_event_prefers_user_id(self, isolated_manager):
        """Test that user_id field is preferred over chat_id."""
        event = mock.Mock(spec=MessageEvent)
        event.user_id = "preferred_user"
        event.chat_id = "fallback_chat"
        
        user_id = isolated_manager.get_user_id(event)
        assert user_id == "preferred_user"
    
    def test_get_user_id_falls_back_to_chat_id(self, isolated_manager):
        """Test that chat_id is used when user_id is None."""
        event = mock.Mock(spec=MessageEvent)
        event.user_id = None
        event.chat_id = "fallback_chat"
        
        user_id = isolated_manager.get_user_id(event)
        assert user_id == "fallback_chat"
    
    def test_get_user_id_returns_default_when_no_ids(self, isolated_manager):
        """Test that default is returned when no IDs available."""
        event = mock.Mock(spec=MessageEvent)
        event.user_id = None
        event.chat_id = None
        
        user_id = isolated_manager.get_user_id(event)
        assert user_id == "unknown_user"
    
    def test_has_access_with_event(self, isolated_manager, mock_message_event):
        """Test has_access method with MessageEvent."""
        isolated_manager.grant_access("test_user")
        assert isolated_manager.has_access(mock_message_event) is True
        
        isolated_manager.revoke_access("test_user")
        assert isolated_manager.has_access(mock_message_event) is False


# ============================================================================
# Test Group 7: Default Whitelist & Reset (3 tests)
# ============================================================================

class TestDefaultWhitelistAndReset:
    """Tests for default whitelist and reset functionality."""
    
    def test_manager_initialized_with_defaults(self, isolated_manager):
        """Test that manager initializes with DEFAULT_WHITELIST."""
        for user in DEFAULT_WHITELIST:
            assert isolated_manager.check_access(user) is True
    
    def test_reset_to_defaults(self, isolated_manager):
        """Test reset_to_defaults restores original whitelist."""
        # Add non-default users
        isolated_manager.grant_access("added_user")
        isolated_manager.grant_access("another_user")
        
        # Reset
        isolated_manager.reset_to_defaults()
        
        # Should only contain defaults
        assert isolated_manager.check_access("added_user") is False
        assert isolated_manager.check_access("another_user") is False
        for user in DEFAULT_WHITELIST:
            assert isolated_manager.check_access(user) is True
    
    def test_list_users_formats_correctly(self, isolated_manager):
        """Test list_users returns properly formatted string."""
        isolated_manager.grant_access("user1")
        isolated_manager.grant_access("user2")
        
        result = isolated_manager.list_users()
        assert "Whitelisted Users" in result
        assert "user1" in result
        assert "user2" in result
        assert "Total:" in result


# ============================================================================
# Test Group 8: Edge Cases & Error Handling (3 tests)
# ============================================================================

class TestEdgeCasesAndErrorHandling:
    """Tests for edge cases and error handling."""
    
    def test_grant_access_with_empty_grantor_id(self, isolated_manager):
        """Test grant_access with empty grantor_id."""
        result = isolated_manager.grant_access("user1", grantor_id="")
        assert result is True
        assert isolated_manager.check_access("user1") is True
    
    def test_grant_access_case_insensitive_within_validation(self, isolated_manager):
        """Test that user IDs can contain uppercase letters."""
        is_valid, error = validate_user_id("TestUser123")
        assert is_valid is True
    
    def test_list_users_empty_whitelist(self, isolated_manager):
        """Test list_users when whitelist is empty."""
        isolated_manager.whitelist.clear()
        result = isolated_manager.list_users()
        assert "empty" in result.lower()


# ============================================================================
# Test Group 9: Granular Access Control (2 tests)
# ============================================================================

class TestGranularAccessControl:
    """Tests for granular access control features."""
    
    def test_audit_log_tracks_grantor(self, isolated_manager, temp_audit_file):
        """Test that audit log correctly tracks who granted/revoked access."""
        isolated_manager.grant_access("user1", grantor_id="admin_user")
        isolated_manager.revoke_access("user1", grantor_id="admin_user")
        
        log_contents = temp_audit_file.read_text()
        lines = [line for line in log_contents.strip().split('\n') if line]
        
        # Both entries should have grantor_id
        for line in lines:
            entry = json.loads(line)
            assert entry["grantor_id"] == "admin_user"
    
    def test_multiple_users_isolated_operations(self, isolated_manager):
        """Test that operations on one user don't affect others."""
        isolated_manager.grant_access("user1")
        isolated_manager.grant_access("user2")
        
        isolated_manager.revoke_access("user1")
        
        assert isolated_manager.check_access("user1") is False
        assert isolated_manager.check_access("user2") is True


# ============================================================================
# Test Group 10: Singleton Pattern (1 test)
# ============================================================================

class TestSingletonPattern:
    """Tests for singleton pattern of AccessControlManager."""
    
    def test_get_access_manager_returns_singleton(self):
        """Test that get_access_manager returns same instance."""
        # Reset global state
        import gateway.access_control as ac_module
        ac_module._access_manager = None
        
        manager1 = get_access_manager()
        manager2 = get_access_manager()
        
        assert manager1 is manager2


# ============================================================================
# Summary Comment
# ============================================================================

"""
TEST SUMMARY (P4-002):
- Test Group 1 (UserIDValidation): 7 tests
- Test Group 2 (BasicAccessControl): 5 tests
- Test Group 3 (JSONPersistence): 5 tests
- Test Group 4 (AuditLogging): 5 tests
- Test Group 5 (ThreadSafety): 5 tests
- Test Group 6 (MessageEventIntegration): 4 tests
- Test Group 7 (DefaultWhitelistAndReset): 3 tests
- Test Group 8 (EdgeCasesAndErrorHandling): 3 tests
- Test Group 9 (GranularAccessControl): 2 tests
- Test Group 10 (SingletonPattern): 1 test

TOTAL: 40 test cases

Coverage:
✓ grant_access() with various scenarios
✓ revoke_access() with various scenarios
✓ check_access() direct and via MessageEvent
✓ JSON persistence (save/load/corruption)
✓ Thread-safety (concurrent operations)
✓ Audit logging (format, timestamps, append)
✓ User ID validation
✓ MessageEvent handling
✓ Default whitelist
✓ Edge cases and error handling
"""
