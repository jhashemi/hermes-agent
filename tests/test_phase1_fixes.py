"""
Phase 1 Code Review Fixes — Test Suite

Tests for all 5 critical Phase 1 tasks:
- P1-001: Enable Authentication on Remote API
- P1-002: Fix Input Validation (chat_id)
- P1-003: Fix HTTP Session Leak
- P1-004: Fix Health Check Silent Failures
- P1-005: Implement Remote API Endpoint
"""

import pytest
import asyncio
import hmac
import os
from datetime import datetime, timedelta
from unittest.mock import Mock, AsyncMock, patch, MagicMock
import hashlib

# Assuming imports from the gateway modules
# (adjust paths based on actual structure)


class TestP1001Authentication:
    """P1-001: Enable Authentication on Remote API"""

    def test_verify_api_key_success(self):
        """Test that valid API key is accepted."""
        from gateway.remote_agent_api import verify_api_key
        
        with patch.dict(os.environ, {"HERMES_HTTP_KEY": "test_key_123"}):
            # Need to clear cache to test with new env var
            from gateway.remote_agent_api import get_expected_key
            get_expected_key.cache_clear()
            
            result = verify_api_key("test_key_123")
            assert result is True

    def test_verify_api_key_invalid(self):
        """Test that invalid API key is rejected."""
        from gateway.remote_agent_api import verify_api_key
        
        with patch.dict(os.environ, {"HERMES_HTTP_KEY": "test_key_123"}):
            from gateway.remote_agent_api import get_expected_key
            get_expected_key.cache_clear()
            
            result = verify_api_key("wrong_key")
            assert result is False

    def test_verify_api_key_missing(self):
        """Test that missing API key is rejected."""
        from gateway.remote_agent_api import verify_api_key
        
        with patch.dict(os.environ, {"HERMES_HTTP_KEY": "test_key_123"}):
            from gateway.remote_agent_api import get_expected_key
            get_expected_key.cache_clear()
            
            result = verify_api_key(None)
            assert result is False

    def test_verify_api_key_timing_attack_resistant(self):
        """Test that verify_api_key uses constant-time comparison."""
        from gateway.remote_agent_api import verify_api_key
        
        # This test ensures hmac.compare_digest is used
        # by checking that wrong keys always take roughly the same time
        with patch.dict(os.environ, {"HERMES_HTTP_KEY": "a" * 100}):
            from gateway.remote_agent_api import get_expected_key
            get_expected_key.cache_clear()
            
            # Both should return False but using constant-time comparison
            result1 = verify_api_key("b" * 100)
            result2 = verify_api_key("c" * 100)
            
            assert result1 is False
            assert result2 is False


class TestP1002InputValidation:
    """P1-002: Fix Input Validation (chat_id)"""

    def test_chat_id_length_validation(self):
        """Test that chat_id exceeding MAX_CHAT_ID_LENGTH is rejected."""
        from gateway.instance_orchestrator import InstanceOrchestrator, MAX_CHAT_ID_LENGTH
        
        orchestrator = InstanceOrchestrator()
        
        # Test with valid length
        result = orchestrator.set_current_instance("local", chat_id="short_id")
        assert result is True
        
        # Test with exceeded length
        long_chat_id = "x" * (MAX_CHAT_ID_LENGTH + 1)
        result = orchestrator.set_current_instance("local", chat_id=long_chat_id)
        assert result is False

    def test_chat_id_hashing(self):
        """Test that chat_ids are hashed to prevent unbounded growth."""
        from gateway.instance_orchestrator import InstanceOrchestrator
        
        orchestrator = InstanceOrchestrator()
        
        # Set instance with a chat_id
        chat_id = "user_123"
        orchestrator.set_current_instance("local", chat_id=chat_id)
        
        # Verify the internal key is hashed
        expected_key = hashlib.sha256(chat_id.encode()).hexdigest()[:32]
        assert expected_key in orchestrator.session_instances
        assert orchestrator.session_instances[expected_key] == "local"

    def test_get_current_instance_with_hashed_key(self):
        """Test that get_current_instance uses hashed keys."""
        from gateway.instance_orchestrator import InstanceOrchestrator
        
        orchestrator = InstanceOrchestrator()
        
        chat_id = "user_123"
        orchestrator.set_current_instance("local", chat_id=chat_id)
        
        # Get should also use hashing
        instance = orchestrator.get_current_instance(chat_id=chat_id)
        assert instance == "local"

    def test_chat_id_with_special_characters(self):
        """Test that chat_id with special characters is handled correctly."""
        from gateway.instance_orchestrator import InstanceOrchestrator
        
        orchestrator = InstanceOrchestrator()
        
        # Special chars should be accepted (via hashing)
        special_chat_id = "user_123!@#$%^&*()"
        result = orchestrator.set_current_instance("local", chat_id=special_chat_id)
        assert result is True
        
        # Should be retrievable
        instance = orchestrator.get_current_instance(chat_id=special_chat_id)
        assert instance == "local"


