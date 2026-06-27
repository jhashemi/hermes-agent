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

# Energy threshold for "speech" detection on int16 PCM samples.
# 200 / 32768 ≈ 0.6% of full scale — picks up normal speech, ignores DC noise.
_SPEECH_ENERGY_THRESHOLD = 200


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
    _last_speech_at: int = 0  # nonzero sentinel once any speech observed
    _silent_bytes_since_speech: int = 0  # bytes appended with no loud sample

    def append(self, pcm: bytes) -> None:
        self._buf.extend(pcm)
        # Cheap energy check: max abs sample > _SPEECH_ENERGY_THRESHOLD = "speech"
        has_speech = any(
            abs(int.from_bytes(pcm[i:i + 2], "little", signed=True))
            > _SPEECH_ENERGY_THRESHOLD
            for i in range(0, len(pcm) - 1, 2)
            if i + 2 <= len(pcm)
        )
        if has_speech:
            self._last_speech_at = 1  # sentinel: speech has been seen
            self._silent_bytes_since_speech = 0
        else:
            if self._last_speech_at != 0:
                self._silent_bytes_since_speech += len(pcm)

    def duration_ms(self) -> float:
        # 16-bit mono int16 → 2 bytes/sample
        return (len(self._buf) / 2) / self.sample_rate * 1000.0

    def is_at_silence_boundary(self) -> bool:
        if self._last_speech_at == 0:
            return False
        silence_duration_ms = (
            self._silent_bytes_since_speech / 2 / self.sample_rate * 1000.0
        )
        return silence_duration_ms >= self.silence_ms

    def drain(self) -> bytes:
        chunk = bytes(self._buf)
        self._buf.clear()
        self._last_speech_at = 0
        self._silent_bytes_since_speech = 0
        return chunk


async def transcribe_via_bridge(bridge_http: str, audio_pcm: bytes, room: str) -> str:
    """POST audio to ADR-008 bridge ``/transcribe`` — returns text or ''.

    The agent emits raw 16-bit mono PCM @ 16kHz from LiveKit AudioStream.
    The bridge accepts non-JSON content types as raw bytes and wraps in WAV
    using the ``X-Sample-Rate`` / ``X-Channels`` hints we send below.
    """
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{bridge_http}/transcribe",
            data=audio_pcm,
            headers={
                "Content-Type": "audio/x-pcm",
                "X-Room": room,
                "X-Sample-Rate": "16000",
                "X-Channels": "1",
            },
            timeout=aiohttp.ClientTimeout(total=15),
        )
        async with resp:
            if resp.status != 200:
                logger.warning("STT bridge returned %s", resp.status)
                return ""
            payload = await resp.json()
            # Hexagonal handler returns ``transcript``; legacy monolith
            # returned ``text``.  Accept either so we work against both.
            return (payload or {}).get("transcript", "") or (payload or {}).get("text", "")


async def stream_synthesize_via_bridge(
    bridge_http: str, text: str, room: str, voice: str = "default"
):
    """POST text to ADR-008 bridge ``/stream_synthesize`` — yields PCM chunks.

    Async generator. Talks to the Resemble streaming WS adapter via the
    bridge's chunked HTTP response (``Content-Type: audio/pcm``).  Output
    is 22050 Hz mono int16 PCM (Resemble's default streaming format).

    The ``voice`` argument is sent as ``agent_id`` so the bridge resolves a
    Resemble voice_uuid via its existing ``AGENT_ALIASES`` + voice config
    table — callers pass agent ids like ``demis_hassabis`` / ``jeff_dean``,
    not raw uuids.
    """
    async with aiohttp.ClientSession() as session:
        resp = await session.post(
            f"{bridge_http}/stream_synthesize",
            json={"text": text, "agent_id": voice, "room": room},
            timeout=aiohttp.ClientTimeout(total=60),  # stream open for synthesis duration
        )
        async with resp:
            if resp.status != 200:
                logger.warning("TTS stream bridge returned %s", resp.status)
                return
            # Use 4 KiB read granularity so we publish frames as Resemble emits them.
            async for chunk in resp.content.iter_chunked(4096):
                if chunk:
                    yield chunk


async def synthesize_via_bridge(
    bridge_http: str, text: str, room: str, voice: str = "default"
) -> bytes:
    """Buffered convenience wrapper — collects ``stream_synthesize_via_bridge`` into bytes.

    Kept as a single-call surface for non-streaming callers and tests.
    """
    out = bytearray()
    async for chunk in stream_synthesize_via_bridge(bridge_http, text, room, voice):
        out.extend(chunk)
    return bytes(out)


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
        """Gateway emitted a turn — stream-synthesize and play into the room.

        Uses ``stream_synthesize_via_bridge`` so we publish PCM frames as
        Resemble emits them rather than waiting for the full clip to render.
        For an N-second utterance, first audio reaches the room in ~200-400ms
        rather than ~N seconds with the buffered ``/synthesize`` path.
        """
        text = msg.get("text", "")
        if not text:
            return
        voice = msg.get("voice", self._voice)
        chunks = 0
        async for chunk in stream_synthesize_via_bridge(
            self.bridge_http, text, self.room_name, voice
        ):
            if chunk:
                await self._publish_audio_frame(chunk)
                chunks += 1
        if chunks == 0:
            logger.warning(
                "stream_synthesize yielded 0 chunks for room=%s voice=%s",
                self.room_name, voice,
            )

    async def _publish_audio_frame(self, audio: bytes) -> None:
        """Push audio bytes into the LiveKit room as a published track.

        For the first iteration we use LiveKit's data channel for PCM
        delivery; the receiving client decodes + plays. Topic
        ``audio/pcm22050`` matches the Resemble streaming format (22050 Hz
        mono int16). A follow-up pass will publish a real audio track via
        ``livekit.rtc.AudioSource`` for true peer playback.
        """
        if self._room is None:
            logger.warning("audio frame dropped — room not connected")
            return
        await self._room.local_participant.publish_data(
            audio, kind="reliable", topic="audio/pcm22050"
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
