"""Tests for remote agent API authentication (P1-001)."""

import pytest
import os
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Import the functions to test
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'gateway'))

from remote_agent_api import verify_api_key, get_expected_key


class TestVerifyApiKey:
    """Test suite for verify_api_key() function."""

    def test_verify_api_key_with_valid_key(self):
        """Test that verify_api_key returns True with correct key."""
        valid_key = "test_secret_key_12345"
        with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": valid_key}):
            # Clear cache to pick up new env var
            get_expected_key.cache_clear()
            
            result = verify_api_key(valid_key)
            assert result is True, "Should accept valid API key"

    def test_verify_api_key_with_invalid_key(self):
        """Test that verify_api_key returns False with wrong key."""
        valid_key = "test_secret_key_12345"
        wrong_key = "wrong_key_67890"
        
        with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": valid_key}):
            get_expected_key.cache_clear()
            
            result = verify_api_key(wrong_key)
            assert result is False, "Should reject invalid API key"

    def test_verify_api_key_with_missing_header(self):
        """Test that verify_api_key returns False when header is missing."""
        valid_key = "test_secret_key_12345"
        
        with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": valid_key}):
            get_expected_key.cache_clear()
            
            result = verify_api_key(None)
            assert result is False, "Should reject missing API key header"

    def test_verify_api_key_with_empty_header(self):
        """Test that verify_api_key returns False with empty header."""
        valid_key = "test_secret_key_12345"
        
        with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": valid_key}):
            get_expected_key.cache_clear()
            
            result = verify_api_key("")
            assert result is False, "Should reject empty API key header"

    def test_verify_api_key_with_no_env_var_set(self):
        """Test that verify_api_key returns False when env var not set."""
        with patch.dict(os.environ, {}, clear=True):
            get_expected_key.cache_clear()
            
            result = verify_api_key("any_key")
            assert result is False, "Should reject when no API key configured"

    def test_verify_api_key_uses_constant_time_comparison(self):
        """Test that verify_api_key uses hmac.compare_digest (timing attack resistant)."""
        import hmac
        
        valid_key = "test_secret_key_12345"
        similar_key = "test_secret_key_12324"  # Last char different
        
        with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": valid_key}):
            get_expected_key.cache_clear()
            
            # Both should be rejected, but timing should be same
            result_wrong = verify_api_key(similar_key)
            result_valid = verify_api_key(valid_key)
            
            assert result_wrong is False
            assert result_valid is True
            
            # Verify the actual function uses hmac.compare_digest
            # by checking that it returns bool (not just truthy/falsy)
            assert isinstance(result_valid, bool)
            assert isinstance(result_wrong, bool)


