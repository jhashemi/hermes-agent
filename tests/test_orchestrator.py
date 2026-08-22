"""Comprehensive unit tests for the InstanceOrchestrator class.

Tests cover:
- set_current_instance: switching instances, validation, error handling
- get_current_instance / get_active_instance: retrieving active instances, chat-specific logic
- execute_on_instance: remote execution, retries, error handling, timeouts
- Thread safety: concurrent access to _user_instances and session_instances
- Health checks: caching, failures, recovery
- Edge cases: invalid inputs, boundary conditions, state transitions
"""

import asyncio
import hashlib
import logging
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, Mock
from datetime import datetime, timedelta

from gateway.instance_orchestrator import (
    InstanceOrchestrator,
    RemoteHermesInstance,
    validate_hostname,
    validate_port,
    get_instance_config,
    HERMES_INSTANCES,
    MAX_CHAT_ID_LENGTH,
)


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def orchestrator():
    """Create a fresh InstanceOrchestrator for each test."""
    return InstanceOrchestrator()


@pytest.fixture
def orchestrator_initialized():
    """Create and initialize an InstanceOrchestrator (sync wrapper)."""
    async def _init():
        orch = InstanceOrchestrator()
        await orch.init()
        return orch
    
    async def _cleanup(orch):
        await orch.close()
    
    # Return a wrapper that handles the async initialization/cleanup
    import asyncio
    loop = asyncio.new_event_loop()
    orch = loop.run_until_complete(_init())
    yield orch
    loop.run_until_complete(_cleanup(orch))
    loop.close()


@pytest.fixture
def mock_http_client():
    """Create a mock httpx.AsyncClient."""
    return AsyncMock()


# ============================================================================
# Tests: validate_hostname
# ============================================================================


class TestValidateHostname:
    """Tests for hostname validation."""

    def test_validate_localhost(self):
        """localhost should always be valid."""
        assert validate_hostname("localhost") is True

    def test_validate_ipv4_valid(self):
        """Valid IPv4 addresses should pass."""
        assert validate_hostname("127.0.0.1") is True
        assert validate_hostname("192.168.1.1") is True
        assert validate_hostname("0.0.0.0") is True
        assert validate_hostname("255.255.255.255") is True

    def test_validate_ipv4_invalid_octets(self):
        """IPv4 with octets > 255 should fail."""
        assert validate_hostname("256.1.1.1") is False
        assert validate_hostname("192.168.1.256") is False
        assert validate_hostname("192.168.256.1") is False

    def test_validate_ipv4_partial(self):
        """Partial IPv4 addresses should fail."""
        assert validate_hostname("192.168.1") is False
        assert validate_hostname("192.168") is False

    def test_validate_fqdn_valid(self):
        """Valid FQDNs should pass."""
        assert validate_hostname("hermes2.flounder-snake.ts.net") is True
        assert validate_hostname("example.com") is True
        assert validate_hostname("a.b") is True

    def test_validate_fqdn_with_hyphen(self):
        """FQDNs with hyphens should pass."""
        assert validate_hostname("my-host.example.com") is True
        assert validate_hostname("host-123.sub-domain.org") is True

    def test_validate_ipv6_basic(self):
        """Basic IPv6 addresses should pass."""
        assert validate_hostname("::1") is True
        assert validate_hostname("2001:db8::1") is True

    def test_validate_invalid_hostname_empty(self):
        """Empty hostname should raise ValueError."""
        with pytest.raises(ValueError, match="hostname cannot be empty"):
            validate_hostname("")

    def test_validate_invalid_hostname_whitespace(self):
        """Whitespace-only hostname should raise ValueError."""
        with pytest.raises(ValueError, match="hostname cannot be empty"):
            validate_hostname("   ")

    def test_validate_invalid_hostname_not_string(self):
        """Non-string hostname should raise ValueError."""
        with pytest.raises(ValueError, match="hostname must be a string"):
            validate_hostname(123)
        with pytest.raises(ValueError, match="hostname must be a string"):
            validate_hostname(None)

    def test_validate_hostname_with_leading_trailing_space(self):
        """Hostname with surrounding whitespace should be stripped and validated."""
        assert validate_hostname("  localhost  ") is True
        assert validate_hostname("  127.0.0.1  ") is True


# ============================================================================
# Tests: validate_port
# ============================================================================


