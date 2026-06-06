"""Tests for .skill file routing in the Telegram document handler.

Asserts that .skill files bypass the generic-document-rejection branch
and route into install_skill_file() instead.

This is an integration-shape test: we don't spin up a real Telegram
adapter; we patch install_skill_file and assert the handler called it
with the right kwargs given a mock Update.message.document with a
.skill filename.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_skill_file_routes_to_install_pipeline():
    """A .skill document hits install_skill_file, not the rejection branch."""
    # Lazy import — test must not pull telegram deps if module didn't change yet
    from gateway.platforms.telegram import TelegramAdapter

    # Build a minimal adapter instance bypassing __init__ networking
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.handle_message = AsyncMock()
    adapter._queue_media_group_event = AsyncMock()
    adapter._enqueue_photo_event = MagicMock()
    adapter._photo_batch_key = MagicMock(return_value="batch_key")

    # Mock document fetched from Telegram
    fake_file = MagicMock()
    fake_file.download_as_bytearray = AsyncMock(
        return_value=bytearray(b"---\nname: my-skill\ndescription: x\n---\n# Body\n")
    )
    fake_doc = MagicMock()
    fake_doc.file_name = "my-skill.skill"
    fake_doc.file_size = 200
    fake_doc.mime_type = "application/octet-stream"
    fake_doc.get_file = AsyncMock(return_value=fake_file)

    fake_user = SimpleNamespace(id=445462521)
    fake_msg = SimpleNamespace(
        document=fake_doc,
        photo=None,
        video=None,
        voice=None,
        audio=None,
        sticker=None,
        animation=None,
        from_user=fake_user,
        media_group_id=None,
    )

    event = SimpleNamespace(
        text="",
        message_type=None,
        media_urls=[],
        media_types=[],
    )

    # Patch the install pipeline so we can observe what it's called with
    fake_report = SimpleNamespace(
        verdict="pass",
        skill_name="my-skill",
        install_path="/fake/path/my-skill",
        scan_summary="OK",
        user_message="✅ Installed skill 'my-skill' (path: /fake/path/my-skill)",
        error="",
        sender_id="445462521",
        platform="telegram",
    )

    with patch(
        "tools.skill_file_install.install_skill_file",
        return_value=fake_report,
    ) as mock_install:
        # Pull the relevant code path — the document-handling logic lives
        # in TelegramAdapter._cache_user_media (or wherever the rejection
        # branch is). We invoke it directly via a helper that mirrors the
        # production call.
        await _invoke_document_handler(adapter, event, fake_msg)

    # Must have called install_skill_file, NOT issued the rejection text
    assert mock_install.called, "install_skill_file should have been invoked"
    call_kwargs = mock_install.call_args.kwargs
    assert call_kwargs["filename"] == "my-skill.skill"
    assert call_kwargs["sender_id"] == "445462521"
    assert call_kwargs["platform"] == "telegram"
    # The handler should have set event.text to the install report message
    assert "Installed skill" in event.text or "✅" in event.text
    # And NOT the unsupported-document-type message
    assert "Unsupported document type" not in event.text


async def _invoke_document_handler(adapter, event, msg):
    """Replay the production document handler block on a mock adapter.

    Mirrors the structure of telegram.py's `elif msg.document:` branch so
    that we test the exact path that runs in production. Updated whenever
    the production code path changes.
    """
    # The production code lives inline in `_cache_user_media`. We exercise
    # the same conditional logic by calling the module-level helper that
    # the production path will call. If the helper doesn't exist yet,
    # the test fails at collection — that's the RED.
    from gateway.platforms.telegram import _try_handle_skill_file
    handled = await _try_handle_skill_file(adapter, event, msg)
    assert handled, "skill file handler should have claimed the document"
