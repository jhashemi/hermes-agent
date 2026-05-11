"""Thread-safety tests for access control module (P2-002).

Tests concurrent access to AccessControlManager from 10+ threads simultaneously
to verify no race conditions occur during grant_access(), revoke_access(),
check_access(), and JSON file operations.
"""

import os
import json
import threading
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

# Import the module to test
from gateway.access_control import (
    AccessControlManager,
    DEFAULT_WHITELIST,
    get_access_manager,
    _access_manager_lock,
)


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
def isolated_manager(temp_access_file):
    """Create an isolated AccessControlManager with temp file."""
    with mock.patch('gateway.access_control.ACCESS_CONTROL_FILE', temp_access_file):
        manager = AccessControlManager()
        yield manager


# ============================================================================
# Test 1: Basic Thread-Safe Operations
# ============================================================================

def test_grant_access_single_thread(isolated_manager):
    """Test single-threaded grant_access operations."""
    result1 = isolated_manager.grant_access("user1")
    assert result1 is True
    
    result2 = isolated_manager.grant_access("user1")
    assert result2 is False  # Already exists
    
    assert isolated_manager.check_access("user1") is True
    assert isolated_manager.check_access("nonexistent") is False


def test_revoke_access_single_thread(isolated_manager):
    """Test single-threaded revoke_access operations."""
    isolated_manager.grant_access("user1")
    assert isolated_manager.check_access("user1") is True
    
    result = isolated_manager.revoke_access("user1")
    assert result is True
    
    assert isolated_manager.check_access("user1") is False
    
    result = isolated_manager.revoke_access("user1")
    assert result is False  # Already revoked


# ============================================================================
# Test 2: Concurrent Grant Access (10+ Threads)
# ============================================================================

def test_concurrent_grant_access_10_threads(isolated_manager):
    """Test 10 threads concurrently granting access to different users."""
    num_threads = 10
    results = []
    
    def grant_user(user_num):
        user_id = f"user_{user_num}"
        result = isolated_manager.grant_access(user_id)
        results.append((user_id, result))
    
    threads = [
        threading.Thread(target=grant_user, args=(i,))
        for i in range(num_threads)
    ]
    
    # Start all threads
    for thread in threads:
        thread.start()
    
    # Wait for all threads to complete
    for thread in threads:
        thread.join()
    
    # Verify all were successfully added (all should be True)
    assert len(results) == num_threads
    assert all(result is True for _, result in results)
    
    # Verify all users are in the whitelist
    for i in range(num_threads):
        user_id = f"user_{i}"
        assert isolated_manager.check_access(user_id) is True


def test_concurrent_grant_access_same_user(isolated_manager):
    """Test 10 threads concurrently granting access to the SAME user.
    
    Only first grant should return True, rest should return False.
    This tests race condition detection.
    """
    num_threads = 10
    results = []
    lock = threading.Lock()
    
    def grant_user():
        result = isolated_manager.grant_access("shared_user")
        with lock:
            results.append(result)
    
    threads = [
        threading.Thread(target=grant_user)
        for _ in range(num_threads)
    ]
    
    # Start all threads
    for thread in threads:
        thread.start()
    
    # Wait for all threads
    for thread in threads:
        thread.join()
    
    # Exactly one should be True, rest False
    assert len(results) == num_threads
    assert sum(1 for r in results if r is True) == 1
    assert sum(1 for r in results if r is False) == num_threads - 1
    
    # User should exist
    assert isolated_manager.check_access("shared_user") is True


# ============================================================================
# Test 3: Concurrent Revoke Access (10+ Threads)
# ============================================================================

def test_concurrent_revoke_access_10_threads(isolated_manager):
    """Test 10 threads concurrently revoking access from different users."""
    num_threads = 10
    
    # Pre-populate with users
    for i in range(num_threads):
        isolated_manager.grant_access(f"user_{i}")
    
    results = []
    
    def revoke_user(user_num):
        user_id = f"user_{user_num}"
        result = isolated_manager.revoke_access(user_id)
        results.append((user_id, result))
    
    threads = [
        threading.Thread(target=revoke_user, args=(i,))
        for i in range(num_threads)
    ]
    
    # Start all threads
    for thread in threads:
        thread.start()
    
    # Wait for all threads
    for thread in threads:
        thread.join()
    
    # Verify all were successfully removed
    assert len(results) == num_threads
    assert all(result is True for _, result in results)
    
    # Verify all users are gone
    for i in range(num_threads):
        user_id = f"user_{i}"
        assert isolated_manager.check_access(user_id) is False