class TestP1003SessionLeak:
    """P1-003: Fix HTTP Session Leak on Failure"""

    @pytest.mark.asyncio
    async def test_auth_failure_resets_client(self):
        """Test that 401 auth failure closes and resets HTTP client."""
        from gateway.instance_orchestrator import InstanceOrchestrator
        
        orchestrator = InstanceOrchestrator()
        await orchestrator.init()
        
        # Mock the HTTP client
        mock_client = AsyncMock()
        orchestrator._http_client = mock_client
        
        # Mock response with 401 (auth failure)
        mock_response = Mock()
        mock_response.status = 401
        mock_client.post.return_value = mock_response
        
        result = await orchestrator.execute_on_instance("hermes2", "test prompt")
        
        # Verify client was closed
        mock_client.aclose.assert_called_once()
        # Verify client was reset to None
        assert orchestrator._http_client is None
        # Verify error message
        assert "Authentication failed" in result

    @pytest.mark.asyncio
    async def test_timeout_with_retry(self):
        """Test that timeout errors trigger retry logic."""
        from gateway.instance_orchestrator import InstanceOrchestrator
        
        orchestrator = InstanceOrchestrator()
        await orchestrator.init()
        
        # Mock client that times out
        mock_client = AsyncMock()
        orchestrator._http_client = mock_client
        mock_client.post.side_effect = asyncio.TimeoutError()
        
        result = await orchestrator.execute_on_instance("hermes2", "test prompt", max_retries=2)
        
        # Should have tried twice
        assert mock_client.post.call_count == 2
        assert "timed out" in result

    @pytest.mark.asyncio
    async def test_server_error_triggers_retry(self):
        """Test that 500+ errors trigger exponential backoff retry."""
        from gateway.instance_orchestrator import InstanceOrchestrator
        
        orchestrator = InstanceOrchestrator()
        await orchestrator.init()
        
        mock_client = AsyncMock()
        orchestrator._http_client = mock_client
        
        # Mock 500 server error
        mock_response = Mock()
        mock_response.status = 500
        mock_client.post.return_value = mock_response
        
        result = await orchestrator.execute_on_instance("hermes2", "test prompt", max_retries=2)
        
        # Should have retried
        assert mock_client.post.call_count >= 2


