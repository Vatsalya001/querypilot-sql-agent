/**
 * HTTP client for the QueryPilot API (contract: docs/API.md). In production the UI and the API share
 * one origin; in development Vite proxies /api to the backend. VITE_API_BASE_URL can point the UI at
 * an API hosted elsewhere.
 */
import { readSSEStream, type SSEMessage } from './sse';
import type {
  AgentResponse,
  ChatRequest,
  ErrorEvent,
  GraphResponse,
  HealthResponse,
  SchemaResponse,
  StartEvent,
  StepEvent,
  StreamEvent,
  TokenEvent,
} from '../types';

const API_BASE = `${(import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '')}/api`;

/** An HTTP error from the API (or a stream that ended without a result). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly retryAfter: number | null;

  constructor(message: string, options: { status: number; code: string; retryAfter?: number | null }) {
    super(message);
    this.name = 'ApiError';
    this.status = options.status;
    this.code = options.code;
    this.retryAfter = options.retryAfter ?? null;
  }
}

/** True for errors raised by `AbortController.abort()`. */
export function isAbortError(error: unknown): boolean {
  return error instanceof DOMException
    ? error.name === 'AbortError'
    : error instanceof Error && error.name === 'AbortError';
}

/** True when the request never reached the server (offline, DNS, refused connection, CORS). */
export function isNetworkError(error: unknown): boolean {
  return error instanceof TypeError;
}

function describeDetail(detail: unknown): string | null {
  if (typeof detail === 'string' && detail.trim()) return detail;
  if (Array.isArray(detail)) {
    // FastAPI's default validation error format: [{ loc, msg, type }, ...]
    const messages = detail
      .map((item) => (item && typeof item === 'object' && 'msg' in item ? String(item.msg) : null))
      .filter((msg): msg is string => Boolean(msg));
    if (messages.length > 0) return messages.join('; ');
  }
  return null;
}

/** Build an ApiError from a non-2xx response, reading the `{ detail, code, retry_after }` body. */
export async function errorFromResponse(response: Response): Promise<ApiError> {
  let message: string | null = null;
  let code = `http_${response.status}`;
  let retryAfter: number | null = null;

  try {
    const text = await response.text();
    const body: unknown = text ? JSON.parse(text) : null;
    if (body && typeof body === 'object') {
      const record = body as Record<string, unknown>;
      message = describeDetail(record.detail);
      if (typeof record.code === 'string' && record.code) code = record.code;
      if (typeof record.retry_after === 'number') retryAfter = record.retry_after;
    }
  } catch {
    // Not JSON (for example a proxy error page): fall back to a generic message below.
  }

  if (retryAfter === null) {
    const header = response.headers.get('Retry-After');
    if (header && /^\d+$/.test(header.trim())) retryAfter = Number(header.trim());
  }
  if (!message) {
    message =
      response.status >= 500
        ? `The QueryPilot API is not responding (HTTP ${response.status}). Make sure the backend is running.`
        : `The request failed with HTTP ${response.status}.`;
  }
  return new ApiError(message, { status: response.status, code, retryAfter });
}

async function getJSON<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { headers: { Accept: 'application/json' }, signal });
  if (!response.ok) throw await errorFromResponse(response);
  return (await response.json()) as T;
}

export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return getJSON<HealthResponse>('/health', signal);
}

export function getSchema(signal?: AbortSignal): Promise<SchemaResponse> {
  return getJSON<SchemaResponse>('/schema', signal);
}

export function getGraph(signal?: AbortSignal): Promise<GraphResponse> {
  return getJSON<GraphResponse>('/graph', signal);
}

/** Forget the server-side memory of a conversation. Fire-and-forget: failures are ignored. */
export function deleteThread(threadId: string): void {
  void fetch(`${API_BASE}/threads/${encodeURIComponent(threadId)}`, { method: 'DELETE', keepalive: true }).catch(
    () => undefined,
  );
}

const STREAM_EVENTS = new Set<StreamEvent['event']>(['start', 'step', 'token', 'final', 'error', 'done']);

/** Decode one SSE message into a typed stream event; unknown events and malformed JSON yield null. */
export function toStreamEvent(message: SSEMessage): StreamEvent | null {
  if (!STREAM_EVENTS.has(message.event as StreamEvent['event'])) return null;
  try {
    const data: unknown = message.data ? JSON.parse(message.data) : {};
    if (!data || typeof data !== 'object') return null;
    return { event: message.event, data } as StreamEvent;
  } catch {
    return null;
  }
}

export interface StreamHandlers {
  onStart?: (data: StartEvent) => void;
  onStep?: (data: StepEvent) => void;
  onToken?: (data: TokenEvent) => void;
  onFinal?: (data: AgentResponse) => void;
  onError?: (data: ErrorEvent) => void;
}

/**
 * POST a chat message to /api/chat/stream and dispatch the SSE events to `handlers`.
 *
 * Resolves with "final" or "error" (the terminal event that was received). Throws an ApiError for
 * HTTP errors before streaming starts and when the stream ends without a terminal event, a TypeError
 * for network failures, and an AbortError when `signal` is aborted.
 */
export async function streamChat(
  request: ChatRequest,
  handlers: StreamHandlers,
  signal?: AbortSignal,
): Promise<'final' | 'error'> {
  const response = await fetch(`${API_BASE}/chat/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'text/event-stream' },
    body: JSON.stringify(request),
    signal,
  });
  if (!response.ok) throw await errorFromResponse(response);
  if (!response.body) {
    throw new ApiError('The server returned an empty response.', { status: response.status, code: 'empty_body' });
  }

  let outcome: 'final' | 'error' | null = null;
  await readSSEStream(response.body, (message) => {
    const event = toStreamEvent(message);
    if (!event) return;
    switch (event.event) {
      case 'start':
        handlers.onStart?.(event.data);
        break;
      case 'step':
        handlers.onStep?.(event.data);
        break;
      case 'token':
        handlers.onToken?.(event.data);
        break;
      case 'final':
        outcome ??= 'final';
        handlers.onFinal?.(event.data);
        break;
      case 'error':
        outcome ??= 'error';
        handlers.onError?.(event.data);
        break;
      case 'done':
        return false;
    }
  });

  if (!outcome) {
    throw new ApiError('The connection closed before the answer was complete. Please try again.', {
      status: response.status,
      code: 'stream_interrupted',
    });
  }
  return outcome;
}
