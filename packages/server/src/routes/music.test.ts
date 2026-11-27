import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import Fastify, { type FastifyInstance } from 'fastify';
import Database from 'better-sqlite3';
import { drizzle } from 'drizzle-orm/better-sqlite3';
import { eq } from 'drizzle-orm';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import * as schema from '../db/schema.js';
import { setWorkerId } from '../utils/snowflake.js';
import { signJwt } from '../utils/auth.js';
import { PermissionBits } from '../utils/permissions.js';
import { parseMusicCommand, validateYouTubeUrl } from '../utils/musicBot.js';

setWorkerId(7);
const __dirname = path.dirname(fileURLToPath(import.meta.url));

type TestDb = ReturnType<typeof drizzle<typeof schema>>;
let sqlite: Database.Database;
let testDb: TestDb;
let app: FastifyInstance;

vi.mock('../db/index.js', () => ({
  getDb: () => testDb,
  getRawDb: () => sqlite,
  schema,
}));

vi.mock('../utils/embedResolver.js', () => ({
  fetchEmbedsForMessages: () => new Map(),
  resolveEmbeds: async () => {},
  reResolveEmbeds: async () => {},
  embedRowToEmbed: (e: unknown) => e,
}));

const mockConfig = vi.hoisted(() => ({
  livekit: {
    url: 'wss://lk.test/livekit',
    apiKey: 'devkey-test-123',
    apiSecret: 'devsecret-test-012345678901234567890123456789',
  },
  musicBot: {
    url: 'http://music-bot.test:3100' as string | undefined,
    secret: 'music-bot-secret-0123456789' as string | undefined,
    livekitUrl: 'ws://10.253.245.2:7880' as string | undefined,
    displayName: '음악 봇',
    requestTimeoutMs: 5_000,
  },
}));

vi.mock('../config.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../config.js')>();
  return {
    config: {
      ...actual.config,
      livekit: mockConfig.livekit,
      musicBot: mockConfig.musicBot,
    },
  };
});

function applyMigrations(db: Database.Database): void {
  const migrationsDir = path.resolve(__dirname, '../../drizzle');
  const files = fs.readdirSync(migrationsDir).filter((f) => f.endsWith('.sql')).sort();
  for (const f of files) {
    const sqlText = fs.readFileSync(path.join(migrationsDir, f), 'utf8');
    const statements = sqlText.split(/-->\s*statement-breakpoint/);
    for (const stmt of statements) {
      const clean = stmt.trim();
      if (clean) db.exec(clean);
    }
  }
}

function decodeJwtPayload(jwt: string): Record<string, unknown> {
  const [, payload] = jwt.split('.');
  return JSON.parse(Buffer.from(payload ?? '', 'base64url').toString('utf8')) as Record<string, unknown>;
}

/**
 * Stateful in-memory simulator of the standalone music-bot HTTP API
 * enforcing the single-room 409 lock and channel-scoped /status & /stop.
 */
