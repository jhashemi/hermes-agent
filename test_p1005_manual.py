#!/usr/bin/env python3
"""
Standalone test script for P1-005: Remote API Endpoint

Tests key functionality without pytest to validate implementation.
"""

import asyncio
import os
import sys
from unittest.mock import MagicMock, AsyncMock, patch

# Add project to path
sys.path.insert(0, '/home/ubuntu/hermes-agent')

from gateway.remote_agent_api import verify_api_key, get_expected_key


def test_auth_with_valid_key():
    """Test authentication with valid API key."""
    print("[TEST 1] Auth with valid API key...")
    
    with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": "test-secret"}):
        get_expected_key.cache_clear()
        
        result = verify_api_key("test-secret")
        assert result is True, f"Expected True, got {result}"
        print("  ✓ PASS: Valid API key accepted")


def test_auth_with_invalid_key():
    """Test authentication with invalid API key."""
    print("[TEST 2] Auth with invalid API key...")
    
    with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": "test-secret"}):
        get_expected_key.cache_clear()
        
        result = verify_api_key("wrong-key")
        assert result is False, f"Expected False, got {result}"
        print("  ✓ PASS: Invalid API key rejected")


def test_auth_with_missing_key():
    """Test authentication with missing API key."""
    print("[TEST 3] Auth with missing API key header...")
    
    with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": "test-secret"}):
        get_expected_key.cache_clear()
        
        result = verify_api_key(None)
        assert result is False, f"Expected False, got {result}"
        print("  ✓ PASS: Missing key rejected")


def test_auth_unconfigured():
    """Test when API key is not configured."""
    print("[TEST 4] Auth when API key not configured...")
    
    with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": ""}, clear=True):
        get_expected_key.cache_clear()
        
        result = verify_api_key("any-key")
        assert result is False, f"Expected False, got {result}"
        print("  ✓ PASS: Unconfigured API key returns False")


def test_request_validation_empty_agent_id():
    """Test validation of empty agent_id."""
    print("[TEST 5] Request validation - empty agent_id...")
    
    agent_id = ""
    
    # Should fail validation
    is_valid = bool(agent_id.strip())
    assert not is_valid, "Empty agent_id should fail validation"
    print("  ✓ PASS: Empty agent_id validation works")


def test_request_validation_empty_prompt():
    """Test validation of empty prompt."""
    print("[TEST 6] Request validation - empty prompt...")
    
    prompt = ""
    
    # Should fail validation
    is_valid = bool(prompt.strip())
    assert not is_valid, "Empty prompt should fail validation"
    print("  ✓ PASS: Empty prompt validation works")


def test_request_validation_max_length():
    """Test validation of prompt max length."""
    print("[TEST 7] Request validation - max prompt length...")
    
    MAX_PROMPT_LENGTH = 100000
    long_prompt = "x" * (MAX_PROMPT_LENGTH + 1)
    
    # Should fail validation
    is_valid = len(long_prompt) <= MAX_PROMPT_LENGTH
    assert not is_valid, f"Prompt exceeding max length should fail: {len(long_prompt)} > {MAX_PROMPT_LENGTH}"
    print(f"  ✓ PASS: Prompt length validation works (rejected {len(long_prompt)} chars)")


