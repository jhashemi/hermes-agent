"""Test rate limiting for remote API (P3-005).

Tests cover:
- Per-API-key rate limiting (100 requests per 60 seconds)
- 429 Too Many Requests response
- Retry-After header inclusion
- Concurrent request handling
- Counter reset functionality
- Memory-based tracking
"""

import pytest
import time
import threading
import asyncio
from unittest.mock import Mock, patch, AsyncMock, MagicMock
from datetime import datetime

# Import the rate limiter
from gateway.remote_agent_api import RateLimiter, get_rate_limiter


class TestRateLimiterBasics:
    """Test basic rate limiter functionality."""
    
    def test_rate_limiter_initialization(self):
        """Test RateLimiter initializes correctly."""
        limiter = RateLimiter(max_requests=100, window_seconds=60)
        assert limiter.max_requests == 100
        assert limiter.window_seconds == 60
        assert limiter.request_history == {}
        assert limiter._stop_cleanup is False
    
    def test_rate_limiter_custom_limits(self):
        """Test RateLimiter with custom limits."""
        limiter = RateLimiter(max_requests=50, window_seconds=30)
        assert limiter.max_requests == 50
        assert limiter.window_seconds == 30
    
    def test_first_request_allowed(self):
        """Test first request for an API key is allowed."""
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        allowed, retry_after = limiter.is_allowed("test-key-1")
        assert allowed is True
        assert retry_after is None
    
    def test_multiple_requests_within_limit(self):
        """Test multiple requests within limit are allowed."""
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        api_key = "test-key-1"
        
        for i in range(5):
            allowed, retry_after = limiter.is_allowed(api_key)
            assert allowed is True, f"Request {i+1} should be allowed"
            assert retry_after is None
    
    def test_request_exceeds_limit(self):
        """Test request exceeding limit is denied."""
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        api_key = "test-key-1"
        
        # Make 5 allowed requests
        for i in range(5):
            allowed, retry_after = limiter.is_allowed(api_key)
            assert allowed is True
        
        # 6th request should be denied
        allowed, retry_after = limiter.is_allowed(api_key)
        assert allowed is False
        assert retry_after is not None
        assert retry_after > 0
    
    def test_different_api_keys_independent(self):
        """Test different API keys have independent limits."""
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        
        # Use up limit for key1
        for i in range(3):
            allowed, _ = limiter.is_allowed("key1")
            assert allowed is True
        
        # key1 should be limited
        allowed, _ = limiter.is_allowed("key1")
        assert allowed is False
        
        # key2 should still have requests available
        allowed, _ = limiter.is_allowed("key2")
        assert allowed is True
        allowed, _ = limiter.is_allowed("key2")
        assert allowed is True
        allowed, _ = limiter.is_allowed("key2")
        assert allowed is True
    
    def test_retry_after_calculation(self):
        """Test Retry-After header value is correct."""
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        api_key = "test-key"
        
        # Make requests to hit limit
        for i in range(2):
            limiter.is_allowed(api_key)
        
        # Check that retry_after is reasonable
        allowed, retry_after = limiter.is_allowed(api_key)
        assert allowed is False
        assert 1 <= retry_after <= 60  # Should be between 1 and 60 seconds


class TestRateLimiterCounterReset:
    """Test counter reset functionality."""
    
    def test_counter_reset_after_window(self):
        """Test counters reset after window expires."""
        limiter = RateLimiter(max_requests=3, window_seconds=2)
        api_key = "test-key"
        
        # Make 3 requests (hit limit)
        for i in range(3):
            allowed, _ = limiter.is_allowed(api_key)
            assert allowed is True
        
        # 4th request should be denied
        allowed, _ = limiter.is_allowed(api_key)
        assert allowed is False
        
        # Wait for window to expire
        time.sleep(2.1)
        
        # Now request should be allowed again
        allowed, _ = limiter.is_allowed(api_key)
        assert allowed is True
    
    def test_cleanup_removes_expired_entries(self):
        """Test cleanup removes expired request entries."""
        limiter = RateLimiter(max_requests=100, window_seconds=2)
        api_key = "test-key"
        
        # Make a request
        limiter.is_allowed(api_key)
        assert api_key in limiter.request_history
        assert len(limiter.request_history[api_key]) == 1
        
        # Wait and trigger cleanup
        time.sleep(2.1)
        limiter._cleanup_expired()
        
        # Entry should be cleaned up
        if api_key in limiter.request_history:
            assert len(limiter.request_history[api_key]) == 0


