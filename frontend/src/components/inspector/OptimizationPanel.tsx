import { Gauge, Unlink } from 'lucide-react';
import { CopyButton } from '../CopyButton';
import { SEVERITY_TONES } from '../meta';
import { Badge, EmptyHint, SectionLabel } from '../ui';
import type { OptimizationResult, OptimizationSuggestion } from '../../types';

function titleCase(text: string): string {
  return text.replace(/[_-]+/g, ' ').replace(/^\w/, (char) => char.toUpperCase());
}

function groupByCategory(suggestions: readonly OptimizationSuggestion[]): [string, OptimizationSuggestion[]][] {
  const groups = new Map<string, OptimizationSuggestion[]>();
  for (const suggestion of suggestions) {
    const key = suggestion.category || 'general';
    groups.set(key, [...(groups.get(key) ?? []), suggestion]);
  }
  return [...groups.entries()];
}

/** Rule-based optimizer output: suggestions by category, index recommendations, removable joins. */
export function OptimizationPanel({ optimization }: { optimization: OptimizationResult | null }) {
  const hasContent =
    optimization &&
    (optimization.suggestions.length > 0 ||
      optimization.index_recommendations.length > 0 ||
      optimization.unnecessary_joins.length > 0);

  if (!optimization || !hasContent) {
    return (
      <EmptyHint icon={Gauge} title={optimization ? 'Nothing to optimize' : 'No optimization for this response'}>
        {optimization
          ? 'The optimizer found no readability, index or join improvements for this query.'
          : 'Optimization runs on validated SQL.'}
      </EmptyHint>
    );
  }

  return (
    <div className="space-y-5">
      {groupByCategory(optimization.suggestions).map(([category, items]) => (
        <section key={category}>
          <SectionLabel className="mb-2">{titleCase(category)}</SectionLabel>
          <ul className="space-y-2">
            {items.map((item, index) => (
              <li
                key={`${index}-${item.message}`}
                className="flex items-start gap-2.5 rounded-lg border border-zinc-200 px-3 py-2.5 dark:border-zinc-800"
              >
                <Badge tone={SEVERITY_TONES[item.severity] ?? 'neutral'} className="mt-px">
                  {item.severity}
                </Badge>
                <span className="min-w-0 text-[0.8125rem] leading-relaxed break-words text-zinc-700 dark:text-zinc-300">
                  {item.message}
                </span>
              </li>
            ))}
          </ul>
        </section>
      ))}

      {optimization.index_recommendations.length > 0 && (
        <section>
          <SectionLabel className="mb-2">Recommended indexes</SectionLabel>
          <ul className="space-y-2">
            {optimization.index_recommendations.map((statement) => (
              <li
                key={statement}
                className="flex items-start gap-2 rounded-lg border border-zinc-200 bg-zinc-50/70 py-1.5 pr-1.5 pl-3 dark:border-zinc-800 dark:bg-zinc-950/40"
              >
                <code className="min-w-0 flex-1 py-1 font-mono text-xs leading-relaxed break-words text-zinc-700 dark:text-zinc-300">
                  {statement}
                </code>
                <CopyButton text={statement} label="Copy index statement" iconOnly variant="ghost" />
              </li>
            ))}
          </ul>
          <p className="mt-2 text-xs text-zinc-400 dark:text-zinc-500">
            Suggestions only: QueryPilot never changes the database.
          </p>
        </section>
      )}

      {optimization.unnecessary_joins.length > 0 && (
        <section>
          <SectionLabel className="mb-2">Unnecessary joins</SectionLabel>
          <ul className="space-y-1.5">
            {optimization.unnecessary_joins.map((join) => (
              <li key={join} className="flex gap-2 text-[0.8125rem] text-zinc-700 dark:text-zinc-300">
                <Unlink aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-amber-500" />
                <span className="min-w-0 break-words">{join}</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
