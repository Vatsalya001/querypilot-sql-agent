import clsx from 'clsx';
import type { HealthState } from '../hooks/useHealth';
import { providerLabel } from '../lib/format';
import { Tooltip } from './ui';

type PillTone = 'neutral' | 'green' | 'amber' | 'blue' | 'red';

interface PillInfo {
  tone: PillTone;
  label: string;
  tooltip: string;
  /** Animate the status dot (connecting, warming up). */
  pulse?: boolean;
}

const PILL_TONES: Record<PillTone, string> = {
  neutral: 'border-zinc-200 bg-zinc-50 text-zinc-500 dark:border-zinc-700 dark:bg-zinc-800/60 dark:text-zinc-400',
  green:
    'border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-400/25 dark:bg-emerald-400/10 dark:text-emerald-300',
  amber: 'border-amber-200 bg-amber-50 text-amber-800 dark:border-amber-400/25 dark:bg-amber-400/10 dark:text-amber-300',
  blue: 'border-sky-200 bg-sky-50 text-sky-700 dark:border-sky-400/25 dark:bg-sky-400/10 dark:text-sky-300',
  red: 'border-red-200 bg-red-50 text-red-700 dark:border-red-400/25 dark:bg-red-400/10 dark:text-red-300',
};

const DOT_TONES: Record<PillTone, string> = {
  neutral: 'bg-zinc-400',
  green: 'bg-emerald-500',
  amber: 'bg-amber-500',
  blue: 'bg-sky-500',
  red: 'bg-red-500',
};

/** Label, colour and tooltip for the current health state. */
export function describeHealth(state: HealthState): PillInfo {
  if (state.status === 'loading') {
    return { tone: 'neutral', label: 'Connecting…', tooltip: 'Checking the API at /api/health.', pulse: true };
  }
  if (state.status === 'offline') {
    return {
      tone: 'red',
      label: 'API offline',
      tooltip:
        'Cannot reach the QueryPilot API at /api. Start the backend with "make dev" (or "make backend"); this page reconnects automatically.',
    };
  }
  const { llm, database } = state.health;
  const provider = providerLabel(llm.provider);
  if (llm.mock) {
    return {
      tone: 'blue',
      label: 'Mock mode',
      tooltip:
        'LLM_PROVIDER=mock: an offline, rule-based engine answers instead of a language model. Set LLM_PROVIDER=ollama (local) or groq (hosted) for real AI answers.',
    };
  }
  if (!llm.configured) {
    const fix =
      llm.provider === 'ollama'
        ? `Start Ollama ("ollama serve" or "make ollama") and make sure ${llm.model} is pulled.`
        : `Set the ${provider} API key in .env (for example GROQ_API_KEY) and restart the backend.`;
    return { tone: 'amber', label: 'LLM offline', tooltip: `${provider} (${llm.model}) is not available. ${fix}` };
  }
  if (llm.warmup === 'warming') {
    return {
      tone: 'amber',
      label: `Warming up · ${llm.model}`,
      tooltip: `${provider} is loading ${llm.model} and caching its prompts (about 1-2 minutes after startup). You can ask now, but the first answers are slower until this finishes.`,
      pulse: true,
    };
  }
  const fast = llm.fast_model && llm.fast_model !== llm.model ? ` Fast model: ${llm.fast_model}.` : '';
  const fallbacks = llm.fallback_models.length > 0 ? ` Fallbacks: ${llm.fallback_models.join(', ')}.` : '';
  return {
    tone: 'green',
    label: `${provider} · ${llm.model}`,
    tooltip: `Connected to ${provider}. Model: ${llm.model}.${fast}${fallbacks} Database: ${database.dialect}, ${database.tables} tables. API v${state.health.version}.`,
  };
}

/** Model status from GET /api/health, with an explanatory tooltip. */
export function StatusPill({ state, compact = false }: { state: HealthState; compact?: boolean }) {
  const info = describeHealth(state);
  return (
    <div className="group relative">
      <span
        tabIndex={0}
        role="status"
        aria-describedby="status-pill-tooltip"
        className={clsx(
          'inline-flex h-7 max-w-[18rem] items-center gap-1.5 rounded-full border text-xs font-medium',
          compact ? 'px-2' : 'px-2.5',
          PILL_TONES[info.tone],
        )}
      >
        <span
          aria-hidden="true"
          className={clsx(
            'size-2 shrink-0 rounded-full',
            DOT_TONES[info.tone],
            info.pulse && 'motion-safe:animate-pulse',
          )}
        />
        <span className={clsx('truncate', compact && 'sr-only')}>{info.label}</span>
      </span>
      <Tooltip id="status-pill-tooltip" align={compact ? 'center' : 'left'}>
        {info.tooltip}
      </Tooltip>
    </div>
  );
}
