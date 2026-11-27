import { AccessToken, TrackSource } from 'livekit-server-sdk';
import { eq } from 'drizzle-orm';
import { config } from '../config.js';
import { getDb, schema } from '../db/index.js';
import {
  getChannelSpaceId,
  hasPermission,
  computePermissions,
  PermissionBits,
} from './permissions.js';
import type { ErrorCode, ErrorDetails } from '@backspace/shared/src/errors';

export const MUSIC_BOT_USER_ID = 'music-bot';

const ALLOWED_YOUTUBE_HOSTS = new Set([
  'youtube.com',
  'www.youtube.com',
  'm.youtube.com',
  'music.youtube.com',
  'youtu.be',
]);
const VIDEO_ID_RE = /^[A-Za-z0-9_-]{11}$/;
const CONTROL_OR_SPACE_RE = /[\s\x00-\x1f\x7f]/;

export interface ValidatedYouTubeUrl {
  videoId: string;
  canonicalUrl: string;
}

export type ParsedMusicCommand =
  | { command: 'play'; url: string }
  | { command: 'stop' }
  | { command: 'invalid_play' };

export interface MusicServiceError {
  ok: false;
  status: number;
  code: ErrorCode;
  details?: ErrorDetails;
  message?: string;
}

export interface MusicServiceSuccess<T> {
  ok: true;
  data: T;
}

export type MusicServiceResult<T> = MusicServiceSuccess<T> | MusicServiceError;

export interface MusicPlaybackStatus {
  state: 'idle' | 'playing';
  channelId: string | null;
  videoId: string | null;
  url: string | null;
  title: string | null;
  durationSeconds: number | null;
  requestedBy: string | null;
  startedAt: number | null;
  lastError: string | null;
}

export interface MusicStopResponse {
  stopped: boolean;
  status: MusicPlaybackStatus;
}

function idleStatusFor(channelId: string): MusicPlaybackStatus {
  return {
    state: 'idle',
    channelId,
    videoId: null,
    url: null,
    title: null,
    durationSeconds: null,
    requestedBy: null,
    startedAt: null,
    lastError: null,
  };
}

/**
 * Defense-in-depth channel scoping: never forward another channel's
 * playback metadata, requester, or error to the requesting channel.
 */
function scopePlaybackStatus(
  channelId: string,
  raw: MusicPlaybackStatus | null | undefined,
): MusicPlaybackStatus {
  if (!raw || raw.channelId !== channelId) {
    return idleStatusFor(channelId);
  }
  return raw;
}

export function isMusicBotConfigured(): boolean {
  return Boolean(config.musicBot.url && config.musicBot.secret);
}

/**
 * Strictly validate and canonicalize a single YouTube video HTTPS URL.
 * Mirrors the Python music-bot validator so invalid or unsafe strings are
 * rejected at the Backspace server boundary before any outbound call.
 */
export function validateYouTubeUrl(rawUrl: string): ValidatedYouTubeUrl | null {
  if (typeof rawUrl !== 'string') return null;
  const candidate = rawUrl.trim();
  if (!candidate || candidate.length > 512) return null;
  if (candidate.startsWith('-')) return null;
  if (CONTROL_OR_SPACE_RE.test(candidate)) return null;

  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    return null;
  }

  if (parsed.protocol !== 'https:') return null;
  if (parsed.username || parsed.password || parsed.port) return null;

  const host = parsed.hostname.toLowerCase();
  if (!ALLOWED_YOUTUBE_HOSTS.has(host)) return null;

  const pathParts = parsed.pathname.split('/').filter(Boolean);
  let videoId: string | null = null;

  if (host === 'youtu.be') {
    if (pathParts.length === 1) {
      videoId = pathParts[0] ?? null;
    }
  } else if (parsed.pathname === '/watch') {
    const values = parsed.searchParams.getAll('v');
    if (values.length === 1) {
      videoId = values[0] ?? null;
    }
  } else if (pathParts.length === 2 && (pathParts[0] === 'shorts' || pathParts[0] === 'live' || pathParts[0] === 'embed')) {
    videoId = pathParts[1] ?? null;
  }

  if (!videoId || !VIDEO_ID_RE.test(videoId)) return null;

  return {
    videoId,
    canonicalUrl: `https://www.youtube.com/watch?v=${videoId}`,
  };
}

