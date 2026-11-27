from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Protocol

logger = logging.getLogger(__name__)


class AudioPublisher(Protocol):
    async def connect(self, livekit_url: str, token: str) -> None: ...
    async def stream_pcm(self, pcm_chunks: AsyncIterator[bytes]) -> None: ...
    async def close(self) -> None: ...


class LiveKitAudioPublisher:
    """Publishes raw 16-bit PCM frames to a LiveKit room as a Microphone track.

    Backspace's web client routes remote ``Track.Source.Microphone`` tracks to
    ``GlobalAudioRenderer``, so every participant in the voice channel hears the
    bot automatically and can control its individual volume or locally mute it.
    """

    def __init__(
        self,
        *,
        sample_rate: int = 48000,
        num_channels: int = 2,
        frame_ms: int = 20,
        track_name: str = "music-bot-audio",
    ) -> None:
        self._sample_rate = sample_rate
        self._num_channels = num_channels
        self._frame_ms = frame_ms
        self._samples_per_channel = (sample_rate * frame_ms) // 1000
        self._frame_bytes = self._samples_per_channel * num_channels * 2  # int16 = 2 bytes
        self._track_name = track_name
        self._room = None
        self._source = None
        self._track = None
        self._publication = None
        self._closed = False

    async def connect(self, livekit_url: str, token: str) -> None:
        from livekit import rtc

        room = rtc.Room()
        source = rtc.AudioSource(self._sample_rate, self._num_channels)
        track = rtc.LocalAudioTrack.create_audio_track(self._track_name, source)

        try:
            await room.connect(
                livekit_url,
                token,
                options=rtc.RoomOptions(auto_subscribe=False),
            )
            options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
            publication = await room.local_participant.publish_track(track, options)
        except Exception:
            with contextlib.suppress(Exception):
                await source.aclose()
            with contextlib.suppress(Exception):
                await room.disconnect()
            raise

        self._room = room
        self._source = source
        self._track = track
        self._publication = publication
        logger.info("Connected to LiveKit room=%s track_sid=%s", room.name, publication.sid)

    async def stream_pcm(self, pcm_chunks: AsyncIterator[bytes]) -> None:
        from livekit import rtc

        if self._source is None:
            raise RuntimeError("LiveKitAudioPublisher is not connected.")

        buffer = bytearray()
        frame_bytes = self._frame_bytes
        frame_interval = self._frame_ms / 1000.0
        loop = asyncio.get_running_loop()
        next_deadline = loop.time()

        async for chunk in pcm_chunks:
            if not chunk:
                continue
            buffer.extend(chunk)
            while len(buffer) >= frame_bytes:
                raw = bytes(buffer[:frame_bytes])
                del buffer[:frame_bytes]
                frame = rtc.AudioFrame(
                    data=raw,
                    sample_rate=self._sample_rate,
                    num_channels=self._num_channels,
                    samples_per_channel=self._samples_per_channel,
                )
                await self._source.capture_frame(frame)
                next_deadline += frame_interval
                sleep_for = next_deadline - loop.time()
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)
                elif sleep_for < -0.5:
                    # Resync clock if decoder stalled briefly.
                    next_deadline = loop.time()

        if buffer:
            buffer.extend(b"\x00" * (frame_bytes - len(buffer)))
            frame = rtc.AudioFrame(
                data=bytes(buffer),
                sample_rate=self._sample_rate,
                num_channels=self._num_channels,
                samples_per_channel=self._samples_per_channel,
            )
            await self._source.capture_frame(frame)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        room = self._room
        source = self._source
        publication = self._publication
        self._room = None
        self._source = None
        self._track = None
        self._publication = None

        if room is not None and publication is not None:
            with contextlib.suppress(Exception):
                await room.local_participant.unpublish_track(publication.sid)
        if source is not None:
            with contextlib.suppress(Exception):
                await source.aclose()
        if room is not None:
            with contextlib.suppress(Exception):
                await room.disconnect()
