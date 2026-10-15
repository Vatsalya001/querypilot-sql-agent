import { useMemo } from 'react';
import { useElapsed } from '../hooks/useElapsed';
import type { PendingRequest } from '../hooks/useChat';
import { formatElapsed } from '../lib/format';
import { AgentAvatar } from './Avatar';
import { buildStepRows, StepTracker } from './StepTracker';
import { SectionLabel, Spinner } from './ui';

/** After this long, reassure the user that slow local models are expected. */
const SLOW_HINT_MS = 12_000;

/** The in-flight answer: live pipeline steps, an elapsed-time counter and streamed text. */
export function PendingMessage({ pending }: { pending: PendingRequest }) {
  const elapsed = useElapsed(pending.startedAt);
  const rows = useMemo(() => buildStepRows(pending.steps), [pending.steps]);
  const current = rows.findLast((row) => row.status === 'running');
  const headline = current?.label ?? (rows.length > 0 ? 'Working' : 'Connecting to the agent');

  return (
    <article
      className="flex gap-3 motion-safe:animate-slide-up"
      aria-label="Response in progress"
      aria-busy="true"
      aria-live="off"
    >
      <AgentAvatar />
      <div className="min-w-0 flex-1 overflow-hidden rounded-xl border border-zinc-200 bg-white shadow-xs dark:border-zinc-800 dark:bg-zinc-900">
        <div className="flex items-center gap-2.5 border-b border-zinc-100 bg-zinc-50/60 px-4 py-2.5 dark:border-zinc-800 dark:bg-zinc-900/60">
          <Spinner className="size-4 shrink-0 text-indigo-500" />
          <p className="min-w-0 truncate text-sm font-medium text-zinc-800 dark:text-zinc-200" role="status">
            {headline}…
          </p>
          <span
            className="ml-auto shrink-0 font-mono text-xs text-zinc-400 tabular-nums dark:text-zinc-500"
            aria-label="Elapsed time"
          >
            {formatElapsed(elapsed)}
          </span>
        </div>

        <div className="space-y-4 px-4 py-3">
          {rows.length > 0 ? (
            <StepTracker rows={rows} />
          ) : (
            <div className="space-y-2" aria-hidden="true">
              <div className="skeleton h-3.5 w-2/5" />
              <div className="skeleton h-3.5 w-1/3" />
            </div>
          )}

          {pending.message && <StreamingText label="Answer" text={pending.message} />}
          {pending.explanation && <StreamingText label="Explanation" text={pending.explanation} />}

          {elapsed > SLOW_HINT_MS && !pending.explanation && !pending.message && (
            <p className="text-xs text-zinc-400 dark:text-zinc-500">
              Local models can take 10–30 s, especially for the first request while the model warms up.
            </p>
          )}
        </div>
      </div>
    </article>
  );
}

function StreamingText({ label, text }: { label: string; text: string }) {
  return (
    <section aria-label={label} className="border-t border-zinc-100 pt-3 dark:border-zinc-800">
      <SectionLabel className="mb-1.5">{label}</SectionLabel>
      <p className="text-[0.9375rem] leading-relaxed break-words whitespace-pre-wrap text-zinc-700 dark:text-zinc-300">
        {text}
        <span
          aria-hidden="true"
          className="ml-0.5 inline-block h-4 w-[2px] translate-y-0.5 bg-indigo-500 motion-safe:animate-caret"
        />
      </p>
    </section>
  );
}