/**
 * Parse `/play <url>` and `/stop` commands from a chat message.
 * Returns `null` for ordinary messages so non-command text is untouched.
 */
export function parseMusicCommand(content: string | null | undefined): ParsedMusicCommand | null {
  if (typeof content !== 'string') return null;
  const trimmed = content.trim();
  if (!trimmed.startsWith('/')) return null;

  const parts = trimmed.split(/\s+/);
  const cmd = parts[0]?.toLowerCase();
  if (cmd === '/stop' && parts.length === 1) {
    return { command: 'stop' };
  }
  if (cmd === '/play') {
    if (parts.length !== 2 || !parts[1]) {
      return { command: 'invalid_play' };
    }
    return { command: 'play', url: parts[1] };
  }
  return null;
}

/**
 * Verify the channel exists, is a voice channel, and the user holds both
 * CONNECT and SPEAK (or ADMINISTRATOR) on that channel.
 */
export function checkVoiceMusicPermission(
  userId: string,
  channelId: string,
): MusicServiceResult<{ spaceId: string }> {
  const spaceId = getChannelSpaceId(channelId);
  if (!spaceId) {
    return { ok: false, status: 404, code: 'channel_not_found' };
  }

  const db = getDb();
  const channel = db
    .select({ id: schema.channels.id, type: schema.channels.type })
    .from(schema.channels)
    .where(eq(schema.channels.id, channelId))
    .get();

  if (!channel) {
    return { ok: false, status: 404, code: 'channel_not_found' };
  }
  if (channel.type !== 'voice') {
    return {
      ok: false,
      status: 400,
      code: 'validation_failed',
      message: 'Music playback is only available in voice channels',
    };
  }

  if (!hasPermission(userId, spaceId, PermissionBits.CONNECT, channelId)) {
    return { ok: false, status: 403, code: 'voice_connect_forbidden' };
  }

  const perms = computePermissions(userId, spaceId, channelId);
  const canSpeak =
    (perms & PermissionBits.SPEAK) !== 0n ||
    (perms & PermissionBits.ADMINISTRATOR) !== 0n;
  if (!canSpeak) {
    return {
      ok: false,
      status: 403,
      code: 'missing_permission',
      details: { permission: 'SPEAK' },
    };
  }

  return { ok: true, data: { spaceId } };
}

/**
 * Mint a LiveKit room JWT for the music bot participant.
 * - Room name matches Backspace space voice channels (`channelId`).
 * - Identity follows `<userId>:<displayName>` so `useLiveKit` parses it cleanly.
 * - Publishes Microphone source only (`canSubscribe: false`) to minimize Pi CPU/bandwidth.
 */
export async function generateMusicBotToken(channelId: string): Promise<string> {
  if (!config.livekit.apiKey || !config.livekit.apiSecret) {
    throw new Error('LiveKit credentials are not configured');
  }

  const safeDisplayName = config.musicBot.displayName.replace(/:/g, ' ').trim() || '음악 봇';
  const token = new AccessToken(config.livekit.apiKey, config.livekit.apiSecret, {
    identity: `${MUSIC_BOT_USER_ID}:${safeDisplayName}`,
    name: safeDisplayName,
    metadata: JSON.stringify({ isBot: true, kind: 'music-bot' }),
    ttl: '2h',
  });

  token.addGrant({
    room: channelId,
    roomJoin: true,
    canPublish: true,
    canPublishSources: [TrackSource.MICROPHONE],
    canSubscribe: false,
    canPublishData: false,
  });

  return token.toJwt();
}