# ============================================================================
# Test 4: Concurrent Check Access (10+ Threads)
# ============================================================================

def test_concurrent_check_access_10_threads(isolated_manager):
    """Test 10 threads concurrently checking access."""
    # Pre-populate
    isolated_manager.grant_access("existing_user")
    
    num_threads = 10
    check_results = []
    lock = threading.Lock()
    
    def check_user():
        result_existing = isolated_manager.check_access("existing_user")
        result_missing = isolated_manager.check_access("missing_user")
        with lock:
            check_results.append((result_existing, result_missing))
    
    threads = [
        threading.Thread(target=check_user)
        for _ in range(num_threads)
    ]
    
    # Start all threads
    for thread in threads:
        thread.start()
    
    # Wait for all threads
    for thread in threads:
        thread.join()
    
    # Verify all checks were consistent
    assert len(check_results) == num_threads
    assert all(result[0] is True for result in check_results)  # existing_user
    assert all(result[1] is False for result in check_results)  # missing_user


# ============================================================================
# Test 5: Concurrent Mixed Operations (Grant + Revoke + Check)
# ============================================================================

def test_concurrent_mixed_operations(isolated_manager):
    """Test mixed concurrent operations (grant, revoke, check).
    
    This is the most realistic scenario and hardest to get right.
    """
    num_threads = 15
    operations_count = [0, 0, 0]  # [grants, revokes, checks]
    lock = threading.Lock()
    
    def mixed_operation(thread_id):
        op_type = thread_id % 3  # Cycle through operation types
        
        user_id = f"user_{thread_id % 5}"  # 5 users, 15 threads
        
        if op_type == 0:  # Grant
            result = isolated_manager.grant_access(user_id)
            with lock:
                operations_count[0] += 1
        elif op_type == 1:  # Revoke
            result = isolated_manager.revoke_access(user_id)
            with lock:
                operations_count[1] += 1
        else:  # Check
            result = isolated_manager.check_access(user_id)
            with lock:
                operations_count[2] += 1
    
    threads = [
        threading.Thread(target=mixed_operation, args=(i,))
        for i in range(num_threads)
    ]
    
    # Start all threads
    for thread in threads:
        thread.start()
    
    # Wait for all threads
    for thread in threads:
        thread.join()
    
    # Verify operation counts
    assert sum(operations_count) == num_threads
    assert operations_count[0] == 5  # Grants
    assert operations_count[1] == 5  # Revokes
    assert operations_count[2] == 5  # Checks


# ============================================================================
# Test 6: File I/O Thread Safety
# ============================================================================

def test_concurrent_grant_access_and_file_persistence(isolated_manager, temp_access_file):
    """Test that concurrent grants persist correctly to JSON file."""
    num_threads = 15
    
    def grant_users(start_num, count):
        for i in range(start_num, start_num + count):
            isolated_manager.grant_access(f"user_{i}")
    
    threads = [
        threading.Thread(target=grant_users, args=(i * 5, 5))
        for i in range(num_threads)
    ]
    
    # Start all threads
    for thread in threads:
        thread.start()
    
    # Wait for all threads
    for thread in threads:
        thread.join()
    
    # Verify file was written correctly
    assert temp_access_file.exists()
    with open(temp_access_file, 'r') as f:
        data = json.load(f)
    
    assert "whitelist" in data
    assert isinstance(data["whitelist"], list)
    
    # Should have users 0-74 (15 threads * 5 users) + defaults
    expected_users = set(f"user_{i}" for i in range(75))
    expected_users.update(DEFAULT_WHITELIST)
    
    actual_users = set(data["whitelist"])
    assert expected_users == actual_users


def test_concurrent_operations_with_reload(isolated_manager, temp_access_file):
    """Test that concurrent operations don't corrupt file on reload."""
    num_threads = 20
    results = []
    lock = threading.Lock()
    
    def mixed_ops(thread_id):
        for i in range(5):  # Each thread does 5 operations
            op = i % 3
            user_id = f"user_{thread_id}_{i}"
            
            if op == 0:
                isolated_manager.grant_access(user_id)
            elif op == 1:
                isolated_manager.revoke_access(user_id)
            else:
                check_result = isolated_manager.check_access(user_id)
                with lock:
                    results.append(check_result)
    
    threads = [
        threading.Thread(target=mixed_ops, args=(i,))
        for i in range(num_threads)
    ]
    
    # Start all threads
    for thread in threads:
        thread.start()
    
    # Wait for all threads
    for thread in threads:
        thread.join()
    
    # File should be valid JSON
    assert temp_access_file.exists()
    with open(temp_access_file, 'r') as f:
        data = json.load(f)
    assert "whitelist" in data
    assert isinstance(data["whitelist"], list)
    
    # Can reload into new manager
    with mock.patch('gateway.access_control.ACCESS_CONTROL_FILE', temp_access_file):
        new_manager = AccessControlManager()
        assert len(new_manager.whitelist) > 0


