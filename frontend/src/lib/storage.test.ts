import { describe, expect, it } from 'vitest';
import { assistantMessage, conversation, makeResponse, userMessage } from '../test/fixtures';
import {
  CONVERSATIONS_KEY,
  DEFAULT_SETTINGS,
  MAX_CONVERSATIONS,
  MAX_MESSAGES_PER_CONVERSATION,
  SETTINGS_KEY,
  capConversations,
  loadActiveConversationId,
  loadConversations,
  loadSettings,
  saveActiveConversationId,
  saveConversations,
  settleInterruptedRequests,
  type StorageLike,
} from './storage';
import type { Conversation } from '../types';

/** In-memory storage that throws QuotaExceededError above `quota` characters per value. */
class MemoryStorage implements StorageLike {
  readonly data = new Map<string, string>();
  private readonly quota: number;
  constructor(quota = Number.POSITIVE_INFINITY) {
    this.quota = quota;
  }
  getItem(key: string): string | null {
    return this.data.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    if (value.length > this.quota) throw new DOMException('The quota has been exceeded.', 'QuotaExceededError');
    this.data.set(key, value);
  }
  removeItem(key: string): void {
    this.data.delete(key);
  }
}

describe('loadConversations', () => {
  it('returns an empty list when nothing is stored', () => {
    expect(loadConversations(new MemoryStorage())).toEqual([]);
  });

  it('survives corrupt JSON', () => {
    const storage = new MemoryStorage();
    storage.setItem(CONVERSATIONS_KEY, '{"oops": [1, 2');
    expect(loadConversations(storage)).toEqual([]);
  });

  it('ignores JSON of the wrong shape', () => {
    const storage = new MemoryStorage();
    for (const value of ['{"a": 1}', '"text"', '42', 'null', '[null, 1, "x", {}]']) {
      storage.setItem(CONVERSATIONS_KEY, value);
      expect(loadConversations(storage)).toEqual([]);
    }
  });

  it('survives storage that throws on access', () => {
    const broken: StorageLike = {
      getItem: () => {
        throw new DOMException('denied', 'SecurityError');
      },
      setItem: () => undefined,
      removeItem: () => undefined,
    };
    expect(loadConversations(broken)).toEqual([]);
    expect(loadConversations(null)).toEqual([]);
  });

  it('keeps valid conversations and messages while dropping invalid ones', () => {
    const valid = conversation('good', 5_000, [userMessage('Show all customers'), assistantMessage()]);
    const storage = new MemoryStorage();
    storage.setItem(
      CONVERSATIONS_KEY,
      JSON.stringify([
        valid,
        { id: 'no-messages', messages: [] },
        { title: 'missing id', messages: [userMessage('x')] },
        {
          id: 'partial',
          updated_at: 4_000,
          messages: [
            userMessage('kept'),
            { id: 'bad', role: 'user', content: 42, created_at: 1 },
            { id: 'done-without-response', role: 'assistant', created_at: 2, state: 'done', response: null },
            { id: 'error-without-detail', role: 'assistant', created_at: 3, state: 'error' },
            { id: 'robot', role: 'robot', content: 'beep', created_at: 4 },
          ],
        },
        valid, // duplicate id
      ]),
    );

    const loaded = loadConversations(storage);
    expect(loaded.map((item) => item.id)).toEqual(['good', 'partial']);
    expect(loaded[0]).toEqual(valid);
    const partial = loaded[1];
    expect(partial.thread_id).toBe('partial');
    expect(partial.title).toBe('kept');
    expect(partial.messages.map((message) => message.id)).toHaveLength(2);
    const errorMessage = partial.messages[1];
    expect(errorMessage.role === 'assistant' && errorMessage.error?.code).toBe('unknown');
  });

  it('repairs or drops corrupt nested response data instead of passing it to the UI', () => {
    const corrupt = {
      ...makeResponse(),
      model: { name: 'not a string' },
      explanation: 42,
      clarification_question: ['?'],
      validation: { valid: true },
      optimization: 'none',
      execution: { columns: ['a'], rows: 'oops' },
      cost: {},
      steps: [{ node: 'detect_intent', label: 'Detecting intent', status: 'completed', duration_ms: 'fast', detail: { x: 1 } }],
    };
    const unknownStatus = { ...makeResponse(), status: 'exploded' };
    const storage = new MemoryStorage();
    storage.setItem(
      CONVERSATIONS_KEY,
      JSON.stringify([
        conversation('c', 1, [
          userMessage('q1'),
          assistantMessage({ response: corrupt as never }),
          userMessage('q2'),
          assistantMessage({ response: unknownStatus as never }),
        ]),
      ]),
    );

    const [loaded] = loadConversations(storage);
    const message = loaded.messages[1];
    expect(message.role).toBe('assistant');
    const response = message.role === 'assistant' ? message.response : null;
    expect(response).toMatchObject({
      status: 'success',
      model: null,
      explanation: null,
      clarification_question: null,
      validation: { valid: true, checks: [], errors: [], warnings: [], tables: [], columns: [], destructive: false },
      optimization: null,
      execution: null,
      cost: null,
    });
    expect(response?.steps).toEqual([
      { node: 'detect_intent', label: 'Detecting intent', status: 'completed', duration_ms: null, detail: null },
    ]);
    // A response with an unknown status cannot be rendered: the message is dropped.
    expect(loaded.messages.map((item) => item.role)).toEqual(['user', 'assistant', 'user']);
  });

  it('fills in list fields missing from older stored responses', () => {
    const { issues: _issues, steps: _steps, ...legacy } = makeResponse();
    const storage = new MemoryStorage();
    storage.setItem(
      CONVERSATIONS_KEY,
      JSON.stringify([conversation('legacy', 1, [userMessage('q'), assistantMessage({ response: legacy as never })])]),
    );
    const [loaded] = loadConversations(storage);
    const message = loaded.messages[1];
    expect(message.role === 'assistant' && message.response?.issues).toEqual([]);
    expect(message.role === 'assistant' && message.response?.steps).toEqual([]);
  });
});

