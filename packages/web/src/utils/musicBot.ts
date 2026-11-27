export const MUSIC_BOT_USER_ID = 'music-bot';
export const DEFAULT_MUSIC_BOT_NAME = '음악 봇';

export function isMusicBotUserId(userId: string | null | undefined): boolean {
  return userId === MUSIC_BOT_USER_ID;
}
