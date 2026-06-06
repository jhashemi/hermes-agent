"""Adapter-agnostic resilience tests for BasePlatformAdapter._send_with_retry +
the new server-disconnected retryable patterns.

Coverage targets:
  - gateway.platforms.base._RETRYABLE_ERROR_PATTERNS
  - gateway.platforms.base.BasePlatformAdapter._is_retryable_error
  - gateway.platforms.base.BasePlatformAdapter._send_with_retry (all branches)
  - gateway.platforms.whatsapp.WhatsAppAdapter.send (ServerDisconnected path)

Test layers:
  Unit: pure logic on _is_retryable_error patterns.
  Integration: _send_with_retry orchestrates send() retries with backoff.
  Edge cases: timeout-not-retryable, all-retries-exhausted notice path,
              non-network → plain-text fallback, formatting fallback also fails,
              switched-to-non-transient mid-retry.
  System: WhatsAppAdapter.send() returns retryable=True on
          aiohttp.ServerDisconnectedError so _send_with_retry catches it.
"""
from __future__ import annotations

import asyncio
from typing import Any, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    _RETRYABLE_ERROR_PATTERNS,
)


# ───────────────────────── helpers ──────────────────────────────────────────

class _AsyncCM:
    def __init__(self, value): self.value = value
    async def __aenter__(self): return self.value
    async def __aexit__(self, *a): return False


class _StubAdapter(BasePlatformAdapter):
    """Concrete adapter for testing base class methods.

    Records every send() call and returns a programmable sequence of results.
    """
    def __init__(self, results: List[SendResult]):
        # Bypass the heavy BasePlatformAdapter.__init__ — only set what
        # _send_with_retry actually touches.
        self.platform = Platform.WHATSAPP
        self._results = list(results)
        self.send_calls: List[dict] = []

    @property
    def name(self): return "stub"
    @property
    def is_running(self): return True
    async def connect(self): return True
    async def disconnect(self): pass
    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.send_calls.append({"chat_id": chat_id, "content": content, "reply_to": reply_to})
        if not self._results:
            return SendResult(success=True, message_id="default")
        return self._results.pop(0)
    async def get_user_info(self, user_id): return None
    async def get_chat_info(self, chat_id): return None
    async def edit_message(self, chat_id, message_id, content, **kw): return SendResult(success=True)
    async def delete_message(self, chat_id, message_id): return True

    # _StubAdapter is instantiated via __new__-bypass to avoid base __init__,
    # but we still want pytest to see it as concrete. Override metaclass check:
    __abstractmethods__ = frozenset()


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if asyncio._get_running_loop() else asyncio.run(coro)


# ───────────────────────── UNIT: retryable patterns ─────────────────────────

class TestRetryablePatternsUnit:
    """Pure-logic tests for _is_retryable_error pattern matching."""

    def test_server_disconnected_aiohttp_class_name_matches(self):
        # Existing bug: bare 'Server disconnected' string — confirm patch.
        assert BasePlatformAdapter._is_retryable_error("Server disconnected: foo")

    def test_serverdisconnected_no_space_matches(self):
        assert BasePlatformAdapter._is_retryable_error("ServerDisconnectedError")

    def test_clientconnectionerror_matches(self):
        assert BasePlatformAdapter._is_retryable_error(
            "aiohttp.ClientConnectionError: Cannot connect"
        )

    @pytest.mark.parametrize("err", [
        "ConnectionError: connection lost",
        "ConnectionResetError",
        "Network unreachable",
        "broken pipe at offset 4096",
        "RemoteDisconnected: Remote end closed",
        "EOFError",
        "ConnectTimeoutError after 30s",
    ])
    def test_legacy_patterns_still_match(self, err):
        assert BasePlatformAdapter._is_retryable_error(err)

    @pytest.mark.parametrize("err", [
        "Permission denied",
        "Invalid chat_id",
        "Rate limited",
        "400 Bad Request: malformed payload",
        "ValueError: bad markup",
        "",
    ])
    def test_non_network_errors_not_retryable(self, err):
        assert not BasePlatformAdapter._is_retryable_error(err)

    def test_none_is_not_retryable(self):
        assert not BasePlatformAdapter._is_retryable_error(None)

    def test_case_insensitive(self):
        assert BasePlatformAdapter._is_retryable_error("SERVER DISCONNECTED")
        assert BasePlatformAdapter._is_retryable_error("server DISCONNECTED")

    def test_pattern_table_includes_disconnect_signals(self):
        for needle in ("serverdisconnected", "server disconnected", "clientconnectionerror"):
            assert needle in _RETRYABLE_ERROR_PATTERNS, (
                f"Regression: {needle!r} missing from _RETRYABLE_ERROR_PATTERNS"
            )


