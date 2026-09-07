"""P3-003 error-message standardization coverage.

Covers the ``EmojiIcon`` enum + ``format_info``/``format_warning``/
``format_success`` helpers introduced in gateway/error_response.py, and
asserts that the two handler modules cited by the DoD (agent_commands.py,
access_control.py) route their user-facing error/info/warn/success replies
through the standard surface (emoji + description).
"""

from __future__ import annotations

import inspect

import pytest

from gateway import access_control, agent_commands
from gateway.error_response import (
    EmojiIcon,
    ErrorCode,
    ErrorResponse,
    format_info,
    format_success,
    format_warning,
)


class TestEmojiIconEnum:
    def test_expected_icons_defined(self):
        # DoD examples: ❌ Error, 🚫 Access Denied, ⚠️ Warning, ℹ️ Info
        assert EmojiIcon.ERROR.value == "❌"
        assert EmojiIcon.ACCESS_DENIED.value == "🚫"
        assert EmojiIcon.WARNING.value == "⚠️"
        assert EmojiIcon.INFO.value == "ℹ️"
        assert EmojiIcon.SUCCESS.value == "✅"
        assert EmojiIcon.PENDING.value == "⏳"

    def test_str_returns_raw_emoji(self):
        # __str__ should render the emoji, not the enum-member repr.
        assert f"{EmojiIcon.INFO} hi" == "ℹ️ hi"
        assert str(EmojiIcon.ERROR) == "❌"


class TestFormatHelpers:
    def test_format_info_prefixes_info_emoji_and_space(self):
        assert format_info("ready") == "ℹ️ ready"

    def test_format_warning_prefixes_warning_emoji_and_space(self):
        assert format_warning("watch out") == "⚠️ watch out"

    def test_format_success_prefixes_check_emoji_and_space(self):
        assert format_success("done") == "✅ done"


class TestToEmojiResponseUsesEnum:
    def test_access_denied_uses_denied_emoji(self):
        err = ErrorResponse(
            code=ErrorCode.ACCESS_DENIED,
            message="nope",
        )
        assert err.to_emoji_response().startswith(f"{EmojiIcon.ACCESS_DENIED} ")

    def test_other_error_uses_error_emoji(self):
        err = ErrorResponse(
            code=ErrorCode.OPERATION_FAILED,
            message="boom",
        )
        assert err.to_emoji_response().startswith(f"{EmojiIcon.ERROR} ")


class TestModuleSourceContainsNoStrayEmoji:
    """The handler files should not carry hand-written emoji literals for
    error/warning/info/success responses — every such reply must go through
    ``ErrorResponse`` or one of the ``format_*`` helpers (or the ``EmojiIcon``
    enum) so the format stays consistent."""

    _forbidden = ["🚫", "❌", "⚠️", "ℹ️", "✅"]

    def _source(self, module) -> str:
        return inspect.getsource(module)

    @pytest.mark.parametrize("module", [agent_commands])
    def test_agent_commands_has_no_raw_emoji_literals(self, module):
        src = self._source(module)
        for emoji in self._forbidden:
            assert emoji not in src, (
                f"{module.__name__} still contains raw {emoji!r} literal; "
                f"route it through EmojiIcon / format_* helpers instead."
            )

    @pytest.mark.parametrize("module", [access_control])
    def test_access_control_has_no_raw_emoji_literals(self, module):
        # access_control legitimately uses these emojis via the EmojiIcon
        # enum (values contain the raw glyph). Read the file as text and
        # confirm no *string literal* wraps a bare emoji outside the enum
        # declaration.
        path = module.__file__
        with open(path, encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                stripped = line.strip()
                # Skip the EmojiIcon enum body (defined in error_response,
                # not in this file) — no allow-list needed here since this
                # file only imports EmojiIcon.
                for emoji in self._forbidden:
                    assert emoji not in line, (
                        f"{path}:{lineno} still contains raw {emoji!r}: "
                        f"{stripped!r}. Use EmojiIcon / format_* helpers."
                    )


class TestRequireAccessDecoratorStandardized:
    """The ``require_access`` decorator used to hand-format a 🚫 string;
    it must now emit a standardized ErrorResponse.to_emoji_response()."""

    def test_require_access_denies_via_standard_helper(self, monkeypatch):
        import asyncio

        class _StubMgr:
            def has_access(self, event):
                return False

            def get_user_id(self, event):
                return "unknown_user"

        monkeypatch.setattr(
            access_control, "get_access_manager", lambda: _StubMgr()
        )

        @access_control.require_access("load-demis")
        async def _handler(gr, ev):  # pragma: no cover - won't be called
            return "ok"

        out = asyncio.run(_handler(None, object()))
        # Standard emoji-response format: starts with 🚫 followed by space
        assert out.startswith(f"{EmojiIcon.ACCESS_DENIED} ")
        # And carries the user_id in the context block (per to_emoji_response)
        assert "unknown_user" in out
