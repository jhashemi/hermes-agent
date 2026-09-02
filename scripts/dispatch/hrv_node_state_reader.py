#!/usr/bin/env python3
"""NATS KV reader for node probe state (ADR-006b Phase 2, Part 2a — task t_561cbe31).

Reads the latest per-node HRV probe state from the shared NATS JetStream KV
bucket ``hrv_node_state`` (populated by ``scripts/vcg_nats_publish.py`` —
Part 1 of the same ADR).

Design contract
---------------
* Read-only companion to :mod:`vcg_nats_publish` — imports its constants
  (``KV_BUCKET``, ``_kv_key``, ``DEFAULT_NATS_URL``) so the reader and the
  publisher can never disagree about bucket name or key encoding. If the
  publisher module can't be imported (out-of-repo consumer, missing scripts
  dir), the constants are duplicated inline as a defensive fallback.
* **Fail-open**: on any error — nats-py missing, connect timeout, bucket
  missing, key missing, decode failure — return an empty ``dict``. Callers
  treat "no state known" as "no gate to enforce", which matches the parent
  umbrella t_88eadaa8's fail-open semantics.
* **Never raises**. Every call site treats an empty dict as "unknown state
  → allow". Raising here would risk silently DoS-ing the dispatcher when
  NATS blips.

Expected value shape (parsed from JSON at ``kv.get(_kv_key(node_id)).value``)
----------------------------------------------------------------------------
The publisher currently writes the ``vcg_resource_reporter`` schema::

    {
        "mem_gb_available": float,
        "swap_pct": float,
        "load_1m": float,
        "load_5m": float,
        "disk_free_gb": float,
        "active_workers": int,
        "max_workers": int,
        "bedrock_tpm_remaining": int | null,
        "ts": str  # ISO8601 Z
    }

The task's parent umbrella (t_88eadaa8) additionally names these keys as the
canonical downstream contract; if the publisher grows to include them, the
reader returns them verbatim without schema knowledge:

    - ``memory_pressure``
    - ``kanban_dispatcher_health``
    - ``bedrock_rate_limit_saturation``
    - ``hrv.status.digest.interval_class``

The reader is intentionally schema-agnostic: it returns whatever the
publisher stored. The gating logic that interprets these fields lives in
sibling task t_c6b97f78 (``_check_nervous_system_gates``).

Public API
----------
* :func:`get_node_probe_state` — async, single-shot read (no persistent
  connection). Suitable for cold-path dispatch gates that run every few
  seconds.
* :class:`NATSNodeStateReader` — reusable reader with a persistent KV
  binding, for hot loops. Uses a lazy connect + reconnect on failure,
  same pattern as :class:`NATSKVHeartbeatAdapter`.
* :func:`get_node_probe_state_sync` — synchronous wrapper. Runs the async
  reader on a fresh loop, or a helper thread if called from an existing
  event loop.

Task: t_561cbe31 — Implement NATS KV bucket reader for node probe state.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ── Constants (import-with-fallback) ───────────────────────────────────────
#
# The publisher owns the canonical values. Import from it when the scripts/
# dir is on sys.path (the normal deployment layout); otherwise fall back to
# a duplicated copy so downstream consumers can vendor this file alone.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

try:
    from vcg_nats_publish import (  # type: ignore[import-not-found]
        DEFAULT_NATS_URL,
        KV_BUCKET,
        _kv_key,
    )
except Exception:  # pragma: no cover — defensive: publisher not on path
    KV_BUCKET = "hrv_node_state"
    DEFAULT_NATS_URL = os.environ.get(
        "NATS_URL",
        os.environ.get(
            "NATS_SERVERS",
            "nats://100.79.15.66:4222,nats://100.79.15.66:4223,"
            "nats://100.107.83.25:4222,nats://100.127.115.56:4222",
        ),
    )
    _KV_KEY_SAFE = str.maketrans({".": "_", "/": "_", "-": "_"})

    def _kv_key(hostname: str) -> str:  # type: ignore[no-redef]
        return hostname.translate(_KV_KEY_SAFE)


DEFAULT_CONNECT_TIMEOUT_S = float(os.environ.get("HRV_NODE_STATE_CONNECT_TIMEOUT_S", "3.0"))
DEFAULT_READ_TIMEOUT_S = float(os.environ.get("HRV_NODE_STATE_READ_TIMEOUT_S", "2.0"))


# ── Async single-shot reader ───────────────────────────────────────────────


async def get_node_probe_state(
    node_id: str,
    *,
    nats_url: str = DEFAULT_NATS_URL,
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
    read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    bucket: str = KV_BUCKET,
) -> dict[str, Any]:
    """Read the latest probe state for ``node_id`` from NATS KV bucket ``bucket``.

    Returns the parsed JSON value as a dict, or ``{}`` on ANY of:

    * ``nats-py`` not installed
    * NATS connect failure / timeout
    * Bucket does not exist
    * Key does not exist for this node
    * Value bytes fail JSON decode
    * Any other exception

    Never raises.
    """
    try:
        import nats  # type: ignore
    except ImportError:
        logger.warning(
            "[HRVReader] nats-py not installed; returning empty state for %s",
            node_id,
        )
        return {}

    # Silence nats-py's failover ERROR spam — same trick vcg_nats_publish uses.
    _nats_client_logger = logging.getLogger("nats.aio.client")
    _prev_level = _nats_client_logger.level
    _nats_client_logger.setLevel(logging.CRITICAL)

    servers = [s.strip() for s in nats_url.split(",") if s.strip()]
    nc = None
    try:
        try:
            nc = await asyncio.wait_for(
                nats.connect(
                    servers=servers,
                    connect_timeout=connect_timeout_s,
                    allow_reconnect=False,
                    max_reconnect_attempts=0,
                ),
                timeout=connect_timeout_s + 1.0,
            )
        except Exception as e:
            logger.warning(
                "[HRVReader] connect failed for %s (%s): %s",
                node_id, nats_url, e,
            )
            return {}

        try:
            js = nc.jetstream()
            kv = await asyncio.wait_for(
                js.key_value(bucket),
                timeout=read_timeout_s,
            )
        except Exception as e:
            # BucketNotFoundError, NotFoundError, timeouts — all treated as
            # "no state, allow through".
            logger.info(
                "[HRVReader] bucket %s unavailable for %s: %s",
                bucket, node_id, e,
            )
            return {}

        try:
            entry = await asyncio.wait_for(
                kv.get(_kv_key(node_id)),
                timeout=read_timeout_s,
            )
        except Exception as e:
            # KeyNotFoundError is expected for nodes that haven't reported yet.
            logger.debug(
                "[HRVReader] key %s not in bucket %s: %s",
                _kv_key(node_id), bucket, e,
            )
            return {}

        try:
            value = entry.value if entry is not None else None
            if value is None:
                return {}
            payload = json.loads(value.decode("utf-8"))
        except Exception as e:
            logger.warning(
                "[HRVReader] malformed JSON for node %s in bucket %s: %s",
                node_id, bucket, e,
            )
            return {}

        if not isinstance(payload, dict):
            logger.warning(
                "[HRVReader] non-dict payload for node %s: %r",
                node_id, type(payload).__name__,
            )
            return {}

        return payload
    finally:
        if nc is not None:
            with contextlib.suppress(Exception):
                await nc.close()
        _nats_client_logger.setLevel(_prev_level)


# ── Reusable class (persistent connection) ─────────────────────────────────


class NATSNodeStateReader:
    """Persistent-connection HRV node-state reader.

    Use this when reading state for many nodes / in a tight loop. It mirrors
    :class:`executive_agents.cluster.adapters.nats_kv_heartbeat.NATSKVHeartbeatAdapter`
    exactly: lazy connect, silent fail-open on connect failure, retry on
    every call so a transient NATS blip self-heals.

    Not thread-safe. Callers running in threaded contexts should either use
    the module-level :func:`get_node_probe_state` per call, or hold a lock
    around the reader.
    """

    def __init__(
        self,
        nats_url: str = DEFAULT_NATS_URL,
        *,
        bucket: str = KV_BUCKET,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    ) -> None:
        self._nats_url = nats_url
        self._bucket_name = bucket
        self._connect_timeout_s = connect_timeout_s
        self._read_timeout_s = read_timeout_s
        self._nc: Any = None
        self._js: Any = None
        self._kv: Any = None
        self._connect_lock = asyncio.Lock()

    async def _ensure_connected(self) -> bool:
        if self._kv is not None:
            return True
        async with self._connect_lock:
            if self._kv is not None:  # raced
                return True
            try:
                import nats  # type: ignore
            except ImportError:
                logger.warning("[HRVReader] nats-py not installed")
                return False
            servers = [s.strip() for s in self._nats_url.split(",") if s.strip()]
            try:
                self._nc = await asyncio.wait_for(
                    nats.connect(
                        servers=servers,
                        connect_timeout=self._connect_timeout_s,
                        allow_reconnect=True,
                    ),
                    timeout=self._connect_timeout_s + 1.0,
                )
                self._js = self._nc.jetstream()
                self._kv = await asyncio.wait_for(
                    self._js.key_value(self._bucket_name),
                    timeout=self._read_timeout_s,
                )
                logger.info(
                    "[HRVReader] connected to %s, bound KV=%s",
                    self._nats_url, self._bucket_name,
                )
                return True
            except Exception as e:
                logger.warning(
                    "[HRVReader] connect failed (%s) — will retry on next call",
                    e,
                )
                await self._reset()
                return False

    async def _reset(self) -> None:
        if self._nc is not None:
            with contextlib.suppress(Exception):
                await self._nc.close()
        self._nc = None
        self._js = None
        self._kv = None

    async def get(self, node_id: str) -> dict[str, Any]:
        """Return latest probe state dict for ``node_id``, or ``{}`` on any failure."""
        if not await self._ensure_connected():
            return {}
        assert self._kv is not None
        try:
            entry = await asyncio.wait_for(
                self._kv.get(_kv_key(node_id)),
                timeout=self._read_timeout_s,
            )
        except Exception as e:
            # KeyNotFound / bucket vanished mid-lifetime / timeout — all fail-open.
            logger.debug(
                "[HRVReader] get(%s) miss/error: %s", node_id, e,
            )
            # If the bucket handle went stale, force reconnect on next call.
            # We don't try to distinguish here — cheapest reliable path is a
            # cold reset when anything unusual happens.
            msg = str(e).lower()
            if "not found" not in msg and "keynotfound" not in msg:
                await self._reset()
            return {}
        try:
            value = entry.value if entry is not None else None
            if value is None:
                return {}
            payload = json.loads(value.decode("utf-8"))
        except Exception as e:
            logger.warning(
                "[HRVReader] malformed JSON for %s: %s", node_id, e,
            )
            return {}
        if not isinstance(payload, dict):
            return {}
        return payload

    async def close(self) -> None:
        await self._reset()


# ── Sync wrapper ───────────────────────────────────────────────────────────


def get_node_probe_state_sync(
    node_id: str,
    *,
    nats_url: str = DEFAULT_NATS_URL,
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
    read_timeout_s: float = DEFAULT_READ_TIMEOUT_S,
    bucket: str = KV_BUCKET,
) -> dict[str, Any]:
    """Synchronous wrapper for callers not in an async context.

    Runs the async reader on a fresh ``asyncio`` loop. If the caller already
    has a running loop, falls back to a dedicated helper thread so we never
    reenter the caller's loop.

    Fail-open: returns ``{}`` on scheduling / runtime errors.
    """
    coro = get_node_probe_state(
        node_id,
        nats_url=nats_url,
        connect_timeout_s=connect_timeout_s,
        read_timeout_s=read_timeout_s,
        bucket=bucket,
    )
    try:
        return asyncio.run(coro)
    except RuntimeError:
        # Caller is inside an event loop — run in a fresh thread.
        import threading

        holder: dict[str, dict[str, Any]] = {"result": {}}

        def _runner() -> None:
            try:
                holder["result"] = asyncio.run(
                    get_node_probe_state(
                        node_id,
                        nats_url=nats_url,
                        connect_timeout_s=connect_timeout_s,
                        read_timeout_s=read_timeout_s,
                        bucket=bucket,
                    )
                )
            except Exception as e:  # pragma: no cover — defensive
                logger.warning("[HRVReader] thread run failed: %s", e)
                holder["result"] = {}

        t = threading.Thread(target=_runner, daemon=True)
        t.start()
        t.join(timeout=connect_timeout_s + read_timeout_s + 2.0)
        return holder["result"]
    except Exception as e:  # pragma: no cover — defensive
        logger.warning("[HRVReader] sync wrapper unexpected error: %s", e)
        return {}


# ── CLI probe ──────────────────────────────────────────────────────────────


def _cli() -> None:  # pragma: no cover — thin main
    import argparse
    import socket

    p = argparse.ArgumentParser(
        description="Read the latest HRV probe state for a node from NATS KV.",
    )
    p.add_argument("node_id", nargs="?", default=socket.gethostname(),
                   help="Hostname / node id (defaults to local hostname)")
    p.add_argument("--nats-url", default=DEFAULT_NATS_URL)
    p.add_argument("--bucket", default=KV_BUCKET)
    p.add_argument("--connect-timeout", type=float, default=DEFAULT_CONNECT_TIMEOUT_S)
    p.add_argument("--read-timeout", type=float, default=DEFAULT_READ_TIMEOUT_S)
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    state = get_node_probe_state_sync(
        args.node_id,
        nats_url=args.nats_url,
        connect_timeout_s=args.connect_timeout,
        read_timeout_s=args.read_timeout,
        bucket=args.bucket,
    )
    print(json.dumps(state, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    _cli()
