"""Load & performance tests for gateway hot paths.

Task: t_dcecc5c7 — [TEST] Load Testing & Performance

These tests exercise the gateway's concurrency, caching, connection-reuse,
and locking hot paths and enforce the DoD SLA:

  * p99 latency < 100ms (measured against in-process mocks — the real
    network is stubbed; what we're validating is that gateway overhead
    itself never blows the budget)
  * error rate < 5%
  * memory stable under stress (no unbounded growth over 1000x iterations)

Design notes
------------
The DoD asks for a *load* suite, not an integration suite against a live
remote. We follow the pattern already established by tests/test_phase1_fixes.py
(P1-003/P1-004): mock the AsyncClient / RemoteHermesInstance boundary and
measure the wrapper. That keeps the suite deterministic in CI and lets us
assert real timing/lock-contention properties without a network dependency.

Every latency-sensitive assertion is generous enough that it won't flake on
a loaded CI runner, but tight enough that a regression in the caching /
pooling / locking hot paths shows up immediately.
"""

from __future__ import annotations

import asyncio
import gc
import statistics
import sys
import threading
import time
import tracemalloc
from typing import List
from unittest.mock import AsyncMock, Mock, patch

import pytest


# Performance targets (from DoD)
P99_LATENCY_MS_LIMIT = 100.0
ERROR_RATE_LIMIT = 0.05


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _percentile(samples_ms: List[float], p: float) -> float:
    """Return the p-th percentile of a list of latency samples in ms.

    Uses linear interpolation like numpy.percentile (default method).
    p is in [0, 100].
    """
    if not samples_ms:
        return 0.0
    xs = sorted(samples_ms)
    if len(xs) == 1:
        return xs[0]
    k = (len(xs) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    frac = k - lo
    return xs[lo] + (xs[hi] - xs[lo]) * frac


def _make_mock_orchestrator_with_remote():
    """Build an InstanceOrchestrator with a stubbed remote instance registered.

    Returns (orchestrator, mock_http_client). Caller owns lifecycle and MUST
    call `await orchestrator.close()` (or clear _http_client) after use.
    """
    from gateway.instance_orchestrator import InstanceOrchestrator, RemoteHermesInstance

    remote = RemoteHermesInstance(
        name="loadtest",
        hostname="127.0.0.1",
        ip="127.0.0.1",
        http_port=8000,
        http_key="test-key",
        username="loadtest-user",
        description="load test target",
        is_local=False,
    )
    orch = InstanceOrchestrator(instances={"loadtest": remote})
    mock_client = AsyncMock()
    orch._http_client = mock_client
    return orch, mock_client


# ---------------------------------------------------------------------------
# 1. Concurrent execute_on_instance calls
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_execute_instances():
    """10+ concurrent execute_on_instance calls stay under SLA.

    Verifies:
      * all N requests complete
      * p99 wrapper latency < 100ms (target)
      * error rate < 5%
      * a single HTTP client is reused across all concurrent calls
    """
    orch, mock_client = _make_mock_orchestrator_with_remote()

    ok_response = Mock()
    ok_response.status = 200
    ok_response.json = Mock(return_value={"response": "ok"})
    ok_response.content = b'{"response":"ok"}'
    mock_client.post.return_value = ok_response

    N = 25
    samples_ms: List[float] = []
    errors = 0

    async def one_call(i: int):
        nonlocal errors
        t0 = time.perf_counter()
        result = await orch.execute_on_instance("loadtest", f"prompt-{i}")
        samples_ms.append((time.perf_counter() - t0) * 1000)
        if result != "ok":
            errors += 1

    await asyncio.gather(*[one_call(i) for i in range(N)])

    error_rate = errors / N
    p99 = _percentile(samples_ms, 99)
    p50 = _percentile(samples_ms, 50)

    assert mock_client.post.call_count == N, (
        f"expected {N} POSTs, got {mock_client.post.call_count}"
    )
    # Client reuse: the same mock_client object should still be attached.
    # execute_on_instance nulls _http_client on failure paths (401/timeout/exc),
    # so if it was still there after N successful calls, pool reuse held.
    assert orch._http_client is mock_client, "HTTP client was reset during happy-path load"
    assert error_rate <= ERROR_RATE_LIMIT, f"error rate {error_rate:.2%} > {ERROR_RATE_LIMIT:.2%}"
    assert p99 < P99_LATENCY_MS_LIMIT, (
        f"p99 latency {p99:.2f}ms > {P99_LATENCY_MS_LIMIT}ms (p50={p50:.2f}ms)"
    )


# ---------------------------------------------------------------------------
# 2. Health-check cache efficiency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_cache_efficiency():
    """Repeated health_check calls in the TTL window must NOT spam the remote.

    P1-004 defines a 30-second TTL on the _health_cache. Under load
    (100 concurrent health_check calls for one instance) exactly ONE
    outbound GET should fire; the other 99 must be served from cache.
    """
    orch, mock_client = _make_mock_orchestrator_with_remote()

    ok_response = Mock()
    ok_response.status = 200
    mock_client.get.return_value = ok_response

    N = 100
    results = await asyncio.gather(
        *[orch.health_check("loadtest") for _ in range(N)]
    )

    assert all(results), "all health_check calls should return True"
    # Cache MUST prevent spam. Because these are coroutines, the first one to
    # await the GET may or may not have populated the cache before the others
    # entered — but health_check has no lock, so several may race through
    # the miss branch on the very first burst. Assert an upper bound tight
    # enough to catch a real regression (e.g. TTL=0 or cache-not-honored)
    # while tolerating that first-burst race. 10x fewer calls than fanout
    # is a real cache; N-call fanout means cache is dead.
    assert mock_client.get.call_count <= max(5, N // 10), (
        f"cache spam: {mock_client.get.call_count} GETs for {N} health_checks"
    )

    # Now a second wave, well inside TTL: it must hit ZERO additional GETs.
    calls_before = mock_client.get.call_count
    await asyncio.gather(*[orch.health_check("loadtest") for _ in range(N)])
    assert mock_client.get.call_count == calls_before, (
        f"post-warm cache leaked {mock_client.get.call_count - calls_before} GETs"
    )


# ---------------------------------------------------------------------------
# 3. Memory stability under stress
# ---------------------------------------------------------------------------


class _FakeAsyncHttpClient:
    """Minimal fake for httpx.AsyncClient — no call-history accumulation.

    We use this instead of AsyncMock for the memory-stability test because
    AsyncMock retains every call's args/kwargs in call_args_list, which
    appears as ~7 KB per iteration of "leak" on a 1000-iter loop when
    tracemalloc is watching. That's a mock artifact, not a wrapper leak.
    """

    def __init__(self):
        self.post_calls = 0
        self._resp = Mock()
        self._resp.status = 200
        self._resp.json = Mock(return_value={"response": "ok"})
        self._resp.content = b'{"response":"ok"}'

    async def post(self, *args, **kwargs):
        self.post_calls += 1
        return self._resp

    async def get(self, *args, **kwargs):
        self.post_calls += 1
        return self._resp

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_memory_stability():
    """Run execute_on_instance 1000x and confirm no unbounded growth.

    Uses a hand-rolled fake HTTP client (not AsyncMock) so the growth we
    measure is entirely the wrapper's own — not mock-machinery bookkeeping.
    tracemalloc snapshots are taken after warmup and after the stress loop;
    the aggregated allocation delta must stay under a tight bound.
    """
    from gateway.instance_orchestrator import (
        InstanceOrchestrator,
        RemoteHermesInstance,
    )

    remote = RemoteHermesInstance(
        name="loadtest",
        hostname="127.0.0.1",
        ip="127.0.0.1",
        http_port=8000,
        http_key="k",
        description="mem test",
        is_local=False,
    )
    orch = InstanceOrchestrator(instances={"loadtest": remote})
    fake = _FakeAsyncHttpClient()
    orch._http_client = fake

    # Warmup: prime allocators & interned strings so the baseline snapshot
    # reflects a running steady state, not import-time overhead.
    for _ in range(50):
        await orch.execute_on_instance("loadtest", "warmup")

    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.take_snapshot()

    N = 1000
    for i in range(N):
        r = await orch.execute_on_instance("loadtest", f"stress-{i}")
        assert r == "ok"

    gc.collect()
    after = tracemalloc.take_snapshot()
    diff = after.compare_to(baseline, "filename")
    total_growth_bytes = sum(stat.size_diff for stat in diff)
    tracemalloc.stop()

    # 2 MB budget for 1000 wrapper iterations. A real per-call leak
    # (list-per-call retention or dict growth) will burn through this
    # inside the first few hundred iters. Some drift is expected —
    # logger buffers, string interning, gc bookkeeping.
    LIMIT_BYTES = 2 * 1024 * 1024
    per_iter_kb = total_growth_bytes / 1024 / N
    print(
        f"[memory] {N} iters: total {total_growth_bytes/1024:.1f} KB "
        f"({per_iter_kb:.3f} KB/iter)",
        file=sys.stderr,
    )
    assert total_growth_bytes < LIMIT_BYTES, (
        f"memory grew {total_growth_bytes/1024:.1f} KB over {N} iterations "
        f"({per_iter_kb:.2f} KB/iter, limit {LIMIT_BYTES/1024:.0f} KB) — "
        "possible leak"
    )
    assert fake.post_calls == N + 50


# ---------------------------------------------------------------------------
# 4. Connection pool reuse
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_pool_reuse():
    """Successive calls reuse the same underlying httpx client.

    The orchestrator lazy-initializes _http_client on first use and keeps
    it across happy-path calls. Only auth / timeout / exception paths
    close it. So over 20 successful calls, .init() must set the client
    exactly once and the client identity must be stable.
    """
    from gateway.instance_orchestrator import InstanceOrchestrator, RemoteHermesInstance

    remote = RemoteHermesInstance(
        name="pooltest",
        hostname="127.0.0.1",
        ip="127.0.0.1",
        http_port=8000,
        http_key="k",
        description="pool",
        is_local=False,
    )
    orch = InstanceOrchestrator(instances={"pooltest": remote})

    # Do NOT pre-set _http_client — let init() run naturally, but patch
    # httpx.AsyncClient so no real socket is opened.
    with patch("gateway.instance_orchestrator.httpx.AsyncClient") as MockClient:
        mock_client_instance = AsyncMock()
        MockClient.return_value = mock_client_instance

        ok_response = Mock()
        ok_response.status = 200
        ok_response.json = Mock(return_value={"response": "ok"})
        ok_response.content = b'{"response":"ok"}'
        mock_client_instance.post.return_value = ok_response

        first_client = None
        for i in range(20):
            await orch.execute_on_instance("pooltest", f"p-{i}")
            if first_client is None:
                first_client = orch._http_client
            # Identity must be stable across ALL happy-path calls.
            assert orch._http_client is first_client, (
                f"HTTP client identity changed at iteration {i} — pool reset unexpectedly"
            )

        # Constructor was called at most once — the whole point of pooling.
        assert MockClient.call_count == 1, (
            f"httpx.AsyncClient was constructed {MockClient.call_count} times, "
            "expected exactly 1 (pool reuse broken)"
        )
        assert mock_client_instance.post.call_count == 20


# ---------------------------------------------------------------------------
# 5 & 6. Remote-API throughput + latency
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_remote_api_throughput():
    """Sustained requests/second through the wrapper stays high.

    With a mocked network, the throughput floor is really a floor on
    Python overhead in execute_on_instance. If someone adds a sleep or
    a per-call log to disk, this will catch it.
    """
    orch, mock_client = _make_mock_orchestrator_with_remote()

    ok_response = Mock()
    ok_response.status = 200
    ok_response.json = Mock(return_value={"response": "ok"})
    ok_response.content = b'{"response":"ok"}'
    mock_client.post.return_value = ok_response

    N = 500
    t0 = time.perf_counter()
    await asyncio.gather(*[orch.execute_on_instance("loadtest", f"t-{i}") for i in range(N)])
    elapsed = time.perf_counter() - t0
    rps = N / elapsed

    # 200 rps is comfortably below what a mocked hot path should deliver
    # on any CI runner (real numbers are usually >5000 rps). The point is
    # to fail loudly if wrapper overhead explodes.
    assert rps > 200, f"throughput regression: {rps:.1f} rps over {N} calls in {elapsed:.3f}s"
    print(f"[throughput] {rps:.1f} rps ({N} calls in {elapsed*1000:.1f}ms)", file=sys.stderr)


@pytest.mark.asyncio
async def test_remote_api_latency():
    """Track p50/p95/p99 for the remote-API wrapper; enforce p99 < 100ms."""
    orch, mock_client = _make_mock_orchestrator_with_remote()

    ok_response = Mock()
    ok_response.status = 200
    ok_response.json = Mock(return_value={"response": "ok"})
    ok_response.content = b'{"response":"ok"}'
    mock_client.post.return_value = ok_response

    # Warmup so cold-start doesn't dominate the histogram.
    for _ in range(20):
        await orch.execute_on_instance("loadtest", "warm")

    N = 200
    samples_ms: List[float] = []
    for i in range(N):
        t0 = time.perf_counter()
        await orch.execute_on_instance("loadtest", f"lat-{i}")
        samples_ms.append((time.perf_counter() - t0) * 1000)

    p50 = _percentile(samples_ms, 50)
    p95 = _percentile(samples_ms, 95)
    p99 = _percentile(samples_ms, 99)
    print(
        f"[latency] N={N} p50={p50:.3f}ms p95={p95:.3f}ms p99={p99:.3f}ms",
        file=sys.stderr,
    )

    assert p99 < P99_LATENCY_MS_LIMIT, (
        f"p99={p99:.2f}ms exceeds SLA {P99_LATENCY_MS_LIMIT}ms (p50={p50:.2f}, p95={p95:.2f})"
    )
    # Sanity: p50 should be well below p99.
    assert p50 <= p99


# ---------------------------------------------------------------------------
# 7. Access-control lock contention
# ---------------------------------------------------------------------------


def test_access_control_lock_contention(tmp_path, monkeypatch):
    """Concurrent grant/revoke across N threads: no lost updates, no deadlock.

    AccessControlManager guards its whitelist with an RLock. Under
    contention, every grant/revoke pair must serialize cleanly and the
    end state must be deterministic.
    """
    # Isolate the audit log + whitelist file per test.
    monkeypatch.setenv("HOME", str(tmp_path))
    # Force a fresh manager singleton so monkeypatched HOME takes effect.
    import gateway.access_control as ac

    # Reset the global singleton for hermetic test isolation.
    ac._access_manager = None
    mgr = ac.get_access_manager()
    # Start clean.
    mgr.reset_to_defaults()

    N_THREADS = 20
    N_ITERS = 50
    errors: List[BaseException] = []
    lock_errors_lock = threading.Lock()

    def worker(worker_id: int):
        try:
            for i in range(N_ITERS):
                uid = f"user-{worker_id}-{i}"
                # Grant + verify + revoke: full lock cycle.
                assert mgr.grant_access(uid, grantor_id="loadtest") is True
                assert mgr.check_access(uid) is True
                assert mgr.revoke_access(uid, grantor_id="loadtest") is True
                assert mgr.check_access(uid) is False
        except BaseException as e:  # noqa: BLE001 - propagate any assertion failure
            with lock_errors_lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(N_THREADS)]

    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        # If the RLock ever deadlocks, this join() will block forever;
        # a 30s cap turns a hang into a visible failure in CI.
        t.join(timeout=30)
        assert not t.is_alive(), f"thread {t.name} deadlocked on access-control lock"
    elapsed = time.perf_counter() - t0

    assert not errors, f"concurrent grant/revoke errors: {errors[:3]!r} ({len(errors)} total)"

    total_ops = N_THREADS * N_ITERS * 2  # grant + revoke each iter
    ops_per_sec = total_ops / elapsed
    print(
        f"[access-lock] {total_ops} grant+revoke ops across {N_THREADS} threads "
        f"in {elapsed*1000:.1f}ms ({ops_per_sec:.0f} ops/s)",
        file=sys.stderr,
    )

    # At least 50 ops/s of grant+revoke work (each op does an audit-log
    # fsync to disk). A pathological regression (deadlock avoidance
    # sleep, per-op reload from disk, etc.) will drop this to single
    # digits. Real observed rate on this machine is ~130 ops/s.
    assert ops_per_sec > 50, (
        f"lock contention regression: only {ops_per_sec:.1f} ops/s "
        f"(elapsed={elapsed:.3f}s for {total_ops} ops)"
    )
