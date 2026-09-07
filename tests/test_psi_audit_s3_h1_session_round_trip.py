"""Ψ-Audit S3/H1 acceptance — Session-search round-trip residual.

KR kr_s3_h1_1 — "Session-search round-trip residual ≤ 0.10 over 100
query-pair runs (idempotency confirmed within compaction tolerance)".

The Free⊣Cofree adjunction predicts ``decode(encode(x)) ≡ x`` at the
SessionDB conversation-memory boundary (``hermes_state.py:1394-1428``).

This test pins the round-trip identity over the JSON-serializable subset
that real LLM message content occupies in production:
  - plain strings (assistant/user text)
  - structured multimodal lists ([{"type": "text", ...}, {"type": "image_url", ...}])
  - nested dicts (tool_calls payloads)
  - scalars (None, int, float)

Residual := fraction(query_pairs where decode(encode(x)) != x) / 100.
Target  := residual ≤ 0.10.

Per RSI Cycle 3 trace (``psi_artifact/cycles/RSI_TRACE_3_S5H2_S3H1.md``)
the round-trip holds for the JSON-serializable subset; the failure mode
documented there (β₆) only fires on non-serializable Python objects, which
the production callsite contract prohibits.
"""
from __future__ import annotations

import random

import pytest

from hermes_state import SessionDB


def _gen_realistic_message_content(rng: random.Random):
    """Generate a content payload from the JSON-serializable subset
    that LLM messages occupy in production."""
    kind = rng.choice([
        "plain_str",
        "empty_str",
        "scalar_none",
        "scalar_int",
        "scalar_float",
        "multimodal_text_only",
        "multimodal_text_image",
        "multimodal_text_multiple",
        "tool_call_dict",
        "nested_dict",
    ])
    if kind == "plain_str":
        return f"hello world {rng.randint(0, 1_000_000)}"
    if kind == "empty_str":
        return ""
    if kind == "scalar_none":
        return None
    if kind == "scalar_int":
        return rng.randint(-1_000_000, 1_000_000)
    if kind == "scalar_float":
        return rng.uniform(-1e6, 1e6)
    if kind == "multimodal_text_only":
        return [{"type": "text", "text": f"msg-{rng.randint(0, 9999)}"}]
    if kind == "multimodal_text_image":
        return [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": f"https://x.test/{rng.randint(0, 999)}.png"}},
        ]
    if kind == "multimodal_text_multiple":
        return [
            {"type": "text", "text": f"part-{i}"}
            for i in range(rng.randint(1, 5))
        ]
    if kind == "tool_call_dict":
        return {
            "id": f"call_{rng.randint(0, 9999):04d}",
            "type": "function",
            "function": {
                "name": rng.choice(["search", "patch", "read_file"]),
                "arguments": '{"path": "/tmp/x"}',
            },
        }
    # nested_dict
    return {
        "tag": "session",
        "depth": rng.randint(1, 5),
        "items": [{"k": i, "v": f"v{i}"} for i in range(rng.randint(1, 4))],
        "meta": {"author": "test", "score": rng.random()},
    }


class TestPsiAuditS3H1RoundTrip:
    """KR kr_s3_h1_1 — 100 query-pair runs, residual ≤ 0.10."""

    N_PAIRS = 100
    RESIDUAL_TARGET = 0.10

    def test_round_trip_residual_below_target_over_100_pairs(self) -> None:
        rng = random.Random(0xDEADBEEF)
        violations = 0
        for _ in range(self.N_PAIRS):
            x = _gen_realistic_message_content(rng)
            encoded = SessionDB._encode_content(x)
            decoded = SessionDB._decode_content(encoded)
            if decoded != x:
                violations += 1

        residual = violations / self.N_PAIRS
        assert residual <= self.RESIDUAL_TARGET, (
            f"S3/H1 round-trip residual {residual:.4f} > "
            f"target {self.RESIDUAL_TARGET}; violations={violations}/{self.N_PAIRS}"
        )

    @pytest.mark.parametrize("scalar", [None, "", "plain", 0, 1, -1, 3.14, -2.7])
    def test_scalar_passthrough_identity(self, scalar) -> None:
        """Per encoder contract: scalars (None/str/bytes/int/float) pass through."""
        assert SessionDB._encode_content(scalar) == scalar
        assert SessionDB._decode_content(scalar) == scalar

    def test_multimodal_list_round_trip(self) -> None:
        """Multimodal message list survives encode/decode."""
        x = [
            {"type": "text", "text": "describe this image"},
            {"type": "image_url", "image_url": {"url": "https://a.test/i.png"}},
        ]
        encoded = SessionDB._encode_content(x)
        assert isinstance(encoded, str)
        assert encoded.startswith(SessionDB._CONTENT_JSON_PREFIX)
        assert SessionDB._decode_content(encoded) == x

    def test_nested_dict_round_trip(self) -> None:
        """Nested dicts (tool_call payloads) survive encode/decode."""
        x = {
            "id": "call_42",
            "function": {"name": "search", "arguments": '{"q": "x"}'},
            "nested": {"a": [1, 2, {"deep": True}]},
        }
        assert SessionDB._decode_content(SessionDB._encode_content(x)) == x

    def test_decode_idempotent_on_already_decoded(self) -> None:
        """decode(decode(encode(x))) == decode(encode(x)) — 2nd decode no-op."""
        x = [{"type": "text", "text": "hi"}]
        once = SessionDB._decode_content(SessionDB._encode_content(x))
        twice = SessionDB._decode_content(once)
        assert once == twice == x
