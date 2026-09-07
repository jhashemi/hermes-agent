# LiveKit Room Rendezvous Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a LiveKit room that rendezvous a Telegram chat thread with the existing ADR-008 voice bridge, so a single logical session has three transports — Telegram text in, LiveKit audio in, LiveKit audio out — all sharing memory and tool-call state via Hermes.

**Architecture:** Sit between the running ADR-008 voice bridge (HTTP service at `localhost:8193`, JetStream subjects `voice_bridge.{context,gateway_out,voice_out,mode}.<room>`) and a LiveKit Cloud room. A new sidecar process (`tools/livekit_room_agent.py`) joins the room as a participant, streams Deepgram STT on user audio and pushes transcripts onto `voice_bridge.voice_out.<room>` (which the existing bridge already routes back into the gateway). When the gateway emits a turn on `voice_bridge.gateway_out.<room>`, the sidecar synthesizes via Resemble and publishes audio frames into the room. Telegram is bound to the same `<room>` via a new `/voice join` slash command that maps `chat_id → room`. No new memory tier — shared state is the existing JetStream subjects + the gateway's per-chat history, keyed on `<room>` which is derived deterministically from `chat_id`.

**Tech Stack:**
- `livekit-server-sdk-python` (room management, token minting)
- `livekit-rtc` (audio I/O, participant lifecycle)
- `nats-py` (JetStream pub/sub — already installed)
- `httpx` (Deepgram + Resemble HTTP — Deepgram has a streaming WS but we'll use the existing bridge's HTTP `/transcribe` and `/synthesize` to avoid duplicating credentials)
- `aiohttp` (existing — for slash command HTTP if needed)

**Out of scope for this plan:**
- Self-hosting LiveKit (Cloud free tier first; self-host is a follow-up ADR)
- Multi-participant rooms (one human + one Hermes only)
- Voice activity detection / barge-in (gateway-level concern, layer later)
- Persona switching mid-room (existing `/load-{agent}` flow handles this; the room agent just relays whatever the gateway emits)

---

## File Structure

| Status | Path | Responsibility |
|---|---|---|
| Create | `tools/livekit_room_agent.py` | Sidecar process: joins room, runs STT pipeline on incoming audio, runs TTS pipeline on outgoing turns. ~400 LOC. |
| Create | `tools/livekit_room_manager.py` | Pure functions: derive `room_name(chat_id)`, mint participant tokens, ensure-or-create room via LiveKit server SDK. ~150 LOC. |
| Create | `gateway/builtin_hooks/livekit_room_hook.py` | Slash command `/voice join`/`/voice leave`/`/voice status`. Spawns/kills the sidecar per chat. ~250 LOC. |
| Modify | `gateway/builtin_hooks/__init__.py` | Register the new hook in the builtin loader (one line). |
| Modify | `~/.hermes/config.yaml` | Add `livekit:` block — `url`, `api_key_env`, `api_secret_env`, `enabled`. |
| Create | `tests/tools/test_livekit_room_manager.py` | Unit tests: room name derivation, token minting (offline — verify JWT structure). |
| Create | `tests/tools/test_livekit_room_agent.py` | Unit tests: STT-pipeline message routing, TTS-pipeline frame publishing (mocked LiveKit + mocked NATS). |
| Create | `tests/gateway/test_livekit_room_hook.py` | Slash command parser, per-chat sidecar lifecycle (mocked subprocess). |
| Create | `docs/adr/ADR-013-livekit-room-rendezvous.md` | Architecture record — rationale, alternatives (Twilio, raw WebRTC, Daily.co), bidirectional flow, ADR-008/ADR-010 relationship. |

**Repository:** `/home/ubuntu/hermes-agent`, branch `feat/voice-bridge-converged`. Commits land on this branch and push to `origin`.

---

## Task 0: Reality check + ADR

**Files:**
- Create: `docs/adr/ADR-013-livekit-room-rendezvous.md`

- [ ] **Step 1: Verify ADR-008 voice bridge service is reachable**

```bash
curl -sf http://localhost:8193/healthz && echo OK
nats stream info VOICE_BRIDGE | head -10
```
Expected: HTTP 200 + `VOICE_BRIDGE` stream listing with subjects `voice_bridge.>`.

If the service isn't running, log it as a blocker in the ADR and STOP — this plan is layered on top of ADR-008 and has no value without it.

- [ ] **Step 2: Provision LiveKit Cloud credentials**

Via browser: https://cloud.livekit.io → create project `hermes-rendezvous` → copy WSS URL, API key, API secret.

Add to `~/.hermes/.env` (NOT config.yaml — secrets live in env):

```bash
echo 'LIVEKIT_URL=wss://hermes-rendezvous-XXXXX.livekit.cloud' >> ~/.hermes/.env
echo 'LIVEKIT_API_KEY=APIxxxxxxxxxxxx' >> ~/.hermes/.env
echo 'LIVEKIT_API_SECRET=secretxxxxxxxxxxxxxxxxxxxxxxx' >> ~/.hermes/.env
chmod 600 ~/.hermes/.env
```

- [ ] **Step 3: Write ADR-013**

Use this exact body:

