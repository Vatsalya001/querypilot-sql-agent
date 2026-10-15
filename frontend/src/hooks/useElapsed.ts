import { useEffect, useState } from 'react';

/** Milliseconds since `startedAt`, refreshed every `intervalMs` while `startedAt` is set. */
export function useElapsed(startedAt: number | null, intervalMs = 100): number {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (startedAt === null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs);
    return () => window.clearInterval(timer);
  }, [startedAt, intervalMs]);

  return startedAt === null ? 0 : Math.max(0, now - startedAt);
}
