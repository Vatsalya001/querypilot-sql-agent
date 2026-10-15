import { useCallback, useEffect, useRef, useState } from 'react';
import { getSchema, isAbortError } from '../lib/api';
import type { SchemaResponse } from '../types';

export type SchemaState =
  | { status: 'loading' }
  | { status: 'ready'; schema: SchemaResponse }
  | { status: 'error'; error: string };

/** Loads GET /api/schema once for the schema explorer; `reload` retries after a failure. */
export function useSchema(): { state: SchemaState; reload: () => void } {
  const [state, setState] = useState<SchemaState>({ status: 'loading' });
  const controllerRef = useRef<AbortController | null>(null);

  const reload = useCallback(() => {
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setState({ status: 'loading' });
    getSchema(controller.signal)
      .then((schema) => setState({ status: 'ready', schema }))
      .catch((error: unknown) => {
        if (isAbortError(error)) return;
        setState({ status: 'error', error: error instanceof Error ? error.message : 'Could not load the schema.' });
      });
  }, []);

  useEffect(() => {
    reload();
    return () => controllerRef.current?.abort();
  }, [reload]);

  return { state, reload };
}