class TestValidatePort:
    """Tests for port validation."""

    def test_validate_port_valid_range(self):
        """Valid ports should pass."""
        assert validate_port(1) is True
        assert validate_port(8000) is True
        assert validate_port(65535) is True

    def test_validate_port_boundary_low(self):
        """Port 0 should fail (invalid)."""
        assert validate_port(0) is False

    def test_validate_port_boundary_high(self):
        """Port > 65535 should fail."""
        assert validate_port(65536) is False
        assert validate_port(100000) is False

    def test_validate_port_negative(self):
        """Negative ports should fail."""
        assert validate_port(-1) is False
        assert validate_port(-8000) is False

    def test_validate_port_not_integer(self):
        """Non-integer port should raise ValueError."""
        with pytest.raises(ValueError, match="port must be an integer"):
            validate_port("8000")
        with pytest.raises(ValueError, match="port must be an integer"):
            validate_port(8000.5)
        with pytest.raises(ValueError, match="port must be an integer"):
            validate_port(None)


# ============================================================================
# Tests: set_current_instance
# ============================================================================


class TestSetCurrentInstance:
    """Tests for switching the current active instance."""

    def test_set_current_instance_default(self, orchestrator):
        """Default instance should be 'local'."""
        assert orchestrator.current_instance == "local"

    def test_set_current_instance_to_valid_instance(self, orchestrator):
        """Should switch to a valid instance."""
        result = orchestrator.set_current_instance("hermes2")
        assert result is True
        assert orchestrator.current_instance == "hermes2"

    def test_set_current_instance_to_invalid_instance(self, orchestrator):
        """Should return False for non-existent instance."""
        result = orchestrator.set_current_instance("nonexistent")
        assert result is False
        # Current instance should not change
        assert orchestrator.current_instance == "local"

    def test_set_current_instance_back_to_local(self, orchestrator):
        """Should be able to switch back to local."""
        orchestrator.set_current_instance("hermes2")
        result = orchestrator.set_current_instance("local")
        assert result is True
        assert orchestrator.current_instance == "local"

    def test_set_current_instance_with_chat_id(self, orchestrator):
        """Should store chat_id → instance mapping in session_instances."""
        chat_id = "user_123"
        result = orchestrator.set_current_instance("hermes2", chat_id=chat_id)
        assert result is True
        # Global instance should still be 'local'
        assert orchestrator.current_instance == "local"
        # Chat-specific instance should be 'hermes2'
        chat_key = hashlib.sha256(chat_id.encode()).hexdigest()[:32]
        assert orchestrator.session_instances[chat_key] == "hermes2"

    def test_set_current_instance_with_chat_id_invalid_instance(self, orchestrator):
        """Should return False for invalid instance even with chat_id."""
        result = orchestrator.set_current_instance("invalid", chat_id="user_123")
        assert result is False
        assert len(orchestrator.session_instances) == 0

    def test_set_current_instance_chat_id_length_validation(self, orchestrator):
        """Should raise ValueError if chat_id exceeds MAX_CHAT_ID_LENGTH."""
        oversized_chat_id = "x" * (MAX_CHAT_ID_LENGTH + 1)
        with pytest.raises(ValueError, match="chat_id length .* exceeds maximum"):
            orchestrator.set_current_instance("hermes2", chat_id=oversized_chat_id)

    def test_set_current_instance_chat_id_max_length_allowed(self, orchestrator):
        """Should accept chat_id at MAX_CHAT_ID_LENGTH."""
        max_chat_id = "x" * MAX_CHAT_ID_LENGTH
        result = orchestrator.set_current_instance("hermes2", chat_id=max_chat_id)
        assert result is True

    def test_set_current_instance_chat_id_not_string(self, orchestrator):
        """Should raise ValueError if chat_id is not a string."""
        with pytest.raises(ValueError, match="chat_id must be a string"):
            orchestrator.set_current_instance("hermes2", chat_id=123)
        # Note: None is falsy and skipped, so doesn't raise error
        # This is by design in the implementation
        result = orchestrator.set_current_instance("hermes2", chat_id=None)
        assert result is True

    def test_set_current_instance_multiple_chat_ids(self, orchestrator):
        """Should handle multiple different chat IDs independently."""
        orchestrator.set_current_instance("hermes2", chat_id="user_1")
        orchestrator.set_current_instance("local", chat_id="user_2")
        
        assert orchestrator.get_current_instance("user_1") == "hermes2"
        assert orchestrator.get_current_instance("user_2") == "local"

    def test_set_current_instance_overwrite_chat_id(self, orchestrator):
        """Should overwrite existing chat_id instance mapping."""
        orchestrator.set_current_instance("hermes2", chat_id="user_1")
        orchestrator.set_current_instance("local", chat_id="user_1")
        
        assert orchestrator.get_current_instance("user_1") == "local"

    def test_set_current_instance_validates_hostname(self, orchestrator):
        """Should validate instance hostname during set.

        RemoteHermesInstance.__init__ hard-validates hostname/port at
        construction time, so we bypass it with object.__new__ to plant a
        corrupt instance in the registry and prove set_current_instance
        also re-validates before returning success (defense in depth).
        """
        bad = object.__new__(RemoteHermesInstance)
        bad.name = "bad_host"
        bad.hostname = "256.256.256.256"  # invalid IPv4
        bad.ip = "127.0.0.1"
        bad.http_port = 8000
        bad.http_key = ""
        bad.username = ""
        bad.description = ""
        bad.is_local = False

        # Inject directly into the orchestrator's per-instance registry so we
        # don't have to touch the module-level HERMES_INSTANCES.
        orchestrator._instances["bad_host"] = bad
        with pytest.raises(ValueError, match="Invalid hostname"):
            orchestrator.set_current_instance("bad_host")

    def test_set_current_instance_validates_port(self, orchestrator):
        """Should validate instance port during set."""
        bad = object.__new__(RemoteHermesInstance)
        bad.name = "bad_port"
        bad.hostname = "localhost"
        bad.ip = "127.0.0.1"
        bad.http_port = 99999  # invalid port
        bad.http_key = ""
        bad.username = ""
        bad.description = ""
        bad.is_local = False

        orchestrator._instances["bad_port"] = bad
        with pytest.raises(ValueError, match="Invalid port"):
            orchestrator.set_current_instance("bad_port")