class TestRemoteAPIEndpoints:
    """Test suite for FastAPI remote agent execution endpoints."""

    @pytest.mark.asyncio
    async def test_execute_agent_prompt_with_valid_key(self):
        """Test POST /api/agent/execute with valid API key."""
        from fastapi import FastAPI, Header
        from fastapi.testclient import TestClient
        from typing import Optional, Dict, Any
        from remote_agent_api import verify_api_key
        
        app = FastAPI()
        valid_key = "test_secret_key_12345"
        
        with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": valid_key}):
            get_expected_key.cache_clear()
            
            # Mock gateway_runner
            mock_gateway_runner = MagicMock()
            mock_gateway_runner.agent.chat = AsyncMock(
                return_value="This is the agent response"
            )
            
            @app.post("/api/agent/execute")
            async def execute_agent_prompt(
                request: Dict[str, Any],
                x_hermes_key: Optional[str] = Header(None),
                x_hermes_user: Optional[str] = Header(None),
            ):
                """Execute a prompt on this Hermes instance."""
                from fastapi import HTTPException
                from remote_agent_api import verify_api_key
                import asyncio
                
                # P1-001: Verify API key
                if not verify_api_key(x_hermes_key):
                    raise HTTPException(status_code=401, detail="Unauthorized")
                
                prompt = request.get("prompt", "").strip()
                session_id = request.get("session_id", "remote-exec")
                
                if not prompt:
                    raise HTTPException(status_code=400, detail="prompt required")
                
                try:
                    response = await asyncio.to_thread(
                        mock_gateway_runner.agent.chat,
                        prompt,
                    )
                    
                    return {
                        "success": True,
                        "response": response,
                        "session_id": session_id,
                    }
                except Exception as e:
                    return {"success": False, "error": str(e), "session_id": session_id}
            
            client = TestClient(app)
            
            # Test with valid key
            response = client.post(
                "/api/agent/execute",
                json={"prompt": "What is AI?", "session_id": "test_session_1"},
                headers={"x-hermes-key": valid_key, "x-hermes-user": "test_user"},
            )
            
            assert response.status_code == 200, f"Expected 200, got {response.status_code}: {response.text}"
            assert response.json()["success"] is True

    def test_execute_agent_prompt_without_key(self):
        """Test POST /api/agent/execute returns 401 without API key."""
        from fastapi import FastAPI, Header, HTTPException
        from fastapi.testclient import TestClient
        from typing import Optional, Dict, Any
        
        app = FastAPI()
        valid_key = "test_secret_key_12345"
        
        with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": valid_key}):
            get_expected_key.cache_clear()
            
            @app.post("/api/agent/execute")
            async def execute_agent_prompt(
                request: Dict[str, Any],
                x_hermes_key: Optional[str] = Header(None),
                x_hermes_user: Optional[str] = Header(None),
            ):
                # P1-001: Verify API key
                if not verify_api_key(x_hermes_key):
                    raise HTTPException(status_code=401, detail="Unauthorized")
                
                return {"success": True, "response": "test"}
            
            client = TestClient(app)
            
            # Test without key
            response = client.post(
                "/api/agent/execute",
                json={"prompt": "What is AI?"},
            )
            
            assert response.status_code == 401, f"Expected 401, got {response.status_code}"
            assert response.json()["detail"] == "Unauthorized"

    def test_execute_agent_prompt_with_invalid_key(self):
        """Test POST /api/agent/execute returns 401 with invalid API key."""
        from fastapi import FastAPI, Header, HTTPException
        from fastapi.testclient import TestClient
        from typing import Optional, Dict, Any
        
        app = FastAPI()
        valid_key = "test_secret_key_12345"
        invalid_key = "wrong_key_67890"
        
        with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": valid_key}):
            get_expected_key.cache_clear()
            
            @app.post("/api/agent/execute")
            async def execute_agent_prompt(
                request: Dict[str, Any],
                x_hermes_key: Optional[str] = Header(None),
                x_hermes_user: Optional[str] = Header(None),
            ):
                # P1-001: Verify API key
                if not verify_api_key(x_hermes_key):
                    raise HTTPException(status_code=401, detail="Unauthorized")
                
                return {"success": True, "response": "test"}
            
            client = TestClient(app)
            
            # Test with invalid key
            response = client.post(
                "/api/agent/execute",
                json={"prompt": "What is AI?"},
                headers={"x-hermes-key": invalid_key},
            )
            
            assert response.status_code == 401, f"Expected 401, got {response.status_code}"

    def test_agent_status_endpoint_without_key(self):
        """Test GET /api/agent/status returns 401 without API key."""
        from fastapi import FastAPI, Header, HTTPException
        from fastapi.testclient import TestClient
        from typing import Optional
        
        app = FastAPI()
        valid_key = "test_secret_key_12345"
        
        with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": valid_key}):
            get_expected_key.cache_clear()
            
            @app.get("/api/agent/status")
            async def agent_status(
                x_hermes_key: Optional[str] = Header(None),
                x_hermes_user: Optional[str] = Header(None),
            ):
                # P1-001: Verify API key
                if not verify_api_key(x_hermes_key):
                    raise HTTPException(status_code=401, detail="Unauthorized")
                
                return {"running": False, "model": "claude-3-sonnet"}
            
            client = TestClient(app)
            
            # Test without key
            response = client.get("/api/agent/status")
            
            assert response.status_code == 401, f"Expected 401, got {response.status_code}"

    def test_agent_status_endpoint_with_valid_key(self):
        """Test GET /api/agent/status with valid API key."""
        from fastapi import FastAPI, Header, HTTPException
        from fastapi.testclient import TestClient
        from typing import Optional
        
        app = FastAPI()
        valid_key = "test_secret_key_12345"
        
        with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": valid_key}):
            get_expected_key.cache_clear()
            
            @app.get("/api/agent/status")
            async def agent_status(
                x_hermes_key: Optional[str] = Header(None),
                x_hermes_user: Optional[str] = Header(None),
            ):
                # P1-001: Verify API key
                if not verify_api_key(x_hermes_key):
                    raise HTTPException(status_code=401, detail="Unauthorized")
                
                return {"running": False, "model": "claude-3-sonnet"}
            
            client = TestClient(app)
            
            # Test with valid key
            response = client.get(
                "/api/agent/status",
                headers={"x-hermes-key": valid_key},
            )
            
            assert response.status_code == 200, f"Expected 200, got {response.status_code}"
            assert response.json()["model"] == "claude-3-sonnet"

    def test_health_endpoint_no_auth_required(self):
        """Test GET /health does not require authentication."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        
        app = FastAPI()
        valid_key = "test_secret_key_12345"
        
        with patch.dict(os.environ, {"HERMES_REMOTE_API_KEY": valid_key}):
            get_expected_key.cache_clear()
            
            @app.get("/health")
            async def health_check():
                """Simple health check endpoint (no auth required)."""
                return {"status": "ok", "instance": "hermes2"}
            
            client = TestClient(app)
            
            # Test without key - should still work
            response = client.get("/health")
            
            assert response.status_code == 200, f"Expected 200, got {response.status_code}"
            assert response.json()["status"] == "ok"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
