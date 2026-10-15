import clsx from 'clsx';
import { ArrowUpRight, BookOpen, Bug, CornerDownRight, MessagesSquare, ShieldCheck, Sparkles, Zap } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { EXAMPLE_GROUPS, type ExampleGroupId, type ExamplePrompt } from '../lib/examples';
import { LogoMark } from './Logo';

const GROUP_STYLE: Record<ExampleGroupId, { icon: LucideIcon; tile: string }> = {
  generate: { icon: Sparkles, tile: 'bg-indigo-50 text-indigo-600 dark:bg-indigo-400/10 dark:text-indigo-300' },
  follow_up: { icon: MessagesSquare, tile: 'bg-violet-50 text-violet-600 dark:bg-violet-400/10 dark:text-violet-300' },
  optimize: { icon: Zap, tile: 'bg-amber-50 text-amber-600 dark:bg-amber-400/10 dark:text-amber-300' },
  debug: { icon: Bug, tile: 'bg-rose-50 text-rose-600 dark:bg-rose-400/10 dark:text-rose-300' },
  explain: { icon: BookOpen, tile: 'bg-sky-50 text-sky-600 dark:bg-sky-400/10 dark:text-sky-300' },
  guardrails: { icon: ShieldCheck, tile: 'bg-emerald-50 text-emerald-600 dark:bg-emerald-400/10 dark:text-emerald-300' },
};

export interface EmptyStateProps {
  onPick: (example: ExamplePrompt) => void;
  disabled?: boolean;
}

/** Hero plus clickable example prompts, grouped by capability. */
export function EmptyState({ onPick, disabled }: EmptyStateProps) {
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col items-center px-4 pt-8 pb-6 sm:px-6 sm:pt-10 @5xl:max-w-5xl">
      <LogoMark className="size-10 rounded-[10px] shadow-lg shadow-indigo-500/20" />
      <h1 className="mt-4 text-center text-2xl font-semibold tracking-tight text-zinc-900 sm:text-[1.75rem] dark:text-zinc-50">
        Ask your database anything
      </h1>
      <p className="mt-2 max-w-xl text-center text-[0.9375rem] leading-relaxed text-zinc-500 dark:text-zinc-400">
        QueryPilot turns plain English into validated, read-only SQL for the sample company database, then explains,
        optimizes and runs it.
      </p>

      <div className="mt-7 grid w-full gap-3 @xl:grid-cols-2 @5xl:grid-cols-3">
        {EXAMPLE_GROUPS.map((group) => {
          const style = GROUP_STYLE[group.id];
          const Icon = style.icon;
          return (
            <section
              key={group.id}
              aria-labelledby={`examples-${group.id}`}
              className="rounded-xl border border-zinc-200 bg-white p-3.5 shadow-xs dark:border-zinc-800 dark:bg-zinc-900"
            >
              <div className="flex items-center gap-2.5 px-1">
                <span className={clsx('flex size-7 shrink-0 items-center justify-center rounded-lg', style.tile)}>
                  <Icon aria-hidden="true" className="size-4" />
                </span>
                <div className="min-w-0">
                  <h2 id={`examples-${group.id}`} className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
                    {group.title}
                  </h2>
                  <p className="truncate text-xs text-zinc-500 dark:text-zinc-400">{group.description}</p>
                </div>
              </div>
              <ul className="mt-2.5 space-y-1">
                {group.examples.map((example) => (
                  <li key={example.prompt}>
                    <button
                      type="button"
                      disabled={disabled}
                      onClick={() => onPick(example)}
                      title={example.followUp ? `${example.prompt}, then: ${example.followUp}` : example.prompt}
                      className="group flex w-full items-start gap-2 rounded-lg border border-transparent px-2.5 py-1.5 text-left text-[0.8125rem] leading-snug text-zinc-700 transition-colors hover:border-zinc-200 hover:bg-zinc-50 disabled:cursor-not-allowed disabled:opacity-60 dark:text-zinc-300 dark:hover:border-zinc-700 dark:hover:bg-zinc-800/60"
                    >
                      <span className="min-w-0 flex-1">
                        <span className="line-clamp-3 break-words">{example.prompt}</span>
                        {example.followUp && (
                          <span className="mt-1 flex items-center gap-1 text-zinc-500 dark:text-zinc-400">
                            <CornerDownRight aria-hidden="true" className="size-3.5 shrink-0" />
                            then “{example.followUp}”
                          </span>
                        )}
                      </span>
                      <ArrowUpRight
                        aria-hidden="true"
                        className="mt-0.5 size-3.5 shrink-0 text-zinc-300 transition-colors group-hover:text-indigo-500 dark:text-zinc-600"
                      />
                    </button>
                  </li>
                ))}
              </ul>
            </section>
          );
        })}
      </div>

      <p className="mt-6 flex items-center gap-1.5 text-center text-xs text-zinc-400 dark:text-zinc-500">
        <ShieldCheck aria-hidden="true" className="size-3.5 shrink-0" />
        Every query passes 7 validation checks and runs on a read-only connection.
      </p>
    </div>
  );
}