# ───────────────────────── INTEGRATION: _send_with_retry ────────────────────

class TestSendWithRetryIntegration:
    """Orchestration tests — _send_with_retry coordinating send() retries."""

    @pytest.mark.asyncio
    async def test_first_call_success_no_retry(self):
        a = _StubAdapter([SendResult(success=True, message_id="ok")])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            r = await a._send_with_retry("chat", "hi")
        assert r.success is True
        assert len(a.send_calls) == 1

    @pytest.mark.asyncio
    async def test_retries_on_server_disconnected(self):
        # First fails with retryable error, second succeeds.
        a = _StubAdapter([
            SendResult(success=False, error="Server disconnected", retryable=False),
            SendResult(success=True, message_id="ok"),
        ])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            r = await a._send_with_retry("chat", "hi", base_delay=0.01)
        assert r.success is True
        assert len(a.send_calls) == 2

    @pytest.mark.asyncio
    async def test_retries_on_explicit_retryable_flag(self):
        a = _StubAdapter([
            SendResult(success=False, error="some opaque thing", retryable=True),
            SendResult(success=True, message_id="ok"),
        ])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            r = await a._send_with_retry("chat", "hi", base_delay=0.01)
        assert r.success is True
        assert len(a.send_calls) == 2

    @pytest.mark.asyncio
    async def test_all_retries_exhausted_sends_user_notice(self):
        # Every send fails network. _send_with_retry will then send a notice.
        a = _StubAdapter([
            SendResult(success=False, error="Server disconnected", retryable=False),
            SendResult(success=False, error="Server disconnected", retryable=False),
            SendResult(success=False, error="Server disconnected", retryable=False),
            SendResult(success=True, message_id="notice"),  # the user notice send
        ])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            r = await a._send_with_retry("chat", "hi", max_retries=2, base_delay=0.01)
        assert r.success is False  # original failure returned
        assert len(a.send_calls) == 4  # initial + 2 retries + notice
        # Last call should be the user-facing notice
        last = a.send_calls[-1]
        assert "delivery failed" in last["content"].lower()

    @pytest.mark.asyncio
    async def test_non_network_error_falls_back_to_plain_text(self):
        a = _StubAdapter([
            SendResult(success=False, error="Bad markdown formatting", retryable=False),
            SendResult(success=True, message_id="plain"),
        ])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            r = await a._send_with_retry("chat", "*broken* _markup_")
        assert r.success is True
        assert len(a.send_calls) == 2
        # Second call must be plain-text fallback
        assert "plain text" in a.send_calls[1]["content"].lower()

    @pytest.mark.asyncio
    async def test_timeout_returns_immediately_no_retry(self):
        # Timeouts NOT safe to retry (might already have been delivered).
        # Match patterns from _is_timeout_error: 'timed out', 'readtimeout', 'writetimeout'.
        a = _StubAdapter([
            SendResult(success=False, error="ReadTimeout: server took too long", retryable=False),
        ])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            r = await a._send_with_retry("chat", "hi")
        assert r.success is False
        assert len(a.send_calls) == 1  # exactly one call, no retry, no fallback

    @pytest.mark.asyncio
    async def test_switches_to_non_transient_mid_retry(self):
        # Network error → retry → permission error (not retryable) → fallback to plain text.
        a = _StubAdapter([
            SendResult(success=False, error="Server disconnected", retryable=False),
            SendResult(success=False, error="Permission denied", retryable=False),
            SendResult(success=True, message_id="plain"),
        ])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            r = await a._send_with_retry("chat", "hi", base_delay=0.01)
        assert r.success is True
        assert len(a.send_calls) == 3
        assert "plain text" in a.send_calls[2]["content"].lower()

    @pytest.mark.asyncio
    async def test_retryable_flag_overrides_pattern_check(self):
        # Even with non-matching error string, retryable=True forces retry.
        a = _StubAdapter([
            SendResult(success=False, error="totally opaque message", retryable=True),
            SendResult(success=True, message_id="ok"),
        ])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            r = await a._send_with_retry("chat", "hi", base_delay=0.01)
        assert r.success is True
        assert len(a.send_calls) == 2

    @pytest.mark.asyncio
    async def test_reply_to_propagated_through_retries(self):
        a = _StubAdapter([
            SendResult(success=False, error="Server disconnected", retryable=False),
            SendResult(success=True, message_id="ok"),
        ])
        with patch("asyncio.sleep", new_callable=AsyncMock):
            await a._send_with_retry("chat", "hi", reply_to="parent_msg_id", base_delay=0.01)
        for call in a.send_calls:
            assert call["reply_to"] == "parent_msg_id"

    @pytest.mark.asyncio
    async def test_user_notice_send_failure_swallowed(self):
        # All retries exhausted AND the delivery-failure notice send itself raises.
        # _send_with_retry must swallow the secondary exception and still return
        # the original result.
        class _ExplodingStub(_StubAdapter):
            def __init__(self):
                super().__init__([
                    SendResult(success=False, error="Server disconnected", retryable=False),
                    SendResult(success=False, error="Server disconnected", retryable=False),
                    SendResult(success=False, error="Server disconnected", retryable=False),
                ])
                self._notice_attempted = False

            async def send(self, chat_id, content, reply_to=None, metadata=None):
                if "delivery failed" in content.lower():
                    self._notice_attempted = True
                    raise RuntimeError("notice send also broken")
                return await super().send(chat_id, content, reply_to, metadata)

        a = _ExplodingStub()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            r = await a._send_with_retry("chat", "hi", max_retries=2, base_delay=0.01)
        assert r.success is False  # original failure surfaces despite notice exception
        assert a._notice_attempted is True


