from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
from collections.abc import AsyncIterator
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

ALLOWED_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "music.youtube.com",
        "youtu.be",
    }
)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
CONTROL_OR_SPACE_RE = re.compile(r"[\s\x00-\x1f\x7f]")


class YouTubeValidationError(ValueError):
    """Raised when the user-provided string is not a valid single YouTube video URL."""


class YouTubeSourceError(RuntimeError):
    """Raised when yt-dlp or ffmpeg fails to resolve or decode the YouTube audio."""


@dataclass(frozen=True)
class YouTubeTrack:
    video_id: str
    canonical_url: str
    title: str
    duration_seconds: int | None


def validate_youtube_url(raw_url: str) -> tuple[str, str]:
    """Validate and canonicalize a single YouTube video HTTPS URL.

    Rejects non-HTTPS schemes, non-YouTube hosts, playlists without a video ID,
    whitespace/control characters, and arbitrary shell-like input. Always returns
    a canonical ``https://www.youtube.com/watch?v=<11-char-id>`` URL.
    """
    if not isinstance(raw_url, str):
        raise YouTubeValidationError("YouTube URL must be a string.")

    candidate = raw_url.strip()
    if not candidate or len(candidate) > 512:
        raise YouTubeValidationError("YouTube URL is empty or too long.")
    if candidate.startswith("-"):
        raise YouTubeValidationError("YouTube URL must start with https://.")
    if CONTROL_OR_SPACE_RE.search(candidate):
        raise YouTubeValidationError("YouTube URL must not contain whitespace or control characters.")

    parsed = urlparse(candidate)
    if parsed.scheme != "https":
        raise YouTubeValidationError("Only https:// YouTube URLs are allowed.")
    if parsed.username or parsed.password or parsed.port is not None:
        raise YouTubeValidationError("Credentials and custom ports are not allowed in YouTube URLs.")

    hostname = (parsed.hostname or "").lower()
    if hostname not in ALLOWED_YOUTUBE_HOSTS:
        raise YouTubeValidationError("Only YouTube video URLs are supported.")

    video_id: str | None = None
    path_parts = [part for part in parsed.path.split("/") if part]

    if hostname == "youtu.be":
        if len(path_parts) == 1:
            video_id = path_parts[0]
    else:
        if parsed.path == "/watch":
            values = parse_qs(parsed.query).get("v")
            if values and len(values) == 1:
                video_id = values[0]
        elif len(path_parts) == 2 and path_parts[0] in {"shorts", "live", "embed"}:
            video_id = path_parts[1]

    if not video_id or not VIDEO_ID_RE.fullmatch(video_id):
        raise YouTubeValidationError("URL must point to a single valid YouTube video.")

    canonical_url = f"https://www.youtube.com/watch?v={video_id}"
    return video_id, canonical_url


async def _terminate_process(proc: asyncio.subprocess.Process | None, label: str) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGTERM)
        else:
            proc.terminate()
    except ProcessLookupError:
        return
    except OSError:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.terminate()

    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
        return
    except asyncio.TimeoutError:
        logger.warning("%s (pid=%s) did not exit after SIGTERM; sending SIGKILL", label, proc.pid)

    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except ProcessLookupError:
        return
    except OSError:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()

    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=2.0)


async def probe_youtube_track(
    canonical_url: str,
    *,
    ytdlp_bin: str = "yt-dlp",
    timeout_seconds: float = 25.0,
    max_duration_seconds: int = 10800,
) -> YouTubeTrack:
    """Resolve metadata for a canonical YouTube video URL without downloading."""
    video_id, normalized = validate_youtube_url(canonical_url)
    args = [
        ytdlp_bin,
        "--no-playlist",
        "--no-exec",
        "--no-config",
        "--no-warnings",
        "--skip-download",
        "--dump-single-json",
        "--",
        normalized,
    ]
    kwargs = {"start_new_session": True} if os.name == "posix" else {}
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except asyncio.TimeoutError as exc:
        await _terminate_process(proc, "yt-dlp(probe)")
        raise YouTubeSourceError("Timed out while resolving YouTube metadata.") from exc
    except asyncio.CancelledError:
        await _terminate_process(proc, "yt-dlp(probe)")
        raise

    if proc.returncode != 0:
        err_text = stderr.decode("utf-8", errors="replace").strip().splitlines()
        summary = err_text[-1] if err_text else f"exit code {proc.returncode}"
        raise YouTubeSourceError(f"yt-dlp could not resolve video: {summary}")

    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise YouTubeSourceError("yt-dlp returned invalid metadata JSON.") from exc

    if payload.get("is_live"):
        raise YouTubeSourceError("Live streams are not supported in v1.")

    raw_duration = payload.get("duration")
    duration = int(raw_duration) if isinstance(raw_duration, (int, float)) else None
    if duration is not None and duration > max_duration_seconds:
        raise YouTubeSourceError(
            f"Track duration ({duration}s) exceeds limit ({max_duration_seconds}s)."
        )

    title = str(payload.get("title") or video_id).strip() or video_id
    return YouTubeTrack(
        video_id=video_id,
        canonical_url=normalized,
        title=title,
        duration_seconds=duration,
    )