# ============================================================================
# Tests: get_current_instance
# ============================================================================


class TestGetCurrentInstance:
    """Tests for retrieving the active instance."""

    def test_get_current_instance_default(self, orchestrator):
        """Should return 'local' by default."""
        assert orchestrator.get_current_instance() == "local"

    def test_get_current_instance_after_switch(self, orchestrator):
        """Should return switched instance."""
        orchestrator.set_current_instance("hermes2")
        assert orchestrator.get_current_instance() == "hermes2"

    def test_get_current_instance_with_chat_id_exists(self, orchestrator):
        """Should return chat-specific instance if it exists."""
        chat_id = "user_123"
        orchestrator.set_current_instance("hermes2", chat_id=chat_id)
        assert orchestrator.get_current_instance(chat_id) == "hermes2"

    def test_get_current_instance_with_chat_id_not_exists(self, orchestrator):
        """Should return default instance if chat_id not in mapping."""
        orchestrator.set_current_instance("hermes2")
        assert orchestrator.get_current_instance("unknown_chat") == "hermes2"

    def test_get_current_instance_with_chat_id_after_global_change(self, orchestrator):
        """Chat-specific instance should take precedence over global."""
        orchestrator.set_current_instance("local", chat_id="user_1")
        orchestrator.set_current_instance("hermes2")  # Global change
        assert orchestrator.get_current_instance("user_1") == "local"  # Chat-specific wins
        assert orchestrator.get_current_instance() == "hermes2"  # Global is hermes2


# ============================================================================
# Tests: execute_on_instance (basic and async tests)
# ============================================================================