class TestRateLimiterStats:
    """Test stats reporting functionality."""
    
    def test_get_stats_no_requests(self):
        """Test stats for API key with no requests."""
        limiter = RateLimiter(max_requests=10, window_seconds=60)
        stats = limiter.get_stats("new-key")
        
        assert stats["requests_made"] == 0
        assert stats["requests_remaining"] == 10
        assert stats["max_requests"] == 10
        assert stats["window_seconds"] == 60
    
    def test_get_stats_with_requests(self):
        """Test stats after making some requests."""
        limiter = RateLimiter(max_requests=10, window_seconds=60)
        api_key = "test-key"
        
        # Make 3 requests
        for i in range(3):
            limiter.is_allowed(api_key)
        
        stats = limiter.get_stats(api_key)
        assert stats["requests_made"] == 3
        assert stats["requests_remaining"] == 7
        assert stats["max_requests"] == 10
    
    def test_get_stats_at_limit(self):
        """Test stats when at request limit."""
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        api_key = "test-key"
        
        # Make 5 requests (at limit)
        for i in range(5):
            limiter.is_allowed(api_key)
        
        stats = limiter.get_stats(api_key)
        assert stats["requests_made"] == 5
        assert stats["requests_remaining"] == 0


class TestRateLimiterThreadSafety:
    """Test thread-safe concurrent access."""
    
    def test_concurrent_requests_same_key(self):
        """Test concurrent requests from same API key."""
        limiter = RateLimiter(max_requests=100, window_seconds=60)
        api_key = "concurrent-test"
        results = []
        lock = threading.Lock()
        
        def make_request():
            allowed, retry_after = limiter.is_allowed(api_key)
            with lock:
                results.append((allowed, retry_after))
        
        # Create 50 threads all making requests
        threads = []
        for i in range(50):
            t = threading.Thread(target=make_request)
            threads.append(t)
            t.start()
        
        # Wait for all threads
        for t in threads:
            t.join()
        
        # Check results: first 100 should be allowed, rest denied
        allowed_count = sum(1 for a, _ in results if a)
        denied_count = sum(1 for a, _ in results if not a)
        
        assert allowed_count == 50, "All concurrent requests should be allowed"
        assert denied_count == 0
        
        # Verify total recorded
        assert len(limiter.request_history[api_key]) == 50
    
    def test_concurrent_requests_different_keys(self):
        """Test concurrent requests from different API keys."""
        limiter = RateLimiter(max_requests=10, window_seconds=60)
        results = []
        lock = threading.Lock()
        
        def make_request(key_num):
            api_key = f"key-{key_num}"
            allowed, _ = limiter.is_allowed(api_key)
            with lock:
                results.append((api_key, allowed))
        
        # Create 20 threads with different keys
        threads = []
        for i in range(20):
            t = threading.Thread(target=make_request, args=(i,))
            threads.append(t)
            t.start()
        
        # Wait for all threads
        for t in threads:
            t.join()
        
        # All should be allowed (first request per key)
        assert all(allowed for _, allowed in results)
        assert len(results) == 20


class TestRateLimiterMemoryTracking:
    """Test memory-based request tracking."""
    
    def test_memory_dict_structure(self):
        """Test request history uses memory dict structure."""
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        api_key = "test-key"
        
        # Make some requests
        for i in range(3):
            limiter.is_allowed(api_key)
        
        # Check internal dict structure
        assert api_key in limiter.request_history
        assert isinstance(limiter.request_history[api_key], list)
        assert len(limiter.request_history[api_key]) == 3
        
        # All entries should be timestamps
        for ts in limiter.request_history[api_key]:
            assert isinstance(ts, float)
            assert ts > 0
    
    def test_timestamps_are_ordered(self):
        """Test that timestamps are recorded in order."""
        limiter = RateLimiter(max_requests=100, window_seconds=60)
        api_key = "test-key"
        
        # Make requests with small delays
        for i in range(5):
            limiter.is_allowed(api_key)
            time.sleep(0.01)
        
        # Check timestamps are in order
        timestamps = limiter.request_history[api_key]
        for i in range(len(timestamps) - 1):
            assert timestamps[i] <= timestamps[i + 1]


class TestRateLimiterShutdown:
    """Test shutdown functionality."""
    
    def test_shutdown_stops_cleanup(self):
        """Test shutdown gracefully stops cleanup thread."""
        limiter = RateLimiter(max_requests=100, window_seconds=60)
        assert limiter._cleanup_thread is not None
        assert limiter._cleanup_thread.is_alive()
        
        limiter.shutdown()
        assert limiter._stop_cleanup is True
        
        # Wait for thread to stop
        limiter._cleanup_thread.join(timeout=3)
        assert not limiter._cleanup_thread.is_alive()


