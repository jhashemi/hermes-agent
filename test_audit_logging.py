#!/usr/bin/env python3
"""Test script for P2-004: User ID Validation + Audit Logging

Tests:
1. User ID validation with various invalid formats
2. Audit logging on grant_access() operations
3. Audit logging on revoke_access() operations
4. Verification that audit log contains all operations with correct fields
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime

# Add gateway to path for imports
sys.path.insert(0, '/home/ubuntu/hermes-agent')

from gateway.access_control import (
    AccessControlManager,
    validate_user_id,
    AUDIT_LOG_FILE,
)


def test_user_id_validation():
    """Test user ID validation logic."""
    print("\n=== Testing User ID Validation ===\n")
    
    test_cases = [
        # (user_id, should_be_valid, description)
        ("john_doe", True, "valid: alphanumeric with underscore"),
        ("user123", True, "valid: alphanumeric"),
        ("USER_456", True, "valid: uppercase with underscore"),
        ("a", True, "valid: single character"),
        ("_leading", True, "valid: leading underscore"),
        ("trailing_", True, "valid: trailing underscore"),
        ("multiple_under_scores", True, "valid: multiple underscores"),
        ("123_456", True, "valid: numbers with underscore"),
        ("user-name", False, "invalid: hyphen not allowed"),
        ("user name", False, "invalid: space not allowed"),
        ("user@domain", False, "invalid: special char not allowed"),
        ("user.name", False, "invalid: dot not allowed"),
        ("user#tag", False, "invalid: hash not allowed"),
        ("", False, "invalid: empty string"),
        ("a" * 257, False, f"invalid: exceeds max length (257 > 256)"),
        ("a" * 256, True, "valid: exactly max length (256)"),
    ]
    
    passed = 0
    failed = 0
    
    for user_id, expected_valid, description in test_cases:
        is_valid, error_msg = validate_user_id(user_id)
        
        if is_valid == expected_valid:
            status = "✓ PASS"
            passed += 1
        else:
            status = "✗ FAIL"
            failed += 1
        
        error_info = f" (error: {error_msg})" if error_msg else ""
        print(f"{status} | {description:50} | {is_valid}{error_info}")
    
    print(f"\nValidation Tests: {passed} passed, {failed} failed")
    return failed == 0


def test_audit_logging():
    """Test audit logging functionality."""
    print("\n=== Testing Audit Logging ===\n")
    
    # Clean up old audit log
    if AUDIT_LOG_FILE.exists():
        AUDIT_LOG_FILE.unlink()
    
    manager = AccessControlManager()
    
    # Perform 10+ operations
    operations = [
        ("user_001", "grant", "admin_user"),
        ("user_002", "grant", "admin_user"),
        ("user_003", "grant", "admin_user"),
        ("user_004", "grant", "admin_user"),
        ("user_005", "grant", "admin_user"),
        ("user_001", "revoke", "admin_user"),
        ("user_002", "revoke", "admin_user"),
        ("user_006", "grant", "admin_user"),
        ("user_007", "grant", "admin_user"),
        ("user_008", "grant", "admin_user"),
        ("user_009", "grant", "admin_user"),
        ("user_010", "grant", "admin_user"),
    ]
    
    print(f"Performing {len(operations)} operations...\n")
    
    for i, (user_id, action, grantor_id) in enumerate(operations, 1):
        if action == "grant":
            manager.grant_access(user_id, grantor_id=grantor_id)
        elif action == "revoke":
            manager.revoke_access(user_id, grantor_id=grantor_id)
        
        print(f"{i:2}. {action:6} access for {user_id:15} by {grantor_id}")
    
    return True


def verify_audit_log():
    """Verify the audit log contains all operations with correct format."""
    print("\n=== Verifying Audit Log ===\n")
    
    if not AUDIT_LOG_FILE.exists():
        print("✗ FAIL: Audit log file does not exist at", AUDIT_LOG_FILE)
        return False
    
    print(f"Audit log file: {AUDIT_LOG_FILE}\n")
    
    try:
        with open(AUDIT_LOG_FILE, 'r') as f:
            lines = f.readlines()
    except Exception as e:
        print(f"✗ FAIL: Could not read audit log: {e}")
        return False
    
    if len(lines) == 0:
        print("✗ FAIL: Audit log is empty")
        return False
    
    print(f"Audit log contains {len(lines)} entries\n")
    
    required_fields = {"timestamp", "user_id", "action", "grantor_id"}
    all_valid = True
    
    for i, line in enumerate(lines, 1):
        try:
            entry = json.loads(line.strip())
            
            # Check required fields
            entry_fields = set(entry.keys())
            if not required_fields.issubset(entry_fields):
                print(f"✗ Entry {i}: Missing fields. Has {entry_fields}, needs {required_fields}")
                all_valid = False
                continue
            
            # Validate field types and values
            timestamp = entry.get("timestamp")
            user_id = entry.get("user_id")
            action = entry.get("action")
            grantor_id = entry.get("grantor_id")
            
            # Verify timestamp format (ISO 8601 with Z suffix)
            if not isinstance(timestamp, str) or not timestamp.endswith("Z"):
                print(f"✗ Entry {i}: Invalid timestamp format: {timestamp}")
                all_valid = False
                continue
            
            # Verify action is grant or revoke
            if action not in ("grant", "revoke"):
                print(f"✗ Entry {i}: Invalid action: {action}")
                all_valid = False
                continue
            
            print(f"✓ Entry {i:2}: {action:6} | user={user_id:15} | grantor={grantor_id:15} | ts={timestamp}")
            
        except json.JSONDecodeError as e:
            print(f"✗ Entry {i}: Invalid JSON: {e}")
            print(f"   Line: {line.strip()}")
            all_valid = False
    
    return all_valid


def verify_audit_log_content():
    """Verify expected operations are in the log."""
    print("\n=== Verifying Audit Log Content ===\n")
    
    try:
        with open(AUDIT_LOG_FILE, 'r') as f:
            entries = [json.loads(line.strip()) for line in f if line.strip()]
    except Exception as e:
        print(f"✗ FAIL: Could not read audit log: {e}")
        return False
    
    expected_operations = [
        ("user_001", "grant"),
        ("user_002", "grant"),
        ("user_003", "grant"),
        ("user_004", "grant"),
        ("user_005", "grant"),
        ("user_001", "revoke"),
        ("user_002", "revoke"),
        ("user_006", "grant"),
        ("user_007", "grant"),
        ("user_008", "grant"),
        ("user_009", "grant"),
        ("user_010", "grant"),
    ]
    
    print(f"Expected {len(expected_operations)} operations\n")
    
    all_found = True
    for expected_user, expected_action in expected_operations:
        found = False
        for entry in entries:
            if entry.get("user_id") == expected_user and entry.get("action") == expected_action:
                found = True
                break
        
        status = "✓" if found else "✗"
        print(f"{status} {expected_action:6} {expected_user:15}: {'FOUND' if found else 'NOT FOUND'}")
        
        if not found:
            all_found = False
    
    return all_found


def main():
    """Run all tests."""
    print("=" * 80)
    print("P2-004: User ID Validation + Audit Logging - Test Suite")
    print("=" * 80)
    
    results = {}
    
    # Test 1: User ID Validation
    results['validation'] = test_user_id_validation()
    
    # Test 2: Audit Logging Operations
    results['operations'] = test_audit_logging()
    
    # Test 3: Verify Audit Log Format
    results['log_format'] = verify_audit_log()
    
    # Test 4: Verify Audit Log Content
    results['log_content'] = verify_audit_log_content()
    
    # Summary
    print("\n" + "=" * 80)
    print("TEST SUMMARY")
    print("=" * 80)
    
    for test_name, passed in results.items():
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status} | {test_name}")
    
    all_passed = all(results.values())
    print("\n" + ("=" * 80))
    
    if all_passed:
        print("✓ All tests passed!")
        print("=" * 80)
        return 0
    else:
        print("✗ Some tests failed!")
        print("=" * 80)
        return 1


if __name__ == "__main__":
    sys.exit(main())