class YouTubePcmStream:
    """Streams decoded 16-bit signed little-endian PCM from YouTube via yt-dlp -> ffmpeg.

    All subprocesses are launched with ``create_subprocess_exec`` (never a shell)
    and placed in their own process group so ``close()`` guarantees full cleanup.
    """

    def __init__(
        self,
        canonical_url: str,
        *,
        sample_rate: int = 48000,
        num_channels: int = 2,
        ytdlp_bin: str = "yt-dlp",
        ffmpeg_bin: str = "ffmpeg",
    ) -> None:
        _, self._canonical_url = validate_youtube_url(canonical_url)
        self._sample_rate = sample_rate
        self._num_channels = num_channels
        self._ytdlp_bin = ytdlp_bin
        self._ffmpeg_bin = ffmpeg_bin
        self._ytdlp: asyncio.subprocess.Process | None = None
        self._ffmpeg: asyncio.subprocess.Process | None = None
        self._pump_task: asyncio.Task[None] | None = None
        self._closed = False

    async def start(self) -> None:
        kwargs = {"start_new_session": True} if os.name == "posix" else {}
        ytdlp_args = [
            self._ytdlp_bin,
            "--no-playlist",
            "--no-exec",
            "--no-config",
            "--no-part",
            "--no-warnings",
            "-f",
            "bestaudio[acodec=opus]/bestaudio/best",
            "-o",
            "-",
            "--",
            self._canonical_url,
        ]
        ffmpeg_args = [
            self._ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-vn",
            "-sn",
            "-dn",
            "-ac",
            str(self._num_channels),
            "-ar",
            str(self._sample_rate),
            "-f",
            "s16le",
            "pipe:1",
        ]
        self._ytdlp = await asyncio.create_subprocess_exec(
            *ytdlp_args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs,
        )
        try:
            self._ffmpeg = await asyncio.create_subprocess_exec(
                *ffmpeg_args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                **kwargs,
            )
        except Exception:
            await _terminate_process(self._ytdlp, "yt-dlp")
            self._ytdlp = None
            raise

        self._pump_task = asyncio.create_task(self._pump_ytdlp_to_ffmpeg())

    async def _pump_ytdlp_to_ffmpeg(self) -> None:
        assert self._ytdlp is not None and self._ytdlp.stdout is not None
        assert self._ffmpeg is not None and self._ffmpeg.stdin is not None
        try:
            while True:
                chunk = await self._ytdlp.stdout.read(32768)
                if not chunk:
                    break
                self._ffmpeg.stdin.write(chunk)
                await self._ffmpeg.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            with contextlib.suppress(Exception):
                self._ffmpeg.stdin.close()
                await self._ffmpeg.stdin.wait_closed()

    async def chunks(self, chunk_size: int = 3840) -> AsyncIterator[bytes]:
        if self._ffmpeg is None or self._ffmpeg.stdout is None:
            raise YouTubeSourceError("YouTubePcmStream has not been started.")

        emitted_any = False
        while True:
            data = await self._ffmpeg.stdout.read(chunk_size)
            if not data:
                break
            emitted_any = True
            yield data

        if not self._closed:
            yt_rc = await self._ytdlp.wait() if self._ytdlp else 0
            ff_rc = await self._ffmpeg.wait() if self._ffmpeg else 0
            if not emitted_any and (yt_rc != 0 or ff_rc != 0):
                yt_err = ""
                ff_err = ""
                if self._ytdlp and self._ytdlp.stderr:
                    yt_err = (await self._ytdlp.stderr.read()).decode("utf-8", errors="replace").strip()
                if self._ffmpeg and self._ffmpeg.stderr:
                    ff_err = (await self._ffmpeg.stderr.read()).decode("utf-8", errors="replace").strip()
                detail = ff_err or yt_err or f"yt-dlp={yt_rc}, ffmpeg={ff_rc}"
                raise YouTubeSourceError(f"Audio pipeline failed before producing audio: {detail}")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pump = self._pump_task
        if pump is not None and not pump.done():
            pump.cancel()
        # The pump can be waiting for FFmpeg to consume buffered input. Stop
        # the decoder first so closing its input cannot block playback teardown.
        await asyncio.gather(
            _terminate_process(self._ytdlp, "yt-dlp"),
            _terminate_process(self._ffmpeg, "ffmpeg"),
        )
        if pump is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(pump, timeout=2.0)
        self._ytdlp = None
        self._ffmpeg = None
        self._pump_task = None
