"""NATS KV bucket reader for hrv.node_state.<node> probe state.

t_561cbe31 (child of t_88eadaa8) — the missing plumbing between "hrv-autoheal
probes publish to a shared NATS JetStream KV bucket" and "the dispatch-time
HRV gate consults per-node probe snapshots".

Contract
========

Task body (verbatim):

    Add a helper method (e.g., ``_get_node_probe_state(node_id)``) that reads
    the latest probe state from the shared NATS KV bucket ``hrv.node_state.<node>``
    for a given node. The method should return a dict containing at least the
    following keys if present:

      - memory_pressure
      - kanban_dispatcher_health
      - bedrock_rate_limit_saturation
      - hrv.status.digest.interval_class

    Use the existing NATS connection/KV handle already available in the VCG
    dispatcher. Do NOT poll the DB; only read from the KV bucket. If the
    bucket or key is missing, return an empty dict.

Design
======

Two layers, mirroring the shape already established by ``dispatch_gate.py``:

1. ``read_node_probe_state(node_id, kv_reader)`` — a **pure sync predicate**
   over an injected ``kv_reader: (key) -> bytes | dict | None`` callable.
   This is the level the dispatcher's spawn-time gate consumes because it has
   zero runtime dependencies (no ``nats``, no ``asyncio``, no ``prometheus``).
   Same idiom as ``dispatch_gate.check_model_liveness``.

2. ``HRVKVReader`` — a small async binder around ``nats.js.KeyValue`` that
   converts the JetStream KV surface into the sync ``kv_reader`` callable
   layer 1 expects. This is the level the *long-lived VCG dispatcher process*
   uses: it holds a NATS connection, opens the ``hrv_node_state`` bucket
   once, and exposes ``read(node_id) -> dict`` for the sync gate to call.

Key layout
==========

The bucket name is ``hrv_node_state`` (JetStream KV bucket names cannot
contain ``.``; subjects can). The **key** inside the bucket is the node
identifier, exactly as the probes publish it. So for ``node_id="hermes2"``
the reader fetches key ``"hermes2"`` and expects a JSON payload whose top
level carries the four contract keys.

The task body's phrasing ``hrv.node_state.<node>`` refers to the *conceptual*
subject path (matching ``dispatch_gate._liveness_key`` which uses the same
prefix); the underlying JetStream KV bucket is named ``hrv_node_state`` per
NATS's bucket-name rules. Both are exposed on the module surface so operators
can pin either one via env vars if their probe publishers disagree.

Fail-open guarantees
====================

Every failure mode returns ``{}`` (empty dict). Callers treat empty as
"no signal" and fall through to the deterministic hard gates (heartbeat,
load ratio, disk free). Specifically:

* Bucket does not exist                 → ``{}``
* Key does not exist                    → ``{}``
* Payload is not valid JSON             → ``{}``
* Payload is JSON but not a dict        → ``{}``
* KV reader callable raises             → ``{}``  (logged at debug)
* NATS connection unavailable / closed  → ``{}``  (logged at debug)

A missing signal must never block real work. This mirrors the same
policy documented in ``dispatch_gate.py``.

Projection
==========

``read_node_probe_state`` returns the *full* decoded payload dict unmodified.
The helper ``project_probe_state(payload)`` returns a projection restricted
to the four contract keys — callers who want exactly the acceptance-criteria
shape use the projection; callers who want extra fields (``ts``, ``swap_pct``,
``bedrock_tpm_remaining``, ``hostname``, …) use the raw dict directly.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Awaitable, Callable, Mapping, Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
# Configuration — env overrides so operators can steer without a redeploy
# ─────────────────────────────────────────────────────────────────────


#: JetStream KV bucket name that hrv-autoheal probes populate. NATS KV
#: bucket names must be a single token (no dots), so this uses underscores.
DEFAULT_BUCKET_NAME = os.environ.get("HERMES_HRV_KV_BUCKET", "hrv_node_state")

#: The four keys named in t_561cbe31's acceptance criteria. Kept as a
#: module constant so tests and callers agree on the projection.
PROBE_STATE_KEYS: tuple[str, ...] = (
    "memory_pressure",
    "kanban_dispatcher_health",
    "bedrock_rate_limit_saturation",
    "hrv.status.digest.interval_class",
)


# ─────────────────────────────────────────────────────────────────────
# Layer 1 — pure sync reader over an injected kv_reader callable
# ─────────────────────────────────────────────────────────────────────


def read_node_probe_state(
    node_id: str,
    kv_reader: Callable[[str], Optional[Any]],
) -> dict[str, Any]:
    """Return the latest probe-state dict for ``node_id``, or ``{}`` if unavailable.

    Args:
        node_id: cluster node identifier (matches the KV key the probes write).
            Coerced to ``str`` and stripped; empty/None ⇒ ``{}``.
        kv_reader: callable invoked as ``kv_reader(node_id)`` that returns the
            raw stored value. Return shapes accepted:

              - ``None``          → treated as "key missing" → ``{}``
              - ``bytes`` / ``bytearray`` → decoded as UTF-8 JSON
              - ``str``           → parsed as JSON
              - ``dict``          → returned as-is (defensive copy)
              - anything else     → ``{}``  (logged at debug)

            Raising is fine — the exception is caught and ``{}`` returned.

    Returns:
        The decoded payload dict on success. ``{}`` on any failure mode.
        Never raises.
    """
    node_slug = str(node_id or "").strip()
    if not node_slug:
        return {}

    try:
        raw = kv_reader(node_slug)
    except Exception as exc:  # noqa: BLE001 — fail-open is the whole point
        logger.debug(
            "hrv_kv_reader: kv_reader(%r) raised %r — returning empty dict",
            node_slug,
            exc,
        )
        return {}

    if raw is None:
        # Key absent from the bucket, or the bucket itself is missing.
        return {}

    payload: Any
    if isinstance(raw, (bytes, bytearray)):
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.debug(
                "hrv_kv_reader: malformed bytes payload for %r (%s) — empty dict",
                node_slug,
                type(exc).__name__,
            )
            return {}
    elif isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.debug(
                "hrv_kv_reader: malformed string payload for %r (%s) — empty dict",
                node_slug,
                exc,
            )
            return {}
    elif isinstance(raw, dict):
        # Defensive shallow copy so callers can mutate without touching
        # a cached upstream value.
        payload = dict(raw)
    else:
        logger.debug(
            "hrv_kv_reader: unexpected payload type %s for %r — empty dict",
            type(raw).__name__,
            node_slug,
        )
        return {}

    if not isinstance(payload, dict):
        # JSON parsed OK but the top level is a list / scalar / null.
        logger.debug(
            "hrv_kv_reader: payload for %r is not a dict (%s) — empty dict",
            node_slug,
            type(payload).__name__,
        )
        return {}

    return payload


def project_probe_state(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the four acceptance-criteria keys from ``payload``.

    Absent keys are omitted (rather than set to ``None``) so callers can
    distinguish "probe hasn't reported this yet" from "probe reported None".
    This matches the fail-open policy: unknown = no opinion.

    Args:
        payload: any Mapping; typically the result of ``read_node_probe_state``.

    Returns:
        A new dict with a subset of ``PROBE_STATE_KEYS`` that were present in
        ``payload``. ``{}`` if none present.
    """
    if not isinstance(payload, Mapping):
        return {}
    return {k: payload[k] for k in PROBE_STATE_KEYS if k in payload}


