#!/usr/bin/env python3
"""HRV vein emitter — drop-in library for autonomous systems.

Usage in any reactor / service:

    from tools.hrv_vein_emitter import VeinEmitter

    vein = VeinEmitter(system="consensus-reactor", node="hermes2")
    vein.start()  # spawns background thread, publishes every 30s

    # During processing:
    vein.observe_loop_lag_ms(12)
    vein.observe_error()
    vein.set_state("healthy", since=now_iso())
    vein.set_dep_status("nats", reachable=True)

    # On shutdown:
    vein.stop()

The emitter publishes two NATS subjects:
  - health.<system>.vital_signs   — every 30s, contains vital_signs envelope
  - health.<system>.state_change  — on state transition (healthy → degraded → critical → offline)

Also exposes a Prometheus /metrics endpoint on a configurable port.
The HRV pacemaker subscribes to health.> and computes hrv.status.digest.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Optional Prometheus exposition. Falls back to NATS-only if unavailable.
try:
    from prometheus_client import Counter, Gauge, Histogram, start_http_server
    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROM_AVAILABLE = False


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def node_name() -> str:
    """Cascade matches skills_broadcast: env → file → gethostname."""
    if env := os.environ.get("HERMES_CLUSTER_NODE_NAME"):
        return env
    cn = Path.home() / ".hermes" / "cluster_node_name"
    if cn.exists():
        return cn.read_text().strip()
    return socket.gethostname()


VALID_STATES = ("healthy", "degraded", "critical", "offline")


@dataclass
class VeinEmitter:
    """Per-system vein emitter. One instance per service.

    Attributes:
        system: kebab-case service name (matches systemd unit name without .service).
        node: cluster node name (defaults to cascade resolver).
        emit_interval_s: how often to publish health.<system>.vital_signs (default 30s).
        prom_port: if set, start Prometheus /metrics server here.
        nats_url: NATS URL. Default reads from env or 127.0.0.1:4222.
    """

    system: str
    node: str = field(default_factory=node_name)
    emit_interval_s: int = 30
    prom_port: Optional[int] = None
    nats_url: str = field(
        default_factory=lambda: os.environ.get("NATS_URL", "nats://127.0.0.1:4222")
    )

    # Mutable state (protected by self._lock):
    _state: str = "healthy"
    _state_since: str = field(default_factory=now_iso)
    _loop_lags: deque = field(default_factory=lambda: deque(maxlen=1000))
    _errors: deque = field(default_factory=lambda: deque(maxlen=1000))
    _queue_depth: int = 0
    _saturation: float = 0.0
    _deps_ok: set = field(default_factory=set)
    _deps_fail: set = field(default_factory=set)

    # Internals:
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop_event: threading.Event = field(default_factory=threading.Event)
    _thread: Optional[threading.Thread] = None

    # Prometheus handles:
    _prom_loop_lag: Optional["Histogram"] = None
    _prom_errors: Optional["Counter"] = None
    _prom_queue: Optional["Gauge"] = None
    _prom_state: Optional["Gauge"] = None

    def _setup_prom(self) -> None:
        if not _PROM_AVAILABLE or self.prom_port is None:
            return
        prefix = self.system.replace("-", "_")
        self._prom_loop_lag = Histogram(
            f"{prefix}_loop_lag_ms",
            "Inner-loop latency (ms)",
            buckets=(1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000),
        )
        self._prom_errors = Counter(
            f"{prefix}_errors_total", "Errors observed by the system."
        )
        self._prom_queue = Gauge(
            f"{prefix}_queue_depth", "Pending work items."
        )
        self._prom_state = Gauge(
            f"{prefix}_state",
            "0=offline 1=critical 2=degraded 3=healthy.",
        )
        start_http_server(self.prom_port)

    # ----- public API ------------------------------------------------------

    def observe_loop_lag_ms(self, ms: float) -> None:
        with self._lock:
            self._loop_lags.append((time.time(), ms))
        if self._prom_loop_lag is not None:
            self._prom_loop_lag.observe(ms)

    def observe_error(self) -> None:
        with self._lock:
            self._errors.append(time.time())
        if self._prom_errors is not None:
            self._prom_errors.inc()

    def set_queue_depth(self, n: int) -> None:
        with self._lock:
            self._queue_depth = n
        if self._prom_queue is not None:
            self._prom_queue.set(n)

    def set_saturation(self, frac: float) -> None:
        with self._lock:
            self._saturation = max(0.0, min(1.0, frac))

    def set_dep_status(self, dep: str, reachable: bool) -> None:
        with self._lock:
            if reachable:
                self._deps_ok.add(dep)
                self._deps_fail.discard(dep)
            else:
                self._deps_fail.add(dep)
                self._deps_ok.discard(dep)

    def set_state(self, new_state: str, since: Optional[str] = None) -> None:
        if new_state not in VALID_STATES:
            raise ValueError(f"state {new_state!r} not in {VALID_STATES}")
        with self._lock:
            transitioned = (new_state != self._state)
            self._state = new_state
            self._state_since = since or now_iso()
        if self._prom_state is not None:
            self._prom_state.set(VALID_STATES[::-1].index(new_state))
        if transitioned:
            self._publish_state_change()

    # ----- envelope construction ------------------------------------------

    def _vital_signs(self) -> dict:
        with self._lock:
            now = time.time()
            cutoff_60 = now - 60
            recent_errors = sum(1 for t in self._errors if t >= cutoff_60)
            recent_lags = [ms for (t, ms) in self._loop_lags if t >= cutoff_60]
            p50 = _percentile(recent_lags, 50)
            p99 = _percentile(recent_lags, 99)
            return {
                "loop_lag_p50_ms": p50,
                "loop_lag_p99_ms": p99,
                "queue_depth": self._queue_depth,
                "error_rate_60s": recent_errors / 60.0,
                "saturation": self._saturation,
                "deps_ok": sorted(self._deps_ok),
                "deps_fail": sorted(self._deps_fail),
            }

    def _envelope(self) -> dict:
        with self._lock:
            return {
                "system": self.system,
                "node": self.node,
                "ts": now_iso(),
                "vital_signs": self._vital_signs(),
                "state": self._state,
                "since": self._state_since,
            }

    # ----- publish path ----------------------------------------------------

    def _publish(self, subject: str, env: dict) -> None:
        """Best-effort NATS publish via subprocess `nats pub`.

        Avoids hard-dep on nats-py inside critical reactors. If publish
        fails, log to stderr but do NOT raise — vein silence is itself
        a signal the HRV pacemaker watchdog will observe.
        """
        import subprocess
        try:
            payload = json.dumps(env, default=str)
            subprocess.run(
                [
                    "nats", "--server", self.nats_url, "pub",
                    "-H", f"Nats-Msg-Id:{self.system}-{int(time.time()*1000)}",
                    "-H", f"X-Hermes-Publisher:{self.system}",
                    subject, payload,
                ],
                capture_output=True, timeout=3, check=False,
            )
        except Exception as e:  # noqa: BLE001
            import sys
            print(f"[vein-emitter] publish failed: {e}", file=sys.stderr)

    def _publish_vitals(self) -> None:
        env = self._envelope()
        self._publish(f"health.{self.system}.vital_signs", env)

    def _publish_state_change(self) -> None:
        env = self._envelope()
        self._publish(f"health.{self.system}.state_change", env)

    # ----- thread loop -----------------------------------------------------

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._publish_vitals()
            except Exception as e:  # noqa: BLE001
                import sys
                print(f"[vein-emitter] _run error: {e}", file=sys.stderr)
            self._stop_event.wait(self.emit_interval_s)

    def start(self) -> None:
        self._setup_prom()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"vein-{self.system}")
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        # Final offline marker so HRV knows we exited cleanly
        self.set_state("offline")
        self._publish_state_change()


def _percentile(xs, p: int) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(len(s) * p / 100)))
    return float(s[k])


if __name__ == "__main__":
    import sys
    sys_name = sys.argv[1] if len(sys.argv) > 1 else "demo"
    v = VeinEmitter(system=sys_name, emit_interval_s=5)
    v.start()
    print(f"Vein emitter for {sys_name} on {v.node}; Ctrl-C to stop.")
    try:
        i = 0
        while True:
            v.observe_loop_lag_ms(10 + i % 30)
            if i % 13 == 0:
                v.observe_error()
            v.set_queue_depth(i % 5)
            time.sleep(1)
            i += 1
    except KeyboardInterrupt:
        v.stop()
        print("\nstopped.")
