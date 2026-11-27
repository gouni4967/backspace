from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import AsyncIterator

from music_bot.app import (
    dispatch_health,
    dispatch_play,
    dispatch_status,
    dispatch_stop,
    is_authorized_header,
)
from music_bot.config import MusicBotConfig
from music_bot.session import PlaybackBusyError, PlaybackManager
from music_bot.youtube import YouTubeTrack, YouTubeValidationError, validate_youtube_url


def _test_config() -> MusicBotConfig:
    return MusicBotConfig(
        host="127.0.0.1",
        port=3100,
        api_secret="test-secret-0123456789",
        default_livekit_url="ws://10.253.245.2:7880",
        sample_rate=48000,
        num_channels=2,
        frame_ms=20,
        max_duration_seconds=3600,
        probe_timeout_seconds=5.0,
        ytdlp_bin="yt-dlp",
        ffmpeg_bin="ffmpeg",
        log_level="INFO",
    )


class FakePcmStream:
    def __init__(self, *, chunks: list[bytes] | None = None, hold_open: bool = False, fail_midstream: bool = False) -> None:
        self.started = False
        self.closed = False
        self._chunks = chunks if chunks is not None else [b"\x01\x02" * 1920]
        self._hold_open = hold_open
        self._fail_midstream = fail_midstream

    async def start(self) -> None:
        self.started = True

    async def chunks(self, chunk_size: int = 3840) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            yield chunk
        if self._fail_midstream:
            raise RuntimeError("secret-room-a-decoder-error")
        if self._hold_open:
            await asyncio.sleep(60)

    async def close(self) -> None:
        self.closed = True


class FakePublisher:
    def __init__(self, *, fail_connect: bool = False) -> None:
        self.connected_args: tuple[str, str] | None = None
        self.received_bytes = 0
        self.closed = False
        self._fail_connect = fail_connect

    async def connect(self, livekit_url: str, token: str) -> None:
        if self._fail_connect:
            raise RuntimeError("secret-room-a-livekit-error")
        self.connected_args = (livekit_url, token)

    async def stream_pcm(self, pcm_chunks: AsyncIterator[bytes]) -> None:
        async for chunk in pcm_chunks:
            self.received_bytes += len(chunk)

    async def close(self) -> None:
        self.closed = True


class YouTubeValidationTests(unittest.TestCase):
    def test_accepts_canonical_and_short_youtube_urls(self) -> None:
        cases = [
            ("https://www.youtube.com/watch?v=aqz-KE-bpKQ", "aqz-KE-bpKQ"),
            ("https://youtube.com/watch?v=aqz-KE-bpKQ&list=PL123&t=12", "aqz-KE-bpKQ"),
            ("https://m.youtube.com/watch?v=jNQXAC9IVRw", "jNQXAC9IVRw"),
            ("https://music.youtube.com/watch?v=jNQXAC9IVRw", "jNQXAC9IVRw"),
            ("https://youtu.be/aqz-KE-bpKQ?si=abc", "aqz-KE-bpKQ"),
            ("https://www.youtube.com/shorts/aqz-KE-bpKQ", "aqz-KE-bpKQ"),
        ]
        for raw, expected_id in cases:
            video_id, canonical = validate_youtube_url(raw)
            self.assertEqual(video_id, expected_id)
            self.assertEqual(canonical, f"https://www.youtube.com/watch?v={expected_id}")

    def test_rejects_non_youtube_and_unsafe_inputs(self) -> None:
        bad_inputs = [
            "",
            "   ",
            "http://www.youtube.com/watch?v=aqz-KE-bpKQ",
            "file:///etc/passwd",
            "https://example.com/watch?v=aqz-KE-bpKQ",
            "https://youtube.com.evil.com/watch?v=aqz-KE-bpKQ",
            "https://www.youtube.com/playlist?list=PL1234567890",
            "https://www.youtube.com/watch?v=short",
            "https://www.youtube.com/watch?v=aqz-KE-bpKQ;rm -rf /",
            "https://www.youtube.com/watch?v=$(id)",
            "--exec 'touch /tmp/pwn' https://youtu.be/aqz-KE-bpKQ",
            "https://user:pass@www.youtube.com/watch?v=aqz-KE-bpKQ",
            "https://www.youtube.com:8443/watch?v=aqz-KE-bpKQ",
        ]
        for raw in bad_inputs:
            with self.assertRaises(YouTubeValidationError, msg=f"Should reject {raw!r}"):
                validate_youtube_url(raw)


class PlaybackIsolationAndLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_room_b_play_status_stop_isolated_while_room_a_keeps_playing(self) -> None:
        cfg = _test_config()
        stream_a = FakePcmStream(hold_open=True)
        pub_a = FakePublisher()
        created_streams: list[FakePcmStream] = []

        def make_stream(_url: str) -> FakePcmStream:
            if not created_streams:
                created_streams.append(stream_a)
                return stream_a
            extra = FakePcmStream(hold_open=True)
            created_streams.append(extra)
            return extra

        async def fake_probe(url: str) -> YouTubeTrack:
            return YouTubeTrack("aqz-KE-bpKQ", url, "Private Room A Track", 634)

        mgr = PlaybackManager(
            cfg,
            probe_fn=fake_probe,
            stream_factory=make_stream,
            publisher_factory=lambda: pub_a,
        )

        # 1. Start playback in Room A
        code_a, body_a = await dispatch_play(
            mgr,
            cfg,
            {
                "channelId": "room-A",
                "livekitUrl": "ws://10.253.245.2:7880",
                "token": "jwt-room-a",
                "url": "https://youtu.be/aqz-KE-bpKQ",
                "requestedBy": "alice-room-a",
            },
        )
        self.assertEqual(code_a, 200)
        self.assertEqual(body_a["state"], "playing")
        self.assertEqual(body_a["channelId"], "room-A")
        await asyncio.sleep(0.02)
        self.assertGreater(pub_a.received_bytes, 0)

        # 2. Room B /play -> 409 Conflict, no Room A info leaked, Room A untouched
        code_b_play, body_b_play = await dispatch_play(
            mgr,
            cfg,
            {
                "channelId": "room-B",
                "livekitUrl": "ws://10.253.245.2:7880",
                "token": "jwt-room-b",
                "url": "https://youtu.be/jNQXAC9IVRw",
                "requestedBy": "bob-room-b",
            },
        )
        self.assertEqual(code_b_play, 409)
        self.assertEqual(body_b_play["code"], "playback_busy")
        self.assertEqual(len(created_streams), 1)
        self.assertFalse(stream_a.closed)
        self.assertFalse(pub_a.closed)

        # 3. Room B /status -> 200 idle scoped to room-B, zero Room A details
        code_b_status, body_b_status = dispatch_status(mgr, "room-B")
        self.assertEqual(code_b_status, 200)
        self.assertEqual(
            body_b_status,
            {
                "state": "idle",
                "channelId": "room-B",
                "videoId": None,
                "url": None,
                "title": None,
                "durationSeconds": None,
                "requestedBy": None,
                "startedAt": None,
                "lastError": None,
            },
        )

        # 4. Room B /stop -> 200 stopped=False scoped to room-B, Room A still playing
        code_b_stop, body_b_stop = await dispatch_stop(mgr, {"channelId": "room-B"})
        self.assertEqual(code_b_stop, 200)
        self.assertFalse(body_b_stop["stopped"])
        self.assertNotIn("reason", body_b_stop)
        self.assertEqual(body_b_stop["status"]["channelId"], "room-B")
        self.assertEqual(body_b_stop["status"]["state"], "idle")

        # Assert none of Room B's responses or /health leak Room A secrets
        _, body_health = dispatch_health(cfg, mgr)
        for resp in (body_b_play, body_b_status, body_b_stop, body_health):
            serialized = json.dumps(resp)
            for secret in ("room-A", "Private Room A Track", "aqz-KE-bpKQ", "alice-room-a"):
                self.assertNotIn(secret, serialized)

        # 5. Room A is STILL playing and only Room A's /stop tears it down
        _, status_a = dispatch_status(mgr, "room-A")
        self.assertEqual(status_a["state"], "playing")
        self.assertEqual(status_a["title"], "Private Room A Track")
        self.assertFalse(stream_a.closed)
        self.assertFalse(pub_a.closed)

        code_a_stop, body_a_stop = await dispatch_stop(mgr, {"channelId": "room-A"})
        self.assertEqual(code_a_stop, 200)
        self.assertTrue(body_a_stop["stopped"])
        self.assertEqual(body_a_stop["status"]["state"], "idle")
        self.assertTrue(stream_a.closed)
        self.assertTrue(pub_a.closed)

    async def test_concurrent_play_requests_serialize_under_same_lock(self) -> None:
        cfg = _test_config()
        probe_started = asyncio.Event()
        allow_probe_finish = asyncio.Event()
        streams: list[FakePcmStream] = []
        pubs: list[FakePublisher] = []

        async def slow_probe(url: str) -> YouTubeTrack:
            probe_started.set()
            await allow_probe_finish.wait()
            return YouTubeTrack("aqz-KE-bpKQ", url, "Room A Slow Probe", 100)

        def make_stream(_url: str) -> FakePcmStream:
            s = FakePcmStream(hold_open=True)
            streams.append(s)
            return s

        def make_pub() -> FakePublisher:
            p = FakePublisher()
            pubs.append(p)
            return p

        mgr = PlaybackManager(
            cfg,
            probe_fn=slow_probe,
            stream_factory=make_stream,
            publisher_factory=make_pub,
        )

        task_a = asyncio.create_task(
            mgr.play(
                channel_id="room-A",
                livekit_url="ws://10.253.245.2:7880",
                token="jwt-a",
                raw_url="https://youtu.be/aqz-KE-bpKQ",
                requested_by="alice",
            )
        )
        await probe_started.wait()

        # Room B arrives while Room A is still probing inside the lock
        task_b = asyncio.create_task(
            mgr.play(
                channel_id="room-B",
                livekit_url="ws://10.253.245.2:7880",
                token="jwt-b",
                raw_url="https://youtu.be/jNQXAC9IVRw",
                requested_by="bob",
            )
        )
        await asyncio.sleep(0.01)
        allow_probe_finish.set()

        status_a = await task_a
        self.assertEqual(status_a["state"], "playing")
        self.assertEqual(status_a["channelId"], "room-A")

        with self.assertRaises(PlaybackBusyError):
            await task_b

        self.assertEqual(len(streams), 1)
        self.assertEqual(len(pubs), 1)
        self.assertFalse(streams[0].closed)
        await mgr.shutdown()

    async def test_room_a_errors_are_not_exposed_to_room_b(self) -> None:
        cfg = _test_config()
        stream = FakePcmStream(fail_midstream=True)
        publisher = FakePublisher()

        async def fake_probe(url: str) -> YouTubeTrack:
            return YouTubeTrack("aqz-KE-bpKQ", url, "Sample", 10)

        mgr = PlaybackManager(
            cfg,
            probe_fn=fake_probe,
            stream_factory=lambda _url: stream,
            publisher_factory=lambda: publisher,
        )

        await mgr.play(
            channel_id="room-A",
            livekit_url="ws://10.253.245.2:7880",
            token="jwt-token",
            raw_url="https://youtu.be/aqz-KE-bpKQ",
        )
        await asyncio.sleep(0.05)
        self.assertTrue(stream.closed)
        self.assertTrue(publisher.closed)

        status_a = mgr.status("room-A")
        self.assertEqual(status_a["state"], "idle")
        self.assertIn("secret-room-a-decoder-error", str(status_a["lastError"]))

        # Room B must NOT see Room A's error
        status_b = mgr.status("room-B")
        self.assertEqual(status_b["state"], "idle")
        self.assertEqual(status_b["channelId"], "room-B")
        self.assertIsNone(status_b["lastError"])

    async def test_connect_failure_cleans_up_decoder_and_scopes_error(self) -> None:
        cfg = _test_config()
        stream = FakePcmStream()
        publisher = FakePublisher(fail_connect=True)

        async def fake_probe(url: str) -> YouTubeTrack:
            return YouTubeTrack("aqz-KE-bpKQ", url, "Sample", 10)

        mgr = PlaybackManager(
            cfg,
            probe_fn=fake_probe,
            stream_factory=lambda _url: stream,
            publisher_factory=lambda: publisher,
        )

        with self.assertRaises(RuntimeError):
            await mgr.play(
                channel_id="room-A",
                livekit_url="ws://10.253.245.2:7880",
                token="jwt-token",
                raw_url="https://youtu.be/aqz-KE-bpKQ",
            )
        self.assertTrue(stream.closed)
        self.assertTrue(publisher.closed)
        self.assertIn("secret-room-a-livekit-error", str(mgr.status("room-A")["lastError"]))
        self.assertIsNone(mgr.status("room-B")["lastError"])

    async def test_http_validation_and_bearer_auth(self) -> None:
        cfg = _test_config()
        mgr = PlaybackManager(cfg)

        self.assertTrue(is_authorized_header(f"Bearer {cfg.api_secret}", cfg.api_secret))
        self.assertFalse(is_authorized_header("Bearer wrong-secret", cfg.api_secret))
        self.assertFalse(is_authorized_header("", cfg.api_secret))

        code_s, _ = dispatch_status(mgr, None)
        self.assertEqual(code_s, 400)
        code_stop, _ = await dispatch_stop(mgr, {})
        self.assertEqual(code_stop, 400)


if __name__ == "__main__":
    unittest.main()
