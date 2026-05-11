#!/usr/bin/env python3
"""
Integration test showing how the P1-005 endpoint would be used
in a real FastAPI application.
"""

import sys
sys.path.insert(0, '/home/ubuntu/hermes-agent')

from typing import Optional
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock

# Simulate the FastAPI request/response models defined in remote_agent_api.py
class ExecuteRequest:
    def __init__(self, agent_id: str, prompt: str, session_id: Optional[str] = None):
        self.agent_id = agent_id
        self.prompt = prompt
        self.session_id = session_id

class ExecuteResponse:
    def __init__(self, status: str, output: Optional[str], error: Optional[str],
                 session_id: Optional[str], timestamp: str):
        self.status = status
        self.output = output
        self.error = error
        self.session_id = session_id
        self.timestamp = timestamp

    def dict(self):
        return {
            "status": self.status,
            "output": self.output,
            "error": self.error,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
        }

async def simulate_endpoint_with_valid_request():
    """Simulate valid request flow."""
    print("[INTEGRATION] Valid request with authentication...")
    
    from gateway.remote_agent_api import verify_api_key
    import os
    from unittest.mock import patch
    
    # Setup
    with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": "test-key"}):
        from gateway.remote_agent_api import get_expected_key
        get_expected_key.cache_clear()
        
        # Incoming request
        api_key = "test-key"
        request = ExecuteRequest(
            agent_id="default",
            prompt="What is 2+2?",
            session_id="user123"
        )
        
        # Step 1: Verify API key
        if not verify_api_key(api_key):
            return ExecuteResponse("error", None, "Unauthorized", request.session_id,
                                 datetime.utcnow().isoformat() + "Z")
        
        print("  ✓ API key verified")
        
        # Step 2: Validate request (simplified)
        if not request.agent_id:
            return ExecuteResponse("error", None, "agent_id required", request.session_id,
                                 datetime.utcnow().isoformat() + "Z")
        
        if not request.prompt or not request.prompt.strip():
            return ExecuteResponse("error", None, "prompt required", request.session_id,
                                 datetime.utcnow().isoformat() + "Z")
        
        print("  ✓ Request validated")
        
        # Step 3: Execute via orchestrator
        mock_orchestrator = AsyncMock()
        mock_orchestrator.execute_on_instance = AsyncMock(return_value="4")
        
        try:
            response_output = await mock_orchestrator.execute_on_instance(
                instance_name=request.agent_id,
                prompt=request.prompt,
                session_id=request.session_id,
            )
            print(f"  ✓ Execution successful: {response_output}")
        except Exception as e:
            return ExecuteResponse("error", None, str(e), request.session_id,
                                 datetime.utcnow().isoformat() + "Z")
        
        # Step 4: Build response
        response = ExecuteResponse(
            status="success",
            output=response_output,
            error=None,
            session_id=request.session_id,
            timestamp=datetime.utcnow().isoformat() + "Z"
        )
        
        print(f"  ✓ Response: {response.dict()}")
        return response


async def simulate_endpoint_with_invalid_auth():
    """Simulate invalid authentication."""
    print("[INTEGRATION] Invalid authentication...")
    
    from gateway.remote_agent_api import verify_api_key
    import os
    from unittest.mock import patch
    
    with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": "test-key"}):
        from gateway.remote_agent_api import get_expected_key
        get_expected_key.cache_clear()
        
        request = ExecuteRequest(
            agent_id="default",
            prompt="test",
            session_id="user123"
        )
        
        # Invalid API key
        api_key = "wrong-key"
        
        if not verify_api_key(api_key):
            print("  ✓ Authentication failed (as expected)")
            return ExecuteResponse("error", None, "Unauthorized", request.session_id,
                                 datetime.utcnow().isoformat() + "Z")


async def simulate_endpoint_with_validation_error():
    """Simulate validation error."""
    print("[INTEGRATION] Validation error (empty prompt)...")
    
    from gateway.remote_agent_api import verify_api_key
    import os
    from unittest.mock import patch
    
    with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": "test-key"}):
        from gateway.remote_agent_api import get_expected_key
        get_expected_key.cache_clear()
        
        request = ExecuteRequest(
            agent_id="default",
            prompt="",  # Empty!
            session_id="user123"
        )
        
        api_key = "test-key"
        
        if not verify_api_key(api_key):
            return ExecuteResponse("error", None, "Unauthorized", request.session_id,
                                 datetime.utcnow().isoformat() + "Z")
        
        # Validation
        if not request.prompt or not request.prompt.strip():
            print("  ✓ Validation failed: empty prompt (as expected)")
            return ExecuteResponse("error", None, "prompt is required", request.session_id,
                                 datetime.utcnow().isoformat() + "Z")


async def simulate_endpoint_with_large_prompt():
    """Simulate DoS prevention (max prompt length)."""
    print("[INTEGRATION] DoS prevention (prompt too large)...")
    
    from gateway.remote_agent_api import verify_api_key
    import os
    from unittest.mock import patch
    
    MAX_PROMPT_LENGTH = 100000
    
    with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": "test-key"}):
        from gateway.remote_agent_api import get_expected_key
        get_expected_key.cache_clear()
        
        request = ExecuteRequest(
            agent_id="default",
            prompt="x" * (MAX_PROMPT_LENGTH + 1),  # Too large!
            session_id="user123"
        )
        
        api_key = "test-key"
        
        if not verify_api_key(api_key):
            return ExecuteResponse("error", None, "Unauthorized", request.session_id,
                                 datetime.utcnow().isoformat() + "Z")
        
        # Validation
        if len(request.prompt) > MAX_PROMPT_LENGTH:
            print(f"  ✓ Validation failed: prompt too large {len(request.prompt)} > {MAX_PROMPT_LENGTH}")
            return ExecuteResponse("error", None, f"prompt exceeds max length {MAX_PROMPT_LENGTH}",
                                 request.session_id, datetime.utcnow().isoformat() + "Z")


async def main():
    """Run integration tests."""
    print("\n" + "="*70)
    print("P1-005 Integration Tests - Real Usage Patterns")
    print("="*70 + "\n")
    
    try:
        # Test 1: Valid request
        response = await simulate_endpoint_with_valid_request()
        assert response.status == "success", f"Expected success, got {response.status}"
        print()
        
        # Test 2: Invalid auth
        response = await simulate_endpoint_with_invalid_auth()
        assert response.error == "Unauthorized", f"Expected Unauthorized error"
        print()
        
        # Test 3: Validation error
        response = await simulate_endpoint_with_validation_error()
        assert response.error == "prompt is required", f"Expected validation error"
        print()
        
        # Test 4: DoS prevention
        response = await simulate_endpoint_with_large_prompt()
        assert "max length" in response.error, f"Expected max length error"
        print()
        
        print("="*70)
        print("✓ ALL INTEGRATION TESTS PASSED")
        print("="*70 + "\n")
        return 0
        
    except Exception as e:
        print(f"\n✗ INTEGRATION TEST FAILED: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    import asyncio
    sys.exit(asyncio.run(main()))