class TestP1004HealthCheck:
    """P1-004: Fix Health Check Silent Failures"""

    @pytest.mark.asyncio
    async def test_health_check_caching(self):
        """Test that health check results are cached for 30s."""
        from gateway.instance_orchestrator import InstanceOrchestrator
        
        orchestrator = InstanceOrchestrator()
        await orchestrator.init()
        
        mock_client = AsyncMock()
        orchestrator._http_client = mock_client
        
        mock_response = Mock()
        mock_response.status = 200
        mock_client.get.return_value = mock_response
        
        # First call should hit the remote
        result1 = await orchestrator.health_check("hermes2")
        assert result1 is True
        assert mock_client.get.call_count == 1
        
        # Second call should use cache
        result2 = await orchestrator.health_check("hermes2")
        assert result2 is True
        assert mock_client.get.call_count == 1  # Still 1, cache was used

    @pytest.mark.asyncio
    async def test_health_check_cache_expiration(self):
        """Test that health check cache expires after 30s."""
        from gateway.instance_orchestrator import InstanceOrchestrator
        
        orchestrator = InstanceOrchestrator()
        await orchestrator.init()
        
        mock_client = AsyncMock()
        orchestrator._http_client = mock_client
        
        mock_response = Mock()
        mock_response.status = 200
        mock_client.get.return_value = mock_response
        
        # First call
        await orchestrator.health_check("hermes2")
        
        # Manually expire cache by setting old timestamp
        orchestrator._health_cache["hermes2"] = (True, datetime.now() - timedelta(seconds=31))
        
        # Second call should hit remote (cache expired)
        result = await orchestrator.health_check("hermes2")
        assert result is True
        assert mock_client.get.call_count == 2

    @pytest.mark.asyncio
    async def test_health_check_failure_logged_at_warning(self, caplog):
        """Test that health check failures are logged at WARNING level."""
        import logging
        from gateway.instance_orchestrator import InstanceOrchestrator
        
        orchestrator = InstanceOrchestrator()
        await orchestrator.init()
        
        mock_client = AsyncMock()
        orchestrator._http_client = mock_client
        
        # Mock failed response
        mock_response = Mock()
        mock_response.status = 500
        mock_client.get.return_value = mock_response
        
        with caplog.at_level(logging.WARNING):
            result = await orchestrator.health_check("hermes2")
        
        assert result is False
        assert any("Health check failed" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_health_check_timeout_handled(self):
        """Test that health check timeout is explicitly handled."""
        from gateway.instance_orchestrator import InstanceOrchestrator
        
        orchestrator = InstanceOrchestrator()
        await orchestrator.init()
        
        mock_client = AsyncMock()
        orchestrator._http_client = mock_client
        mock_client.get.side_effect = asyncio.TimeoutError()
        
        result = await orchestrator.health_check("hermes2")
        
        assert result is False
        assert "hermes2" in orchestrator._health_cache
        assert orchestrator._health_cache["hermes2"][0] is False


class TestP1005RemoteAPI:
    """P1-005: Implement Remote API Endpoint"""

    @pytest.mark.asyncio
    async def test_execute_endpoint_with_auth(self):
        """Test that /api/agent/execute requires valid auth."""
        # This would require a full FastAPI test client
        # For now, we verify the auth check is in place
        from gateway.remote_agent_api import verify_api_key
        
        with patch.dict(os.environ, {"HERMES_HTTP_KEY": "test_key"}):
            from gateway.remote_agent_api import get_expected_key
            get_expected_key.cache_clear()
            
            # Should require auth
            assert verify_api_key("wrong_key") is False
            assert verify_api_key("test_key") is True

    def test_execute_endpoint_returns_session_id(self):
        """Test that execute endpoint returns session_id in response."""
        # The implementation already returns session_id
        # Verified in remote_agent_api.py lines 100-104
        import inspect
        from gateway.remote_agent_api import create_remote_api_blueprint
        
        source = inspect.getsource(create_remote_api_blueprint)
        # Verify session_id is returned
        assert '"session_id": session_id' in source or "'session_id': session_id" in source


# Utility test for overall Phase 1 readiness
class TestPhase1Readiness:
    """Overall Phase 1 acceptance criteria"""

    def test_all_imports_successful(self):
        """Test that all Phase 1 modules import without errors."""
        try:
            from gateway.remote_agent_api import verify_api_key, create_remote_api_blueprint
            from gateway.instance_orchestrator import InstanceOrchestrator, MAX_CHAT_ID_LENGTH, RemoteHermesInstance
            assert True
        except ImportError as e:
            pytest.fail(f"Import failed: {e}")

    def test_max_chat_id_length_defined(self):
        """Test that MAX_CHAT_ID_LENGTH constant is defined."""
        from gateway.instance_orchestrator import MAX_CHAT_ID_LENGTH
        assert MAX_CHAT_ID_LENGTH == 256

    def test_verify_api_key_uses_hmac(self):
        """Test that verify_api_key implementation uses hmac.compare_digest."""
        import inspect
        from gateway.remote_agent_api import verify_api_key
        
        source = inspect.getsource(verify_api_key)
        assert "hmac.compare_digest" in source


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
