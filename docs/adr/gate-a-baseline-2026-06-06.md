# GATE-A Baseline Measurement Results

**Date:** 2026-06-06
**ADR:** ADR-010 voice-bridge §A.5 GATE-A
**Threshold:** p95 ≤ 600ms over 1000 samples
**Verdict:** ✅ **PASS** (with caveats — see §3)

## §1 Test setup

- **Server:** `gate_a_query_port.py` HTTPServer on hermes2:18080
- **Payload:** 4 KiB JSON (matches ADR-010 manifest cap)
- **Harness:** `gate_a_harness.py`, 1000 samples + 50-sample warmup, plain HTTP (no TLS), `urllib.request`
- **Methodology:** raw `time.perf_counter()` around `urlopen().read()`; reported p50/p90/p95/p99/min/max/mean

## §2 Results

| Path | n | min | p50 | p95 | p99 | max | Verdict |
|---|---|---|---|---|---|---|---|
| hermes2 → hermes2 (loopback) | 1000 | 0.34ms | 0.47ms | **1.01ms** | 1.99ms | 6.78ms | PASS |
| hermes1 → hermes2 (intra-AZ Tailscale) | 1000 | 1.23ms | 1.48ms | **3.05ms** | 7.16ms | 105.65ms | PASS |
| rust-build → hermes2 (intra-AZ Tailscale) | 1000 | 0.84ms | 1.05ms | **2.15ms** | 3.58ms | 34.11ms | PASS |
| hermes2 → S3 us-east-1 (HTTPS, internet) | 200 | 57.12ms | 71.46ms | **109.95ms** | 174.69ms | 264.00ms | PASS |

All four paths pass the 600ms p95 gate by ≥5× margin.

## §3 Caveats

**This is intra-AZ, not cross-AZ.** All three Hermes cluster machines are in `us-east-1a`:
- hermes2: `us-east-1a`
- hermes1 (`ip-172-31-30-216`): `us-east-1a`
- rust-build (`100.127.115.56`): `us-east-1a`

The S3 sample is the closest proxy to true cross-region (still us-east-1, but exits the VPC and traverses internet ↔ AWS edge). It came in at p95 **110ms** with HTTPS handshake amortized across the connection pool.

### Cross-AZ realistic projection

AWS-published intra-region inter-AZ latency (us-east-1a ↔ us-east-1b/c) is typically **0.5–2.0ms** added on top of intra-AZ. Even with a 10× safety margin (worst-case GC pause, network event), that puts cross-AZ baseline at **~5-10ms p95** for an HTTP query port — still 60× under the 600ms gate.

### Cross-region (if voice agent ends up in a different region from gateway)

The S3 sample shows ~110ms p95 for a single HTTPS round-trip across the public internet. Cross-region same-continent (us-east-1 ↔ us-west-2): expect 60-80ms p95. Cross-continent (us-east-1 ↔ eu-west-1): 90-120ms p95. **All still well within the 600ms gate**, but this would consume ~15-20% of the per-turn budget instead of the current <1%.

## §4 What this means for ADR-010

- **Spike 2 is unblocked.** GATE-A passes; query port design is viable.
- **No redesign needed.** ADR-010's 200ms p50 / 500ms p95 / 900ms p99 targets for the lazy port query (Hamilton's table) remain achievable with substantial headroom for intra-AZ deployment.
- **One follow-up:** when the voice agent's actual deployment AZ/region is decided, re-run GATE-A from that location specifically. The current measurement is a *best-case* baseline, not a deployment commitment.
- **Tail risk noted:** hermes1's max was 105ms (well above its p99 of 7ms) — likely a single GC pause or scheduler hiccup. The 3000ms hard timeout in §A.3.3 covers this comfortably.

## §5 Reproduction

```bash
# Server (on gateway host)
python3 /tmp/gate_a_query_port.py &

# Harness (on voice-agent host)
python3 /tmp/gate_a_harness.py \
    --url http://<gateway-ip>:18080/context/recent_skills \
    -n 1000 --warmup 50 --label "<your-az>"
```

Files preserved at `/tmp/gate_a_query_port.py` and `/tmp/gate_a_harness.py` on hermes2.
