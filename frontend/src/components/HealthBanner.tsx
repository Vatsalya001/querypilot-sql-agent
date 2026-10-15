import clsx from 'clsx';
import { Info, RotateCcw, TriangleAlert, X } from 'lucide-react';
import { useState } from 'react';
import type { HealthState } from '../hooks/useHealth';
import { providerLabel } from '../lib/format';
import { Button, IconButton } from './ui';

type BannerKind = 'offline' | 'unconfigured' | 'mock';

function bannerKind(state: HealthState): BannerKind | null {
  if (state.status === 'offline') return 'offline';
  if (state.status !== 'ok') return null;
  if (state.health.llm.mock) return 'mock';
  return state.health.llm.configured ? null : 'unconfigured';
}

/** Slim, dismissible banner when the API is down, the LLM is unavailable, or mock mode is on. */
export function HealthBanner({ state, onRetry }: { state: HealthState; onRetry: () => void }) {
  const [dismissed, setDismissed] = useState<BannerKind | null>(null);
  const kind = bannerKind(state);
  if (!kind || dismissed === kind) return null;

  let message: string;
  if (kind === 'offline') {
    message = 'Cannot reach the QueryPilot API. Start the backend with "make dev"; this page reconnects automatically.';
  } else if (kind === 'mock') {
    message = 'Mock mode: answers come from an offline, rule-based engine instead of a language model.';
  } else {
    const llm = state.status === 'ok' ? state.health.llm : null;
    const name = llm ? `${providerLabel(llm.provider)} (${llm.model})` : 'The language model';
    message =
      llm?.provider === 'ollama'
        ? `${name} is not reachable, so questions will fail. Start it with "make ollama".`
        : `${name} is not configured, so questions will fail. Add the API key to .env and restart the backend.`;
  }

  const warning = kind !== 'mock';
  const Icon = warning ? TriangleAlert : Info;
  return (
    <div
      role={warning ? 'alert' : 'status'}
      className={clsx(
        'flex shrink-0 items-center gap-2.5 border-b px-3 py-2 text-[0.8125rem] sm:px-4',
        kind === 'offline' && 'border-red-200 bg-red-50 text-red-800 dark:border-red-400/20 dark:bg-red-400/10 dark:text-red-200',
        kind === 'unconfigured' &&
          'border-amber-200 bg-amber-50 text-amber-900 dark:border-amber-400/20 dark:bg-amber-400/10 dark:text-amber-200',
        kind === 'mock' && 'border-sky-200 bg-sky-50 text-sky-900 dark:border-sky-400/20 dark:bg-sky-400/10 dark:text-sky-200',
      )}
    >
      <Icon aria-hidden="true" className="size-4 shrink-0" />
      <p className="min-w-0 flex-1">{message}</p>
      {kind !== 'mock' && (
        <Button size="xs" variant="ghost" onClick={onRetry} className="text-current hover:bg-black/5 dark:hover:bg-white/10">
          <RotateCcw aria-hidden="true" className="size-3.5" />
          <span className="hidden sm:inline">Check again</span>
        </Button>
      )}
      <IconButton
        label="Dismiss"
        size="sm"
        onClick={() => setDismissed(kind)}
        className="size-7 text-current hover:bg-black/5 dark:text-current dark:hover:bg-white/10"
      >
        <X aria-hidden="true" className="size-4" />
      </IconButton>
    </div>
  );
}
