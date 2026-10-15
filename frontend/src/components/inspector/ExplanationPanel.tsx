import { Bug, CircleCheck, CircleQuestionMark, Info, Lightbulb } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Markdown } from '../Markdown';
import { EmptyHint, SectionLabel } from '../ui';
import type { AgentResponse } from '../../types';

function BulletList({ items, icon: Icon, iconClass }: { items: string[]; icon: LucideIcon; iconClass: string }) {
  return (
    <ul className="space-y-1.5">
      {items.map((item, index) => (
        <li key={`${index}-${item}`} className="flex gap-2 text-[0.8125rem] leading-relaxed text-zinc-700 dark:text-zinc-300">
          <Icon aria-hidden="true" className={`mt-0.5 size-4 shrink-0 ${iconClass}`} />
          <span className="min-w-0 break-words">{item}</span>
        </li>
      ))}
    </ul>
  );
}

/** Plain-English explanation plus debug issues, optimization changes, suggestions and assumptions. */
export function ExplanationPanel({ response }: { response: AgentResponse }) {
  const { explanation, issues, changes, suggestions, assumptions, clarification_question } = response;
  const empty =
    !explanation && !issues.length && !changes.length && !suggestions.length && !assumptions.length && !clarification_question;
  if (empty) {
    return (
      <EmptyHint icon={Info} title="No explanation for this response">
        Explanations are generated for validated SQL.
      </EmptyHint>
    );
  }

  return (
    <div className="space-y-5">
      {explanation && (
        <section>
          <SectionLabel className="mb-2">What this query does</SectionLabel>
          <Markdown>{explanation}</Markdown>
        </section>
      )}

      {clarification_question && (
        <section>
          <SectionLabel className="mb-2">Clarifying question</SectionLabel>
          <BulletList items={[clarification_question]} icon={CircleQuestionMark} iconClass="text-amber-500" />
        </section>
      )}

      {issues.length > 0 && (
        <section>
          <SectionLabel className="mb-2">Issues found</SectionLabel>
          <ol className="space-y-2">
            {issues.map((issue, index) => (
              <li
                key={`${index}-${issue.problem}`}
                className="rounded-lg border border-zinc-200 p-3 dark:border-zinc-800"
              >
                <p className="flex gap-2 text-[0.8125rem] font-medium text-zinc-900 dark:text-zinc-100">
                  <Bug aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-violet-500" />
                  <span className="min-w-0 break-words">{issue.problem}</span>
                </p>
                <p className="mt-1 pl-6 text-[0.8125rem] leading-relaxed break-words text-zinc-600 dark:text-zinc-400">
                  {issue.reason}
                </p>
              </li>
            ))}
          </ol>
        </section>
      )}

      {changes.length > 0 && (
        <section>
          <SectionLabel className="mb-2">Changes made</SectionLabel>
          <BulletList items={changes} icon={CircleCheck} iconClass="text-emerald-500" />
        </section>
      )}

      {suggestions.length > 0 && (
        <section>
          <SectionLabel className="mb-2">Suggestions</SectionLabel>
          <BulletList items={suggestions} icon={Lightbulb} iconClass="text-amber-500" />
        </section>
      )}

      {assumptions.length > 0 && (
        <section>
          <SectionLabel className="mb-2">Assumptions</SectionLabel>
          <BulletList items={assumptions} icon={Info} iconClass="text-sky-500" />
        </section>
      )}
    </div>
  );
}
