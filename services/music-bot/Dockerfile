# Raspberry Pi ARM64 (and amd64) compatible image for Backspace YouTube Music Bot.
# - python:3.12-slim-bookworm provides glibc >= 2.28 required by livekit aarch64 wheels.
# - denoland/deno:bin supplies the native JS runtime required by yt-dlp EJS YouTube solvers.
FROM denoland/deno:bin AS deno-bin

FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg \
       ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno-bin /deno /usr/local/bin/deno

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY music_bot ./music_bot

RUN useradd --system --uid 10001 --create-home musicbot
USER musicbot

EXPOSE 3100

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3100/health', timeout=3)" || exit 1

CMD ["python", "-m", "music_bot"]