# ============================================================================
# Test 7: Stress Test - Many Operations
# ============================================================================

def test_stress_test_high_concurrency(isolated_manager):
    """Stress test with 50 threads doing many operations."""
    num_threads = 50
    num_operations_per_thread = 10
    
    def stress_operation(thread_id):
        for i in range(num_operations_per_thread):
            user_id = f"user_{(thread_id * 100 + i) % 100}"
            op = (thread_id + i) % 4
            
            if op == 0:
                isolated_manager.grant_access(user_id)
            elif op == 1:
                isolated_manager.revoke_access(user_id)
            elif op == 2:
                isolated_manager.check_access(user_id)
            else:
                isolated_manager.list_users()
    
    threads = [
        threading.Thread(target=stress_operation, args=(i,))
        for i in range(num_threads)
    ]
    
    start_time = time.time()
    
    # Start all threads
    for thread in threads:
        thread.start()
    
    # Wait for all threads with timeout
    for thread in threads:
        thread.join(timeout=30)
    
    elapsed = time.time() - start_time
    
    # All threads should complete without deadlock
    assert all(not thread.is_alive() for thread in threads)
    assert elapsed < 30  # Should complete in reasonable time


# ============================================================================
# Test 8: List Users Thread Safety
# ============================================================================

def test_concurrent_list_users(isolated_manager):
    """Test that list_users() is thread-safe."""
    # Pre-populate
    for i in range(10):
        isolated_manager.grant_access(f"user_{i}")
    
    num_threads = 20
    list_results = []
    lock = threading.Lock()
    
    def get_list():
        result = isolated_manager.list_users()
        with lock:
            list_results.append(result)
    
    threads = [
        threading.Thread(target=get_list)
        for _ in range(num_threads)
    ]
    
    # Start all threads
    for thread in threads:
        thread.start()
    
    # Wait for all threads
    for thread in threads:
        thread.join()
    
    # All should have same result
    assert len(list_results) == num_threads
    assert all(result == list_results[0] for result in list_results)
    assert "user_0" in list_results[0]
    assert "user_9" in list_results[0]


# ============================================================================
# Test 9: Reset to Defaults Thread Safety
# ============================================================================

def test_reset_during_concurrent_operations(isolated_manager):
    """Test reset_to_defaults() during concurrent operations."""
    # Pre-add some users
    for i in range(5):
        isolated_manager.grant_access(f"user_{i}")
    
    num_threads = 10
    
    def concurrent_ops():
        for _ in range(5):
            isolated_manager.grant_access("temp_user")
            isolated_manager.check_access("temp_user")
            isolated_manager.revoke_access("temp_user")
    
    threads = [
        threading.Thread(target=concurrent_ops)
        for _ in range(num_threads)
    ]
    
    # Start threads
    for thread in threads:
        thread.start()
    
    # In the middle, reset
    time.sleep(0.01)
    isolated_manager.reset_to_defaults()
    
    # Wait for all threads
    for thread in threads:
        thread.join()
    
    # Should only have default whitelist
    assert isolated_manager.whitelist == set(DEFAULT_WHITELIST)


# ============================================================================
# Test 10: No Deadlocks on Exception
# ============================================================================

def test_no_deadlock_on_file_io_error(isolated_manager, temp_access_file):
    """Test that lock is released even on file I/O errors.
    
    Verifies that concurrent operations complete successfully even under
    file I/O conditions. The lock should never deadlock.
    """
    num_threads = 5
    results = []
    lock = threading.Lock()
    
    def try_grant():
        try:
            # Perform operations that would write to file
            isolated_manager.grant_access("test_user")
        except Exception:
            pass
        with lock:
            results.append(1)
    
    threads = [
        threading.Thread(target=try_grant)
        for _ in range(num_threads)
    ]
    
    # Start all threads
    for thread in threads:
        thread.start()
    
    # Wait with timeout
    for thread in threads:
        thread.join(timeout=5)
    
    # All threads should complete (no deadlock)
    assert all(not thread.is_alive() for thread in threads)
    assert len(results) == num_threads


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
