"""Tests for hermes_cli.hrv_kv_reader — the NATS KV probe-state reader.

Covers t_561cbe31's acceptance:
  * Returns a dict with (at least) the four contract keys when the KV
    entry carries them.
  * Returns an empty dict when the bucket or key is missing.
  * Does not touch any DB — only reads from the injected KV reader.

Also covers the fail-open guarantees documented in the module: raising
readers, malformed JSON, non-dict payloads, unexpected types, and the
async NATS binder using a fake JetStream context.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

import pytest

from hermes_cli.hrv_kv_reader import (
    DEFAULT_BUCKET_NAME,
    PROBE_STATE_KEYS,
    HRVKVReader,
    project_probe_state,
    read_node_probe_state,
)


# ─────────────────────────────────────────────────────────────────────
# read_node_probe_state — the pure sync predicate over a kv_reader callable
# ─────────────────────────────────────────────────────────────────────


class TestReadNodePayloadShapes:
    """Payload shape acceptance for the sync reader."""

    def test_returns_full_payload_dict_when_reader_yields_dict(self):
        payload = {
            "memory_pressure": "green",
            "kanban_dispatcher_health": "green",
            "bedrock_rate_limit_saturation": "green",
            "hrv.status.digest.interval_class": "calm",
            "hostname": "hermes2",
            "ts": "2026-08-22T02:10:00Z",
        }
        result = read_node_probe_state("hermes2", lambda k: payload)
        assert result == payload
        # All four acceptance keys are present.
        for key in PROBE_STATE_KEYS:
            assert key in result

    def test_defensive_copy_when_reader_returns_dict(self):
        # Mutating the returned dict must not corrupt the reader's source.
        source = {"memory_pressure": "green", "hrv.status.digest.interval_class": "calm"}
        result = read_node_probe_state("hermes2", lambda k: source)
        result["memory_pressure"] = "RED"
        assert source["memory_pressure"] == "green"

    def test_decodes_bytes_payload(self):
        payload = {
            "memory_pressure": "yellow",
            "kanban_dispatcher_health": "green",
            "bedrock_rate_limit_saturation": "green",
            "hrv.status.digest.interval_class": "alert",
        }
        raw = json.dumps(payload).encode("utf-8")
        result = read_node_probe_state("hermes2", lambda k: raw)
        assert result == payload

    def test_decodes_str_payload(self):
        payload = {"memory_pressure": "green"}
        raw = json.dumps(payload)
        result = read_node_probe_state("hermes2", lambda k: raw)
        assert result == payload

    def test_decodes_bytearray_payload(self):
        payload = {"memory_pressure": "green"}
        raw = bytearray(json.dumps(payload).encode("utf-8"))
        result = read_node_probe_state("hermes2", lambda k: raw)
        assert result == payload

    def test_reader_receives_stripped_node_id(self):
        captured: list[str] = []

        def reader(k: str):
            captured.append(k)
            return {"memory_pressure": "green"}

        read_node_probe_state("  hermes2  ", reader)
        assert captured == ["hermes2"]


class TestReadNodeFailOpen:
    """Every failure mode must return {} and never raise."""

    def test_empty_dict_when_key_missing(self):
        result = read_node_probe_state("hermes2", lambda k: None)
        assert result == {}

    def test_empty_dict_when_reader_raises(self):
        def raising_reader(k: str):
            raise RuntimeError("bucket does not exist")

        result = read_node_probe_state("hermes2", raising_reader)
        assert result == {}

    def test_empty_dict_when_reader_raises_key_error(self):
        # nats.js raises `KeyNotFoundError`; the reader should treat any
        # exception class as "no signal".
        def raising_reader(k: str):
            raise KeyError(k)

        assert read_node_probe_state("hermes2", raising_reader) == {}

    def test_empty_dict_on_malformed_json_bytes(self):
        result = read_node_probe_state("hermes2", lambda k: b"not-json{{{")
        assert result == {}

    def test_empty_dict_on_malformed_json_str(self):
        result = read_node_probe_state("hermes2", lambda k: "still not json")
        assert result == {}

    def test_empty_dict_on_invalid_utf8_bytes(self):
        result = read_node_probe_state("hermes2", lambda k: b"\xff\xfe\xfd")
        assert result == {}

    def test_empty_dict_when_json_top_level_is_list(self):
        result = read_node_probe_state(
            "hermes2", lambda k: json.dumps(["memory_pressure"])
        )
        assert result == {}

    def test_empty_dict_when_json_top_level_is_null(self):
        result = read_node_probe_state("hermes2", lambda k: "null")
        assert result == {}

    def test_empty_dict_when_json_top_level_is_scalar(self):
        result = read_node_probe_state("hermes2", lambda k: "42")
        assert result == {}

    def test_empty_dict_on_unexpected_payload_type(self):
        # Reader returned an int — not something we know how to decode.
        result = read_node_probe_state("hermes2", lambda k: 42)
        assert result == {}

    def test_empty_dict_on_empty_node_id(self):
        # The reader must not be called at all for an empty node id.
        calls: list[str] = []

        def reader(k: str):
            calls.append(k)
            return {"memory_pressure": "green"}

        assert read_node_probe_state("", reader) == {}
        assert read_node_probe_state("   ", reader) == {}
        assert read_node_probe_state(None, reader) == {}  # type: ignore[arg-type]
        assert calls == []


# ─────────────────────────────────────────────────────────────────────
# project_probe_state — projection to the four acceptance-criteria keys
# ─────────────────────────────────────────────────────────────────────


class TestProjectProbeState:

    def test_projects_only_the_four_contract_keys(self):
        payload = {
            "memory_pressure": "green",
            "kanban_dispatcher_health": "yellow",
            "bedrock_rate_limit_saturation": "green",
            "hrv.status.digest.interval_class": "calm",
            "hostname": "hermes2",
            "ts": "2026-08-22T02:10:00Z",
            "swap_pct": 41.0,
        }
        result = project_probe_state(payload)
        assert set(result.keys()) == set(PROBE_STATE_KEYS)
        assert result["memory_pressure"] == "green"
        assert result["kanban_dispatcher_health"] == "yellow"
        assert result["bedrock_rate_limit_saturation"] == "green"
        assert result["hrv.status.digest.interval_class"] == "calm"

    def test_omits_missing_keys_rather_than_none(self):
        # Only two of four keys present — projection must NOT invent None
        # values for the missing two. This lets callers distinguish
        # "probe has no opinion" from "probe reports None".
        payload = {
            "memory_pressure": "green",
            "hrv.status.digest.interval_class": "calm",
            "ignored_extra": True,
        }
        result = project_probe_state(payload)
        assert result == {
            "memory_pressure": "green",
            "hrv.status.digest.interval_class": "calm",
        }
        assert "kanban_dispatcher_health" not in result
        assert "bedrock_rate_limit_saturation" not in result

    def test_empty_dict_on_empty_input(self):
        assert project_probe_state({}) == {}

    def test_empty_dict_on_non_mapping_input(self):
        assert project_probe_state(None) == {}  # type: ignore[arg-type]
        assert project_probe_state("string") == {}  # type: ignore[arg-type]
        assert project_probe_state([1, 2, 3]) == {}  # type: ignore[arg-type]

    def test_preserves_none_value_when_key_present(self):
        # If the probe explicitly reported None (i.e. it evaluated and said
        # "no data"), we keep the None so callers see the reported shape.
        payload = {"memory_pressure": None, "kanban_dispatcher_health": "green"}
        result = project_probe_state(payload)
        assert result == {"memory_pressure": None, "kanban_dispatcher_health": "green"}


# ─────────────────────────────────────────────────────────────────────
# HRVKVReader — async NATS binder, using a fake JetStream context
# ─────────────────────────────────────────────────────────────────────


class _FakeEntry:
    """Minimal stand-in for nats.js.KeyValue.Entry (only needs .value)."""

    def __init__(self, value: Optional[bytes]):
        self.value = value


class _FakeKV:
    """Fake KeyValue bucket. Configurable to return values or raise."""

    def __init__(self, entries: Optional[dict[str, Any]] = None,
                 raise_on: Optional[dict[str, Exception]] = None):
        self._entries = entries or {}
        self._raise = raise_on or {}
        self.get_calls: list[str] = []

    async def get(self, key: str) -> Optional[_FakeEntry]:
        self.get_calls.append(key)
        if key in self._raise:
            raise self._raise[key]
        if key not in self._entries:
            # nats-py's real behaviour is to raise KeyNotFoundError here;
            # our reader catches every exception, so we simulate both
            # shapes across separate tests.
            from nats.js.errors import KeyNotFoundError  # type: ignore
            raise KeyNotFoundError(f"key {key!r} not found")
        return _FakeEntry(self._entries[key])


class _FakeJS:
    """Fake JetStreamContext. Returns a preconfigured _FakeKV or raises."""

    def __init__(self, kv: Optional[_FakeKV] = None,
                 raise_on_bind: Optional[Exception] = None):
        self._kv = kv
        self._raise_on_bind = raise_on_bind
        self.key_value_calls: list[str] = []

    async def key_value(self, bucket: str) -> _FakeKV:
        self.key_value_calls.append(bucket)
        if self._raise_on_bind is not None:
            raise self._raise_on_bind
        assert self._kv is not None, "test bug: bind attempted with no fake KV"
        return self._kv


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


class TestHRVKVReaderAsync:

    def test_bucket_name_default(self):
        reader = HRVKVReader(js=_FakeJS(kv=_FakeKV()))
        assert reader.bucket_name == DEFAULT_BUCKET_NAME

    def test_bucket_name_override(self):
        reader = HRVKVReader(js=_FakeJS(kv=_FakeKV()), bucket_name="custom_bucket")
        assert reader.bucket_name == "custom_bucket"

    def test_read_returns_full_payload_dict(self):
        payload = {
            "memory_pressure": "green",
            "kanban_dispatcher_health": "green",
            "bedrock_rate_limit_saturation": "yellow",
            "hrv.status.digest.interval_class": "alert",
        }
        entries = {"hermes2": json.dumps(payload).encode("utf-8")}
        kv = _FakeKV(entries=entries)
        js = _FakeJS(kv=kv)
        reader = HRVKVReader(js=js)

        result = _run(reader.read("hermes2"))
        assert result == payload
        assert js.key_value_calls == [DEFAULT_BUCKET_NAME]
        assert kv.get_calls == ["hermes2"]

    def test_read_caches_kv_handle_across_calls(self):
        entries = {
            "hermes2": json.dumps({"memory_pressure": "green"}).encode("utf-8"),
            "hermes3": json.dumps({"memory_pressure": "yellow"}).encode("utf-8"),
        }
        kv = _FakeKV(entries=entries)
        js = _FakeJS(kv=kv)
        reader = HRVKVReader(js=js)

        async def _drive():
            r1 = await reader.read("hermes2")
            r2 = await reader.read("hermes3")
            r3 = await reader.read("hermes2")
            return r1, r2, r3

        r1, r2, r3 = _run(_drive())
        assert r1["memory_pressure"] == "green"
        assert r2["memory_pressure"] == "yellow"
        assert r3["memory_pressure"] == "green"
        # key_value() opened exactly once across three reads.
        assert js.key_value_calls == [DEFAULT_BUCKET_NAME]
        assert kv.get_calls == ["hermes2", "hermes3", "hermes2"]

    def test_read_returns_empty_when_bucket_missing(self):
        # `key_value()` raises → bucket unavailable → empty dict.
        js = _FakeJS(raise_on_bind=RuntimeError("bucket 'hrv_node_state' not found"))
        reader = HRVKVReader(js=js)
        assert _run(reader.read("hermes2")) == {}
        # A subsequent call must not re-attempt the same failed open()
        # (avoid slamming NATS on every dispatch tick).
        assert _run(reader.read("hermes2")) == {}
        assert js.key_value_calls == [DEFAULT_BUCKET_NAME]

    def test_read_returns_empty_when_key_missing(self):
        kv = _FakeKV(entries={})  # get() will raise KeyNotFoundError
        js = _FakeJS(kv=kv)
        reader = HRVKVReader(js=js)
        assert _run(reader.read("hermes2")) == {}

    def test_read_returns_empty_when_get_raises_generic_error(self):
        kv = _FakeKV(raise_on={"hermes2": RuntimeError("nats connection closed")})
        js = _FakeJS(kv=kv)
        reader = HRVKVReader(js=js)
        assert _run(reader.read("hermes2")) == {}

    def test_read_returns_empty_when_entry_value_is_none(self):
        kv = _FakeKV(entries={"hermes2": None})
        js = _FakeJS(kv=kv)
        reader = HRVKVReader(js=js)
        assert _run(reader.read("hermes2")) == {}

    def test_read_returns_empty_when_entry_payload_is_malformed(self):
        kv = _FakeKV(entries={"hermes2": b"not-json{{{"})
        js = _FakeJS(kv=kv)
        reader = HRVKVReader(js=js)
        assert _run(reader.read("hermes2")) == {}

    def test_read_projection_returns_only_four_keys(self):
        payload = {
            "memory_pressure": "green",
            "kanban_dispatcher_health": "green",
            "bedrock_rate_limit_saturation": "green",
            "hrv.status.digest.interval_class": "calm",
            "extra_field": "ignore-me",
            "ts": "2026-08-22T02:10:00Z",
        }
        entries = {"hermes2": json.dumps(payload).encode("utf-8")}
        kv = _FakeKV(entries=entries)
        js = _FakeJS(kv=kv)
        reader = HRVKVReader(js=js)

        result = _run(reader.read_projection("hermes2"))
        assert set(result.keys()) == set(PROBE_STATE_KEYS)
        assert result["memory_pressure"] == "green"

    def test_rebind_forgets_cached_handle(self):
        # First bind fails → sticky unavailable. rebind() clears that.
        kv_after = _FakeKV(
            entries={"hermes2": json.dumps({"memory_pressure": "green"}).encode("utf-8")}
        )

        # A JS whose first bind fails, second succeeds. We flip
        # _raise_on_bind manually after rebind().
        js = _FakeJS(raise_on_bind=RuntimeError("bucket missing"))
        reader = HRVKVReader(js=js)
        assert _run(reader.read("hermes2")) == {}

        # Simulate NATS came back — new bucket exists now.
        js._raise_on_bind = None
        js._kv = kv_after

        # Without rebind() the reader is still in sticky-unavailable mode.
        assert _run(reader.read("hermes2")) == {}

        reader.rebind()
        result = _run(reader.read("hermes2"))
        assert result == {"memory_pressure": "green"}


# ─────────────────────────────────────────────────────────────────────
# as_sync_reader — bridge from async NATS binder to sync gate callable
# ─────────────────────────────────────────────────────────────────────


class TestAsSyncReader:

    def test_sync_bridge_returns_dict_from_async_read(self):
        payload = {
            "memory_pressure": "green",
            "kanban_dispatcher_health": "green",
            "bedrock_rate_limit_saturation": "green",
            "hrv.status.digest.interval_class": "calm",
        }
        entries = {"hermes2": json.dumps(payload).encode("utf-8")}
        kv = _FakeKV(entries=entries)
        js = _FakeJS(kv=kv)
        reader = HRVKVReader(js=js)

        # Runner just runs the coroutine on a fresh event loop each call
        # (fine for tests; production callers use run_coroutine_threadsafe).
        def runner(coro):
            return asyncio.new_event_loop().run_until_complete(coro)

        sync_read = reader.as_sync_reader(runner)
        result = sync_read("hermes2")
        assert result == payload

    def test_sync_bridge_returns_empty_dict_when_runner_raises(self):
        kv = _FakeKV(entries={"hermes2": b"{}"})
        js = _FakeJS(kv=kv)
        reader = HRVKVReader(js=js)

        def bad_runner(coro):
            # Simulate the dispatcher's async->sync bridge timing out.
            coro.close()
            raise TimeoutError("sync bridge exceeded 1s budget")

        sync_read = reader.as_sync_reader(bad_runner)
        assert sync_read("hermes2") == {}

    def test_sync_bridge_composes_with_read_node_probe_state(self):
        # End-to-end: HRVKVReader → as_sync_reader → read_node_probe_state.
        # This is the shape the dispatcher's gate will actually invoke.
        payload = {
            "memory_pressure": "green",
            "kanban_dispatcher_health": "green",
            "bedrock_rate_limit_saturation": "green",
            "hrv.status.digest.interval_class": "calm",
        }
        entries = {"hermes2": json.dumps(payload).encode("utf-8")}
        kv = _FakeKV(entries=entries)
        js = _FakeJS(kv=kv)
        reader = HRVKVReader(js=js)

        def runner(coro):
            return asyncio.new_event_loop().run_until_complete(coro)

        # The sync bridge returns the already-decoded dict, so wrapping
        # it in read_node_probe_state passes through cleanly.
        sync_reader = reader.as_sync_reader(runner)
        result = read_node_probe_state("hermes2", sync_reader)
        assert result == payload
        for key in PROBE_STATE_KEYS:
            assert key in result
