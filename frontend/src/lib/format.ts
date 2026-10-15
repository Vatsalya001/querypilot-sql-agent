/** Display formatting helpers (numbers, durations, relative times, labels). */
import type { Dialect } from '../types';

const numberFormat = new Intl.NumberFormat('en-US');

export function formatNumber(value: number): string {
  return numberFormat.format(value);
}

/** `1 row`, `42 rows`, `1,500 rows`. */
export function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${formatNumber(count)} ${count === 1 ? singular : plural}`;
}

/** `850 ms`, `1.2 s`, `14 s`, `1m 05s`. */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return '';
  if (ms < 1) return '<1 ms';
  if (ms < 1000) return `${Math.round(ms)} ms`;
  const seconds = ms / 1000;
  if (seconds < 10) return `${seconds.toFixed(1)} s`;
  if (seconds < 60) return `${Math.round(seconds)} s`;
  const minutes = Math.floor(seconds / 60);
  const rest = Math.round(seconds % 60);
  return `${minutes}m ${String(rest).padStart(2, '0')}s`;
}

/** A live stopwatch reading: `0.4s`, `12.3s`, `1m 05s`. */
export function formatElapsed(ms: number): string {
  if (ms < 60_000) return `${(Math.max(0, ms) / 1000).toFixed(1)}s`;
  const totalSeconds = Math.floor(ms / 1000);
  return `${Math.floor(totalSeconds / 60)}m ${String(totalSeconds % 60).padStart(2, '0')}s`;
}

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** `just now`, `5m ago`, `3h ago`, `Yesterday`, `Mon`, `Oct 3`, `Oct 3, 2025`. */
export function formatRelativeTime(timestamp: number, now: number = Date.now()): string {
  const diff = now - timestamp;
  if (diff < MINUTE) return 'just now';
  if (diff < HOUR) return `${Math.floor(diff / MINUTE)}m ago`;
  const date = new Date(timestamp);
  const today = new Date(now);
  const startOfToday = new Date(today.getFullYear(), today.getMonth(), today.getDate()).getTime();
  if (timestamp >= startOfToday) return `${Math.floor(diff / HOUR)}h ago`;
  if (timestamp >= startOfToday - DAY) return 'Yesterday';
  if (timestamp >= startOfToday - 6 * DAY) return date.toLocaleDateString('en-US', { weekday: 'short' });
  if (date.getFullYear() === today.getFullYear()) {
    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  }
  return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

/** `9:41 PM`. */
export function formatClockTime(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

export const DIALECT_LABELS: Record<Dialect, string> = {
  sqlite: 'SQLite',
  postgres: 'PostgreSQL',
  mysql: 'MySQL',
};

export function dialectLabel(dialect: string): string {
  return DIALECT_LABELS[dialect as Dialect] ?? dialect;
}

const PROVIDER_LABELS: Record<string, string> = {
  ollama: 'Ollama',
  groq: 'Groq',
  google: 'Gemini',
  gemini: 'Gemini',
  openai: 'OpenAI',
  mock: 'Mock',
};

export function providerLabel(provider: string): string {
  return PROVIDER_LABELS[provider.toLowerCase()] ?? provider.charAt(0).toUpperCase() + provider.slice(1);
}

/** Collapse whitespace and cut to `max` characters with an ellipsis. */
export function truncate(text: string, max: number): string {
  const singleLine = text.replace(/\s+/g, ' ').trim();
  return singleLine.length > max ? `${singleLine.slice(0, max - 1).trimEnd()}…` : singleLine;
}