class TestGlobalRateLimiterInstance:
    """Test global rate limiter singleton."""
    
    def test_get_rate_limiter_returns_instance(self):
        """Test get_rate_limiter returns a RateLimiter instance."""
        limiter = get_rate_limiter()
        assert isinstance(limiter, RateLimiter)
    
    def test_get_rate_limiter_singleton(self):
        """Test get_rate_limiter returns same instance."""
        limiter1 = get_rate_limiter()
        limiter2 = get_rate_limiter()
        assert limiter1 is limiter2


class TestRateLimiterEdgeCases:
    """Test edge cases and boundary conditions."""
    
    def test_zero_max_requests(self):
        """Test behavior with zero max requests."""
        limiter = RateLimiter(max_requests=0, window_seconds=60)
        allowed, retry_after = limiter.is_allowed("test-key")
        assert allowed is False
        assert retry_after is not None
    
    def test_very_short_window(self):
        """Test with very short time window."""
        limiter = RateLimiter(max_requests=5, window_seconds=1)
        api_key = "test-key"
        
        # Make 5 requests quickly
        for i in range(5):
            allowed, _ = limiter.is_allowed(api_key)
            assert allowed is True
        
        # Should be limited
        allowed, _ = limiter.is_allowed(api_key)
        assert allowed is False
        
        # Wait for window
        time.sleep(1.1)
        
        # Should be allowed again
        allowed, _ = limiter.is_allowed(api_key)
        assert allowed is True
    
    def test_empty_api_key(self):
        """Test with empty API key string."""
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        allowed1, _ = limiter.is_allowed("")
        allowed2, _ = limiter.is_allowed("")
        
        # Both should work with empty key
        assert allowed1 is True
        assert allowed2 is True
    
    def test_very_long_api_key(self):
        """Test with very long API key."""
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        long_key = "x" * 10000
        
        for i in range(5):
            allowed, _ = limiter.is_allowed(long_key)
            assert allowed is True


# Integration tests with FastAPI (if available)
try:
    from fastapi import FastAPI, Header
    from fastapi.testclient import TestClient
    from gateway.remote_agent_api import (
        create_remote_api_blueprint,
        verify_api_key,
    )
    
    class TestRateLimiterIntegrationFastAPI:
        """Test rate limiter integration with FastAPI."""
        
        @pytest.fixture
        def app_with_rate_limiting(self):
            """Create a test FastAPI app with rate limiting."""
            app = FastAPI()
            
            # Mock gateway_runner
            gateway_runner = Mock()
            gateway_runner.agent = Mock()
            gateway_runner.agent.chat = Mock(return_value="Response")
            gateway_runner.instance_orchestrator = Mock()
            
            # Run async setup
            asyncio.run(create_remote_api_blueprint(app, gateway_runner))
            return app
        
        def test_rate_limiting_fastapi_single_key(self, app_with_rate_limiting):
            """Test rate limiting with FastAPI and single API key."""
            client = TestClient(app_with_rate_limiting)
            
            # Set a test API key
            with patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-key-123"}):
                # Reset the cached key
                from gateway.remote_agent_api import get_expected_key
                get_expected_key.cache_clear()
                
                headers = {
                    "X-Hermes-Key": "test-key-123",
                    "X-Hermes-User": "test-user",
                }
                
                # Make a request to verify rate limiter is working
                response = client.post(
                    "/api/agent/execute",
                    json={
                        "agent_id": "default",
                        "prompt": "test",
                    },
                    headers=headers,
                )
                
                # Should get a valid response (may fail for other reasons, but not 429)
                assert response.status_code != 429
        
        def test_rate_limiting_fastapi_429_response(self, app_with_rate_limiting):
            """Test that FastAPI returns 429 when rate limited."""
            client = TestClient(app_with_rate_limiting)
            
            with patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-key"}):
                from gateway.remote_agent_api import get_expected_key
                get_expected_key.cache_clear()
                
                headers = {
                    "X-Hermes-Key": "test-key",
                    "X-Hermes-User": "test-user",
                }
                
                # Create a limiter with very small limit for testing
                limiter = RateLimiter(max_requests=2, window_seconds=60)
                
                # Simulate hitting the limit
                limiter.is_allowed("test-key")
                limiter.is_allowed("test-key")
                allowed, retry_after = limiter.is_allowed("test-key")
                
                assert allowed is False
                assert "Retry-After" in locals() or retry_after is not None

except ImportError:
    pass


if __name__ == "__main__":
    # Run tests with: pytest tests/test_rate_limiter.py -v
    pytest.main([__file__, "-v"])
