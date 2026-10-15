import clsx from 'clsx';
import { CircleCheck, CircleX } from 'lucide-react';
import { formatDuration } from '../lib/format';
import { REPAIR_LOOP_NODES } from '../lib/pipeline';
import { NODE_ICONS } from './meta';
import { Spinner } from './ui';
import type { StepEvent } from '../types';

export interface StepRow {
  key: string;
  node: string;
  label: string;
  /** `interrupted`: the request ended (error or stop) while this node was still running. */
  status: 'running' | 'done' | 'interrupted';
  duration_ms: number | null;
  detail: string | null;
  /** Repair attempt number for repeated generate/validate steps (null for the first run). */
  attempt: number | null;
}

/**
 * Fold streamed step events into one row per node execution: a `started` event opens a row and the
 * matching `completed` event closes it. Completed events without a start (as in `AgentResponse.steps`)
 * become finished rows. Repeated `generate_sql` / `validate_sql` runs are numbered as repair attempts.
 * When `settled` (the request is over), rows that never completed are marked `interrupted`.
 */
export function buildStepRows(steps: readonly StepEvent[], settled = false): StepRow[] {
  const rows: StepRow[] = [];
  const runs = new Map<string, number>();

  const openRow = (step: StepEvent, status: StepRow['status']): StepRow => {
    const run = (runs.get(step.node) ?? 0) + 1;
    runs.set(step.node, run);
    const row: StepRow = {
      key: `${step.node}-${run}`,
      node: step.node,
      label: step.label,
      status,
      duration_ms: null,
      detail: null,
      attempt: REPAIR_LOOP_NODES.has(step.node) && run > 1 ? run - 1 : null,
    };
    rows.push(row);
    return row;
  };

  for (const step of steps) {
    if (step.status === 'started') {
      openRow(step, 'running');
      continue;
    }
    const open = rows.findLast((row) => row.node === step.node && row.status === 'running') ?? openRow(step, 'done');
    open.status = 'done';
    open.duration_ms = step.duration_ms;
    open.detail = step.detail;
    if (step.label) open.label = step.label;
  }
  if (settled) {
    for (const row of rows) {
      if (row.status === 'running') row.status = 'interrupted';
    }
  }
  return rows;
}

export interface StepTrackerProps {
  rows: readonly StepRow[];
  /** Show node icons instead of status icons for finished rows (compact trace view). */
  dense?: boolean;
  className?: string;
}

/** The LangGraph pipeline as a checklist: spinner while a node runs, check plus duration when done. */
export function StepTracker({ rows, dense = false, className }: StepTrackerProps) {
  return (
    <ol className={clsx(dense ? 'space-y-1' : 'space-y-1.5', className)}>
      {rows.map((row) => {
        const NodeIcon = NODE_ICONS[row.node];
        const running = row.status === 'running';
        const interrupted = row.status === 'interrupted';
        return (
          <li key={row.key} className="flex min-w-0 items-center gap-2.5 text-[0.8125rem] motion-safe:animate-slide-up">
            <span className="flex size-4 shrink-0 items-center justify-center">
              {running ? (
                <Spinner className="size-4 text-indigo-500" />
              ) : interrupted ? (
                <CircleX aria-hidden="true" className="size-4 text-red-500" />
              ) : dense && NodeIcon ? (
                <NodeIcon aria-hidden="true" className="size-3.5 text-zinc-400 dark:text-zinc-500" />
              ) : (
                <CircleCheck aria-hidden="true" className="size-4 text-emerald-500" />
              )}
            </span>
            <span
              className={clsx(
                'shrink-0',
                running ? 'font-medium text-zinc-900 dark:text-zinc-100' : 'text-zinc-600 dark:text-zinc-400',
              )}
            >
              {row.label}
            </span>
            {row.attempt !== null && (
              <span className="shrink-0 rounded-md bg-amber-50 px-1.5 py-px text-[0.6875rem] font-medium text-amber-700 dark:bg-amber-400/10 dark:text-amber-300">
                repair attempt {row.attempt}
              </span>
            )}
            {row.detail && (
              <span className="min-w-0 truncate text-xs text-zinc-400 dark:text-zinc-500" title={row.detail}>
                {row.detail}
              </span>
            )}
            <span className="ml-auto shrink-0 pl-2 text-xs text-zinc-400 tabular-nums dark:text-zinc-500">
              {running ? (
                <span className="sr-only">running</span>
              ) : interrupted ? (
                <span className="text-red-500 dark:text-red-400">did not finish</span>
              ) : (
                formatDuration(row.duration_ms)
              )}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