# ─────────────────────────────────────────────────────────────────────
# Layer 2 — async NATS KV binder for the long-lived dispatcher process
# ─────────────────────────────────────────────────────────────────────


class HRVKVReader:
    """Async binder over a NATS JetStream KV bucket.

    The VCG dispatcher (a long-lived process) holds a live NATS connection
    and JetStream handle. This class lazily opens the ``hrv_node_state`` KV
    bucket and provides ``async read(node_id) -> dict``. Convert the async
    result to the sync ``kv_reader`` callable ``read_node_probe_state``
    expects via ``self.as_sync_reader(runner)`` where ``runner`` is a
    ``lambda coro: loop.run_until_complete(coro)`` or ``asyncio.run_coroutine_threadsafe``
    wrapper appropriate for the dispatcher's event-loop model.

    The class is deliberately tolerant of NATS-side errors:
      - Bucket missing on first access → ``read()`` returns ``{}`` and caches
        the "unavailable" state so we don't retry the same open() on every
        dispatch tick. Call ``rebind()`` to force a re-open (e.g. after
        NATS reconnects).
      - Bucket present but key absent → ``read()`` returns ``{}``.
      - Entry present but payload malformed → ``read()`` returns ``{}``.

    Zero polling. ``read`` is a single ``get`` per call. Callers that want
    a hot cache should watch the bucket via ``nats.js.KeyValue.watch`` and
    populate their own snapshot dict from the changes — that's out of
    scope for this reader; the reader answers a point-in-time question.
    """

    def __init__(self, js: Any, bucket_name: str = DEFAULT_BUCKET_NAME):
        """
        Args:
            js: a ``nats.js.JetStreamContext`` (from ``await nc.jetstream()``).
                Typed as ``Any`` because ``nats-py`` types are heavy and
                importing them at module scope forces a hard NATS dependency
                on every caller that just wants the sync layer.
            bucket_name: JetStream KV bucket to open. Defaults to
                ``hrv_node_state`` (overridable via ``HERMES_HRV_KV_BUCKET``).
        """
        self._js = js
        self._bucket_name = bucket_name
        self._kv: Optional[Any] = None
        self._kv_unavailable = False  # sticky "we already tried and failed"

    @property
    def bucket_name(self) -> str:
        return self._bucket_name

    async def _bind(self) -> Optional[Any]:
        """Return the KV handle, opening it lazily. ``None`` if unavailable."""
        if self._kv is not None:
            return self._kv
        if self._kv_unavailable:
            return None
        try:
            self._kv = await self._js.key_value(self._bucket_name)
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "hrv_kv_reader: bucket %r unavailable (%s: %r) — fail-open",
                self._bucket_name,
                type(exc).__name__,
                exc,
            )
            self._kv_unavailable = True
            self._kv = None
        return self._kv

    def rebind(self) -> None:
        """Forget the cached KV handle so the next ``read`` re-opens it.

        Call this after a NATS reconnect if the dispatcher wants to retry
        a bucket that was previously unavailable.
        """
        self._kv = None
        self._kv_unavailable = False

    async def read(self, node_id: str) -> dict[str, Any]:
        """Return the latest probe-state dict for ``node_id``, or ``{}``.

        See module docstring for the fail-open guarantees.
        """
        node_slug = str(node_id or "").strip()
        if not node_slug:
            return {}

        kv = await self._bind()
        if kv is None:
            return {}

        try:
            entry = await kv.get(node_slug)
        except Exception as exc:  # noqa: BLE001
            # `KeyNotFoundError` from nats.js.errors lands here too; we
            # treat every failure mode as "no signal" per the contract.
            logger.debug(
                "hrv_kv_reader: kv.get(%r) raised %s: %r — empty dict",
                node_slug,
                type(exc).__name__,
                exc,
            )
            return {}

        if entry is None:
            return {}

        value = getattr(entry, "value", None)
        if value is None:
            return {}

        # Reuse the sync layer for decoding. Wrap value in a trivial
        # closure to preserve the (key) → raw contract.
        return read_node_probe_state(node_slug, lambda _k: value)

    async def read_projection(self, node_id: str) -> dict[str, Any]:
        """Return ``read(node_id)`` projected to the four contract keys."""
        return project_probe_state(await self.read(node_id))

    def as_sync_reader(
        self,
        runner: Callable[[Awaitable[dict[str, Any]]], dict[str, Any]],
    ) -> Callable[[str], dict[str, Any]]:
        """Return a sync ``(node_id) -> dict`` callable suitable for the gate.

        Args:
            runner: converts an awaitable to its result. In the VCG
                dispatcher (which owns its own event loop) this is
                typically::

                    def run(coro):
                        fut = asyncio.run_coroutine_threadsafe(coro, loop)
                        try:
                            return fut.result(timeout=1.0)
                        except Exception:
                            return {}

                For a single-threaded async caller, just use
                ``asyncio.get_event_loop().run_until_complete``.

        Returns:
            A sync callable that mirrors the ``kv_reader`` contract expected
            by ``read_node_probe_state`` — ``(node_id) -> dict``.
        """

        def _sync_read(node_id: str) -> dict[str, Any]:
            try:
                return runner(self.read(node_id))
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "hrv_kv_reader: sync bridge raised %s: %r — empty dict",
                    type(exc).__name__,
                    exc,
                )
                return {}

        return _sync_read
