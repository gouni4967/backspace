# Backspace YouTube Music Bot (v1)

Backspace 음성 채널에 참가해 YouTube 단일 곡을 재생·정지하는 별도 컨테이너 서비스입니다.

## 아키텍처
- **YouTube 입력 계층 (`music_bot/youtube.py`)**: HTTPS YouTube 단일 영상 URL만 엄격히 검증·정규화하고, 셸을 거치지 않고(`create_subprocess_exec`) `yt-dlp` → `ffmpeg` 파이프로 48 kHz 스테레오 `s16le` PCM을 디코딩합니다.
- **LiveKit 송출 계층 (`music_bot/livekit_publisher.py`)**: YouTube 입력과 완전히 분리되어 있으며, 전달받은 PCM 스트림을 `TrackSource.SOURCE_MICROPHONE` 트랙으로 LiveKit 방(`channelId`)에 송출합니다(`auto_subscribe=False`, `canSubscribe=false`).
- **권한·명령 제어 (`packages/server/src/routes/music.ts`, `messages.ts`)**: Backspace 서버가 요청 사용자의 음성 채널 `CONNECT` + `SPEAK` 권한을 검사한 뒤 짧은 TTL의 봇 전용 LiveKit 토큰을 발급해 내부 네트워크로 `music-bot`에 전달합니다.

## v1 지원 범위
- 음성 채널 채팅에서 `/play <YouTube URL>` / `/stop`
- REST API: `POST /api/channels/:id/music/play`, `POST /api/channels/:id/music/stop`, `GET /api/channels/:id/music/status`
- 단일 음성방 1곡 재생·정지 (검색, 대기열, 다중 음성방 동시 재생 제외)

## 단위 테스트 실행
```bash
PYTHONPATH=services/music-bot python3 -m unittest discover -s services/music-bot/tests -v
```
