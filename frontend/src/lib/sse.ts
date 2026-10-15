/**
 * A small, spec-following parser for `text/event-stream` bodies read with `fetch` (EventSource cannot
 * send a POST body). It is robust to arbitrary chunk boundaries (including a CRLF split across two
 * chunks), accepts LF, CR and CRLF line endings, joins multi-line `data:` fields with "\n", and ignores
 * comment lines such as the server's `: ping` keep-alives.
 */

export interface SSEMessage {
  /** Event name; "message" when the server sent no `event:` field. */
  event: string;
  /** The data lines joined with "\n". */
  data: string;
  /** The last `id:` seen on the stream, or null. */
  id: string | null;
}

export type SSEMessageHandler = (message: SSEMessage) => void;

export class SSEParser {
  private buffer = '';
  private dataLines: string[] = [];
  private eventType = '';
  private lastEventId: string | null = null;
  private pendingCR = false;
  private started = false;
  private readonly onMessage: SSEMessageHandler;

  constructor(onMessage: SSEMessageHandler) {
    this.onMessage = onMessage;
  }

  /** Feed the next decoded chunk of the stream. */
  feed(chunk: string): void {
    if (!chunk) return;
    let text = chunk;
    if (!this.started) {
      this.started = true;
      if (text.charCodeAt(0) === 0xfeff) text = text.slice(1); // byte-order mark
    }
    // A chunk that ended with CR was already treated as a line break; a leading LF completes that CRLF.
    if (this.pendingCR && text.startsWith('\n')) text = text.slice(1);
    this.pendingCR = false;

    const buffer = this.buffer + text;
    let lineStart = 0;
    for (let i = 0; i < buffer.length; i++) {
      const char = buffer[i];
      if (char !== '\r' && char !== '\n') continue;
      this.processLine(buffer.slice(lineStart, i));
      if (char === '\r') {
        if (i + 1 < buffer.length) {
          if (buffer[i + 1] === '\n') i++;
        } else {
          this.pendingCR = true;
        }
      }
      lineStart = i + 1;
    }
    this.buffer = buffer.slice(lineStart);
  }

  /**
   * Signal the end of the stream. Unlike the browser EventSource, a final event that is missing its
   * terminating blank line is still delivered, so a truncated last frame is not silently lost.
   */
  flush(): void {
    if (this.buffer) {
      this.processLine(this.buffer);
      this.buffer = '';
    }
    this.dispatch();
  }

  private processLine(line: string): void {
    if (line === '') {
      this.dispatch();
      return;
    }
    if (line.startsWith(':')) return; // comment, e.g. a keep-alive ping

    const colon = line.indexOf(':');
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? '' : line.slice(colon + 1);
    if (value.startsWith(' ')) value = value.slice(1);

    switch (field) {
      case 'event':
        this.eventType = value;
        break;
      case 'data':
        this.dataLines.push(value);
        break;
      case 'id':
        if (!value.includes('\0')) this.lastEventId = value;
        break;
      default:
        // `retry` and unknown fields are ignored: this client never reconnects automatically.
        break;
    }
  }

  private dispatch(): void {
    if (this.dataLines.length === 0) {
      this.eventType = '';
      return;
    }
    const message: SSEMessage = {
      event: this.eventType || 'message',
      data: this.dataLines.join('\n'),
      id: this.lastEventId,
    };
    this.dataLines = [];
    this.eventType = '';
    this.onMessage(message);
  }
}

/**
 * Read an SSE response body to the end, calling `onMessage` for every event. Return `false` from the
 * handler to stop reading early (the body is then cancelled).
 */
export async function readSSEStream(
  body: ReadableStream<Uint8Array>,
  onMessage: (message: SSEMessage) => boolean | void,
): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let stopped = false;
  const parser = new SSEParser((message) => {
    if (stopped) return;
    if (onMessage(message) === false) stopped = true;
  });

  try {
    while (!stopped) {
      const { value, done } = await reader.read();
      if (done) break;
      parser.feed(decoder.decode(value, { stream: true }));
    }
    if (!stopped) {
      parser.feed(decoder.decode());
      parser.flush();
    }
  } finally {
    if (stopped) {
      await reader.cancel().catch(() => undefined);
    }
    try {
      reader.releaseLock();
    } catch {
      // The stream is already errored or closed; nothing left to release.
    }
  }
}
