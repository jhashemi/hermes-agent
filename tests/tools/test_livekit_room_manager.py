# tests/tools/test_livekit_room_manager.py
import os
import re
from unittest import mock

import pytest

from tools.livekit_room_manager import (
    derive_room_name,
    mint_participant_token,
    LiveKitConfig,
)


class TestDeriveRoomName:
    def test_deterministic(self):
        assert derive_room_name("445462521") == derive_room_name("445462521")

    def test_distinct_chat_ids_distinct_rooms(self):
        assert derive_room_name("1") != derive_room_name("2")

    def test_format_is_safe_for_livekit(self):
        # LiveKit room names must be alphanumeric + hyphen, ≤ 64 chars
        room = derive_room_name("445462521")
        assert re.match(r"^[a-z0-9-]{1,64}$", room), room
        assert room.startswith("hermes-"), room

    def test_handles_negative_telegram_ids(self):
        # Telegram supergroup ids are negative
        assert derive_room_name("-1001234567890").startswith("hermes-")


class TestLiveKitConfig:
    def test_from_env_picks_up_vars(self, monkeypatch):
        monkeypatch.setenv("LIVEKIT_URL", "wss://x.livekit.cloud")
        monkeypatch.setenv("LIVEKIT_API_KEY", "APIabc")
        monkeypatch.setenv("LIVEKIT_API_SECRET", "secretxyz")
        cfg = LiveKitConfig.from_env()
        assert cfg.url == "wss://x.livekit.cloud"
        assert cfg.api_key == "APIabc"
        assert cfg.api_secret == "secretxyz"

    def test_from_env_raises_when_missing(self, monkeypatch):
        monkeypatch.delenv("LIVEKIT_URL", raising=False)
        with pytest.raises(RuntimeError, match="LIVEKIT_URL"):
            LiveKitConfig.from_env()


class TestMintParticipantToken:
    def test_returns_jwt_string(self):
        cfg = LiveKitConfig(
            url="wss://x.livekit.cloud",
            api_key="APIabc",
            api_secret="secret-must-be-long-enough-for-hs256-32b",
        )
        token = mint_participant_token(
            cfg, room_name="hermes-test", identity="hermes-bot", ttl_seconds=3600,
        )
        # JWT is three base64url segments separated by dots
        assert token.count(".") == 2
        assert all(seg for seg in token.split("."))

    def test_identity_collision_raises(self):
        cfg = LiveKitConfig(
            url="wss://x.livekit.cloud", api_key="APIabc",
            api_secret="secret-must-be-long-enough-for-hs256-32b",
        )
        with pytest.raises(ValueError, match="identity"):
            mint_participant_token(cfg, room_name="hermes-test", identity="", ttl_seconds=3600)