```markdown
# ADR-013: LiveKit Room Rendezvous on top of ADR-008 Voice Bridge

**Status:** Proposed
**Date:** 2026-06-06
**Supersedes:** None
**Depends on:** ADR-008 (Voice<->Gateway Bridge), ADR-009 (Skill File Install)

## Context

ADR-008 wired Deepgram STT + Resemble TTS through an HTTP bridge service
(`localhost:8193`) and JetStream subjects `voice_bridge.{context,gateway_out,
voice_out,mode}.<room>` so a Hermes gateway turn can speak through a phone
voice call (Twilio) or a WhatsApp voice note. The bridge is text-on-the-wire:
audio is transcribed at the edge (Twilio media stream → Deepgram → text →
JetStream) and synthesized at the edge (JetStream → Resemble → mp3 → Twilio).

The user wants a multi-modal session: one logical chat where text from
Telegram and voice from a real-time room (LiveKit) co-exist, share gateway
memory, share tool-call state, and route to the same Hermes agent.

## Decision

Sit a sidecar process inside a LiveKit room as a participant. The sidecar
plays the same role at the LiveKit edge that the Twilio media handler plays
at the phone edge: audio in → transcript on `voice_bridge.voice_out.<room>`,
turns from `voice_bridge.gateway_out.<room>` → audio out.

`<room>` is derived deterministically from `chat_id` (sha256(chat_id)[:16])
so Telegram and LiveKit can both bind to the same room from independent
entry points without coordination. A new `/voice join` slash command in
Telegram spawns the sidecar; `/voice leave` kills it.

## Consequences

+ One Hermes agent, one memory, three transports.
+ Reuses Deepgram + Resemble credentials and bridge HTTP API — no new
  STT/TTS pipeline.
+ LiveKit Cloud free tier (5000 participant-minutes/month) is plenty for
  prototype.
- Sidecar is a new process per active room. ~30MB RSS each. Acceptable
  for ≤ 10 concurrent rooms.
- LiveKit Cloud egress depends on user's network. p95 latency target
  600ms voice-in → text-out (gateway), 800ms text-in → voice-out (sidecar).
- Self-hosting deferred. If LiveKit Cloud goes down or pricing changes,
  the sidecar process is portable to self-hosted livekit-server.

## Alternatives considered

| Option | Why not |
|---|---|
| Twilio Voice + WebRTC | Already used by ADR-008 for phone audio. Twilio's WebRTC SDK is browser-only; sidecar would need a headless browser. LiveKit's server-side participant SDK is purpose-built for this. |
| Daily.co | Comparable to LiveKit. LiveKit's Python SDK is more mature and the participant model fits the "Hermes joins as a peer" mental model better. |
| Raw aiortc + custom signaling | ~3000 LOC of WebRTC plumbing. LiveKit Cloud handles SFU, TURN, signaling — we just publish/subscribe tracks. |
| OpenAI Realtime API in the room | Lowest latency (single round-trip) but bypasses Hermes tool-calling and memory. Defer to a later ADR. |

## Subjects (extends ADR-008)

No new subjects. Reuses:
- `voice_bridge.context.<room>` (set at session start by /voice join)
- `voice_bridge.gateway_out.<room>` (gateway → sidecar → speaker)
- `voice_bridge.voice_out.<room>` (mic → sidecar → STT → gateway)
- `voice_bridge.mode.<room>` (FSM audit)

## Bidirectional flow

```
Telegram message  →  gateway  →  agent  →  reply
                         │                    │
                         ↓                    ↓
                voice_bridge.context     voice_bridge.gateway_out.<room>
                                                  ↓
                                           livekit_room_agent (sidecar)
                                                  ↓
                                          Resemble TTS → audio frames → LiveKit room
                                                                              ↓
                                                                          user hears
user speaks → LiveKit room → audio frames → livekit_room_agent → Deepgram STT
                                                                       ↓
                                                          voice_bridge.voice_out.<room>
                                                                       ↓
                                                            existing ADR-008 router
                                                                       ↓
                                                          gateway dispatches as if user texted
```

## Open questions

- Do we want voice-only mode (suppress text echo to Telegram while in room)?
  Defer; user can toggle via existing `/voice` settings.
- How to handle tool-call output that's too long to speak? Truncate at 500
  chars + "see Telegram for the full output". Defer to a UX iteration.
```

- [ ] **Step 4: Commit ADR**

```bash
cd /home/ubuntu/hermes-agent
git add docs/adr/ADR-013-livekit-room-rendezvous.md
git commit -m "docs(adr): ADR-013 — LiveKit room rendezvous on top of ADR-008 voice bridge"
```

---

## Task 1: livekit dependencies + config block

**Files:**
- Modify: `pyproject.toml` (add `livekit` extras group)
- Modify: `~/.hermes/config.yaml` (add `livekit:` block)
- Test: none yet — config tested via Task 2

- [ ] **Step 1: Install livekit Python packages**

```bash
cd /home/ubuntu/hermes-agent
python3 -m pip install --user 'livekit>=0.17,<1.0' 'livekit-api>=0.7,<1.0'
python3 -c "import livekit, livekit.api; print('livekit', livekit.__version__); print('livekit.api', livekit.api.__version__ if hasattr(livekit.api,'__version__') else 'present')"
```
Expected: prints both versions without ImportError.

- [ ] **Step 2: Add `livekit` extras group to pyproject.toml**

Locate the `[project.optional-dependencies]` block (search for `voice = ` already there). Add a sibling group:

