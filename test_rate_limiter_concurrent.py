#!/usr/bin/env python3
"""Concurrent rate limiter testing script (P3-005).

Demonstrates:
- Per-API-key rate limiting under concurrent load
- 429 Too Many Requests responses
- Retry-After header behavior
- Recovery after window reset

Usage:
    python3 test_rate_limiter_concurrent.py
"""

import asyncio
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from gateway.remote_agent_api import RateLimiter


def test_concurrent_load():
    """Test rate limiter under concurrent load."""
    print("\n" + "="*70)
    print("TEST 1: Concurrent Load (100 threads, 100 request limit per 60s)")
    print("="*70)
    
    limiter = RateLimiter(max_requests=100, window_seconds=60)
    api_key = "load-test-key"
    
    results = {
        "allowed": 0,
        "denied": 0,
        "retry_afters": [],
    }
    lock = threading.Lock()
    
    def make_request(request_num):
        """Make a single request and record result."""
        allowed, retry_after = limiter.is_allowed(api_key)
        with lock:
            if allowed:
                results["allowed"] += 1
            else:
                results["denied"] += 1
                if retry_after:
                    results["retry_afters"].append(retry_after)
        return (request_num, allowed, retry_after)
    
    print(f"\nMaking 120 concurrent requests from {threading.active_count()} thread(s)...")
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(make_request, i) for i in range(120)]
        responses = [f.result() for f in as_completed(futures)]
    
    elapsed = time.time() - start_time
    
    print(f"\nResults after {elapsed:.2f} seconds:")
    print(f"  ✓ Allowed:  {results['allowed']}")
    print(f"  ✗ Denied:   {results['denied']}")
    print(f"  Expected:   100 allowed, 20 denied")
    
    if results["retry_afters"]:
        print(f"  Retry-After values: {results['retry_afters'][:5]}... (avg: {sum(results['retry_afters'])/len(results['retry_afters']):.1f}s)")
    
    # Verify correctness
    assert results["allowed"] == 100, f"Expected 100 allowed, got {results['allowed']}"
    assert results["denied"] == 20, f"Expected 20 denied, got {results['denied']}"
    print("\n✓ Test 1 PASSED")


def test_multiple_api_keys():
    """Test that different API keys have independent limits."""
    print("\n" + "="*70)
    print("TEST 2: Multiple API Keys (10 keys, 10 requests each, 50 limit total)")
    print("="*70)
    
    limiter = RateLimiter(max_requests=50, window_seconds=60)
    
    results = {}
    lock = threading.Lock()
    
    def make_requests_for_key(key_num):
        """Make 10 requests for a specific key."""
        api_key = f"key-{key_num}"
        key_results = {"allowed": 0, "denied": 0}
        
        for req_num in range(10):
            allowed, _ = limiter.is_allowed(api_key)
            if allowed:
                key_results["allowed"] += 1
            else:
                key_results["denied"] += 1
        
        with lock:
            results[api_key] = key_results
        
        return key_results
    
    print("\nMaking 100 concurrent requests across 10 different API keys...")
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(make_requests_for_key, i) for i in range(10)]
        key_results = [f.result() for f in as_completed(futures)]
    
    elapsed = time.time() - start_time
    
    print(f"\nResults after {elapsed:.2f} seconds:")
    total_allowed = sum(r["allowed"] for r in key_results)
    total_denied = sum(r["denied"] for r in key_results)
    
    print(f"  ✓ Total allowed: {total_allowed}")
    print(f"  ✗ Total denied:  {total_denied}")
    print(f"  Expected: 100 allowed (10 per key), 0 denied")
    
    for i, res in sorted(results.items()):
        print(f"  {i}: {res['allowed']} allowed, {res['denied']} denied")
    
    # Verify
    assert total_allowed == 100, f"Expected 100 allowed total, got {total_allowed}"
    assert total_denied == 0, f"Expected 0 denied total, got {total_denied}"
    print("\n✓ Test 2 PASSED")


def test_window_reset():
    """Test that counter resets after window expires."""
    print("\n" + "="*70)
    print("TEST 3: Window Reset (5 requests, 3 second window)")
    print("="*70)
    
    limiter = RateLimiter(max_requests=5, window_seconds=3)
    api_key = "window-reset-key"
    
    print("\nPhase 1: Making 5 requests (should all be allowed)...")
    for i in range(5):
        allowed, _ = limiter.is_allowed(api_key)
        print(f"  Request {i+1}: {'✓ Allowed' if allowed else '✗ Denied'}")
        assert allowed, f"Request {i+1} should be allowed"
    
    print("\nPhase 2: 6th request (should be denied, window not expired)...")
    allowed, retry_after = limiter.is_allowed(api_key)
    print(f"  Request 6: {'✓ Allowed' if allowed else '✗ Denied'} (retry after {retry_after}s)")
    assert not allowed, "Request 6 should be denied"
    assert 1 <= retry_after <= 3, f"Retry-After should be 1-3s, got {retry_after}s"
    
    print("\nWaiting 3.2 seconds for window to reset...")
    time.sleep(3.2)
    
    print("\nPhase 3: 7th request (should be allowed after reset)...")
    allowed, retry_after = limiter.is_allowed(api_key)
    print(f"  Request 7: {'✓ Allowed' if allowed else '✗ Denied'}")
    assert allowed, "Request 7 should be allowed after window reset"
    assert retry_after is None, "Retry-After should be None for allowed request"
    
    print("\n✓ Test 3 PASSED")