async function callMusicBotApi<T>(
  path: string,
  init: { method: 'GET' | 'POST'; body?: Record<string, unknown> },
): Promise<MusicServiceResult<T>> {
  if (!config.musicBot.url || !config.musicBot.secret) {
    return {
      ok: false,
      status: 503,
      code: 'voice_disabled',
      message: 'Music bot service is not configured',
    };
  }

  let response: Response;
  try {
    response = await fetch(`${config.musicBot.url}${path}`, {
      method: init.method,
      headers: {
        Authorization: `Bearer ${config.musicBot.secret}`,
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
      },
      body: init.body ? JSON.stringify(init.body) : undefined,
      signal: AbortSignal.timeout(config.musicBot.requestTimeoutMs),
    });
  } catch (err) {
    const detail = err instanceof Error ? err.message : 'Music bot request failed';
    return {
      ok: false,
      status: 502,
      code: 'internal_error',
      message: `Music bot unreachable: ${detail}`,
    };
  }

  let payload: unknown = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const remoteMessage =
      typeof payload === 'object' && payload !== null && typeof (payload as { error?: unknown }).error === 'string'
        ? (payload as { error: string }).error
        : `Music bot error (HTTP ${response.status})`;
    const status =
      response.status === 409
        ? 409
        : response.status === 400 || response.status === 422
          ? 400
          : 502;
    const code: ErrorCode = status === 502 ? 'internal_error' : 'validation_failed';
    return { ok: false, status, code, message: remoteMessage };
  }

  return { ok: true, data: payload as T };
}

export async function requestMusicPlay(params: {
  userId: string;
  username: string;
  channelId: string;
  rawUrl: string;
}): Promise<MusicServiceResult<MusicPlaybackStatus>> {
  if (!config.livekit.apiKey || !config.livekit.apiSecret) {
    return { ok: false, status: 503, code: 'voice_disabled' };
  }

  const livekitUrl = config.musicBot.livekitUrl || config.livekit.url;
  if (!livekitUrl) {
    return { ok: false, status: 503, code: 'voice_disabled' };
  }

  const permCheck = checkVoiceMusicPermission(params.userId, params.channelId);
  if (!permCheck.ok) return permCheck;

  const validated = validateYouTubeUrl(params.rawUrl);
  if (!validated) {
    return {
      ok: false,
      status: 400,
      code: 'validation_failed',
      message: 'Only valid https:// YouTube video URLs are supported',
    };
  }

  const token = await generateMusicBotToken(params.channelId);
  return callMusicBotApi<MusicPlaybackStatus>('/play', {
    method: 'POST',
    body: {
      channelId: params.channelId,
      livekitUrl,
      token,
      url: validated.canonicalUrl,
      requestedBy: params.username,
    },
  });
}

export async function requestMusicStop(params: {
  userId: string;
  channelId: string;
}): Promise<MusicServiceResult<MusicStopResponse>> {
  if (!config.livekit.apiKey || !config.livekit.apiSecret) {
    return { ok: false, status: 503, code: 'voice_disabled' };
  }

  const permCheck = checkVoiceMusicPermission(params.userId, params.channelId);
  if (!permCheck.ok) return permCheck;

  const res = await callMusicBotApi<MusicStopResponse>('/stop', {
    method: 'POST',
    body: { channelId: params.channelId },
  });
  if (!res.ok) return res;
  return {
    ok: true,
    data: {
      stopped: Boolean(res.data.stopped),
      status: scopePlaybackStatus(params.channelId, res.data.status),
    },
  };
}

export async function requestMusicStatus(params: {
  userId: string;
  channelId: string;
}): Promise<MusicServiceResult<MusicPlaybackStatus>> {
  const permCheck = checkVoiceMusicPermission(params.userId, params.channelId);
  if (!permCheck.ok) return permCheck;

  const query = new URLSearchParams({ channelId: params.channelId }).toString();
  const res = await callMusicBotApi<MusicPlaybackStatus>(`/status?${query}`, { method: 'GET' });
  if (!res.ok) return res;
  return {
    ok: true,
    data: scopePlaybackStatus(params.channelId, res.data),
  };
}
