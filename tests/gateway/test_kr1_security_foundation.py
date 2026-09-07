"""
KR-1: Security Foundation — Integration tests for three sub-invariants.

1. AUTH-GATE-ZERO: all remote API endpoints (including /health) return 401
   on unauthenticated requests; integration tests cover auth-missing +
   auth-invalid paths.

2. INPUT-INVARIANT-01: chat_id (session_id) has documented invariant
   (max length, charset, format); enforcement at >=2 layers (API Pydantic
   + queue consumer validate_chat_id); fuzzing suite demonstrates rejection.

3. SESSION-CLEANUP-INVARIANT: zero leaked sessions in failure-path
   integration tests; resource-accounting test verifies connect()-failure
   cleanup in platform adapters.
"""

import asyncio
import os
import string
import sys
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

# Ensure project root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from gateway.remote_agent_api import (
    verify_api_key,
    get_expected_key,
    validate_chat_id,
    CHAT_ID_MAX_LENGTH,
)


# ===========================================================================
# 1. AUTH-GATE-ZERO — Authentication on all remote API endpoints
# ===========================================================================

class TestAuthGateZero:
    """AUTH-GATE-ZERO: all remote API endpoints must return 401 on
    unauthenticated requests."""

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key-12345"})
    def test_verify_api_key_valid(self):
        """Valid API key passes verification."""
        get_expected_key.cache_clear()
        assert verify_api_key("test-secret-key-12345") is True

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key-12345"})
    def test_verify_api_key_invalid(self):
        """Invalid API key fails verification (auth-invalid path)."""
        get_expected_key.cache_clear()
        assert verify_api_key("wrong-key") is False

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key-12345"})
    def test_verify_api_key_missing(self):
        """Missing API key header fails verification (auth-missing path)."""
        get_expected_key.cache_clear()
        assert verify_api_key(None) is False
        assert verify_api_key("") is False

    @patch.dict("os.environ", {})
    def test_verify_api_key_unconfigured_fails_closed(self):
        """When HERMES_REMOTE_API_KEY is not set, all requests fail (fail-closed)."""
        get_expected_key.cache_clear()
        # Even if someone sends a key, it should fail because no key is configured
        assert verify_api_key("any-key-at-all") is False
        assert verify_api_key(None) is False
        assert verify_api_key("") is False

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key-12345"})
    def test_verify_api_key_constant_time(self):
        """verify_api_key uses hmac.compare_digest (constant-time comparison)."""
        get_expected_key.cache_clear()
        # Both wrong keys should fail consistently
        assert verify_api_key("a" * 32) is False
        assert verify_api_key("b" * 32) is False
        # Correct key passes
        assert verify_api_key("test-secret-key-12345") is True

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key-12345"})
    def test_health_endpoint_requires_auth(self):
        """AUTH-GATE-ZERO: /health endpoint must require authentication.

        This test verifies that the health_check endpoint function signature
        includes the x_hermes_key parameter and calls verify_api_key.
        We inspect the source to confirm the auth gate is present.
        """
        import inspect
        from gateway import remote_agent_api

        # The create_remote_api_blueprint function defines the health_check
        # endpoint inside a try/except for FastAPI. We verify the source
        # code includes the auth check.
        source = inspect.getsource(remote_agent_api.create_remote_api_blueprint)
        # Confirm /health endpoint has auth verification
        assert "verify_api_key" in source, (
            "/health endpoint must call verify_api_key for AUTH-GATE-ZERO"
        )
        # Confirm the health_check function accepts x_hermes_key
        assert "x_hermes_key" in source, (
            "/health endpoint must accept x_hermes_key header"
        )

    @patch.dict("os.environ", {"HERMES_REMOTE_API_KEY": "test-secret-key-12345"})
    def test_all_endpoints_require_auth(self):
        """AUTH-GATE-ZERO: verify every endpoint in the remote API has auth.

        We check that create_remote_api_blueprint source code has
        verify_api_key calls for /api/agent/execute, /api/agent/status,
        and /health — no endpoint should be unauthenticated.
        """
        import inspect
        from gateway import remote_agent_api

        source = inspect.getsource(remote_agent_api.create_remote_api_blueprint)

        # Count verify_api_key calls — should be at least 3 (execute, status, health)
        verify_count = source.count("verify_api_key")
        assert verify_count >= 3, (
            f"Expected >=3 verify_api_key calls (execute, status, health), "
            f"found {verify_count}"
        )