describe('settleInterruptedRequests', () => {
  it('answers a question left without a reply (page closed mid-request) with a retryable error', () => {
    const answered = conversation('answered', 2, [userMessage('Show all customers'), assistantMessage()]);
    const question = userMessage('Only those from California', 3_000);
    const orphan = conversation('orphan', 3, [userMessage('Show all customers'), assistantMessage(), question]);

    const [first, second] = settleInterruptedRequests([answered, orphan]);
    expect(first).toBe(answered);
    expect(second.messages).toHaveLength(4);
    expect(second.messages.slice(0, 3)).toEqual(orphan.messages);
    expect(second.messages[3]).toMatchObject({
      role: 'assistant',
      prompt: 'Only those from California',
      state: 'error',
      response: null,
      error: { code: 'stream_interrupted', retry_after: null },
      created_at: 3_000,
    });
  });
});

describe('capConversations', () => {
  it('keeps the 30 most recently updated conversations, newest first', () => {
    const many = Array.from({ length: MAX_CONVERSATIONS + 5 }, (_, index) => conversation(`c${index}`, index));
    const capped = capConversations(many);
    expect(capped).toHaveLength(MAX_CONVERSATIONS);
    expect(capped[0].id).toBe(`c${MAX_CONVERSATIONS + 4}`);
    expect(capped.some((item) => item.id === 'c0')).toBe(false);
  });

  it('keeps the newest 100 messages of a long conversation', () => {
    const messages = Array.from({ length: MAX_MESSAGES_PER_CONVERSATION + 50 }, (_, index) =>
      userMessage(`message ${index}`, index),
    );
    const [capped] = capConversations([conversation('long', 1, messages)]);
    expect(capped.messages).toHaveLength(MAX_MESSAGES_PER_CONVERSATION);
    expect(capped.messages[0]).toEqual(messages[50]);
  });
});

