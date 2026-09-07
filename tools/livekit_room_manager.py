"""LiveKit room manager — pure functions for the rendezvous (ADR-013).

This module is intentionally small and side-effect-free except for the
one network call in ``ensure_room`` (which talks to LiveKit Cloud's REST
API). All other functions are deterministic so they can be unit-tested
without a live LiveKit deployment.
"""
from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
from datetime import timedelta
from typing import Optional

logger = logging.getLogger(__name__)

ROOM_PREFIX = "hermes-"
ROOM_HASH_LEN = 16  # 64 bits — enough to avoid collisions for any plausible chat_id volume
_ROOM_EMPTY_TIMEOUT_SECS = 300  # auto-close empty room after 5 min idle


@dataclasses.dataclass(frozen=True)
class LiveKitConfig:
    """Loaded from environment. Never log api_secret."""
    url: str
    api_key: str
    api_secret: str

    @classmethod
    def from_env(cls) -> "LiveKitConfig":
        url = os.environ.get("LIVEKIT_URL")
        api_key = os.environ.get("LIVEKIT_API_KEY")
        api_secret = os.environ.get("LIVEKIT_API_SECRET")
        missing = [n for n, v in [
            ("LIVEKIT_URL", url),
            ("LIVEKIT_API_KEY", api_key),
            ("LIVEKIT_API_SECRET", api_secret),
        ] if not v]
        if missing:
            raise RuntimeError(
                f"LiveKit config missing env vars: {', '.join(missing)}. "
                f"See ADR-013 step 2 for setup."
            )
        return cls(url=url, api_key=api_key, api_secret=api_secret)


def derive_room_name(chat_id: str) -> str:
    """Map a chat_id to a deterministic LiveKit room name.

    Telegram chat IDs include negative numbers (supergroups) and very long
    integers. LiveKit allows up to 64 chars of [a-zA-Z0-9_-]; we use only
    lowercase + hyphen for safety.
    """
    if not chat_id:
        raise ValueError("chat_id must be non-empty")
    digest = hashlib.sha256(str(chat_id).encode("utf-8")).hexdigest()[:ROOM_HASH_LEN]
    return f"{ROOM_PREFIX}{digest}"


def mint_participant_token(
    cfg: LiveKitConfig,
    room_name: str,
    identity: str,
    ttl_seconds: int = 3600,
    can_publish: bool = True,
    can_subscribe: bool = True,
) -> str:
    """Mint a JWT for a participant joining ``room_name``.

    Uses livekit-api AccessToken builder (HS256). Returned string is the
    raw JWT — no Bearer prefix, no quoting.
    """
    if not identity:
        raise ValueError("identity must be non-empty")
    if not room_name:
        raise ValueError("room_name must be non-empty")
    # Lazy import so test collection works without livekit installed
    from livekit import api as lk_api

    grants = lk_api.VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=can_publish,
        can_subscribe=can_subscribe,
    )
    token = (
        lk_api.AccessToken(cfg.api_key, cfg.api_secret)
        .with_identity(identity)
        .with_ttl(timedelta(seconds=ttl_seconds))
        .with_grants(grants)
        .to_jwt()
    )
    return token


async def ensure_room(cfg: LiveKitConfig, room_name: str, max_participants: int = 8) -> None:
    """Idempotent: creates the room if it doesn't exist, no-op otherwise.

    Uses LiveKit's RoomService REST API. Network call.
    """
    from livekit import api as lk_api

    async with lk_api.LiveKitAPI(cfg.url, cfg.api_key, cfg.api_secret) as client:
        try:
            await client.room.create_room(
                lk_api.CreateRoomRequest(
                    name=room_name,
                    max_participants=max_participants,
                    empty_timeout=_ROOM_EMPTY_TIMEOUT_SECS,
                )
            )
            logger.info("Created LiveKit room %s", room_name)
        except Exception as e:
            # AlreadyExists is non-fatal — this function is idempotent
            if "already exists" in str(e).lower() or "AlreadyExists" in type(e).__name__:
                logger.debug("LiveKit room %s already exists", room_name)
                return
            raise
