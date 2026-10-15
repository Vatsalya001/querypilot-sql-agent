import clsx from 'clsx';
import { Gauge } from 'lucide-react';
import { formatNumber } from '../../lib/format';
import { COST_TONES } from '../meta';
import { Badge, EmptyHint, SectionLabel } from '../ui';
import type { CostEstimate } from '../../types';

const LEVELS = ['low', 'medium', 'high'] as const;

const SEGMENT_COLORS: Record<string, string> = {
  low: 'bg-emerald-500',
  medium: 'bg-amber-500',
  high: 'bg-red-500',
};

/** Query cost estimate from SQLite's query plan: level, rows scanned, plan and notes. */
export function CostPanel({ cost }: { cost: CostEstimate | null }) {
  if (!cost) {
    return (
      <EmptyHint icon={Gauge} title="No cost estimate for this response">
        Cost is estimated for validated SQL from the SQLite query plan.
      </EmptyHint>
    );
  }

  const levelIndex = Math.max(0, LEVELS.indexOf(cost.level));
  const levelLabel = cost.level.charAt(0).toUpperCase() + cost.level.slice(1);

  return (
    <div className="space-y-5">
      <div className="rounded-xl border border-zinc-200 p-4 dark:border-zinc-800">
        <div className="flex items-center justify-between gap-3">
          <div>
            <SectionLabel>Estimated cost</SectionLabel>
            <p className="mt-1 text-2xl font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">{levelLabel}</p>
          </div>
          <Badge tone={COST_TONES[cost.level] ?? 'neutral'} icon={Gauge}>
            Score {formatNumber(cost.score)}
          </Badge>
        </div>
        <div className="mt-3 flex gap-1" aria-hidden="true">
          {LEVELS.map((level, index) => (
            <div
              key={level}
              className={clsx(
                'h-1.5 flex-1 rounded-full',
                index <= levelIndex ? SEGMENT_COLORS[cost.level] : 'bg-zinc-200 dark:bg-zinc-800',
              )}
            />
          ))}
        </div>
        <dl className="mt-4 grid grid-cols-2 gap-3 text-sm">
          <div>
            <dt className="text-xs text-zinc-500 dark:text-zinc-400">Rows scanned (est.)</dt>
            <dd className="font-medium text-zinc-900 tabular-nums dark:text-zinc-100">
              {formatNumber(cost.estimated_rows_scanned)}
            </dd>
          </div>
          <div>
            <dt className="text-xs text-zinc-500 dark:text-zinc-400">Plan steps</dt>
            <dd className="font-medium text-zinc-900 tabular-nums dark:text-zinc-100">{cost.plan.length}</dd>
          </div>
        </dl>
      </div>

      {cost.plan.length > 0 && (
        <section>
          <SectionLabel className="mb-2">Query plan</SectionLabel>
          <pre className="overflow-x-auto rounded-lg border border-zinc-200 bg-zinc-50/70 p-3 font-mono text-xs leading-6 text-zinc-700 dark:border-zinc-800 dark:bg-zinc-950/40 dark:text-zinc-300">
            {cost.plan.join('\n')}
          </pre>
        </section>
      )}

      {cost.notes.length > 0 && (
        <section>
          <SectionLabel className="mb-2">Notes</SectionLabel>
          <ul className="list-disc space-y-1 pl-5 text-[0.8125rem] text-zinc-700 marker:text-zinc-400 dark:text-zinc-300">
            {cost.notes.map((note) => (
              <li key={note} className="break-words">
                {note}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