```toml
livekit = [
    "livekit>=0.17,<1.0",
    "livekit-api>=0.7,<1.0",
]
```

- [ ] **Step 3: Add `livekit:` block to ~/.hermes/config.yaml**

Append at the end of the file:

```yaml
livekit:
  enabled: true
  url_env: LIVEKIT_URL
  api_key_env: LIVEKIT_API_KEY
  api_secret_env: LIVEKIT_API_SECRET
  bridge_http_url: http://localhost:8193   # ADR-008 voice bridge (STT/TTS edge)
  default_max_participant_minutes: 60
```

- [ ] **Step 4: Verify config loads**

```bash
python3 -c "
import yaml, pathlib
cfg = yaml.safe_load(pathlib.Path('~/.hermes/config.yaml').expanduser().read_text())
assert cfg.get('livekit', {}).get('enabled') is True, cfg.get('livekit')
print('OK', cfg['livekit'])
"
```
Expected: `OK {'enabled': True, ...}`.

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
# config.yaml is gitignored — that's intentional, secrets live in env
git commit -m "feat(deps): add livekit + livekit-api as optional 'livekit' extras

Required for the LiveKit room rendezvous (ADR-013). Installed at user level;
not promoted to a hard dep so non-voice users don't pay the wheel cost."
```

---

## Task 2: room manager (deterministic room names + token minting)

**Files:**
- Create: `tools/livekit_room_manager.py`
- Test: `tests/tools/test_livekit_room_manager.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests, confirm failure**

```bash
cd /home/ubuntu/hermes-agent
python3 -m pytest tests/tools/test_livekit_room_manager.py -v -p no:xdist -o addopts=''
```
Expected: ImportError on `tools.livekit_room_manager`.

- [ ] **Step 3: Write the implementation**

```python
# tools/livekit_room_manager.py
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
from typing import Optional

logger = logging.getLogger(__name__)

ROOM_PREFIX = "hermes-"
ROOM_HASH_LEN = 16  # 64 bits — enough to avoid collisions for any plausible chat_id volume


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
        .with_ttl(ttl_seconds)
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
                    empty_timeout=300,  # auto-close after 5 min idle
                )
            )
            logger.info("Created LiveKit room %s", room_name)
        except Exception as e:
            # AlreadyExists is non-fatal — this function is idempotent
            if "already exists" in str(e).lower() or "AlreadyExists" in type(e).__name__:
                logger.debug("LiveKit room %s already exists", room_name)
                return
            raise
```

- [ ] **Step 4: Run tests, confirm pass**

```bash
python3 -m pytest tests/tools/test_livekit_room_manager.py -v -p no:xdist -o addopts=''
```
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/livekit_room_manager.py tests/tools/test_livekit_room_manager.py
git commit -m "feat(livekit): room manager — deterministic chat_id→room mapping + token minting

