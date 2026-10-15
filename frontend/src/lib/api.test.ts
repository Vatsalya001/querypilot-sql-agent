import { describe, expect, it, vi } from 'vitest';
import { makeResponse } from '../test/fixtures';
import { jsonResponse, sseResponse } from '../test/sse';
import { ApiError, errorFromResponse, streamChat, toStreamEvent } from './api';
import type { ChatRequest } from '../types';

const REQUEST: ChatRequest = {
  message: 'Show all customers',
  thread_id: 'thread-1',
  dialect: 'sqlite',
  execute: true,
  history: null,
};

describe('errorFromResponse', () => {
  it('reads detail, code and retry_after from the JSON error body', async () => {
    const error = await errorFromResponse(
      jsonResponse({ detail: 'Too many requests.', code: 'rate_limited', retry_after: 30 }, 429),
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ message: 'Too many requests.', status: 429, code: 'rate_limited', retryAfter: 30 });
  });

  it('falls back to the Retry-After header and FastAPI validation details', async () => {
    const error = await errorFromResponse(
      jsonResponse({ detail: [{ loc: ['body', 'message'], msg: 'Field required' }] }, 422, { 'Retry-After': '12' }),
    );
    expect(error).toMatchObject({ message: 'Field required', code: 'http_422', retryAfter: 12 });
  });

  it('explains a non-JSON gateway error', async () => {
    const error = await errorFromResponse(new Response('<html>Bad Gateway</html>', { status: 502 }));
    expect(error.message).toMatch(/not responding \(HTTP 502\)/);
  });
});

describe('toStreamEvent', () => {
  it('decodes known events and rejects unknown events or bad JSON', () => {
    expect(toStreamEvent({ event: 'done', data: '{}', id: null })).toEqual({ event: 'done', data: {} });
    expect(toStreamEvent({ event: 'surprise', data: '{}', id: null })).toBeNull();
    expect(toStreamEvent({ event: 'step', data: '{oops', id: null })).toBeNull();
  });
});

describe('streamChat', () => {
  it('posts the request and dispatches every event in order', async () => {
    const final = makeResponse();
    const fetchMock = vi.fn().mockResolvedValue(
      sseResponse([
        ['start', { thread_id: 'thread-1', message_id: 'message-1' }],
        ['step', { node: 'detect_intent', label: 'Detecting intent', status: 'started', duration_ms: null, detail: null }],
        ['token', { node: 'explain_sql', target: 'explanation', text: 'This query' }],
        ['final', final],
        ['done', {}],
      ]),
    );
    vi.stubGlobal('fetch', fetchMock);
    const seen: string[] = [];

    const outcome = await streamChat(REQUEST, {
      onStart: (data) => seen.push(`start:${data.thread_id}`),
      onStep: (data) => seen.push(`step:${data.node}:${data.status}`),
      onToken: (data) => seen.push(`token:${data.text}`),
      onFinal: (data) => seen.push(`final:${data.status}`),
    });

    expect(outcome).toBe('final');
    expect(seen).toEqual(['start:thread-1', 'step:detect_intent:started', 'token:This query', 'final:success']);
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe('/api/chat/stream');
    expect(init.method).toBe('POST');
    expect(JSON.parse(init.body)).toEqual(REQUEST);
  });

  it('reports a server error event', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        sseResponse([
          ['start', { thread_id: 't', message_id: 'm' }],
          ['error', { message: 'The model is offline.', code: 'llm_not_configured' }],
          ['done', {}],
        ]),
      ),
    );
    const onError = vi.fn();
    await expect(streamChat(REQUEST, { onError })).resolves.toBe('error');
    expect(onError).toHaveBeenCalledWith({ message: 'The model is offline.', code: 'llm_not_configured' });
  });

  it('throws an ApiError for HTTP errors before streaming', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(jsonResponse({ detail: 'Slow down.', code: 'rate_limited', retry_after: 30 }, 429)),
    );
    await expect(streamChat(REQUEST, {})).rejects.toMatchObject({ code: 'rate_limited', retryAfter: 30 });
  });

  it('throws when the stream ends without a final or error event', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(sseResponse([['start', { thread_id: 't', message_id: 'm' }]])));
    await expect(streamChat(REQUEST, {})).rejects.toMatchObject({ code: 'stream_interrupted' });
  });
});