# ───────────────────────── SYSTEM: WhatsAppAdapter.send ─────────────────────

class TestWhatsAppSendDisconnectedReturnsRetryable:
    """End-to-end on the actual whatsapp adapter — confirm a real
    aiohttp.ServerDisconnectedError surfaces as SendResult(retryable=True)
    so _send_with_retry catches it.
    """

    def _make_adapter(self):
        from gateway.platforms.whatsapp import WhatsAppAdapter
        a = WhatsAppAdapter.__new__(WhatsAppAdapter)
        a.platform = Platform.WHATSAPP
        a._running = True
        a._bridge_port = 19999
        a._http_session = MagicMock()
        a._check_managed_bridge_exit = AsyncMock(return_value=None)
        a._outgoing_chunk_limit = lambda: 4096
        # send() calls self.format_message and self.truncate_message — give passthrough
        a.format_message = lambda c: c
        a.truncate_message = lambda c, lim: [c]
        return a

    @pytest.mark.asyncio
    async def test_server_disconnected_returns_retryable(self):
        import aiohttp
        a = self._make_adapter()

        class _DisconnectingPost:
            def __call__(self, *args, **kw):
                return self
            async def __aenter__(self):
                raise aiohttp.ServerDisconnectedError("connection reset by peer")
            async def __aexit__(self, *a): return False

        a._http_session.post = _DisconnectingPost()
        result = await a.send("chat_id_x", "hello")
        assert result.success is False
        assert result.retryable is True
        assert "server disconnected" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_client_connection_error_returns_retryable(self):
        import aiohttp
        a = self._make_adapter()

        class _ConnErrPost:
            def __call__(self, *args, **kw): return self
            async def __aenter__(self):
                raise aiohttp.ClientConnectionError("EPIPE")
            async def __aexit__(self, *a): return False

        a._http_session.post = _ConnErrPost()
        result = await a.send("chat_id_x", "hello")
        assert result.success is False
        assert result.retryable is True

    @pytest.mark.asyncio
    async def test_not_connected_returns_explicit_error(self):
        a = self._make_adapter()
        a._running = False
        result = await a.send("chat_id_x", "hello")
        assert result.success is False
        assert "not connected" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_no_http_session_returns_explicit_error(self):
        a = self._make_adapter()
        a._http_session = None
        result = await a.send("chat_id_x", "hello")
        assert result.success is False
        assert "not connected" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_empty_content_returns_success_no_id(self):
        a = self._make_adapter()
        result = await a.send("chat_id_x", "")
        assert result.success is True
        assert result.message_id is None

    @pytest.mark.asyncio
    async def test_whitespace_only_content_returns_success_no_id(self):
        a = self._make_adapter()
        result = await a.send("chat_id_x", "   \n\t  ")
        assert result.success is True
        assert result.message_id is None

    @pytest.mark.asyncio
    async def test_bridge_exit_short_circuits(self):
        a = self._make_adapter()
        a._check_managed_bridge_exit = AsyncMock(return_value="bridge crashed")
        result = await a.send("chat_id_x", "hello")
        assert result.success is False
        assert "bridge crashed" in (result.error or "")

    @pytest.mark.asyncio
    async def test_non_200_returns_error_text(self):
        a = self._make_adapter()

        class _BadResp:
            status = 500
            async def text(self): return "internal server error"

        class _Post:
            def __call__(self, *a_, **kw): return self
            async def __aenter__(self): return _BadResp()
            async def __aexit__(self, *a_): return False

        a._http_session.post = _Post()
        result = await a.send("chat_id_x", "hello")
        assert result.success is False
        # Not retryable — this is a server error, not a connection drop
        assert result.retryable is False
        assert "internal server error" in (result.error or "")

    @pytest.mark.asyncio
    async def test_200_success_returns_message_id(self):
        a = self._make_adapter()

        class _GoodResp:
            status = 200
            async def json(self): return {"messageId": "wa-msg-123"}

        class _Post:
            def __call__(self, *a_, **kw): return self
            async def __aenter__(self): return _GoodResp()
            async def __aexit__(self, *a_): return False

        a._http_session.post = _Post()
        result = await a.send("chat_id_x", "hello")
        assert result.success is True
        assert result.message_id == "wa-msg-123"

    @pytest.mark.asyncio
    async def test_chunked_send_only_first_chunk_gets_reply_to(self):
        # Multi-chunk message: reply_to should appear only on chunk[0]'s payload.
        a = self._make_adapter()
        a.truncate_message = lambda c, lim: ["chunk1", "chunk2", "chunk3"]
        seen_payloads: List[dict] = []

        class _GoodResp:
            status = 200
            async def json(self): return {"messageId": f"id-{len(seen_payloads)}"}

        class _Post:
            def __call__(self, url, json=None, **kw):
                seen_payloads.append(json)
                return self
            async def __aenter__(self): return _GoodResp()
            async def __aexit__(self, *a_): return False

        a._http_session.post = _Post()
        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await a.send("chat_id_x", "long content", reply_to="parent")
        assert result.success is True
        assert len(seen_payloads) == 3
        assert seen_payloads[0].get("replyTo") == "parent"
        assert "replyTo" not in seen_payloads[1]
        assert "replyTo" not in seen_payloads[2]

    @pytest.mark.asyncio
    async def test_unexpected_exception_returns_non_retryable(self):
        # Generic exception path — NOT retryable (could be programming error).
        a = self._make_adapter()

        class _RaisingPost:
            def __call__(self, *a_, **kw): return self
            async def __aenter__(self):
                raise RuntimeError("totally unexpected internal error")
            async def __aexit__(self, *a_): return False

        a._http_session.post = _RaisingPost()
        result = await a.send("chat_id_x", "hello")
        assert result.success is False
        assert result.retryable is False
        assert "totally unexpected" in (result.error or "")