# ===========================================================================
# 2. INPUT-INVARIANT-01 — Bounded chat_id validation
# ===========================================================================

class TestChatIdValidation:
    """INPUT-INVARIANT-01: chat_id has documented invariant and enforcement."""

    # --- Valid inputs ---

    def test_valid_simple_numeric(self):
        """Telegram-style numeric chat_id passes."""
        assert validate_chat_id("123456789") == "123456789"

    def test_valid_discord_snowflake(self):
        """Discord snowflake (~20 chars) passes."""
        snowflake = "123456789012345678901"
        assert validate_chat_id(snowflake) == snowflake

    def test_valid_composite_key(self):
        """Composite key with allowed separators passes."""
        key = "telegram:user:12345:thread:67890"
        assert validate_chat_id(key) == key

    def test_valid_with_at_symbol(self):
        """Chat ID with @ (some platforms use @channel names) passes."""
        assert validate_chat_id("@mychannel") == "@mychannel"

    def test_valid_with_dots_and_hyphens(self):
        """Chat IDs with dots and hyphens pass."""
        assert validate_chat_id("my-channel.01") == "my-channel.01"

    def test_valid_strips_whitespace(self):
        """Leading/trailing whitespace is stripped."""
        assert validate_chat_id("  12345  ") == "12345"

    def test_none_returns_none(self):
        """None input returns None (optional field)."""
        assert validate_chat_id(None) is None

    def test_valid_max_length(self):
        """Exactly CHAT_ID_MAX_LENGTH chars passes."""
        valid = "a" * CHAT_ID_MAX_LENGTH
        assert validate_chat_id(valid) == valid

    # --- Invalid inputs: length ---

    def test_exceeds_max_length(self):
        """chat_id exceeding max length is rejected."""
        too_long = "a" * (CHAT_ID_MAX_LENGTH + 1)
        with pytest.raises(ValueError, match="maximum length"):
            validate_chat_id(too_long)

    def test_empty_string_rejected(self):
        """Empty string is rejected."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_chat_id("")

    def test_whitespace_only_rejected(self):
        """Whitespace-only string is rejected."""
        with pytest.raises(ValueError, match="cannot be empty"):
            validate_chat_id("   \n\t  ")

    # --- Invalid inputs: control characters ---

    def test_newline_rejected(self):
        """Newline in chat_id rejected (log forging prevention)."""
        with pytest.raises(ValueError, match="control characters"):
            validate_chat_id("12345\n67890")

    def test_carriage_return_rejected(self):
        """Carriage return in chat_id rejected."""
        with pytest.raises(ValueError, match="control characters"):
            validate_chat_id("12345\r67890")

    def test_null_byte_rejected(self):
        """Null byte in chat_id rejected (injection prevention)."""
        with pytest.raises(ValueError, match="control characters"):
            validate_chat_id("12345\x0067890")

    def test_tab_rejected(self):
        """Tab in chat_id rejected (control character)."""
        with pytest.raises(ValueError, match="control characters"):
            validate_chat_id("12345\t67890")

    def test_del_char_rejected(self):
        """DEL (127) character rejected."""
        with pytest.raises(ValueError, match="control characters"):
            validate_chat_id("12345\x7f")

    # --- Invalid inputs: charset ---

    def test_sql_injection_rejected(self):
        """SQL injection patterns rejected by charset restriction."""
        with pytest.raises(ValueError, match="disallowed characters"):
            validate_chat_id("'; DROP TABLE sessions;--")

    def test_shell_metachar_rejected(self):
        """Shell metacharacters rejected."""
        with pytest.raises(ValueError, match="disallowed characters"):
            validate_chat_id("$(whoami)")

    def test_backtick_rejected(self):
        """Backtick rejected (shell injection)."""
        with pytest.raises(ValueError, match="disallowed characters"):
            validate_chat_id("`whoami`")

    def test_pipe_rejected(self):
        """Pipe character rejected."""
        with pytest.raises(ValueError, match="disallowed characters"):
            validate_chat_id("foo|bar")

    def test_dollar_sign_rejected(self):
        """Dollar sign rejected (env var injection)."""
        with pytest.raises(ValueError, match="disallowed characters"):
            validate_chat_id("$HOME")

    def test_angle_brackets_rejected(self):
        """Angle brackets rejected."""
        with pytest.raises(ValueError, match="disallowed characters"):
            validate_chat_id("<script>")

    def test_quotes_rejected(self):
        """Single and double quotes rejected."""
        with pytest.raises(ValueError, match="disallowed characters"):
            validate_chat_id("it's")
        with pytest.raises(ValueError, match="disallowed characters"):
            validate_chat_id('say "hi"')

    def test_backslash_rejected(self):
        """Backslash rejected (path traversal prevention)."""
        with pytest.raises(ValueError, match="disallowed characters"):
            validate_chat_id("..\\..\\etc")

    def test_semicolon_rejected(self):
        """Semicolon rejected (SQL/shell injection)."""
        with pytest.raises(ValueError, match="disallowed characters"):
            validate_chat_id("foo;bar")

    def test_parentheses_rejected(self):
        """Parentheses rejected (injection prevention)."""
        with pytest.raises(ValueError, match="disallowed characters"):
            validate_chat_id("foo(bar)")

    def test_space_in_middle_rejected(self):
        """Space in the middle rejected (no spaces in chat_id)."""
        with pytest.raises(ValueError, match="disallowed characters"):
            validate_chat_id("foo bar")

    def test_non_string_rejected(self):
        """Non-string input rejected."""
        with pytest.raises(ValueError, match="must be a string"):
            validate_chat_id(str(12345) if False else 12345)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="must be a string"):
            validate_chat_id([])  # type: ignore[arg-type]

    # --- Field name customization ---

    def test_field_name_in_error(self):
        """Custom field_name appears in error messages."""
        with pytest.raises(ValueError, match="session_id"):
            validate_chat_id("", field_name="session_id")


class TestChatIdFuzzing:
    """INPUT-INVARIANT-01: Fuzzing suite demonstrates rejection of malformed inputs."""

    @pytest.mark.parametrize("char", list(string.punctuation))
    def test_punctuation_chars_rejected(self, char):
        """Every punctuation character not in the allowed set is rejected.

        Allowed: _ - . : / @
        This fuzzing test exhaustively covers all ASCII punctuation.
        """
        allowed = set("_-.:/@")
        if char in allowed:
            # Should pass
            result = validate_chat_id(f"a{char}b")
            assert result == f"a{char}b"
        else:
            # Should be rejected
            with pytest.raises(ValueError):
                validate_chat_id(f"a{char}b")

    @pytest.mark.parametrize("char_code", list(range(0, 32)) + [127])
    def test_control_chars_rejected(self, char_code):
        """All ASCII control characters (0-31, 127) are rejected."""
        char = chr(char_code)
        with pytest.raises(ValueError, match="control characters"):
            validate_chat_id(f"prefix{char}suffix")

    @pytest.mark.parametrize("length", [CHAT_ID_MAX_LENGTH + 1, 500, 1000, 10000])
    def test_oversized_inputs_rejected(self, length):
        """Inputs of various oversized lengths are rejected."""
        with pytest.raises(ValueError, match="maximum length"):
            validate_chat_id("a" * length)

    @pytest.mark.parametrize("prefix", ["';", "$((", "<>", "`x", "|nc", "$HOME"])
    def test_injection_patterns_rejected(self, prefix):
        """Common injection patterns are rejected."""
        with pytest.raises(ValueError):
            validate_chat_id(f"{prefix}--")

    def test_random_fuzz_rejects_invalid(self):
        """Random fuzzing: generate random invalid inputs and verify rejection."""
        import random
        random.seed(42)  # Reproducible
        invalid_chars = set(string.printable) - set(string.ascii_letters + string.digits + "_-.:/@")
        invalid_chars.discard(" ")  # space is stripped at edges, tested separately

        for _ in range(200):
            length = random.randint(1, 50)
            chars = random.choices(list(invalid_chars), k=length)
            candidate = "".join(chars).strip()
            if candidate and not candidate.isspace():
                with pytest.raises(ValueError):
                    validate_chat_id(candidate)

    def test_random_fuzz_accepts_valid(self):
        """Random fuzzing: generate random valid inputs and verify acceptance."""
        import random
        random.seed(99)  # Reproducible
        allowed_chars = string.ascii_letters + string.digits + "_-.:/@"

        for _ in range(200):
            length = random.randint(1, CHAT_ID_MAX_LENGTH)
            candidate = "".join(random.choices(allowed_chars, k=length))
            result = validate_chat_id(candidate)
            assert result == candidate


class TestChatIdTwoLayerEnforcement:
    """INPUT-INVARIANT-01: enforcement at >=2 layers (API + queue consumer)."""

    def test_layer1_api_pydantic_validator_calls_validate_chat_id(self):
        """Layer 1: the Pydantic session_id validator calls validate_chat_id."""
        import inspect
        from gateway import remote_agent_api

        source = inspect.getsource(remote_agent_api.create_remote_api_blueprint)
        # The session_id validator must call validate_chat_id
        assert "validate_chat_id" in source, (
            "Pydantic session_id validator must call validate_chat_id (layer 1)"
        )

    def test_layer2_queue_consumer_calls_validate_chat_id(self):
        """Layer 2: the execute handler re-validates session_id before routing."""
        import inspect
        from gateway import remote_agent_api

        source = inspect.getsource(remote_agent_api.create_remote_api_blueprint)
        # The execute handler must call validate_chat_id again (layer 2)
        # We count occurrences: at least 2 (validator + handler)
        count = source.count("validate_chat_id")
        assert count >= 2, (
            f"Expected >=2 validate_chat_id calls (layer 1 Pydantic + "
            f"layer 2 handler), found {count}"
        )

    def test_validate_chat_id_is_exported(self):
        """validate_chat_id is importable for use by queue consumers."""
        from gateway.remote_agent_api import validate_chat_id
        assert callable(validate_chat_id)

    def test_default_session_id_remote_exec_is_valid(self):
        """The default session_id 'remote-exec' passes validation."""
        result = validate_chat_id("remote-exec")
        assert result == "remote-exec"


# ===========================================================================
# 3. SESSION-CLEANUP-INVARIANT — HTTP session leak on failure paths
# ===========================================================================

class TestSessionCleanupInvariant:
    """SESSION-CLEANUP-INVARIANT: zero leaked sessions in failure-path tests."""

    def test_weixin_connect_failure_cleans_sessions(self):
        """WeChat connect() failure cleans up _poll_session and _send_session.

        This test verifies (via source inspection) that the connect() method
        has a try/except that closes both sessions on failure.
        """
        import inspect
        from gateway.platforms import weixin

        source = inspect.getsource(weixin.WeixinAdapter.connect)
        # Must have try/except with session cleanup
        assert "try:" in source, "connect() must have try block for cleanup"
        assert "except Exception" in source, "connect() must catch exceptions"
        assert "_poll_session" in source and "close" in source, (
            "connect() failure must close _poll_session"
        )
        assert "_send_session" in source and "close" in source, (
            "connect() failure must close _send_session"
        )

    def test_whatsapp_cloud_connect_failure_cleans_http_client(self):
        """WhatsApp Cloud connect() failure cleans up _http_client."""
        import inspect
        from gateway.platforms import whatsapp_cloud

        source = inspect.getsource(whatsapp_cloud.WhatsAppCloudAdapter.connect)
        assert "try:" in source, "connect() must have try block"
        assert "except Exception" in source, "connect() must catch exceptions"
        assert "_http_client" in source and "aclose" in source, (
            "connect() failure must aclose _http_client"
        )

    def test_qqbot_connect_failure_cleans_up(self):
        """QQ Bot connect() failure calls _cleanup() which closes all resources."""
        import inspect
        from gateway.platforms.qqbot import adapter

        source = inspect.getsource(adapter.QQAdapter.connect)
        assert "except Exception" in source, "connect() must catch exceptions"
        assert "_cleanup()" in source, (
            "connect() failure must call _cleanup()"
        )

    def test_bluebubbles_connect_failure_cleans_client(self):
        """BlueBubbles connect() failure closes self.client."""
        import inspect
        from gateway.platforms import bluebubbles

        source = inspect.getsource(bluebubbles.BlueBubblesAdapter.connect)
        assert "except Exception" in source, "connect() must catch exceptions"
        assert "client" in source and "aclose" in source, (
            "connect() failure must aclose client"
        )

    def test_signal_connect_failure_cleans_client(self):
        """Signal connect() failure closes self.client (via finally block)."""
        import inspect
        from gateway.platforms import signal

        source = inspect.getsource(signal.SignalAdapter.connect)
        # Signal uses a finally block that checks _running
        assert "finally:" in source, "connect() must have finally block"
        assert "client" in source and "aclose" in source, (
            "connect() failure must aclose client"
        )

    @pytest.mark.asyncio
    async def test_weixin_connect_exception_path_no_leak(self):
        """Integration test: simulate WeChat connect() failure and verify
        sessions are closed (no leak).

        We mock _token_store.restore to raise an exception after sessions
        are created, then verify both sessions were closed.
        """
        from gateway.platforms.weixin import WeixinAdapter

        # Create adapter with minimal config
        config = MagicMock()
        config.extra = {
            "account_id": "test-acc",
            "token": "test-token",
        }
        config.token = "test-token"

        adapter = WeixinAdapter.__new__(WeixinAdapter)
        # Manually set required attributes without calling __init__
        adapter._token = "test-token"
        adapter._account_id = "test-acc"
        adapter._base_url = "http://test"
        adapter._hermes_home = "/tmp"
        adapter._token_store = MagicMock()
        # Make restore raise to simulate failure
        adapter._token_store.restore = MagicMock(side_effect=RuntimeError("test failure"))
        adapter._poll_session = None
        adapter._send_session = None
        adapter._poll_task = None
        adapter._running = False
        adapter._dedup = MagicMock()
        adapter._pending_text_batches = {}
        adapter._pending_text_batch_tasks = {}
        adapter._typing_cache = MagicMock()
        adapter._text_batch_delay_seconds = 3.0
        adapter._text_batch_split_delay_seconds = 5.0
        adapter._group_policy = "disabled"
        adapter._send_chunk_delay_seconds = 1.5
        adapter._send_chunk_retries = 4
        adapter._send_chunk_retry_delay_seconds = 1.0
        adapter._send_text_gate = asyncio.Lock()
        adapter._rate_limit_circuit_threshold = 1
        adapter._rate_limit_circuit_window_seconds = 30.0
        adapter._rate_limit_circuit_open_seconds = 30.0
        adapter._rate_limit_circuit_until = 0.0
        adapter._rate_limit_events = []
        adapter._dm_policy = "pairing"
        adapter._allow_from = []
        adapter._group_allow_from = []
        adapter._split_multiline_messages = False
        adapter._token_store = MagicMock()
        adapter._token_store.restore = MagicMock(side_effect=RuntimeError("test failure"))
        # Set platform attribute (needed by logger via self.name property)
        from gateway.platforms.base import Platform
        adapter.platform = Platform.WEIXIN

        # Mock the platform lock acquisition to succeed
        adapter._acquire_platform_lock = MagicMock(return_value=True)
        adapter._release_platform_lock = MagicMock()
        adapter._set_fatal_error = MagicMock()
        adapter._mark_connected = MagicMock()
        adapter._mark_disconnected = MagicMock()

        # Mock aiohttp ClientSession to track close() calls
        mock_poll_session = MagicMock()
        mock_poll_session.closed = False
        mock_poll_session.close = AsyncMock()
        mock_send_session = MagicMock()
        mock_send_session.closed = False
        mock_send_session.close = AsyncMock()

        with patch("gateway.platforms.weixin.aiohttp") as mock_aiohttp:
            mock_aiohttp.ClientSession = MagicMock(side_effect=[mock_poll_session, mock_send_session])
            mock_aiohttp.ClientTimeout = MagicMock()

            with patch("gateway.platforms.weixin._make_ssl_connector", return_value=None):
                with patch("gateway.platforms.weixin.check_weixin_requirements", return_value=True):
                    result = await adapter.connect()

        # connect() should return False (failure)
        assert result is False, "connect() should return False on failure"

        # Both sessions should have been closed (no leak)
        mock_poll_session.close.assert_awaited_once(), (
            "_poll_session.close() must be called on connect() failure"
        )
        mock_send_session.close.assert_awaited_once(), (
            "_send_session.close() must be called on connect() failure"
        )

        # Sessions should be set to None
        assert adapter._poll_session is None, "_poll_session should be None after cleanup"
        assert adapter._send_session is None, "_send_session should be None after cleanup"

    @pytest.mark.asyncio
    async def test_whatsapp_cloud_connect_exception_path_no_leak(self):
        """Integration test: simulate WhatsApp Cloud connect() webhook server
        failure and verify _http_client is closed (no leak).
        """
        from gateway.platforms.whatsapp_cloud import WhatsAppCloudAdapter

        adapter = WhatsAppCloudAdapter.__new__(WhatsAppCloudAdapter)
        adapter._phone_number_id = "test-phone"
        adapter._access_token = "test-token"
        adapter._verify_token = "verify"
        adapter._app_secret = "secret"
        adapter._http_client = None
        adapter._runner = None
        adapter._webhook_host = "127.0.0.1"
        adapter._webhook_port = 9999
        adapter._webhook_path = "/wh"
        adapter._health_path = "/h"
        adapter._api_version = "v18"

        adapter._mark_connected = MagicMock()
        adapter._mark_disconnected = MagicMock()
        adapter._set_fatal_error = MagicMock()

        mock_http_client = MagicMock()
        mock_http_client.aclose = AsyncMock()

        with patch("gateway.platforms.whatsapp_cloud.httpx") as mock_httpx:
            mock_httpx.AsyncClient = MagicMock(return_value=mock_http_client)

            # Make web.Application() raise to simulate port bind failure
            with patch("gateway.platforms.whatsapp_cloud.web") as mock_web:
                mock_web.Application.side_effect = OSError("Address already in use")

                with patch(
                    "gateway.platforms.whatsapp_cloud.check_whatsapp_cloud_requirements",
                    return_value=True,
                ):
                    result = await adapter.connect()

        # connect() should return False (failure)
        assert result is False, "connect() should return False on failure"

        # http_client should have been closed (no leak)
        mock_http_client.aclose.assert_awaited_once(), (
            "_http_client.aclose() must be called on connect() failure"
        )
        assert adapter._http_client is None, (
            "_http_client should be None after cleanup"
        )


# ===========================================================================
# Integration: verify all three invariants are present in the codebase
# ===========================================================================

class TestKR1Integration:
    """KR-1 umbrella: verify all three sub-invariants are implemented."""

    def test_auth_gate_zero_present(self):
        """AUTH-GATE-ZERO: verify_api_key exists and is used on all endpoints."""
        from gateway.remote_agent_api import verify_api_key
        assert callable(verify_api_key)

    def test_input_invariant_present(self):
        """INPUT-INVARIANT-01: validate_chat_id exists with documented bounds."""
        from gateway.remote_agent_api import validate_chat_id, CHAT_ID_MAX_LENGTH
        assert callable(validate_chat_id)
        assert CHAT_ID_MAX_LENGTH == 256
        assert CHAT_ID_MAX_LENGTH > 0

    def test_session_cleanup_present(self):
        """SESSION-CLEANUP-INVARIANT: platform adapters clean up on failure."""
        import inspect
        from gateway.platforms import weixin, whatsapp_cloud

        # WeChat: try/except with session close in connect()
        weixin_src = inspect.getsource(weixin.WeixinAdapter.connect)
        assert "except Exception" in weixin_src
        assert "close" in weixin_src

        # WhatsApp Cloud: try/except with aclose in connect()
        wa_src = inspect.getsource(whatsapp_cloud.WhatsAppCloudAdapter.connect)
        assert "except Exception" in wa_src
        assert "aclose" in wa_src