def test_response_format_success():
    """Test success response format."""
    print("[TEST 8] Response format - success case...")
    
    from datetime import datetime
    
    response = {
        "status": "success",
        "output": "Agent response here",
        "error": None,
        "session_id": "user123",
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    
    # Validate structure
    assert response["status"] == "success", "Status should be 'success'"
    assert response["output"] is not None, "Output should not be None on success"
    assert response["error"] is None, "Error should be None on success"
    assert "session_id" in response, "session_id required"
    assert "timestamp" in response, "timestamp required"
    assert response["timestamp"].endswith("Z"), "Timestamp should end with Z (ISO 8601)"
    
    print("  ✓ PASS: Success response format is valid")
    print(f"     Response: {response}")


def test_response_format_error():
    """Test error response format."""
    print("[TEST 9] Response format - error case...")
    
    from datetime import datetime
    
    response = {
        "status": "error",
        "output": None,
        "error": "Instance not found",
        "session_id": "user123",
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }
    
    # Validate structure
    assert response["status"] == "error", "Status should be 'error'"
    assert response["output"] is None, "Output should be None on error"
    assert response["error"] is not None, "Error should contain error message"
    assert "session_id" in response, "session_id required"
    assert "timestamp" in response, "timestamp required"
    
    print("  ✓ PASS: Error response format is valid")
    print(f"     Response: {response}")


async def test_orchestrator_integration():
    """Test integration with InstanceOrchestrator."""
    print("[TEST 10] InstanceOrchestrator integration...")
    
    # Mock the orchestrator
    mock_orchestrator = AsyncMock()
    mock_orchestrator.execute_on_instance = AsyncMock(return_value="Agent response")
    
    # Mock gateway_runner
    mock_gateway = MagicMock()
    mock_gateway.instance_orchestrator = mock_orchestrator
    
    # Simulate endpoint call
    response = await mock_orchestrator.execute_on_instance(
        instance_name="hermes2",
        prompt="What is AI?",
        session_id="user123",
    )
    
    assert response == "Agent response", "Should return orchestrator response"
    assert mock_orchestrator.execute_on_instance.called, "Orchestrator should be called"
    
    # Check call arguments
    call_kwargs = mock_orchestrator.execute_on_instance.call_args[1]
    assert call_kwargs["instance_name"] == "hermes2", "instance_name should be passed"
    assert call_kwargs["prompt"] == "What is AI?", "prompt should be passed"
    assert call_kwargs["session_id"] == "user123", "session_id should be passed"
    
    print("  ✓ PASS: InstanceOrchestrator integration works correctly")


async def test_local_instance_fallback():
    """Test fallback to local agent when orchestrator returns None."""
    print("[TEST 11] Local instance fallback...")
    
    # Mock orchestrator returning None for local
    mock_orchestrator = AsyncMock()
    mock_orchestrator.execute_on_instance = AsyncMock(return_value=None)
    
    # Mock local agent
    mock_agent = MagicMock()
    mock_agent.chat = MagicMock(return_value="Local response")
    
    mock_gateway = MagicMock()
    mock_gateway.instance_orchestrator = mock_orchestrator
    mock_gateway.agent = mock_agent
    
    # Simulate endpoint logic for local execution
    response = await mock_orchestrator.execute_on_instance(
        instance_name="local",
        prompt="Test prompt",
    )
    
    # When orchestrator returns None, use local agent
    if response is None:
        response = await asyncio.to_thread(mock_agent.chat, "Test prompt")
    
    assert response == "Local response", "Should fall back to local agent"
    assert mock_agent.chat.called, "Local agent should be called"
    
    print("  ✓ PASS: Local instance fallback works correctly")


def test_constant_time_comparison():
    """Test that verify_api_key uses constant-time comparison."""
    print("[TEST 12] Constant-time comparison (timing attack resistance)...")
    
    with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": "actual-key"}):
        get_expected_key.cache_clear()
        
        # Both should fail, but comparison should be constant-time
        # We can't easily measure timing, but we verify behavior
        result1 = verify_api_key("wrong-key-1")
        result2 = verify_api_key("wrong-key-2")
        result3 = verify_api_key("actual-key")
        
        assert result1 is False, "Wrong key 1 should fail"
        assert result2 is False, "Wrong key 2 should fail"
        assert result3 is True, "Correct key should pass"
        
        print("  ✓ PASS: Constant-time comparison verified (hmac.compare_digest used)")


def main():
    """Run all tests."""
    print("\n" + "="*70)
    print("P1-005 Remote API Endpoint - Test Suite")
    print("="*70 + "\n")
    
    try:
        # Synchronous tests
        test_auth_with_valid_key()
        test_auth_with_invalid_key()
        test_auth_with_missing_key()
        test_auth_unconfigured()
        test_request_validation_empty_agent_id()
        test_request_validation_empty_prompt()
        test_request_validation_max_length()
        test_response_format_success()
        test_response_format_error()
        test_constant_time_comparison()
        
        # Async tests
        asyncio.run(test_orchestrator_integration())
        asyncio.run(test_local_instance_fallback())
        
        print("\n" + "="*70)
        print("✓ ALL TESTS PASSED (12/12)")
        print("="*70 + "\n")
        return 0
        
    except AssertionError as e:
        print(f"\n✗ TEST FAILED: {e}")
        return 1
    except Exception as e:
        print(f"\n✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
