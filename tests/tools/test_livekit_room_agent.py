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