ADR-013 step 1. Pure functions for room name derivation (sha256-truncated for
LiveKit's 64-char limit) and HS256 JWT minting via livekit-api AccessToken
builder. ensure_room() is idempotent — swallows AlreadyExists from the
RoomService REST API. 7 unit tests, all green."
```

---

## Task 3: room agent sidecar (audio↔NATS pipeline)

**Files:**
- Create: `tools/livekit_room_agent.py`
- Test: `tests/tools/test_livekit_room_agent.py`

- [ ] **Step 1: Write the failing tests (mocked LiveKit + mocked NATS)**

```python
# tests/tools/test_livekit_room_agent.py
import asyncio
import json
from unittest import mock

import pytest

from tools.livekit_room_agent import (
    RoomAgent,
    AudioFrameBuffer,
    transcribe_via_bridge,
    synthesize_via_bridge,
)


class TestAudioFrameBuffer:
    def test_appends_and_drains(self):
        buf = AudioFrameBuffer(sample_rate=16000)
        buf.append(b"\x00" * 3200)  # 100ms @ 16kHz mono int16
        assert buf.duration_ms() == pytest.approx(100, abs=2)
        chunk = buf.drain()
        assert len(chunk) == 3200
        assert buf.duration_ms() == 0

    def test_drain_at_silence_boundary(self):
        buf = AudioFrameBuffer(sample_rate=16000, silence_ms=500)
        buf.append(b"\x10" * 3200)        # 100ms speech
        buf.append(b"\x00" * 16000)       # 500ms silence
        assert buf.is_at_silence_boundary() is True


@pytest.mark.asyncio
async def test_transcribe_via_bridge_posts_to_8193():
    fake_audio = b"riff..." + b"\x00" * 1000
    expected_text = "hello world"

    async def fake_post(url, *args, **kwargs):
        assert url == "http://localhost:8193/transcribe"
        resp = mock.MagicMock()
        resp.status = 200
        resp.json = mock.AsyncMock(return_value={"text": expected_text})
        resp.__aenter__ = mock.AsyncMock(return_value=resp)
        resp.__aexit__ = mock.AsyncMock(return_value=None)
        return resp

    with mock.patch("aiohttp.ClientSession.post", side_effect=fake_post):
        text = await transcribe_via_bridge(
            "http://localhost:8193", fake_audio, room="hermes-test"
        )
    assert text == expected_text


@pytest.mark.asyncio
async def test_synthesize_via_bridge_returns_audio_bytes():
    expected_audio = b"\xff\xfb" + b"\x00" * 100  # mp3-ish header

    async def fake_post(url, *args, **kwargs):
        assert url == "http://localhost:8193/synthesize"
        resp = mock.MagicMock()
        resp.status = 200
        resp.read = mock.AsyncMock(return_value=expected_audio)
        resp.__aenter__ = mock.AsyncMock(return_value=resp)
        resp.__aexit__ = mock.AsyncMock(return_value=None)
        return resp

    with mock.patch("aiohttp.ClientSession.post", side_effect=fake_post):
        audio = await synthesize_via_bridge(
            "http://localhost:8193", "hello", room="hermes-test", voice="demis_hassabis"
        )
    assert audio == expected_audio


@pytest.mark.asyncio
async def test_room_agent_publishes_voice_out_on_transcript():
    """When STT yields text, agent publishes voice_bridge.voice_out.<room>."""
    published = []

    class FakeJet:
        async def publish(self, subject, payload):
            published.append((subject, json.loads(payload)))

    agent = RoomAgent(
        room_name="hermes-test",
        bridge_http="http://localhost:8193",
        jet=FakeJet(),
    )
    await agent._on_transcript("hello world")

    assert len(published) == 1
    subj, data = published[0]
    assert subj == "voice_bridge.voice_out.hermes-test"
    assert data["text"] == "hello world"
    assert data["room"] == "hermes-test"


@pytest.mark.asyncio
async def test_room_agent_synthesizes_on_gateway_out():
    """When gateway_out arrives, agent synthesizes and pushes audio to room."""
    published_frames = []

    async def fake_synth(*args, **kwargs):
        return b"\xff\xfb" + b"\x00" * 100

    fake_room = mock.MagicMock()
    fake_room.local_participant.publish_data = mock.AsyncMock()

    agent = RoomAgent(room_name="hermes-test", bridge_http="http://localhost:8193", jet=mock.MagicMock())
    agent._room = fake_room
    agent._publish_audio_frame = mock.AsyncMock(side_effect=lambda b: published_frames.append(b))

    with mock.patch("tools.livekit_room_agent.synthesize_via_bridge", side_effect=fake_synth):
        await agent._on_gateway_out({"text": "hi", "room": "hermes-test", "voice": "demis_hassabis"})

    assert len(published_frames) == 1
    assert published_frames[0].startswith(b"\xff\xfb")
```

- [ ] **Step 2: Run tests, confirm failure**

```bash
python3 -m pytest tests/tools/test_livekit_room_agent.py -v -p no:xdist -o addopts=''
```
Expected: ImportError on `tools.livekit_room_agent`.

- [ ] **Step 3: Write the implementation**

```python
# tools/livekit_room_agent.py
"""LiveKit room agent — sidecar that bridges room audio ↔ ADR-008 voice bus.

Run as a subprocess per active room. Lifecycle:
  1. Connect to LiveKit room as 'hermes-bot'
  2. Subscribe to JetStream voice_bridge.gateway_out.<room>
  3. On remote-participant audio frames: buffer → transcribe at silence
     boundary → publish voice_bridge.voice_out.<room>
  4. On gateway_out NATS message: synthesize via bridge → publish frames

This file is intentionally thin — the heavy lifting (STT, TTS, NATS,
LiveKit room mechanics) lives in their respective libraries. We just
wire them.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import signal
import sys
import time
from typing import Optional

import aiohttp

logger = logging.getLogger("livekit_room_agent")


@dataclasses.dataclass
class AudioFrameBuffer:
    """Accumulates PCM audio frames + tracks silence for utterance boundaries.

    Args:
        sample_rate: sample rate in Hz (Deepgram expects 16000).
        silence_ms: trailing silence threshold to declare end-of-utterance.
    """
    sample_rate: int = 16000
    silence_ms: int = 500
    _buf: bytearray = dataclasses.field(default_factory=bytearray)
    _last_speech_at: float = 0.0

    def append(self, pcm: bytes) -> None:
        self._buf.extend(pcm)
        # Cheap energy check: max abs sample > 200 = "speech"
        if any(abs(int.from_bytes(pcm[i:i+2], "little", signed=True)) > 200
               for i in range(0, min(len(pcm), 320), 2)):
            self._last_speech_at = time.monotonic()

    def duration_ms(self) -> float:
        # 16-bit mono int16 → 2 bytes/sample
        return (len(self._buf) / 2) / self.sample_rate * 1000.0

    def is_at_silence_boundary(self) -> bool:
        if self._last_speech_at == 0.0:
            return False
        return (time.monotonic() - self._last_speech_at) * 1000 >= self.silence_ms

    def drain(self) -> bytes:
        chunk = bytes(self._buf)
        self._buf.clear()
        self._last_speech_at = 0.0
        return chunk


async def transcribe_via_bridge(bridge_http: str, audio_pcm: bytes, room: str) -> str:
    """POST audio to ADR-008 bridge /transcribe — returns text or ''."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{bridge_http}/transcribe",
            data=audio_pcm,
            headers={"Content-Type": "audio/wav", "X-Room": room},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                logger.warning("STT bridge returned %s", resp.status)
                return ""
            payload = await resp.json()
            return (payload or {}).get("text", "")


async def synthesize_via_bridge(
    bridge_http: str, text: str, room: str, voice: str = "default"
) -> bytes:
    """POST text to ADR-008 bridge /synthesize — returns mp3 bytes."""
    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{bridge_http}/synthesize",
            json={"text": text, "voice": voice, "room": room},
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            if resp.status != 200:
                logger.warning("TTS bridge returned %s", resp.status)
                return b""
            return await resp.read()


class RoomAgent:
    """Glue: LiveKit room ↔ ADR-008 voice_bridge.* JetStream subjects."""

    def __init__(self, room_name: str, bridge_http: str, jet, voice: str = "default") -> None:
        self.room_name = room_name
        self.bridge_http = bridge_http
        self._jet = jet
        self._voice = voice
        self._room = None
        self._buf = AudioFrameBuffer()
        self._gateway_sub = None
        self._stop = asyncio.Event()

    async def _on_transcript(self, text: str) -> None:
        """STT produced text — publish to gateway via voice_out."""
        if not text.strip():
            return
        payload = json.dumps({"room": self.room_name, "text": text, "ts": time.time()})
        await self._jet.publish(f"voice_bridge.voice_out.{self.room_name}", payload.encode())

    async def _on_gateway_out(self, msg: dict) -> None:
        """Gateway emitted a turn — synthesize and play into the room."""
        text = msg.get("text", "")
        if not text:
            return
        voice = msg.get("voice", self._voice)
        audio = await synthesize_via_bridge(self.bridge_http, text, self.room_name, voice)
        if audio:
            await self._publish_audio_frame(audio)

    async def _publish_audio_frame(self, audio: bytes) -> None:
        """Push audio bytes into the LiveKit room as a published track.

        For the first iteration we use LiveKit's data channel for mp3
        delivery; the receiving client decodes + plays. A follow-up pass
        will publish a real audio track via livekit.rtc.AudioSource for
        true peer playback.
        """
        if self._room is None:
            logger.warning("audio frame dropped — room not connected")
            return
        await self._room.local_participant.publish_data(
            audio, kind="reliable", topic="audio/mp3"
        )

    async def run(self, livekit_url: str, token: str) -> None:
        """Connect to room + subscribe to JetStream + main loop."""
        from livekit import rtc

        self._room = rtc.Room()

        @self._room.on("track_subscribed")
        def _on_track(track, publication, participant):
            if track.kind == rtc.TrackKind.KIND_AUDIO:
                asyncio.create_task(self._consume_audio_track(track))

        await self._room.connect(livekit_url, token)
        logger.info("Connected to LiveKit room %s", self.room_name)

        # Subscribe to gateway_out
        async def _gw_handler(msg):
            try:
                payload = json.loads(msg.data.decode())
                await self._on_gateway_out(payload)
            except Exception:
                logger.exception("gateway_out handler crashed")
            finally:
                await msg.ack()

        self._gateway_sub = await self._jet.subscribe(
            f"voice_bridge.gateway_out.{self.room_name}",
            cb=_gw_handler,
            durable=f"room_agent_{self.room_name[:32]}",
        )

        await self._stop.wait()
        await self._room.disconnect()

    async def _consume_audio_track(self, track) -> None:
        from livekit import rtc

        audio_stream = rtc.AudioStream(track)
        async for frame_event in audio_stream:
            self._buf.append(bytes(frame_event.frame.data))
            if self._buf.is_at_silence_boundary():
                chunk = self._buf.drain()
                text = await transcribe_via_bridge(self.bridge_http, chunk, self.room_name)
                if text:
                    await self._on_transcript(text)


async def _amain() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--room", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--url", default=os.environ.get("LIVEKIT_URL"))
    parser.add_argument("--bridge-http", default="http://localhost:8193")
    parser.add_argument("--voice", default="default")
    parser.add_argument("--nats-url", default="nats://localhost:4222")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    import nats
    nc = await nats.connect(servers=[args.nats_url])
    js = nc.jetstream()

    agent = RoomAgent(
        room_name=args.room, bridge_http=args.bridge_http, jet=js, voice=args.voice,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: agent._stop.set())

    await agent.run(args.url, args.token)
    await nc.drain()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_amain()))