describe('saveConversations', () => {
  it('round-trips through storage', () => {
    const storage = new MemoryStorage();
    const items = [conversation('a', 2, [userMessage('Show all customers'), assistantMessage()]), conversation('b', 1)];
    expect(saveConversations(items, storage)).toBe(true);
    expect(loadConversations(storage)).toEqual(items);
  });

  it('applies the caps when saving', () => {
    const storage = new MemoryStorage();
    const many = Array.from({ length: 40 }, (_, index) => conversation(`c${index}`, index));
    saveConversations(many, storage);
    expect(JSON.parse(storage.getItem(CONVERSATIONS_KEY) ?? '[]')).toHaveLength(MAX_CONVERSATIONS);
  });

  it('removes the key when there is nothing to save', () => {
    const storage = new MemoryStorage();
    storage.setItem(CONVERSATIONS_KEY, '[]');
    expect(saveConversations([], storage)).toBe(true);
    expect(storage.getItem(CONVERSATIONS_KEY)).toBeNull();
  });

  it('compacts result rows and then drops the oldest chats when the quota is exceeded', () => {
    const bigRows = Array.from({ length: 400 }, (_, index) => [index, `customer-${index}`, 'California']);
    const withRows = (id: string, updatedAt: number): Conversation =>
      conversation(id, updatedAt, [
        userMessage('Show all customers'),
        assistantMessage({
          response: makeResponse({
            execution: { columns: ['id', 'name', 'state'], rows: bigRows, row_count: 400, truncated: false, elapsed_ms: 3, error: null },
          }),
        }),
      ]);
    const items = [withRows('new', 3), withRows('mid', 2), withRows('old', 1)];
    const fullSize = JSON.stringify(capConversations(items)).length;

    // Room for roughly one conversation with full rows: rows get trimmed and marked truncated.
    const storage = new MemoryStorage(Math.floor(fullSize / 2));
    expect(saveConversations(items, storage)).toBe(true);
    const saved = loadConversations(storage);
    expect(saved.map((item) => item.id)).toEqual(['new', 'mid', 'old']);
    const newest = saved[0].messages[1];
    expect(newest.role === 'assistant' && newest.response?.execution?.rows.length).toBeLessThan(400);
    expect(newest.role === 'assistant' && newest.response?.execution?.truncated).toBe(true);

    // Tiny quota: only the newest conversation survives.
    const tiny = new MemoryStorage(JSON.stringify(capConversations([withRows('new', 3)])).length / 3);
    expect(saveConversations(items, tiny)).toBe(true);
    expect(loadConversations(tiny).map((item) => item.id)).toEqual(['new']);
  });

  it('reports failure when even the smallest version does not fit', () => {
    expect(saveConversations([conversation('a', 1)], new MemoryStorage(5))).toBe(false);
    expect(saveConversations([conversation('a', 1)], null)).toBe(false);
  });
});

describe('settings', () => {
  it('falls back to defaults for missing, corrupt or invalid values', () => {
    const storage = new MemoryStorage();
    expect(loadSettings(storage)).toEqual(DEFAULT_SETTINGS);
    storage.setItem(SETTINGS_KEY, 'not json');
    expect(loadSettings(storage)).toEqual(DEFAULT_SETTINGS);
    storage.setItem(SETTINGS_KEY, JSON.stringify({ dialect: 'oracle', execute: 'yes', sidebarOpen: false }));
    expect(loadSettings(storage)).toEqual({ ...DEFAULT_SETTINGS, sidebarOpen: false });
    storage.setItem(SETTINGS_KEY, JSON.stringify({ dialect: 'postgres', execute: false }));
    expect(loadSettings(storage)).toEqual({ ...DEFAULT_SETTINGS, dialect: 'postgres', execute: false });
  });

  it('stores and clears the active conversation id', () => {
    const storage = new MemoryStorage();
    saveActiveConversationId('abc', storage);
    expect(loadActiveConversationId(storage)).toBe('abc');
    saveActiveConversationId(null, storage);
    expect(loadActiveConversationId(storage)).toBeNull();
  });
});
