import { describe, expect, it } from 'vitest';
import { assistantMessage, makeResponse, userMessage } from '../test/fixtures';
import { buildHistory, HISTORY_LIMIT } from './history';

describe('buildHistory', () => {
  it('maps messages to { role, content, sql } using the SQLite query', () => {
    const response = makeResponse({ dialect: 'postgres', sql: 'SELECT "pg"', sql_sqlite: 'SELECT 1' });
    const history = buildHistory([userMessage('Show all customers'), assistantMessage({ response })]);
    expect(history).toEqual([
      { role: 'user', content: 'Show all customers', sql: null },
      { role: 'assistant', content: response.message, sql: 'SELECT 1' },
    ]);
  });

  it('skips failed and stopped requests', () => {
    const history = buildHistory([
      userMessage('first'),
      assistantMessage({ state: 'error', response: null, error: { message: 'boom', code: 'internal', retry_after: null } }),
      userMessage('second'),
      assistantMessage({ state: 'stopped', response: null }),
    ]);
    expect(history.map((item) => item.role)).toEqual(['user', 'user']);
  });

  it('keeps only the most recent items', () => {
    const messages = Array.from({ length: 15 }, (_, index) => userMessage(`message ${index}`));
    const history = buildHistory(messages);
    expect(history).toHaveLength(HISTORY_LIMIT);
    expect(history[0].content).toBe('message 5');
  });
});