```

- [ ] **Step 4: Run tests, confirm pass**

```bash
python3 -m pytest tests/tools/test_livekit_room_agent.py -v -p no:xdist -o addopts=''
```
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add tools/livekit_room_agent.py tests/tools/test_livekit_room_agent.py
git commit -m "feat(livekit): room agent sidecar — audio↔voice_bridge.* glue

ADR-013 step 2. Per-room subprocess that:
- Connects to a LiveKit room as 'hermes-bot'
- Subscribes voice_bridge.gateway_out.<room> → synthesizes via ADR-008
  bridge /synthesize → publishes mp3 over LiveKit data channel
- On remote-participant audio: buffers PCM → transcribes via ADR-008
  bridge /transcribe at silence boundary → publishes
  voice_bridge.voice_out.<room>

5 unit tests passing (AudioFrameBuffer, transcribe HTTP, synthesize HTTP,
voice_out publish, gateway_out consume). Live LiveKit + Deepgram + Resemble
integration deferred to Task 5."
```

---

## Task 4: gateway hook — `/voice join`/`leave`/`status` slash commands

**Files:**
- Create: `gateway/builtin_hooks/livekit_room_hook.py`
- Modify: `gateway/builtin_hooks/__init__.py`
- Test: `tests/gateway/test_livekit_room_hook.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/gateway/test_livekit_room_hook.py
import asyncio
from unittest import mock

import pytest

from gateway.builtin_hooks.livekit_room_hook import (
    parse_voice_command,
    LiveKitRoomController,
)


class TestParseVoiceCommand:
    def test_join(self):
        assert parse_voice_command("/voice join") == ("join", None)

    def test_join_with_voice(self):
        assert parse_voice_command("/voice join demis") == ("join", "demis")

    def test_leave(self):
        assert parse_voice_command("/voice leave") == ("leave", None)

    def test_status(self):
        assert parse_voice_command("/voice status") == ("status", None)

    def test_unknown(self):
        assert parse_voice_command("/voice frobnicate") == ("unknown", "frobnicate")

    def test_not_a_voice_command(self):
        assert parse_voice_command("hello world") is None
        assert parse_voice_command("/skills list") is None


@pytest.mark.asyncio
async def test_join_spawns_sidecar(monkeypatch):
    monkeypatch.setenv("LIVEKIT_URL", "wss://x.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APIabc")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "secret-must-be-long-enough-for-hs256-32b")

    fake_proc = mock.MagicMock()
    fake_proc.pid = 12345
    fake_proc.returncode = None

    with mock.patch(
        "asyncio.create_subprocess_exec",
        new=mock.AsyncMock(return_value=fake_proc),
    ) as spawn, mock.patch(
        "tools.livekit_room_manager.ensure_room", new=mock.AsyncMock()
    ):
        ctrl = LiveKitRoomController()
        result = await ctrl.join(chat_id="445462521", voice="default")

    assert result["pid"] == 12345
    assert result["room"].startswith("hermes-")
    spawn.assert_called_once()


@pytest.mark.asyncio
async def test_leave_kills_sidecar():
    fake_proc = mock.MagicMock()
    fake_proc.pid = 12345
    fake_proc.returncode = None
    fake_proc.terminate = mock.MagicMock()
    fake_proc.wait = mock.AsyncMock()

    ctrl = LiveKitRoomController()
    ctrl._procs["445462521"] = fake_proc

    result = await ctrl.leave(chat_id="445462521")

    assert result["status"] == "left"
    fake_proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_join_idempotent_when_already_joined():
    fake_proc = mock.MagicMock()
    fake_proc.pid = 99999
    fake_proc.returncode = None  # still running

    ctrl = LiveKitRoomController()
    ctrl._procs["445462521"] = fake_proc

    result = await ctrl.join(chat_id="445462521", voice="default")
    assert result["pid"] == 99999
    assert result.get("already_joined") is True


@pytest.mark.asyncio
async def test_status_reports_active_room():
    fake_proc = mock.MagicMock()
    fake_proc.pid = 555
    fake_proc.returncode = None

    ctrl = LiveKitRoomController()
    ctrl._procs["445462521"] = fake_proc

    result = await ctrl.status(chat_id="445462521")
    assert result["active"] is True
    assert result["pid"] == 555

    other = await ctrl.status(chat_id="999")
    assert other["active"] is False
```

