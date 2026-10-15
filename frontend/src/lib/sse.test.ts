import { describe, expect, it } from 'vitest';
import { readSSEStream, SSEParser, type SSEMessage } from './sse';

function parseChunks(chunks: string[], flush = true): SSEMessage[] {
  const messages: SSEMessage[] = [];
  const parser = new SSEParser((message) => messages.push(message));
  for (const chunk of chunks) parser.feed(chunk);
  if (flush) parser.flush();
  return messages;
}

const STREAM =
  'event: start\ndata: {"thread_id":"t1","message_id":"m1"}\n\n' +
  ': ping - 2026-10-15 21:00:00\n\n' +
  'event: step\ndata: {"node":"detect_intent","status":"started"}\n\n' +
  'event: token\ndata: {"text":"Hello, wörld ✓"}\n\n' +
  'event: done\ndata: {}\n\n';

const EXPECTED: SSEMessage[] = [
  { event: 'start', data: '{"thread_id":"t1","message_id":"m1"}', id: null },
  { event: 'step', data: '{"node":"detect_intent","status":"started"}', id: null },
  { event: 'token', data: '{"text":"Hello, wörld ✓"}', id: null },
  { event: 'done', data: '{}', id: null },
];

describe('SSEParser', () => {
  it('parses named events with JSON data and skips comment pings', () => {
    expect(parseChunks([STREAM])).toEqual(EXPECTED);
  });

  it('gives identical results for every possible split into two chunks', () => {
    for (let index = 0; index <= STREAM.length; index++) {
      expect(parseChunks([STREAM.slice(0, index), STREAM.slice(index)])).toEqual(EXPECTED);
    }
  });

  it('handles a stream delivered one character at a time', () => {
    expect(parseChunks([...STREAM])).toEqual(EXPECTED);
  });

  it('accepts CRLF line endings, including a CRLF split across chunks', () => {
    const crlf = STREAM.replace(/\n/g, '\r\n');
    expect(parseChunks([crlf])).toEqual(EXPECTED);
    for (let index = 0; index <= crlf.length; index++) {
      expect(parseChunks([crlf.slice(0, index), crlf.slice(index)])).toEqual(EXPECTED);
    }
  });

  it('accepts bare CR line endings', () => {
    expect(parseChunks([STREAM.replace(/\n/g, '\r')])).toEqual(EXPECTED);
  });

  it('joins multi-line data fields with newlines', () => {
    const messages = parseChunks(['event: final\ndata: {"a":\ndata: 1,\ndata:  "b": 2}\n\n']);
    expect(messages).toEqual([{ event: 'final', data: '{"a":\n1,\n "b": 2}', id: null }]);
    expect(JSON.parse(messages[0].data)).toEqual({ a: 1, b: 2 });
  });

  it('strips only one leading space and supports fields without a value', () => {
    expect(parseChunks(['data:no-space\n\n', 'data:  two spaces\n\n', 'data\n\n'])).toEqual([
      { event: 'message', data: 'no-space', id: null },
      { event: 'message', data: ' two spaces', id: null },
      { event: 'message', data: '', id: null },
    ]);
  });

  it('defaults the event name to "message" and resets it after each dispatch', () => {
    expect(parseChunks(['event: custom\ndata: 1\n\ndata: 2\n\n'])).toEqual([
      { event: 'custom', data: '1', id: null },
      { event: 'message', data: '2', id: null },
    ]);
  });

  it('ignores events without data, comments, retry and unknown fields', () => {
    const messages = parseChunks([':\n: keep-alive\nevent: ping\n\nretry: 3000\nfoo: bar\nid: 7\ndata: x\n\n']);
    expect(messages).toEqual([{ event: 'message', data: 'x', id: '7' }]);
  });

  it('strips a leading byte-order mark', () => {
    expect(parseChunks(['\uFEFFevent: done\ndata: {}\n\n'])).toEqual([{ event: 'done', data: '{}', id: null }]);
  });

  it('delivers a final event without a trailing blank line on flush', () => {
    expect(parseChunks(['event: done\ndata: {}'], false)).toEqual([]);
    expect(parseChunks(['event: done\ndata: {}'])).toEqual([{ event: 'done', data: '{}', id: null }]);
  });
});

function streamOf(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
  return new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(chunk);
      controller.close();
    },
  });
}

describe('readSSEStream', () => {
  it('decodes UTF-8 characters split across network chunks', async () => {
    const bytes = new TextEncoder().encode(STREAM);
    const split = STREAM.indexOf('ö') + 1; // inside the two-byte "ö"
    const messages: SSEMessage[] = [];
    await readSSEStream(streamOf([bytes.slice(0, split), bytes.slice(split)]), (message) => {
      messages.push(message);
    });
    expect(messages).toEqual(EXPECTED);
  });

  it('stops reading when the handler returns false', async () => {
    const messages: SSEMessage[] = [];
    await readSSEStream(streamOf([new TextEncoder().encode(STREAM)]), (message) => {
      messages.push(message);
      return message.event !== 'step';
    });
    expect(messages.map((message) => message.event)).toEqual(['start', 'step']);
  });
});
