/** Builds the `history` field of a chat request from the stored conversation. */
import type { ChatMessage, HistoryItem } from '../types';

/** The API sends at most this many history items (docs/API.md: the last <= 10 turns). */
export const HISTORY_LIMIT = 10;
/** Each history text is clipped to stay well below the server's per-item limit. */
export const HISTORY_TEXT_LIMIT = 4000;

function clip(text: string): string {
  return text.length > HISTORY_TEXT_LIMIT ? text.slice(0, HISTORY_TEXT_LIMIT) : text;
}

/**
 * The last `limit` messages as `{ role, content, sql }`. Assistant items carry the SQLite form of the
 * query, which the server uses to rehydrate `last_sql` for follow-ups. Failed or stopped requests are
 * skipped because they carry no answer.
 */
export function buildHistory(messages: readonly ChatMessage[], limit: number = HISTORY_LIMIT): HistoryItem[] {
  const items: HistoryItem[] = [];
  for (const message of messages) {
    if (message.role === 'user') {
      items.push({ role: 'user', content: clip(message.content), sql: null });
    } else if (message.state === 'done' && message.response) {
      const { response } = message;
      items.push({
        role: 'assistant',
        content: clip(response.message || response.explanation || ''),
        sql: response.sql_sqlite ?? response.sql ?? null,
      });
    }
  }
  return items.slice(-limit);
}
