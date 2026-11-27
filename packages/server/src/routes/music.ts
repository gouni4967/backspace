import type { FastifyInstance, FastifyReply, FastifyRequest } from 'fastify';
import { authenticate } from '../utils/auth.js';
import { sendError } from '../utils/httpErrors.js';
import {
  requestMusicPlay,
  requestMusicStatus,
  requestMusicStop,
  type MusicServiceError,
} from '../utils/musicBot.js';

function replyMusicError(reply: FastifyReply, err: MusicServiceError) {
  if (err.message) {
    return reply.code(err.status).send({
      error: err.message,
      code: err.code,
      ...(err.details ? { details: err.details } : {}),
      statusCode: err.status,
    });
  }
  return sendError(reply, err.status, err.code, err.details);
}

export async function musicRoutes(app: FastifyInstance): Promise<void> {
  // POST /api/channels/:id/music/play - Start YouTube playback in a voice channel
  app.post<{ Params: { id: string }; Body: { url?: string } }>(
    '/api/channels/:id/music/play',
    {
      preHandler: authenticate,
      config: {
        rateLimit: {
          max: 5,
          timeWindow: '15 seconds',
          keyGenerator: (request: FastifyRequest) => request.ip,
        },
      },
    },
    async (request, reply) => {
      const { id } = request.params;
      const url = typeof request.body?.url === 'string' ? request.body.url : '';
      if (!url.trim()) {
        return sendError(reply, 400, 'validation_failed');
      }

      const result = await requestMusicPlay({
        userId: request.userId,
        username: request.username,
        channelId: id,
        rawUrl: url,
      });
      if (!result.ok) {
        return replyMusicError(reply, result);
      }
      return reply.code(200).send(result.data);
    },
  );

  // POST /api/channels/:id/music/stop - Stop active playback in a voice channel
  app.post<{ Params: { id: string } }>(
    '/api/channels/:id/music/stop',
    {
      preHandler: authenticate,
      config: {
        rateLimit: {
          max: 10,
          timeWindow: '15 seconds',
          keyGenerator: (request: FastifyRequest) => request.ip,
        },
      },
    },
    async (request, reply) => {
      const { id } = request.params;
      const result = await requestMusicStop({
        userId: request.userId,
        channelId: id,
      });
      if (!result.ok) {
        return replyMusicError(reply, result);
      }
      return reply.code(200).send(result.data);
    },
  );

  // GET /api/channels/:id/music/status - Query current music bot playback state
  app.get<{ Params: { id: string } }>(
    '/api/channels/:id/music/status',
    {
      preHandler: authenticate,
    },
    async (request, reply) => {
      const { id } = request.params;
      const result = await requestMusicStatus({
        userId: request.userId,
        channelId: id,
      });
      if (!result.ok) {
        return replyMusicError(reply, result);
      }
      return reply.code(200).send(result.data);
    },
  );
}
