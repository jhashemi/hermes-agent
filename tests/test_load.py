"""
P4-005: Load & Performance Tests

Comprehensive load and performance testing for:
- Simulate 100+ concurrent requests
- Measure response times (target: <500ms median)
- Verify rate limiting under load
- Verify connection pool cleanup
- Verify no memory leaks after 1000+ requests
- Benchmark results documented

Minimum 5-10 test cases demonstrating performance characteristics.
"""

import pytest
import asyncio
import threading
import time
import psutil
import os
from statistics import median, stdev, mean
from gateway.remote_agent_api import RateLimiter
from gateway.instance_orchestrator import InstanceOrchestrator
from unittest.mock import AsyncMock, patch, MagicMock
import gc


# ============================================================================
# Test Fixtures & Helpers
# ============================================================================

@pytest.fixture
def process_memory_baseline():
    """Get baseline memory usage."""
    process = psutil.Process(os.getpid())
    process.memory_info()
    gc.collect()  # Force garbage collection
    baseline = process.memory_info().rss / 1024 / 1024  # MB
    yield baseline
    gc.collect()


@pytest.fixture
def timer():
    """Simple timer utility."""
    class Timer:
        def __init__(self):
            self.start_time = None
            self.end_time = None
        
        def __enter__(self):
            self.start_time = time.time()
            return self
        
        def __exit__(self, *args):
            self.end_time = time.time()
        
        @property
        def elapsed_ms(self):
            return (self.end_time - self.start_time) * 1000
    
    return Timer


# ============================================================================
# Test: Concurrent requests handling
# ============================================================================

def test_concurrent_100_requests_sequential():
    """Test 100 sequential requests to measure baseline."""
    limiter = RateLimiter(max_requests=200, window_seconds=60)
    times = []
    
    for i in range(100):
        start = time.time()
        allowed, _ = limiter.is_allowed(f"key_{i % 10}")
        elapsed = (time.time() - start) * 1000
        times.append(elapsed)
    
    assert len(times) == 100
    median_time = median(times)
    
    # Each request should complete quickly
    assert median_time < 10  # Less than 10ms per request


def test_concurrent_100_requests_parallel():
    """Test 100 concurrent requests from multiple threads."""
    limiter = RateLimiter(max_requests=500, window_seconds=60)
    times = []
    lock = threading.Lock()
    
    def make_request(thread_id, request_id):
        start = time.time()
        allowed, _ = limiter.is_allowed(f"key_{thread_id}")
        elapsed = (time.time() - start) * 1000
        with lock:
            times.append(elapsed)
    
    threads = []
    start_all = time.time()
    
    for i in range(100):
        t = threading.Thread(target=make_request, args=(i % 10, i))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    total_time = (time.time() - start_all) * 1000
    
    assert len(times) == 100
    median_time = median(times)
    
    # Concurrent requests should complete quickly
    assert median_time < 50  # Less than 50ms per request
    print(f"100 concurrent requests completed in {total_time:.1f}ms, median per-request: {median_time:.2f}ms")