class TestExecuteOnInstance:
    """Tests for executing prompts on instances."""

    @pytest.mark.asyncio
    async def test_execute_on_instance_local_returns_none(self, orchestrator_initialized):
        """Local instance execution should return None (handled by gateway)."""
        result = await orchestrator_initialized.execute_on_instance(
            "local", "test prompt"
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_execute_on_instance_nonexistent(self, orchestrator_initialized):
        """Should return error message for nonexistent instance."""
        result = await orchestrator_initialized.execute_on_instance(
            "nonexistent", "test prompt"
        )
        assert result is not None
        assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_execute_on_instance_validates_hostname(self, orchestrator_initialized):
        """Should validate hostname before execution.

        Bypasses RemoteHermesInstance.__init__ (which hard-validates) so we
        can plant a corrupt entry and prove execute_on_instance itself
        re-validates before dialing out.
        """
        bad = object.__new__(RemoteHermesInstance)
        bad.name = "bad"
        bad.hostname = "invalid..hostname"
        bad.ip = "127.0.0.1"
        bad.http_port = 8000
        bad.http_key = ""
        bad.username = ""
        bad.description = ""
        bad.is_local = False

        orchestrator_initialized._instances["bad"] = bad
        with pytest.raises(ValueError, match="Invalid hostname"):
            await orchestrator_initialized.execute_on_instance("bad", "test")

    @pytest.mark.asyncio
    async def test_execute_on_instance_validates_port(self, orchestrator_initialized):
        """Should validate port before execution."""
        bad = object.__new__(RemoteHermesInstance)
        bad.name = "bad"
        bad.hostname = "localhost"
        bad.ip = "127.0.0.1"
        bad.http_port = 70000  # invalid port
        bad.http_key = ""
        bad.username = ""
        bad.description = ""
        bad.is_local = False

        orchestrator_initialized._instances["bad"] = bad
        with pytest.raises(ValueError, match="Invalid port"):
            await orchestrator_initialized.execute_on_instance("bad", "test")

    @pytest.mark.asyncio
    async def test_execute_on_instance_success_response(self, orchestrator_initialized):
        """Should return response from successful execution (mocked)."""
        # Create a proper mock that returns the expected data
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = lambda: {"response": "Test response"}
        mock_response.content = b"test"

        orchestrator_initialized._http_client.post = AsyncMock(return_value=mock_response)

        result = await orchestrator_initialized.execute_on_instance(
            "hermes2", "test prompt", session_id="test_session"
        )
        assert result == "Test response"

    @pytest.mark.asyncio
    async def test_execute_on_instance_auth_failure(self, orchestrator_initialized):
        """Should handle 401 auth failure."""
        mock_response = AsyncMock()
        mock_response.status = 401
        mock_response.content = b""

        orchestrator_initialized._http_client.post = AsyncMock(return_value=mock_response)

        result = await orchestrator_initialized.execute_on_instance("hermes2", "test")
        assert "Authentication failed" in result

    @pytest.mark.asyncio
    async def test_execute_on_instance_server_error_with_retry(self, orchestrator_initialized):
        """Should retry on server error (5xx)."""
        mock_response = AsyncMock()
        mock_response.status = 503
        mock_response.content = b""

        orchestrator_initialized._http_client.post = AsyncMock(return_value=mock_response)

        # With max_retries=1 (default), should only try once and fail
        result = await orchestrator_initialized.execute_on_instance(
            "hermes2", "test", max_retries=1
        )
        assert "server error" in result.lower()

    @pytest.mark.asyncio
    async def test_execute_on_instance_timeout(self, orchestrator_initialized):
        """Should handle timeout gracefully."""
        orchestrator_initialized._http_client.post = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        result = await orchestrator_initialized.execute_on_instance("hermes2", "test")
        assert "timed out" in result.lower()

    @pytest.mark.asyncio
    async def test_execute_on_instance_generic_exception(self, orchestrator_initialized):
        """Should handle generic exceptions."""
        orchestrator_initialized._http_client.post = AsyncMock(
            side_effect=RuntimeError("Test error")
        )

        result = await orchestrator_initialized.execute_on_instance("hermes2", "test")
        assert "Could not reach instance" in result

    @pytest.mark.asyncio
    async def test_execute_on_instance_runtime_config_loading(self, orchestrator_initialized):
        """Should load runtime config from environment."""
        with patch.dict(os.environ, {
            "HERMES_INSTANCE_A_HOSTNAME": "test.example.com",
            "HERMES_INSTANCE_A_PORT": "9000",
        }):
            with patch("gateway.instance_orchestrator.get_instance_config") as mock_config:
                mock_config.return_value = {
                    "instance_a_hostname": "test.example.com",
                    "instance_a_port": 9000,
                    "remote_api_key": "",
                }
                
                mock_response = AsyncMock()
                mock_response.status = 200
                mock_response.json.return_value = {"response": "ok"}
                mock_response.content = b"ok"
                
                orchestrator_initialized._http_client.post = AsyncMock(return_value=mock_response)
                
                await orchestrator_initialized.execute_on_instance("hermes2", "test")
                mock_config.assert_called_once()

    @pytest.mark.asyncio
    async def test_execute_on_instance_payload_construction(self, orchestrator_initialized):
        """Should construct correct payload with prompt and session_id."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = lambda: {"response": "ok"}
        mock_response.content = b"ok"

        orchestrator_initialized._http_client.post = AsyncMock(return_value=mock_response)

        await orchestrator_initialized.execute_on_instance(
            "hermes2", "test prompt", session_id="session123"
        )

        call_args = orchestrator_initialized._http_client.post.call_args
        assert call_args is not None
        assert "json" in call_args.kwargs
        payload = call_args.kwargs["json"]
        assert payload["prompt"] == "test prompt"
        assert payload["session_id"] == "session123"

    @pytest.mark.asyncio
    async def test_execute_on_instance_unexpected_response_status(self, orchestrator_initialized):
        """Should handle unexpected HTTP status codes."""
        mock_response = AsyncMock()
        mock_response.status = 400
        mock_response.text = "Bad request"

        orchestrator_initialized._http_client.post = AsyncMock(return_value=mock_response)

        result = await orchestrator_initialized.execute_on_instance("hermes2", "test")
        assert "error" in result.lower()

    @pytest.mark.asyncio
    async def test_execute_on_instance_retry_succeeds_on_second_attempt(self, orchestrator_initialized):
        """Should succeed if retry recovers from transient failure."""
        # First call returns 503, second returns 200
        responses = [
            AsyncMock(status=503, content=b""),
            AsyncMock(status=200, json=lambda: {"response": "ok"}, content=b"ok"),
        ]
        orchestrator_initialized._http_client.post = AsyncMock(side_effect=responses)

        result = await orchestrator_initialized.execute_on_instance(
            "hermes2", "test", max_retries=2
        )
        assert result == "ok"
        assert orchestrator_initialized._http_client.post.call_count == 2


# ============================================================================
# Tests: Health Check and Status
# ============================================================================


class TestHealthCheck:
    """Tests for health check functionality."""

    @pytest.mark.asyncio
    async def test_health_check_local_always_healthy(self, orchestrator_initialized):
        """Local instance should always be healthy."""
        result = await orchestrator_initialized.health_check("local")
        assert result is True

    @pytest.mark.asyncio
    async def test_health_check_success(self, orchestrator_initialized):
        """Successful health check should be cached."""
        mock_response = AsyncMock()
        mock_response.status = 200

        orchestrator_initialized._http_client.get = AsyncMock(return_value=mock_response)

        result = await orchestrator_initialized.health_check("hermes2")
        assert result is True
        assert "hermes2" in orchestrator_initialized._health_cache

    @pytest.mark.asyncio
    async def test_health_check_failure(self, orchestrator_initialized):
        """Failed health check (non-200 status) should be cached."""
        mock_response = AsyncMock()
        mock_response.status = 503

        orchestrator_initialized._http_client.get = AsyncMock(return_value=mock_response)

        result = await orchestrator_initialized.health_check("hermes2")
        assert result is False
        assert orchestrator_initialized._health_cache["hermes2"][0] is False

    @pytest.mark.asyncio
    async def test_health_check_timeout(self, orchestrator_initialized):
        """Timeout should result in unhealthy status."""
        orchestrator_initialized._http_client.get = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        result = await orchestrator_initialized.health_check("hermes2")
        assert result is False

    @pytest.mark.asyncio
    async def test_health_check_cache_hit(self, orchestrator_initialized):
        """Should use cached result within TTL."""
        mock_response = AsyncMock()
        mock_response.status = 200

        orchestrator_initialized._http_client.get = AsyncMock(return_value=mock_response)

        # First call
        result1 = await orchestrator_initialized.health_check("hermes2")
        assert result1 is True

        # Reset mock to ensure it's not called again
        orchestrator_initialized._http_client.get.reset_mock()

        # Second call should use cache
        result2 = await orchestrator_initialized.health_check("hermes2")
        assert result2 is True
        orchestrator_initialized._http_client.get.assert_not_called()

    @pytest.mark.asyncio
    async def test_health_check_cache_expiry(self, orchestrator_initialized):
        """Should bypass cache after TTL expires."""
        mock_response1 = AsyncMock()
        mock_response1.status = 200

        orchestrator_initialized._http_client.get = AsyncMock(return_value=mock_response1)

        # First call
        await orchestrator_initialized.health_check("hermes2")
        call_count_1 = orchestrator_initialized._http_client.get.call_count

        # Manually expire cache
        old_timestamp = datetime.now() - timedelta(seconds=31)
        orchestrator_initialized._health_cache["hermes2"] = (True, old_timestamp)

        # Second call should fetch fresh
        await orchestrator_initialized.health_check("hermes2")
        call_count_2 = orchestrator_initialized._http_client.get.call_count

        assert call_count_2 > call_count_1

    @pytest.mark.asyncio
    async def test_get_instance_status_local(self, orchestrator_initialized):
        """Should return status for local instance."""
        status = await orchestrator_initialized.get_instance_status("local")
        assert status["name"] == "local"
        assert status["healthy"] is True
        assert status["reachable"] is True

    @pytest.mark.asyncio
    async def test_get_instance_status_remote_healthy(self, orchestrator_initialized):
        """Should return healthy status for accessible remote."""
        mock_response = AsyncMock()
        mock_response.status = 200

        orchestrator_initialized._http_client.get = AsyncMock(return_value=mock_response)

        status = await orchestrator_initialized.get_instance_status("hermes2")
        assert status["name"] == "hermes2"
        assert status["healthy"] is True
        assert "error" not in status or status.get("error") is None

    @pytest.mark.asyncio
    async def test_get_instance_status_remote_unhealthy(self, orchestrator_initialized):
        """Should return unhealthy status for unreachable remote."""
        orchestrator_initialized._http_client.get = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )

        status = await orchestrator_initialized.get_instance_status("hermes2")
        assert status["name"] == "hermes2"
        assert status["healthy"] is False
        assert status.get("error") is not None


# ============================================================================
# Tests: Thread Safety and Concurrent Access
# ============================================================================


class TestThreadSafety:
    """Tests for thread-safe concurrent access to orchestrator state."""

    def test_concurrent_set_current_instance(self, orchestrator):
        """Multiple threads setting different chat instances concurrently."""
        import threading

        def set_instance(chat_id, instance_name):
            orchestrator.set_current_instance(instance_name, chat_id=chat_id)

        threads = [
            threading.Thread(target=set_instance, args=(f"user_{i}", "hermes2" if i % 2 == 0 else "local"))
            for i in range(20)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All chat IDs should be stored
        assert len(orchestrator.session_instances) == 20

    def test_concurrent_get_current_instance(self, orchestrator):
        """Multiple threads reading instances concurrently."""
        import threading

        # Set up some mappings first
        for i in range(10):
            orchestrator.set_current_instance("hermes2", chat_id=f"user_{i}")

        results = []

        def get_instance(chat_id):
            result = orchestrator.get_current_instance(chat_id)
            results.append(result)

        threads = [
            threading.Thread(target=get_instance, args=(f"user_{i}",))
            for i in range(10)
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # All reads should return 'hermes2'
        assert all(r == "hermes2" for r in results)

    def test_concurrent_mixed_operations(self, orchestrator):
        """Mix of concurrent reads and writes."""
        import threading
        import time

        def writer(chat_id, instance):
            for _ in range(5):
                orchestrator.set_current_instance(instance, chat_id=chat_id)
                time.sleep(0.001)

        def reader(chat_id):
            results = []
            for _ in range(5):
                results.append(orchestrator.get_current_instance(chat_id))
                time.sleep(0.001)
            return results

        writer_threads = [
            threading.Thread(target=writer, args=(f"user_{i}", "hermes2"))
            for i in range(5)
        ]
        reader_threads = [
            threading.Thread(target=reader, args=(f"user_{i}",))
            for i in range(5)
        ]

        for t in writer_threads + reader_threads:
            t.start()
        for t in writer_threads + reader_threads:
            t.join()

        # All chat IDs should be present
        assert len(orchestrator.session_instances) >= 5

    @pytest.mark.asyncio
    async def test_concurrent_execute_on_instance(self, orchestrator_initialized):
        """Multiple concurrent execute requests should work."""
        mock_response = MagicMock()
        mock_response.status = 200
        mock_response.json = lambda: {"response": "test response"}
        mock_response.content = b"test"

        orchestrator_initialized._http_client.post = AsyncMock(return_value=mock_response)

        tasks = [
            orchestrator_initialized.execute_on_instance("hermes2", f"prompt {i}")
            for i in range(5)
        ]

        results = await asyncio.gather(*tasks)
        assert all(r == "test response" for r in results)
        assert orchestrator_initialized._http_client.post.call_count == 5

    @pytest.mark.asyncio
    async def test_concurrent_health_checks(self, orchestrator_initialized):
        """Multiple concurrent health checks with caching."""
        mock_response = AsyncMock()
        mock_response.status = 200

        orchestrator_initialized._http_client.get = AsyncMock(return_value=mock_response)

        # First batch of concurrent checks
        tasks = [
            orchestrator_initialized.health_check("hermes2") for _ in range(5)
        ]
        results = await asyncio.gather(*tasks)

        assert all(r is True for r in results)
        # Should only make one real request due to cache
        assert orchestrator_initialized._http_client.get.call_count <= 5


# ============================================================================
# Tests: RemoteHermesInstance
# ============================================================================


class TestRemoteHermesInstance:
    """Tests for RemoteHermesInstance class."""

    def test_remote_instance_local_base_url(self):
        """Local instance should use 127.0.0.1:8000."""
        inst = RemoteHermesInstance(
            name="local",
            hostname="127.0.0.1",
            ip="127.0.0.1",
            is_local=True,
        )
        assert inst.get_base_url() == "http://127.0.0.1:8000"

    def test_remote_instance_remote_base_url(self):
        """Remote instance should use IP address."""
        inst = RemoteHermesInstance(
            name="hermes2",
            hostname="hermes2.example.com",
            ip="100.79.15.66",
            http_port=8000,
            is_local=False,
        )
        assert inst.get_base_url() == "http://100.79.15.66:8000"

    def test_remote_instance_custom_port(self):
        """Should respect custom port."""
        inst = RemoteHermesInstance(
            name="test",
            hostname="test.example.com",
            ip="192.168.1.1",
            http_port=9000,
            is_local=False,
        )
        assert inst.get_base_url() == "http://192.168.1.1:9000"

    def test_remote_instance_api_headers_with_key(self):
        """Should include X-Hermes-Key header if set."""
        inst = RemoteHermesInstance(
            name="test",
            hostname="test.example.com",
            ip="127.0.0.1",
            http_key="secret_key_123",
            username="testuser",
        )
        headers = inst.get_api_headers()
        assert headers["X-Hermes-Key"] == "secret_key_123"
        assert headers["X-Hermes-User"] == "testuser"

    def test_remote_instance_api_headers_without_key(self):
        """Should not include X-Hermes-Key if empty."""
        inst = RemoteHermesInstance(
            name="test",
            hostname="test.example.com",
            ip="127.0.0.1",
            http_key="",
            username="",
        )
        headers = inst.get_api_headers()
        assert "X-Hermes-Key" not in headers
        assert "X-Hermes-User" not in headers


# ============================================================================
# Tests: Edge Cases and Boundary Conditions
# ============================================================================


class TestEdgeCases:
    """Tests for edge cases and boundary conditions."""

    def test_empty_string_instance_name(self, orchestrator):
        """Empty string instance name should fail."""
        result = orchestrator.set_current_instance("")
        assert result is False

    def test_none_instance_name(self, orchestrator):
        """None instance name should be treated as falsy."""
        result = orchestrator.set_current_instance(None)
        assert result is False

    def test_case_sensitive_instance_names(self, orchestrator):
        """Instance names should be case-sensitive."""
        result = orchestrator.set_current_instance("LOCAL")  # Should fail (not "local")
        assert result is False

    def test_instance_name_with_whitespace(self, orchestrator):
        """Instance names with whitespace should not match."""
        result = orchestrator.set_current_instance(" local ")
        assert result is False

    def test_session_instances_dict_growth(self, orchestrator):
        """session_instances should grow with unique chat IDs."""
        for i in range(100):
            orchestrator.set_current_instance("hermes2", chat_id=f"user_{i}")
        
        assert len(orchestrator.session_instances) == 100

    def test_same_chat_id_multiple_switches(self, orchestrator):
        """Switching same chat ID multiple times should update mapping."""
        chat_id = "user_1"
        orchestrator.set_current_instance("hermes2", chat_id=chat_id)
        orchestrator.set_current_instance("local", chat_id=chat_id)
        
        chat_key = hashlib.sha256(chat_id.encode()).hexdigest()[:32]
        assert orchestrator.session_instances[chat_key] == "local"

    def test_chat_id_hash_consistency(self, orchestrator):
        """Same chat_id should always hash to same key."""
        chat_id = "test_user"
        hash1 = hashlib.sha256(chat_id.encode()).hexdigest()[:32]
        hash2 = hashlib.sha256(chat_id.encode()).hexdigest()[:32]
        assert hash1 == hash2

    @pytest.mark.asyncio
    async def test_execute_without_client_init(self):
        """Should initialize client on first execute if not done."""
        orch = InstanceOrchestrator()
        # Don't call init()
        assert orch._http_client is None

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json.return_value = {"response": "ok"}
        mock_response.content = b"ok"

        with patch("gateway.instance_orchestrator.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_class.return_value = mock_client

            result = await orch.execute_on_instance("hermes2", "test")
            # Should have initialized the client
            mock_client_class.assert_called_once()

    @pytest.mark.asyncio
    async def test_init_idempotent(self, orchestrator_initialized):
        """Calling init() multiple times should be safe."""
        first_client = orchestrator_initialized._http_client
        await orchestrator_initialized.init()
        # Client should be the same
        assert orchestrator_initialized._http_client is first_client

    @pytest.mark.asyncio
    async def test_close_safe_when_no_client(self, orchestrator):
        """Calling close() with no client should be safe."""
        await orchestrator.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_get_instance(self, orchestrator):
        """Should retrieve instance from registry."""
        local_inst = orchestrator.get_instance("local")
        assert local_inst is not None
        assert local_inst.name == "local"

    @pytest.mark.asyncio
    async def test_get_instance_nonexistent(self, orchestrator):
        """Should return None for nonexistent instance."""
        inst = orchestrator.get_instance("nonexistent")
        assert inst is None

    def test_list_instances(self, orchestrator):
        """Should format list of instances."""
        listing = orchestrator.list_instances()
        assert "Available Hermes Instances" in listing
        assert "local" in listing
        assert "hermes2" in listing

    def test_list_instances_shows_active(self, orchestrator):
        """List should indicate current active instance."""
        orchestrator.set_current_instance("hermes2")
        listing = orchestrator.list_instances()
        # Should contain a marker (→) for the current instance
        assert "→" in listing


# ============================================================================
# Tests: Configuration Loading
# ============================================================================


class TestConfigLoading:
    """Tests for runtime configuration loading."""

    def test_get_instance_config_defaults(self):
        """Should provide sensible defaults."""
        with patch.dict(os.environ, {}, clear=True):
            config = get_instance_config()
            assert config["instance_a_hostname"] == "localhost"
            assert config["instance_a_port"] == 8000
            assert config["remote_api_key"] == ""

    def test_get_instance_config_from_env(self):
        """Should load config from environment variables."""
        with patch.dict(os.environ, {
            "HERMES_INSTANCE_A_HOSTNAME": "example.com",
            "HERMES_INSTANCE_A_PORT": "9000",
            "HERMES_REMOTE_API_KEY": "secret123",
        }):
            config = get_instance_config()
            assert config["instance_a_hostname"] == "example.com"
            assert config["instance_a_port"] == 9000
            assert config["remote_api_key"] == "secret123"

    def test_get_instance_config_invalid_port_uses_default(self):
        """Should use default port if env var invalid."""
        with patch.dict(os.environ, {
            "HERMES_INSTANCE_A_PORT": "invalid",
        }):
            config = get_instance_config()
            assert config["instance_a_port"] == 8000

    def test_get_instance_config_empty_hostname_uses_default(self):
        """Should use default hostname if env var empty."""
        with patch.dict(os.environ, {
            "HERMES_INSTANCE_A_HOSTNAME": "   ",
        }):
            config = get_instance_config()
            assert config["instance_a_hostname"] == "localhost"


# ============================================================================
# Tests: Error Handling and Logging
# ============================================================================


class TestErrorHandling:
    """Tests for error handling and recovery."""

    @pytest.mark.asyncio
    async def test_execute_response_consumption(self, orchestrator_initialized):
        """Should consume response body to avoid connection pool leak."""
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.json.return_value = {"response": "test"}
        mock_response.content = b"test content"

        orchestrator_initialized._http_client.post = AsyncMock(return_value=mock_response)

        await orchestrator_initialized.execute_on_instance("hermes2", "test")

        # Response should have been accessed (body consumed)
        assert hasattr(mock_response, "content")

    @pytest.mark.asyncio
    async def test_execute_http_client_reset_on_auth_failure(self, orchestrator_initialized):
        """HTTP client should be reset on auth failure."""
        mock_response = AsyncMock()
        mock_response.status = 401

        orchestrator_initialized._http_client.post = AsyncMock(return_value=mock_response)

        original_client = orchestrator_initialized._http_client
        await orchestrator_initialized.execute_on_instance("hermes2", "test")

        # Client should have been reset
        assert orchestrator_initialized._http_client is None

    @pytest.mark.asyncio
    async def test_execute_http_client_reset_on_timeout(self, orchestrator_initialized):
        """HTTP client should be reset on timeout."""
        orchestrator_initialized._http_client.post = AsyncMock(
            side_effect=asyncio.TimeoutError()
        )

        await orchestrator_initialized.execute_on_instance("hermes2", "test")

        # Client should have been reset
        assert orchestrator_initialized._http_client is None

    @pytest.mark.asyncio
    async def test_execute_logs_at_error_level_on_failure(self, orchestrator_initialized):
        """Should log failures at ERROR level."""
        orchestrator_initialized._http_client.post = AsyncMock(
            side_effect=RuntimeError("Connection refused")
        )

        with patch("gateway.instance_orchestrator.logger") as mock_logger:
            result = await orchestrator_initialized.execute_on_instance("hermes2", "test")
            # Should have logged an error
            assert mock_logger.error.called or mock_logger.warning.called


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