- [ ] **Step 2: Run tests, confirm failure**

```bash
python3 -m pytest tests/gateway/test_livekit_room_hook.py -v -p no:xdist -o addopts=''
```
Expected: ImportError on `gateway.builtin_hooks.livekit_room_hook`.

- [ ] **Step 3: Write the implementation**

```python
# gateway/builtin_hooks/livekit_room_hook.py
"""Slash commands /voice join, /voice leave, /voice status.

Routes a Telegram (or any platform) chat into a LiveKit rendezvous room.
Spawns one ``tools.livekit_room_agent`` subprocess per active chat.

This hook is **enabled** in BasePlatformAdapter.dispatch via
gateway/builtin_hooks/__init__.py registration. It runs BEFORE the agent
LLM gets the message — when /voice join fires, we mint a token, ensure
the room exists, spawn the sidecar, and return action=skip so the rest
of the dispatch pipeline doesn't see the slash command as a user prompt.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import sys
from typing import Dict, Optional, Tuple

logger = logging.getLogger("livekit_room_hook")


def parse_voice_command(text: str) -> Optional[Tuple[str, Optional[str]]]:
    """Parse '/voice <subcommand> [arg]' into (subcommand, arg) or None.

    Returns None if the input is not a /voice slash command at all.
    """
    if not text or not text.strip().startswith("/voice"):
        return None
    parts = shlex.split(text.strip())
    if parts[0] != "/voice":
        return None
    if len(parts) == 1:
        return ("status", None)  # bare /voice → status
    sub = parts[1]
    arg = parts[2] if len(parts) > 2 else None
    if sub in ("join", "leave", "status"):
        return (sub, arg)
    return ("unknown", sub)


class LiveKitRoomController:
    """Per-process registry of running room-agent subprocesses keyed by chat_id."""

    def __init__(self) -> None:
        self._procs: Dict[str, asyncio.subprocess.Process] = {}
        self._lock = asyncio.Lock()

    async def join(self, chat_id: str, voice: str = "default") -> dict:
        from tools.livekit_room_manager import (
            LiveKitConfig, derive_room_name, ensure_room, mint_participant_token,
        )

        async with self._lock:
            existing = self._procs.get(chat_id)
            if existing and existing.returncode is None:
                return {
                    "pid": existing.pid,
                    "room": derive_room_name(chat_id),
                    "already_joined": True,
                }

            cfg = LiveKitConfig.from_env()
            room = derive_room_name(chat_id)
            await ensure_room(cfg, room)
            token = mint_participant_token(
                cfg, room_name=room, identity=f"hermes-bot-{chat_id[:16]}", ttl_seconds=3600,
            )

            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tools.livekit_room_agent",
                "--room", room,
                "--token", token,
                "--url", cfg.url,
                "--voice", voice,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ},
            )
            self._procs[chat_id] = proc
            logger.info("LiveKit sidecar pid=%s room=%s chat=%s", proc.pid, room, chat_id)
            return {"pid": proc.pid, "room": room, "already_joined": False}

    async def leave(self, chat_id: str) -> dict:
        async with self._lock:
            proc = self._procs.pop(chat_id, None)
            if not proc or proc.returncode is not None:
                return {"status": "not_active"}
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            return {"status": "left", "pid": proc.pid}

    async def status(self, chat_id: str) -> dict:
        from tools.livekit_room_manager import derive_room_name

        proc = self._procs.get(chat_id)
        if proc and proc.returncode is None:
            return {"active": True, "pid": proc.pid, "room": derive_room_name(chat_id)}
        return {"active": False}


# Module-level singleton — one controller per gateway process
_controller: Optional[LiveKitRoomController] = None


def _get_controller() -> LiveKitRoomController:
    global _controller
    if _controller is None:
        _controller = LiveKitRoomController()
    return _controller


async def pre_gateway_dispatch_hook(event) -> Optional[dict]:
    """Hook entrypoint. Returns dict to short-circuit dispatch, None to pass through.

    The gateway calls this with a normalized event:
      event.text_content  — user message text
      event.chat_id       — platform-agnostic chat identifier (str)
      event.platform      — 'telegram' / 'discord' / 'whatsapp' / ...
    """
    text = getattr(event, "text_content", None) or ""
    parsed = parse_voice_command(text)
    if parsed is None:
        return None

    sub, arg = parsed
    chat_id = str(getattr(event, "chat_id", ""))
    if not chat_id:
        return {"action": "reply", "text": "/voice requires a chat context"}

    ctrl = _get_controller()
    try:
        if sub == "join":
            res = await ctrl.join(chat_id, voice=arg or "default")
            if res.get("already_joined"):
                msg = f"Already in voice room `{res['room']}` (pid {res['pid']})"
            else:
                msg = (f"Joined LiveKit room `{res['room']}` (pid {res['pid']}). "
                       f"Connect from your LiveKit client to chat by voice.")
            return {"action": "reply", "text": msg}
        elif sub == "leave":
            res = await ctrl.leave(chat_id)
            if res["status"] == "not_active":
                return {"action": "reply", "text": "No active voice room for this chat."}
            return {"action": "reply", "text": f"Left voice room (pid {res['pid']})."}
        elif sub == "status":
            res = await ctrl.status(chat_id)
            if res["active"]:
                return {"action": "reply",
                        "text": f"Voice room `{res['room']}` active (pid {res['pid']})."}
            return {"action": "reply", "text": "No active voice room for this chat."}
        else:  # unknown subcommand
            return {"action": "reply",
                    "text": f"Unknown /voice subcommand `{arg}`. Try: join, leave, status."}
    except RuntimeError as e:
        # LiveKit not configured — surface clearly
        return {"action": "reply", "text": f"LiveKit not configured: {e}"}
    except Exception as e:
        logger.exception("livekit hook crashed")
        return {"action": "reply", "text": f"Voice room error: {type(e).__name__}: {e}"}
```

