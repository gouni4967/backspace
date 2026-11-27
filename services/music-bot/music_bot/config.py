from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class MusicBotConfig:
    host: str
    port: int
    api_secret: str
    default_livekit_url: str | None
    sample_rate: int
    num_channels: int
    frame_ms: int
    max_duration_seconds: int
    probe_timeout_seconds: float
    ytdlp_bin: str
    ffmpeg_bin: str
    log_level: str


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw.strip())
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def load_config() -> MusicBotConfig:
    api_secret = (os.environ.get("MUSIC_BOT_API_SECRET") or "").strip()
    if len(api_secret) < 16:
        raise ValueError(
            "MUSIC_BOT_API_SECRET must be set and at least 16 characters long."
        )

    default_lk = (os.environ.get("MUSIC_BOT_LIVEKIT_URL") or "").strip() or None
    return MusicBotConfig(
        host=(os.environ.get("MUSIC_BOT_HOST") or "0.0.0.0").strip(),
        port=_env_int("MUSIC_BOT_PORT", 3100),
        api_secret=api_secret,
        default_livekit_url=default_lk,
        sample_rate=_env_int("MUSIC_BOT_SAMPLE_RATE", 48000, minimum=8000),
        num_channels=_env_int("MUSIC_BOT_CHANNELS", 2, minimum=1),
        frame_ms=_env_int("MUSIC_BOT_FRAME_MS", 20, minimum=10),
        max_duration_seconds=_env_int("MUSIC_BOT_MAX_DURATION_SECONDS", 10800, minimum=1),
        probe_timeout_seconds=float(os.environ.get("MUSIC_BOT_PROBE_TIMEOUT_SECONDS", "25")),
        ytdlp_bin=(os.environ.get("MUSIC_BOT_YTDLP_BIN") or "yt-dlp").strip(),
        ffmpeg_bin=(os.environ.get("MUSIC_BOT_FFMPEG_BIN") or "ffmpeg").strip(),
        log_level=(os.environ.get("MUSIC_BOT_LOG_LEVEL") or "INFO").strip().upper(),
    )