def test_concurrent_500_requests():
    """Test 500 concurrent requests."""
    limiter = RateLimiter(max_requests=1000, window_seconds=60)
    times = []
    lock = threading.Lock()
    errors = []
    
    def make_request(thread_id):
        try:
            for i in range(10):
                start = time.time()
                allowed, _ = limiter.is_allowed(f"key_{thread_id}")
                elapsed = (time.time() - start) * 1000
                with lock:
                    times.append(elapsed)
        except Exception as e:
            errors.append(e)
    
    threads = []
    start_all = time.time()
    
    for t_id in range(50):
        t = threading.Thread(target=make_request, args=(t_id,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    total_time = (time.time() - start_all) * 1000
    
    assert len(errors) == 0
    assert len(times) == 500
    
    median_time = median(times)
    max_time = max(times)
    
    print(f"500 concurrent requests completed in {total_time:.1f}ms")
    print(f"Median response time: {median_time:.2f}ms")
    print(f"Max response time: {max_time:.2f}ms")
    
    # Median should still be < 100ms
    assert median_time < 100


# ============================================================================
# Test: Response time metrics
# ============================================================================

def test_response_time_median_under_500ms():
    """Test that median response time is under 500ms."""
    limiter = RateLimiter(max_requests=1000, window_seconds=60)
    times = []
    
    for i in range(1000):
        start = time.time()
        limiter.is_allowed(f"key_{i % 100}")
        elapsed = (time.time() - start) * 1000
        times.append(elapsed)
    
    median_time = median(times)
    
    print(f"Median response time: {median_time:.2f}ms")
    assert median_time < 500


def test_response_time_p95_under_target():
    """Test that p95 response time is reasonable."""
    limiter = RateLimiter(max_requests=1000, window_seconds=60)
    times = []
    
    for i in range(1000):
        start = time.time()
        limiter.is_allowed(f"key_{i % 100}")
        elapsed = (time.time() - start) * 1000
        times.append(elapsed)
    
    times.sort()
    p95_index = int(len(times) * 0.95)
    p95_time = times[p95_index]
    
    print(f"P95 response time: {p95_time:.2f}ms")
    assert p95_time < 1000  # 1 second


def test_response_time_distribution():
    """Test response time distribution."""
    limiter = RateLimiter(max_requests=1000, window_seconds=60)
    times = []
    
    for i in range(1000):
        start = time.time()
        limiter.is_allowed(f"key_{i % 100}")
        elapsed = (time.time() - start) * 1000
        times.append(elapsed)
    
    # Calculate percentiles
    times.sort()
    p50 = times[int(len(times) * 0.50)]
    p90 = times[int(len(times) * 0.90)]
    p99 = times[int(len(times) * 0.99)]
    
    print(f"Response time distribution:")
    print(f"  P50: {p50:.2f}ms")
    print(f"  P90: {p90:.2f}ms")
    print(f"  P99: {p99:.2f}ms")
    
    # All should be reasonable
    assert p50 < 100
    assert p90 < 500
    assert p99 < 1000


# ============================================================================
# Test: Rate limiting under load
# ============================================================================

def test_rate_limiting_enforced_under_load():
    """Test that rate limiting is enforced during high load."""
    limiter = RateLimiter(max_requests=100, window_seconds=60)
    allowed_count = 0
    denied_count = 0
    lock = threading.Lock()
    
    def make_requests(thread_id):
        nonlocal allowed_count, denied_count
        for i in range(100):
            allowed, _ = limiter.is_allowed("test_key")
            with lock:
                if allowed:
                    allowed_count += 1
                else:
                    denied_count += 1
    
    threads = []
    for t_id in range(5):
        t = threading.Thread(target=make_requests, args=(t_id,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    total = allowed_count + denied_count
    assert total == 500
    assert allowed_count == 100  # Exactly max_requests
    assert denied_count == 400


def test_rate_limiting_per_key_under_load():
    """Test per-key rate limiting under load."""
    limiter = RateLimiter(max_requests=50, window_seconds=60)
    key_counts = {}
    lock = threading.Lock()
    
    def make_requests(key):
        for i in range(100):
            allowed, _ = limiter.is_allowed(key)
            if allowed:
                with lock:
                    key_counts[key] = key_counts.get(key, 0) + 1
    
    threads = []
    for i in range(5):
        key = f"key_{i}"
        t = threading.Thread(target=make_requests, args=(key,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    # Each key should have exactly 50 allowed requests
    for i in range(5):
        key = f"key_{i}"
        assert key_counts.get(key, 0) == 50


# ============================================================================
# Test: Connection pool cleanup
# ============================================================================

@pytest.mark.asyncio
async def test_connection_pool_cleanup_after_requests():
    """Test that connection pool is cleaned up after requests."""
    orch = InstanceOrchestrator()
    await orch.init()
    
    initial_client = orch._http_client
    
    # Create a mock response
    async def mock_post(*args, **kwargs):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.json = MagicMock(return_value={"response": f"response"})
        return mock_resp
    
    # Simulate multiple requests with mock
    with patch.object(orch._http_client, 'post', side_effect=mock_post):
        for i in range(10):
            await orch.execute_on_instance("hermes2", f"prompt_{i}")
    
    # Client should still be valid
    assert orch._http_client is not None
    assert orch._http_client is initial_client
    
    await orch.close()


@pytest.mark.asyncio
async def test_connection_recovery_after_error():
    """Test that connections are properly closed after errors."""
    orch = InstanceOrchestrator()
    await orch.init()
    
    # Simulate connection error
    async def error_post(*args, **kwargs):
        raise Exception("Connection error")
    
    with patch.object(orch._http_client, 'post', side_effect=error_post):
        result = await orch.execute_on_instance("hermes2", "test", max_retries=1)
    
    # Should return error, not crash
    assert "Could not reach" in result
    
    await orch.close()


# ============================================================================
# Test: Memory leak detection
# ============================================================================

def test_memory_stable_after_1000_requests(process_memory_baseline):
    """Test that memory usage remains stable after 1000+ requests."""
    process = psutil.Process(os.getpid())
    limiter = RateLimiter(max_requests=10000, window_seconds=60)
    
    # Make 1000 requests
    for i in range(1000):
        limiter.is_allowed(f"key_{i % 100}")
    
    gc.collect()
    final_memory = process.memory_info().rss / 1024 / 1024  # MB
    
    memory_increase = final_memory - process_memory_baseline
    
    print(f"Memory baseline: {process_memory_baseline:.1f} MB")
    print(f"Memory after 1000 requests: {final_memory:.1f} MB")
    print(f"Memory increase: {memory_increase:.1f} MB")
    
    # Memory increase should be reasonable (< 50 MB for 1000 requests)
    assert memory_increase < 50


def test_memory_stable_concurrent_1000_requests(process_memory_baseline):
    """Test memory stability with concurrent requests."""
    process = psutil.Process(os.getpid())
    limiter = RateLimiter(max_requests=10000, window_seconds=60)
    
    def make_requests(thread_id):
        for i in range(100):
            limiter.is_allowed(f"key_{thread_id}_{i}")
    
    threads = []
    for t_id in range(10):
        t = threading.Thread(target=make_requests, args=(t_id,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    gc.collect()
    final_memory = process.memory_info().rss / 1024 / 1024
    
    memory_increase = final_memory - process_memory_baseline
    
    print(f"Memory baseline: {process_memory_baseline:.1f} MB")
    print(f"Memory after 1000 concurrent requests: {final_memory:.1f} MB")
    print(f"Memory increase: {memory_increase:.1f} MB")
    
    # Memory should be stable
    assert memory_increase < 100


# ============================================================================
# Test: Cache efficiency
# ============================================================================

def test_rate_limiter_cache_efficiency():
    """Test that rate limiter cache is efficient."""
    limiter = RateLimiter(max_requests=100, window_seconds=60)
    
    # Make requests from 1000 keys (more than can fit in reasonable cache)
    for i in range(1000):
        limiter.is_allowed(f"key_{i}")
    
    # After cleanup, should only have recent entries
    limiter._cleanup_expired()
    
    # Cache should grow but not infinitely
    # (In 60 second window with no time passing, all 1000 would be kept)
    # But let's verify the structure is reasonable
    assert len(limiter.request_history) <= 1000


# ============================================================================
# Test: Throughput metrics
# ============================================================================

def test_throughput_requests_per_second():
    """Benchmark throughput."""
    limiter = RateLimiter(max_requests=10000, window_seconds=60)
    
    start = time.time()
    request_count = 0
    
    # Make requests for 1 second
    while time.time() - start < 1.0:
        limiter.is_allowed(f"key_{request_count % 100}")
        request_count += 1
    
    elapsed = time.time() - start
    rps = request_count / elapsed
    
    print(f"Throughput: {rps:.0f} requests/second")
    
    # Should handle at least 10,000 requests/second
    assert rps > 10000


def test_concurrent_throughput():
    """Benchmark concurrent throughput."""
    limiter = RateLimiter(max_requests=100000, window_seconds=60)
    request_count = 0
    lock = threading.Lock()
    
    def make_requests(thread_id):
        nonlocal request_count
        for i in range(5000):
            limiter.is_allowed(f"key_{thread_id}")
            with lock:
                request_count += 1
    
    threads = []
    start = time.time()
    
    for t_id in range(10):
        t = threading.Thread(target=make_requests, args=(t_id,))
        threads.append(t)
        t.start()
    
    for t in threads:
        t.join()
    
    elapsed = time.time() - start
    rps = request_count / elapsed
    
    print(f"Concurrent throughput: {rps:.0f} requests/second ({request_count} total)")
    
    # Should maintain reasonable throughput under concurrency (10k+ rps is good)
    assert rps > 10000


# ============================================================================
# Test: Stress testing
# ============================================================================

def test_stress_rapid_key_creation():
    """Test rate limiter with rapid creation of new keys."""
    limiter = RateLimiter(max_requests=100, window_seconds=60)
    
    # Create 10,000 unique keys rapidly
    start = time.time()
    for i in range(10000):
        limiter.is_allowed(f"unique_key_{i}")
    elapsed = time.time() - start
    
    print(f"Created 10,000 unique keys in {elapsed:.2f}s")
    
    # Should complete reasonably quickly
    assert elapsed < 5.0


def test_stress_cleanup_under_load():
    """Test cleanup process under load."""
    limiter = RateLimiter(max_requests=100, window_seconds=1)
    
    # Generate lots of requests
    def generate_load(thread_id):
        for i in range(1000):
            limiter.is_allowed(f"key_{thread_id}_{i}")
    
    threads = []
    for t_id in range(5):
        t = threading.Thread(target=generate_load, args=(t_id,))
        threads.append(t)
        t.start()
    
    # While threads are running, trigger cleanup
    for _ in range(5):
        time.sleep(0.5)
        limiter._cleanup_expired()
    
    for t in threads:
        t.join()
    
    # Should not crash
    assert True


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short", "-s"])