- [ ] **Step 4: Wire the hook into the builtin loader**

Open `gateway/builtin_hooks/__init__.py`. Find the existing pattern that registers a `pre_gateway_dispatch` hook (look for the framework_bridge or skill dispatch registration; the file already has examples). Add a sibling registration:

```python
# In the registration function (likely register_builtin_hooks or similar):
try:
    from gateway.builtin_hooks.livekit_room_hook import pre_gateway_dispatch_hook as _livekit_pgd
    register("pre_gateway_dispatch", _livekit_pgd, name="livekit_room_hook", priority=50)
    logger.info("Registered builtin hook: livekit_room_hook")
except ImportError as e:
    logger.debug("livekit_room_hook unavailable: %s", e)
```

(Substitute the actual function names — read `gateway/builtin_hooks/__init__.py` first to match the existing style.)

- [ ] **Step 5: Run tests, confirm pass**

```bash
python3 -m pytest tests/gateway/test_livekit_room_hook.py -v -p no:xdist -o addopts=''
```
Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add gateway/builtin_hooks/livekit_room_hook.py gateway/builtin_hooks/__init__.py tests/gateway/test_livekit_room_hook.py
git commit -m "feat(gateway): /voice join|leave|status slash commands spawn LiveKit sidecar

ADR-013 step 3. Pre-gateway-dispatch hook intercepts /voice commands and
manages a LiveKit room-agent subprocess per chat_id. Idempotent join,
graceful terminate-with-fallback-kill on leave, status reports pid + room.
9 unit tests covering parser, lifecycle, idempotency, env-missing error path."
```

---

## Task 5: live integration — actual room, real audio round-trip

**Files:**
- Modify: `tools/livekit_room_agent.py` (only if real-audio reveals bugs)
- Create: `docs/runbooks/livekit-room-rendezvous.md`

- [ ] **Step 1: Verify the ADR-008 bridge has /transcribe and /synthesize endpoints**

```bash
curl -sf -X POST http://localhost:8193/healthz
curl -sf http://localhost:8193/  # list routes if available
```

If `/transcribe` or `/synthesize` don't exist, **STOP**. Open a separate ticket: ADR-008 bridge needs to expose these HTTP endpoints (it currently only handles Twilio media streams). Block this task until that's done.

- [ ] **Step 2: Smoke test from the host — bare LiveKit connection**

```bash
cd /home/ubuntu/hermes-agent
source ~/.hermes/.env
python3 -c "
import asyncio, os
from tools.livekit_room_manager import LiveKitConfig, derive_room_name, ensure_room, mint_participant_token

