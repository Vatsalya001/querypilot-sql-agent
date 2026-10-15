import { useCallback, useEffect, useRef, useState } from 'react';
import { getHealth, isAbortError } from '../lib/api';
import type { HealthResponse } from '../types';

export type HealthState =
  | { status: 'loading' }
  | { status: 'ok'; health: HealthResponse }
  | { status: 'offline'; error: string };

const WARMUP_POLL_MS = 5_000;

/** Polls GET /api/health (on mount, every `pollMs`, and when the window regains focus). */
export function useHealth(pollMs = 30_000): { state: HealthState; refresh: () => void } {
  const [state, setState] = useState<HealthState>({ status: 'loading' });
  const controllerRef = useRef<AbortController | null>(null);

  const refresh = useCallback(() => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    getHealth(controller.signal)
      .then((health) => setState({ status: 'ok', health }))
      .catch((error: unknown) => {
        if (isAbortError(error)) return;
        setState({ status: 'offline', error: error instanceof Error ? error.message : 'The API is unreachable.' });
      });
  }, []);

  useEffect(() => {
    refresh();
    window.addEventListener('focus', refresh);
    return () => {
      window.removeEventListener('focus', refresh);
      controllerRef.current?.abort();
    };
  }, [refresh]);

  // Poll faster while the local model warms up so the pill flips to ready promptly.
  const warming = state.status === 'ok' && state.health.llm.warmup === 'warming';
  const intervalMs = warming ? Math.min(pollMs, WARMUP_POLL_MS) : pollMs;
  useEffect(() => {
    const timer = window.setInterval(refresh, intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs, refresh]);

  return { state, refresh };
}
