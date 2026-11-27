from __future__ import annotations

import hmac
import logging
import shutil
from typing import Any

from .config import MusicBotConfig, load_config
from .session import PlaybackBusyError, PlaybackManager, PlaybackQueueFullError
from .youtube import YouTubeSourceError, YouTubeValidationError

logger = logging.getLogger(__name__)


def is_authorized_header(header: str, secret: str) -> bool:
    prefix = "Bearer "
    if not header.startswith(prefix):
        return False
    provided = header[len(prefix) :].strip()
    if not provided:
        return False
    return hmac.compare_digest(provided, secret)


def dispatch_health(config: MusicBotConfig, playback: PlaybackManager) -> tuple[int, dict[str, Any]]:
    return (
        200,
        {
            "status": "ok",
            "tools": {
                "ytdlp": shutil.which(config.ytdlp_bin) is not None,
                "ffmpeg": shutil.which(config.ffmpeg_bin) is not None,
                "deno": shutil.which("deno") is not None,
            },
            "busy": playback.is_playing(),
        },
    )


def dispatch_status(playback: PlaybackManager, channel_id: str | None) -> tuple[int, dict[str, Any]]:
    if not isinstance(channel_id, str) or not channel_id.strip():
        return 400, {"error": "channelId query parameter is required"}
    return 200, playback.status(channel_id.strip())


async def dispatch_play(
    playback: PlaybackManager,
    config: MusicBotConfig,
    body: Any,
) -> tuple[int, dict[str, Any]]:
    if not isinstance(body, dict):
        return 400, {"error": "JSON body must be an object"}

    channel_id = body.get("channelId")
    token = body.get("token")
    url = body.get("url")
    livekit_url = body.get("livekitUrl") or config.default_livekit_url
    requested_by = body.get("requestedBy")

    if not isinstance(channel_id, str) or not channel_id.strip():
        return 400, {"error": "channelId is required"}
    if not isinstance(token, str) or not token.strip():
        return 400, {"error": "token is required"}
    if not isinstance(livekit_url, str) or not livekit_url.strip():
        return 400, {"error": "livekitUrl is required"}
    if not isinstance(url, str) or not url.strip():
        return 400, {"error": "url is required"}

    try:
        status = await playback.play(
            channel_id=channel_id.strip(),
            livekit_url=livekit_url.strip(),
            token=token.strip(),
            raw_url=url.strip(),
            requested_by=str(requested_by).strip() if isinstance(requested_by, str) and requested_by.strip() else None,
        )
        return 200, status
    except PlaybackQueueFullError as exc:
        return 409, {"error": str(exc), "code": "queue_full"}
    except PlaybackBusyError as exc:
        return (
            409,
            {
                "error": str(exc).rstrip("."),
                "code": "playback_busy",
            },
        )
    except YouTubeValidationError as exc:
        return 400, {"error": str(exc), "code": "invalid_youtube_url"}
    except YouTubeSourceError as exc:
        return 422, {"error": str(exc), "code": "youtube_source_failed"}
    except Exception as exc:
        logger.exception("Failed to start playback")
        return 502, {"error": str(exc), "code": "playback_failed"}


async def dispatch_stop(playback: PlaybackManager, body: Any) -> tuple[int, dict[str, Any]]:
    if not isinstance(body, dict):
        return 400, {"error": "JSON body must be an object"}
    channel_id = body.get("channelId")
    if not isinstance(channel_id, str) or not channel_id.strip():
        return 400, {"error": "channelId is required"}
    action = playback.skip if body.get("skip") is True else playback.stop
    result = await action(channel_id=channel_id.strip())
    return 200, result


def create_app(
    config: MusicBotConfig,
    manager: PlaybackManager | None = None,
):
    from aiohttp import web

    playback = manager or PlaybackManager(config)
    app = web.Application()
    app["config"] = config
    app["playback"] = playback

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        if request.path == "/health":
            return await handler(request)
        if not is_authorized_header(request.headers.get("Authorization", ""), config.api_secret):
            return web.json_response({"error": "Unauthorized"}, status=401)
        return await handler(request)

    app.middlewares.append(auth_middleware)

    async def handle_health(_request: web.Request) -> web.Response:
        status, payload = dispatch_health(config, playback)
        return web.json_response(payload, status=status)

    async def handle_status(request: web.Request) -> web.Response:
        status, payload = dispatch_status(playback, request.query.get("channelId"))
        return web.json_response(payload, status=status)

    async def handle_play(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        status, payload = await dispatch_play(playback, config, body)
        return web.json_response(payload, status=status)

    async def handle_stop(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)
        status, payload = await dispatch_stop(playback, body)
        return web.json_response(payload, status=status)

    async def on_shutdown(_app: web.Application) -> None:
        await playback.shutdown()

    app.router.add_get("/health", handle_health)
    app.router.add_get("/status", handle_status)
    app.router.add_post("/play", handle_play)
    app.router.add_post("/stop", handle_stop)
    app.on_shutdown.append(on_shutdown)
    return app


def main() -> None:
    from aiohttp import web

    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    app = create_app(config)
    web.run_app(app, host=config.host, port=config.port)