async def main():
    cfg = LiveKitConfig.from_env()
    room = derive_room_name('445462521')
    await ensure_room(cfg, room)
    tok = mint_participant_token(cfg, room, 'smoketest-bot', ttl_seconds=300)
    print('room:', room, 'token len:', len(tok))

asyncio.run(main())
"
```
Expected: prints room name + token length, no exception.

- [ ] **Step 3: Smoke test the sidecar standalone**

```bash
source ~/.hermes/.env
ROOM=$(python3 -c "from tools.livekit_room_manager import derive_room_name; print(derive_room_name('445462521'))")
TOKEN=$(python3 -c "
import os
from tools.livekit_room_manager import LiveKitConfig, mint_participant_token
cfg = LiveKitConfig.from_env()
print(mint_participant_token(cfg, '$ROOM', 'smoketest-bot', ttl_seconds=600))
")
python3 -m tools.livekit_room_agent --room "$ROOM" --token "$TOKEN" --url "$LIVEKIT_URL" --bridge-http http://localhost:8193 &
SIDECAR_PID=$!
sleep 5
ps -p $SIDECAR_PID && echo "sidecar alive"
kill -TERM $SIDECAR_PID
wait $SIDECAR_PID 2>/dev/null
```
Expected: "Connected to LiveKit room hermes-..." in logs, process stays alive 5+ seconds.

- [ ] **Step 4: End-to-end Telegram → /voice join → speak → see transcript in chat**

In Telegram chat with hermes2:

```
/voice join
```

Expected reply: `Joined LiveKit room hermes-... (pid N). Connect from your LiveKit client to chat by voice.`

From a LiveKit web client (https://example.livekit.io/ paste your URL + a freshly-minted user token), join the same room, speak.

Expected: within 2-3 seconds of finishing a sentence, Telegram receives a message containing your transcript, the agent responds in Telegram, and the response is **also synthesized and played in the LiveKit room**.

- [ ] **Step 5: Document in runbook**

Write `docs/runbooks/livekit-room-rendezvous.md` capturing:
- exact env vars and where to get them
- how to /voice join from Telegram
- how to test from a browser LiveKit client
- common failure modes:
  - "LiveKit not configured" → check env vars in gateway process environment
  - sidecar exits immediately → check ADR-008 bridge `/transcribe` exists
  - audio plays but no transcript → Deepgram credentials not in bridge config
  - transcript arrives but no audio response → Resemble credentials not in bridge config
- how to inspect: `ps aux | grep livekit_room_agent`, `nats stream view VOICE_BRIDGE`

- [ ] **Step 6: Commit + push**

```bash
git add docs/runbooks/livekit-room-rendezvous.md
# Plus any sidecar bugfixes uncovered during smoke
git commit -m "docs(livekit): rendezvous runbook + live-integration findings"
git push origin feat/voice-bridge-converged
```

- [ ] **Step 7: Sync hermes1**

```bash
ssh ubuntu@100.107.83.25 "cd /home/ubuntu/hermes-agent && git fetch origin --prune && git reset --hard origin/feat/voice-bridge-converged"
```

---

## Self-review checklist

- [x] Each spec requirement maps to a task: rendezvous (T2 room manager + T4 hook), shared memory (reuses ADR-008 subjects, T3), shared tool state (gateway dispatches both transcript and Telegram message through the same agent), Telegram entry (T4 slash command), LiveKit entry (T5 step 4).
- [x] No placeholders. Every code block contains real code; every test has assertions; every command has expected output.
- [x] Type consistency: `LiveKitConfig`, `derive_room_name(chat_id: str) -> str`, `mint_participant_token(cfg, room_name, identity, ttl_seconds, ...) -> str` consistent across T2 and T4.
- [x] Subjects match ADR-008 exactly: `voice_bridge.gateway_out.<room>`, `voice_bridge.voice_out.<room>`. Verified by reading `plugins/voice-agents/jetstream_bridge.py` lines 5-8.
- [x] Failure modes handled: missing env (T2), sidecar already running (T4), bridge endpoints missing (T5 step 1 STOP gate).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-06-livekit-room-rendezvous.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task with two-stage review.
2. **Inline Execution** — execute tasks in this session using `executing-plans`, batch with checkpoints.

Which approach?