function createStatefulMusicBotFetch() {
  let active: {
    channelId: string;
    videoId: string;
    url: string;
    title: string;
    durationSeconds: number;
    requestedBy: string;
    startedAt: number;
  } | null = null;
  const lastErrors = new Map<string, string>();

  const statusFor = (channelId: string) => {
    if (active && active.channelId === channelId) {
      return {
        state: 'playing' as const,
        channelId,
        videoId: active.videoId,
        url: active.url,
        title: active.title,
        durationSeconds: active.durationSeconds,
        requestedBy: active.requestedBy,
        startedAt: active.startedAt,
        lastError: null,
      };
    }
    return {
      state: 'idle' as const,
      channelId,
      videoId: null,
      url: null,
      title: null,
      durationSeconds: null,
      requestedBy: null,
      startedAt: null,
      lastError: lastErrors.get(channelId) ?? null,
    };
  };

  const fetchMock = vi.fn(async (input: string | URL, init?: RequestInit) => {
    const urlObj = new URL(String(input));
    const method = (init?.method ?? 'GET').toUpperCase();

    if (urlObj.pathname === '/play' && method === 'POST') {
      const body = JSON.parse(String(init?.body ?? '{}')) as {
        channelId: string;
        url: string;
        requestedBy?: string;
      };
      if (active && active.channelId !== body.channelId) {
        return new Response(
          JSON.stringify({
            error: 'Music bot is already playing in another voice channel',
            code: 'playback_busy',
          }),
          { status: 409, headers: { 'content-type': 'application/json' } },
        );
      }
      const videoId = new URL(body.url).searchParams.get('v') ?? 'unknown0000';
      active = {
        channelId: body.channelId,
        videoId,
        url: body.url,
        title: `Secret Track (${body.channelId})`,
        durationSeconds: 321,
        requestedBy: body.requestedBy ?? 'unknown',
        startedAt: Date.now(),
      };
      return new Response(JSON.stringify(statusFor(body.channelId)), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }

    if (urlObj.pathname === '/status' && method === 'GET') {
      const channelId = urlObj.searchParams.get('channelId');
      if (!channelId) {
        return new Response(JSON.stringify({ error: 'channelId query parameter is required' }), {
          status: 400,
          headers: { 'content-type': 'application/json' },
        });
      }
      return new Response(JSON.stringify(statusFor(channelId)), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      });
    }

    if (urlObj.pathname === '/stop' && method === 'POST') {
      const body = JSON.parse(String(init?.body ?? '{}')) as { channelId?: string };
      if (!body.channelId) {
        return new Response(JSON.stringify({ error: 'channelId is required' }), {
          status: 400,
          headers: { 'content-type': 'application/json' },
        });
      }
      if (!active || active.channelId !== body.channelId) {
        return new Response(
          JSON.stringify({ stopped: false, status: statusFor(body.channelId) }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        );
      }
      active = null;
      return new Response(
        JSON.stringify({ stopped: true, status: statusFor(body.channelId) }),
        { status: 200, headers: { 'content-type': 'application/json' } },
      );
    }

    return new Response(JSON.stringify({ error: 'Not found' }), { status: 404 });
  });

  return {
    fetchMock,
    getActiveChannelId: () => active?.channelId ?? null,
    setChannelError: (channelId: string, err: string) => lastErrors.set(channelId, err),
  };
}

const OWNER_ID = '100001';
const SPEAKER_ID = '100002';
const LISTENER_ID = '100003';
const OUTSIDER_ID = '100004';
const SPACE_ID = '200001';
const VOICE_CH_A = '300001';
const VOICE_CH_B = '300003';
const TEXT_CH_ID = '300002';

beforeEach(async () => {
  mockConfig.musicBot.url = 'http://music-bot.test:3100';
  mockConfig.musicBot.secret = 'music-bot-secret-0123456789';
  mockConfig.musicBot.livekitUrl = 'ws://10.253.245.2:7880';

  sqlite = new Database(':memory:');
  sqlite.pragma('foreign_keys = ON');
  applyMigrations(sqlite);
  testDb = drizzle(sqlite, { schema });

  const now = Date.now();
  testDb.insert(schema.users).values([
    { id: OWNER_ID, username: 'owner', passwordHash: 'x', createdAt: now },
    { id: SPEAKER_ID, username: 'speaker', passwordHash: 'x', createdAt: now },
    { id: LISTENER_ID, username: 'listener', passwordHash: 'x', createdAt: now },
    { id: OUTSIDER_ID, username: 'outsider', passwordHash: 'x', createdAt: now },
  ]).run();

  testDb.insert(schema.spaces).values({
    id: SPACE_ID,
    name: 'Test Space',
    ownerId: OWNER_ID,
    createdAt: now,
  }).run();

  // @everyone role: VIEW_CHANNEL | SEND_MESSAGES | CONNECT (no SPEAK by default)
  const everyonePerms = (
    PermissionBits.VIEW_CHANNEL |
    PermissionBits.SEND_MESSAGES |
    PermissionBits.CONNECT
  ).toString();
  testDb.insert(schema.roles).values([
    { id: SPACE_ID, spaceId: SPACE_ID, name: '@everyone', position: 0, permissions: everyonePerms, createdAt: now },
    { id: '400002', spaceId: SPACE_ID, name: 'DJ', position: 1, permissions: PermissionBits.SPEAK.toString(), createdAt: now },
  ]).run();

  testDb.insert(schema.spaceMembers).values([
    { spaceId: SPACE_ID, userId: OWNER_ID, joinedAt: now },
    { spaceId: SPACE_ID, userId: SPEAKER_ID, joinedAt: now },
    { spaceId: SPACE_ID, userId: LISTENER_ID, joinedAt: now },
  ]).run();

  testDb.insert(schema.memberRoles).values({
    spaceId: SPACE_ID,
    userId: SPEAKER_ID,
    roleId: '400002',
  }).run();

  testDb.insert(schema.channels).values([
    { id: VOICE_CH_A, spaceId: SPACE_ID, name: 'music-room-a', type: 'voice', position: 0, createdAt: now },
    { id: VOICE_CH_B, spaceId: SPACE_ID, name: 'music-room-b', type: 'voice', position: 1, createdAt: now },
    { id: TEXT_CH_ID, spaceId: SPACE_ID, name: 'general', type: 'text', position: 2, createdAt: now },
  ]).run();

  const { musicRoutes } = await import('./music.js');
  const { messageRoutes } = await import('./messages.js');
  app = Fastify();
  await app.register(musicRoutes);
  await app.register(messageRoutes);
});

afterEach(async () => {
  vi.unstubAllGlobals();
  await app?.close();
  sqlite?.close();
});

describe('validateYouTubeUrl & parseMusicCommand', () => {
  it('accepts valid https YouTube video URLs and canonicalizes them', () => {
    expect(validateYouTubeUrl('https://youtu.be/aqz-KE-bpKQ?si=test')).toEqual({
      videoId: 'aqz-KE-bpKQ',
      canonicalUrl: 'https://www.youtube.com/watch?v=aqz-KE-bpKQ',
    });
    expect(validateYouTubeUrl('https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PL1')).toEqual({
      videoId: 'jNQXAC9IVRw',
      canonicalUrl: 'https://www.youtube.com/watch?v=jNQXAC9IVRw',
    });
  });

  it('rejects non-YouTube URLs, http, flags, and shell characters', () => {
    expect(validateYouTubeUrl('http://www.youtube.com/watch?v=aqz-KE-bpKQ')).toBeNull();
    expect(validateYouTubeUrl('https://example.com/watch?v=aqz-KE-bpKQ')).toBeNull();
    expect(validateYouTubeUrl('https://www.youtube.com/watch?v=aqz-KE-bpKQ;id')).toBeNull();
    expect(validateYouTubeUrl('--exec id')).toBeNull();
  });

  it('parses /play and /stop while ignoring normal chat text', () => {
    expect(parseMusicCommand('/play https://youtu.be/aqz-KE-bpKQ')).toEqual({
      command: 'play',
      url: 'https://youtu.be/aqz-KE-bpKQ',
    });
    expect(parseMusicCommand('  /STOP  ')).toEqual({ command: 'stop' });
    expect(parseMusicCommand('/play')).toEqual({ command: 'invalid_play' });
    expect(parseMusicCommand('hello world')).toBeNull();
  });
});

describe('music bot routes & voice channel commands', () => {
  it('enforces CONNECT + SPEAK permissions and voice channel type', async () => {
    const listenerToken = signJwt({ userId: LISTENER_ID, username: 'listener' });
    const outsiderToken = signJwt({ userId: OUTSIDER_ID, username: 'outsider' });
    const speakerToken = signJwt({ userId: SPEAKER_ID, username: 'speaker' });

    const noSpeak = await app.inject({
      method: 'POST',
      url: `/api/channels/${VOICE_CH_A}/music/play`,
      headers: { authorization: `Bearer ${listenerToken}` },
      payload: { url: 'https://youtu.be/aqz-KE-bpKQ' },
    });
    expect(noSpeak.statusCode).toBe(403);
    expect(noSpeak.json().code).toBe('missing_permission');

    const noConnect = await app.inject({
      method: 'POST',
      url: `/api/channels/${VOICE_CH_A}/music/play`,
      headers: { authorization: `Bearer ${outsiderToken}` },
      payload: { url: 'https://youtu.be/aqz-KE-bpKQ' },
    });
    expect(noConnect.statusCode).toBe(403);
    expect(noConnect.json().code).toBe('voice_connect_forbidden');

    const textChannel = await app.inject({
      method: 'POST',
      url: `/api/channels/${TEXT_CH_ID}/music/play`,
      headers: { authorization: `Bearer ${speakerToken}` },
      payload: { url: 'https://youtu.be/aqz-KE-bpKQ' },
    });
    expect(textChannel.statusCode).toBe(400);
  });

  it('mints a Microphone-only LiveKit token for the target room and calls the bot service', async () => {
    const bot = createStatefulMusicBotFetch();
    vi.stubGlobal('fetch', bot.fetchMock);

    const speakerToken = signJwt({ userId: SPEAKER_ID, username: 'speaker' });
    const res = await app.inject({
      method: 'POST',
      url: `/api/channels/${VOICE_CH_A}/music/play`,
      headers: { authorization: `Bearer ${speakerToken}` },
      payload: { url: 'https://youtu.be/aqz-KE-bpKQ?si=abc' },
    });

    expect(res.statusCode).toBe(200);
    expect(res.json().channelId).toBe(VOICE_CH_A);
    expect(bot.fetchMock).toHaveBeenCalledTimes(1);

    const [calledUrl, calledInit] = bot.fetchMock.mock.calls[0] as [string, RequestInit];
    expect(calledUrl).toBe('http://music-bot.test:3100/play');
    expect((calledInit.headers as Record<string, string>).Authorization).toBe('Bearer music-bot-secret-0123456789');

    const sentBody = JSON.parse(String(calledInit.body)) as Record<string, string>;
    expect(sentBody.channelId).toBe(VOICE_CH_A);
    expect(sentBody.livekitUrl).toBe('ws://10.253.245.2:7880');
    expect(sentBody.url).toBe('https://www.youtube.com/watch?v=aqz-KE-bpKQ');
    expect(sentBody.requestedBy).toBe('speaker');

    expect(sentBody.token).toBeTypeOf('string');
    const jwtPayload = decodeJwtPayload(sentBody.token ?? '');
    expect(jwtPayload.sub).toBe('music-bot:음악 봇');
    expect(jwtPayload.video).toMatchObject({
      room: VOICE_CH_A,
      roomJoin: true,
      canPublish: true,
      canSubscribe: false,
      canPublishData: false,
      canPublishSources: ['microphone'],
    });
  });

  it('isolates Room B /play (409), /status, and /stop while Room A stays playing (HTTP API)', async () => {
    const bot = createStatefulMusicBotFetch();
    bot.setChannelError(VOICE_CH_A, 'room-a-internal-error-detail');
    vi.stubGlobal('fetch', bot.fetchMock);

    const ownerToken = signJwt({ userId: OWNER_ID, username: 'owner' });
    const speakerToken = signJwt({ userId: SPEAKER_ID, username: 'speaker' });

    // 1. Start playback in Room A
    const playA = await app.inject({
      method: 'POST',
      url: `/api/channels/${VOICE_CH_A}/music/play`,
      headers: { authorization: `Bearer ${ownerToken}` },
      payload: { url: 'https://youtu.be/aqz-KE-bpKQ' },
    });
    expect(playA.statusCode).toBe(200);
    expect(bot.getActiveChannelId()).toBe(VOICE_CH_A);

    // 2. Room B /play -> 409 Conflict, Room A keeps playing
    const playB = await app.inject({
      method: 'POST',
      url: `/api/channels/${VOICE_CH_B}/music/play`,
      headers: { authorization: `Bearer ${speakerToken}` },
      payload: { url: 'https://youtu.be/jNQXAC9IVRw' },
    });
    expect(playB.statusCode).toBe(409);
    expect(bot.getActiveChannelId()).toBe(VOICE_CH_A);

    // 3. Room B /status -> 200 idle scoped to Room B
    const statusB = await app.inject({
      method: 'GET',
      url: `/api/channels/${VOICE_CH_B}/music/status`,
      headers: { authorization: `Bearer ${speakerToken}` },
    });
    expect(statusB.statusCode).toBe(200);
    expect(statusB.json()).toEqual({
      state: 'idle',
      channelId: VOICE_CH_B,
      videoId: null,
      url: null,
      title: null,
      durationSeconds: null,
      requestedBy: null,
      startedAt: null,
      lastError: null,
    });

    // 4. Room B /stop -> 200 stopped: false, Room A still playing
    const stopB = await app.inject({
      method: 'POST',
      url: `/api/channels/${VOICE_CH_B}/music/stop`,
      headers: { authorization: `Bearer ${speakerToken}` },
    });
    expect(stopB.statusCode).toBe(200);
    expect(stopB.json()).toEqual({
      stopped: false,
      status: {
        state: 'idle',
        channelId: VOICE_CH_B,
        videoId: null,
        url: null,
        title: null,
        durationSeconds: null,
        requestedBy: null,
        startedAt: null,
        lastError: null,
      },
    });

    // Confirm zero Room A details leaked into any Room B response
    for (const resp of [playB.body, statusB.body, stopB.body]) {
      expect(resp).not.toContain(VOICE_CH_A);
      expect(resp).not.toContain('aqz-KE-bpKQ');
      expect(resp).not.toContain('Secret Track');
      expect(resp).not.toContain('owner');
      expect(resp).not.toContain('room-a-internal-error-detail');
    }

    // 5. Room A is still actively playing
    const statusA = await app.inject({
      method: 'GET',
      url: `/api/channels/${VOICE_CH_A}/music/status`,
      headers: { authorization: `Bearer ${ownerToken}` },
    });
    expect(statusA.statusCode).toBe(200);
    expect(statusA.json().state).toBe('playing');
    expect(statusA.json().channelId).toBe(VOICE_CH_A);
    expect(bot.getActiveChannelId()).toBe(VOICE_CH_A);
  });

  it('enforces the same 409 busy and room-scoped /stop isolation on chat commands', async () => {
    const bot = createStatefulMusicBotFetch();
    vi.stubGlobal('fetch', bot.fetchMock);

    const ownerToken = signJwt({ userId: OWNER_ID, username: 'owner' });
    const speakerToken = signJwt({ userId: SPEAKER_ID, username: 'speaker' });

    // Start in Room A via chat /play
    const chatPlayA = await app.inject({
      method: 'POST',
      url: `/api/channels/${VOICE_CH_A}/messages`,
      headers: { authorization: `Bearer ${ownerToken}` },
      payload: { content: '/play https://youtu.be/aqz-KE-bpKQ' },
    });
    expect(chatPlayA.statusCode).toBe(201);
    expect(bot.getActiveChannelId()).toBe(VOICE_CH_A);

    // Room B chat /play -> 409 Conflict, no Room A info leaked, message not persisted
    const chatPlayB = await app.inject({
      method: 'POST',
      url: `/api/channels/${VOICE_CH_B}/messages`,
      headers: { authorization: `Bearer ${speakerToken}` },
      payload: { content: '/play https://youtu.be/jNQXAC9IVRw' },
    });
    expect(chatPlayB.statusCode).toBe(409);
    expect(chatPlayB.body).not.toContain(VOICE_CH_A);
    expect(chatPlayB.body).not.toContain('aqz-KE-bpKQ');
    expect(chatPlayB.body).not.toContain('owner');
    expect(bot.getActiveChannelId()).toBe(VOICE_CH_A);

    const roomBMessagesAfterPlay = testDb
      .select()
      .from(schema.messages)
      .where(eq(schema.messages.channelId, VOICE_CH_B))
      .all();
    expect(roomBMessagesAfterPlay).toHaveLength(0);

    // Room B chat /stop -> does not stop Room A
    const chatStopB = await app.inject({
      method: 'POST',
      url: `/api/channels/${VOICE_CH_B}/messages`,
      headers: { authorization: `Bearer ${speakerToken}` },
      payload: { content: '/stop' },
    });
    expect(chatStopB.statusCode).toBe(201);
    expect(bot.getActiveChannelId()).toBe(VOICE_CH_A);

    // Room A chat /stop -> stops Room A cleanly
    const chatStopA = await app.inject({
      method: 'POST',
      url: `/api/channels/${VOICE_CH_A}/messages`,
      headers: { authorization: `Bearer ${ownerToken}` },
      payload: { content: '/stop' },
    });
    expect(chatStopA.statusCode).toBe(201);
    expect(bot.getActiveChannelId()).toBeNull();
  });
});
