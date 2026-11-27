from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from .config import MusicBotConfig
from .livekit_publisher import AudioPublisher, LiveKitAudioPublisher
from .youtube import YouTubePcmStream, YouTubeTrack, probe_youtube_track, validate_youtube_url

logger = logging.getLogger(__name__)


class PlaybackBusyError(RuntimeError):
    """Raised when playback is requested in another channel while already active."""


class PcmStream(Protocol):
    async def start(self) -> None: ...
    def chunks(self, chunk_size: int = 3840) -> AsyncIterator[bytes]: ...
    async def close(self) -> None: ...


ProbeFn = Callable[[str], Awaitable[YouTubeTrack]]
StreamFactory = Callable[[str], PcmStream]
PublisherFactory = Callable[[], AudioPublisher]


@dataclass
class ActivePlayback:
    channel_id: str
    track: YouTubeTrack
    requested_by: str | None
    started_at: float
    stream: PcmStream
    publisher: AudioPublisher
    task: asyncio.Task[None] | None = None


class PlaybackManager:
    """Manages a single active voice-channel playback session (v1 scope).

    All busy checks, session starts, and stops are serialized under ``self._lock``.
    All status and error responses are strictly scoped to the requested ``channel_id``
    so another channel never learns what is playing or failed elsewhere.
    """

    def __init__(
        self,
        config: MusicBotConfig,
        *,
        probe_fn: ProbeFn | None = None,
        stream_factory: StreamFactory | None = None,
        publisher_factory: PublisherFactory | None = None,
    ) -> None:
        self._config = config
        self._probe_fn = probe_fn or (
            lambda url: probe_youtube_track(
                url,
                ytdlp_bin=config.ytdlp_bin,
                timeout_seconds=config.probe_timeout_seconds,
                max_duration_seconds=config.max_duration_seconds,
            )
        )
        self._stream_factory = stream_factory or (
            lambda url: YouTubePcmStream(
                url,
                sample_rate=config.sample_rate,
                num_channels=config.num_channels,
                ytdlp_bin=config.ytdlp_bin,
                ffmpeg_bin=config.ffmpeg_bin,
            )
        )
        self._publisher_factory = publisher_factory or (
            lambda: LiveKitAudioPublisher(
                sample_rate=config.sample_rate,
                num_channels=config.num_channels,
                frame_ms=config.frame_ms,
            )
        )
        self._lock = asyncio.Lock()
        self._active: ActivePlayback | None = None
        self._last_errors: dict[str, str] = {}

    def is_playing(self) -> bool:
        return self._active is not None

    def status(self, channel_id: str) -> dict[str, object]:
        """Return playback status strictly scoped to ``channel_id``."""
        if not isinstance(channel_id, str) or not channel_id.strip():
            raise ValueError("channelId is required.")
        cid = channel_id.strip()
        active = self._active
        if active is not None and active.channel_id == cid:
            return {
                "state": "playing",
                "channelId": cid,
                "videoId": active.track.video_id,
                "url": active.track.canonical_url,
                "title": active.track.title,
                "durationSeconds": active.track.duration_seconds,
                "requestedBy": active.requested_by,
                "startedAt": int(active.started_at * 1000),
                "lastError": None,
            }
        return {
            "state": "idle",
            "channelId": cid,
            "videoId": None,
            "url": None,
            "title": None,
            "durationSeconds": None,
            "requestedBy": None,
            "startedAt": None,
            "lastError": self._last_errors.get(cid),
        }

    async def play(
        self,
        *,
        channel_id: str,
        livekit_url: str,
        token: str,
        raw_url: str,
        requested_by: str | None = None,
    ) -> dict[str, object]:
        _, canonical_url = validate_youtube_url(raw_url)
        cid = channel_id.strip() if isinstance(channel_id, str) else ""
        if not cid or not livekit_url or not token:
            raise ValueError("channelId, livekitUrl, and token are required.")

        async with self._lock:
            if self._active is not None and self._active.channel_id != cid:
                raise PlaybackBusyError("Music bot is already playing in another voice channel.")

            # Same-channel replacement: cleanly stop the previous track in this channel first.
            if self._active is not None and self._active.channel_id == cid:
                await self._stop_locked()

            self._last_errors.pop(cid, None)

            try:
                track = await self._probe_fn(canonical_url)
            except Exception as exc:
                self._last_errors[cid] = str(exc)
                raise

            stream = self._stream_factory(track.canonical_url)
            publisher = self._publisher_factory()

            try:
                await stream.start()
                await publisher.connect(livekit_url, token)
            except Exception as exc:
                self._last_errors[cid] = str(exc)
                await self._cleanup_resources(stream, publisher)
                raise

            playback = ActivePlayback(
                channel_id=cid,
                track=track,
                requested_by=requested_by,
                started_at=time.time(),
                stream=stream,
                publisher=publisher,
            )
            self._active = playback
            playback.task = asyncio.create_task(self._run_playback(playback))
            return self.status(cid)

    async def stop(self, *, channel_id: str) -> dict[str, object]:
        cid = channel_id.strip() if isinstance(channel_id, str) else ""
        if not cid:
            raise ValueError("channelId is required.")

        async with self._lock:
            if self._active is None or self._active.channel_id != cid:
                return {
                    "stopped": False,
                    "status": self.status(cid),
                }
            await self._stop_locked()
            return {
                "stopped": True,
                "status": self.status(cid),
            }

    async def shutdown(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        active = self._active
        if active is None:
            return
        self._active = None
        if active.task is not None and not active.task.done():
            active.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await active.task
        await self._cleanup_resources(active.stream, active.publisher)

    async def _run_playback(self, playback: ActivePlayback) -> None:
        frame_bytes = ((self._config.sample_rate * self._config.frame_ms) // 1000) * self._config.num_channels * 2
        try:
            await playback.publisher.stream_pcm(playback.stream.chunks(frame_bytes))
            logger.info(
                "Playback finished cleanly channel=%s video=%s",
                playback.channel_id,
                playback.track.video_id,
            )
        except asyncio.CancelledError:
            logger.info("Playback cancelled channel=%s", playback.channel_id)
            raise
        except Exception as exc:
            self._last_errors[playback.channel_id] = str(exc)
            logger.exception("Playback failed channel=%s", playback.channel_id)
        finally:
            await self._cleanup_resources(playback.stream, playback.publisher)
            if self._active is playback:
                self._active = None

    @staticmethod
    async def _cleanup_resources(stream: PcmStream, publisher: AudioPublisher) -> None:
        with contextlib.suppress(Exception):
            await stream.close()
        with contextlib.suppress(Exception):
            await publisher.close()