def test_stats_reporting():
    """Test rate limiter stats reporting."""
    print("\n" + "="*70)
    print("TEST 4: Stats Reporting")
    print("="*70)
    
    limiter = RateLimiter(max_requests=10, window_seconds=60)
    api_key = "stats-key"
    
    print("\nPhase 1: Before any requests")
    stats = limiter.get_stats(api_key)
    print(f"  Requests made:     {stats['requests_made']}")
    print(f"  Requests remaining: {stats['requests_remaining']}")
    print(f"  Reset in:          {stats['reset_in_seconds']}s")
    assert stats["requests_made"] == 0
    assert stats["requests_remaining"] == 10
    
    print("\nPhase 2: After 3 requests")
    for i in range(3):
        limiter.is_allowed(api_key)
    
    stats = limiter.get_stats(api_key)
    print(f"  Requests made:      {stats['requests_made']}")
    print(f"  Requests remaining: {stats['requests_remaining']}")
    print(f"  Reset in:           {stats['reset_in_seconds']}s")
    assert stats["requests_made"] == 3
    assert stats["requests_remaining"] == 7
    
    print("\nPhase 3: After 10 requests (at limit)")
    for i in range(7):
        limiter.is_allowed(api_key)
    
    stats = limiter.get_stats(api_key)
    print(f"  Requests made:      {stats['requests_made']}")
    print(f"  Requests remaining: {stats['requests_remaining']}")
    assert stats["requests_made"] == 10
    assert stats["requests_remaining"] == 0
    
    print("\n✓ Test 4 PASSED")


def test_burst_then_recover():
    """Test burst of requests followed by recovery."""
    print("\n" + "="*70)
    print("TEST 5: Burst Then Recover (50 requests, wait, 50 more)")
    print("="*70)
    
    limiter = RateLimiter(max_requests=50, window_seconds=5)
    api_key = "burst-key"
    
    print("\nPhase 1: Burst of 60 requests (first 50 allowed, rest denied)...")
    allowed_count = 0
    denied_count = 0
    for i in range(60):
        allowed, _ = limiter.is_allowed(api_key)
        if allowed:
            allowed_count += 1
        else:
            denied_count += 1
    
    print(f"  Allowed: {allowed_count}, Denied: {denied_count}")
    assert allowed_count == 50
    assert denied_count == 10
    
    print("\nWaiting 5.2 seconds for window to reset...")
    time.sleep(5.2)
    
    print("\nPhase 2: Another burst of 50 requests (all should be allowed)...")
    allowed_count = 0
    for i in range(50):
        allowed, _ = limiter.is_allowed(api_key)
        if allowed:
            allowed_count += 1
    
    print(f"  Allowed: {allowed_count}")
    assert allowed_count == 50
    
    print("\n✓ Test 5 PASSED")


def test_high_concurrency():
    """Test with very high concurrency (500 threads)."""
    print("\n" + "="*70)
    print("TEST 6: High Concurrency (500 threads)")
    print("="*70)
    
    limiter = RateLimiter(max_requests=200, window_seconds=60)
    api_key = "high-concurrency"
    
    results = {
        "allowed": 0,
        "denied": 0,
    }
    lock = threading.Lock()
    
    def make_request():
        allowed, _ = limiter.is_allowed(api_key)
        with lock:
            if allowed:
                results["allowed"] += 1
            else:
                results["denied"] += 1
    
    print("\nMaking 500 concurrent requests from thread pool...")
    start_time = time.time()
    
    with ThreadPoolExecutor(max_workers=50) as executor:
        futures = [executor.submit(make_request) for _ in range(500)]
        for f in as_completed(futures):
            f.result()
    
    elapsed = time.time() - start_time
    
    print(f"\nResults after {elapsed:.2f} seconds:")
    print(f"  ✓ Allowed: {results['allowed']}")
    print(f"  ✗ Denied:  {results['denied']}")
    print(f"  Expected:  200 allowed, 300 denied")
    
    assert results["allowed"] == 200, f"Expected 200 allowed, got {results['allowed']}"
    assert results["denied"] == 300, f"Expected 300 denied, got {results['denied']}"
    print("\n✓ Test 6 PASSED")


def main():
    """Run all tests."""
    print("\n" + "="*70)
    print("RATE LIMITER CONCURRENCY TEST SUITE (P3-005)")
    print("="*70)
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    
    start_time = time.time()
    
    try:
        test_concurrent_load()
        test_multiple_api_keys()
        test_window_reset()
        test_stats_reporting()
        test_burst_then_recover()
        test_high_concurrency()
        
        elapsed = time.time() - start_time
        print("\n" + "="*70)
        print(f"✓ ALL TESTS PASSED ({elapsed:.2f}s)")
        print("="*70)
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
    exit(main())
