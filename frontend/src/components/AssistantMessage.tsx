import clsx from 'clsx';
import {
  Bug,
  ChevronDown,
  CircleQuestionMark,
  Clock,
  Gauge,
  PanelRightOpen,
  ShieldCheck,
  ShieldX,
  Table2,
  TriangleAlert,
  Wrench,
} from 'lucide-react';
import { useMemo, useState, type ReactNode } from 'react';
import { dialectLabel, formatClockTime, formatDuration, pluralize } from '../lib/format';
import { AgentAvatar } from './Avatar';
import { CopyButton } from './CopyButton';
import { ErrorCard, StoppedCard } from './ErrorCard';
import { Markdown } from './Markdown';
import { COST_TONES, showIntentChip } from './meta';
import { SqlBlock } from './SqlBlock';
import { IntentChip, StatusBadge } from './StatusBadge';
import { buildStepRows, StepTracker } from './StepTracker';
import { TONE_CLASSES, Button, SectionLabel, type Tone } from './ui';
import type { InspectorTab } from './inspector/tabs';
import { hasSqlDetails } from './inspector/tabs';
import type { AgentResponse, AssistantChatMessage } from '../types';

export interface AssistantMessageProps {
  message: AssistantChatMessage;
  selected: boolean;
  /** Disable Retry while another request runs. */
  busy: boolean;
  onOpenDetails: (messageId: string, tab?: InspectorTab) => void;
  onRetry: (messageId: string) => void;
}

export function AssistantMessage({ message, selected, busy, onOpenDetails, onRetry }: AssistantMessageProps) {
  if (message.state === 'error' && message.error) {
    return <ErrorCard error={message.error} onRetry={() => onRetry(message.id)} retryDisabled={busy} />;
  }
  if (message.state === 'stopped' || !message.response) {
    return <StoppedCard onRetry={() => onRetry(message.id)} retryDisabled={busy} />;
  }
  return (
    <ResponseCard
      message={message}
      response={message.response}
      selected={selected}
      onOpenDetails={(tab) => onOpenDetails(message.id, tab)}
    />
  );
}

interface ResponseCardProps {
  message: AssistantChatMessage;
  response: AgentResponse;
  selected: boolean;
  onOpenDetails: (tab?: InspectorTab) => void;
}

function ResponseCard({ message, response, selected, onOpenDetails }: ResponseCardProps) {
  const [showTrace, setShowTrace] = useState(false);
  const steps = response.steps.length > 0 ? response.steps : message.steps;
  const rows = useMemo(() => buildStepRows(steps, true), [steps]);
  const failedErrors = response.status === 'failed' ? (response.validation?.errors ?? []) : [];
  const clarification =
    response.status === 'clarification' &&
    response.clarification_question &&
    !response.message.includes(response.clarification_question)
      ? response.clarification_question
      : null;
  const detailsAvailable = hasSqlDetails(response) || rows.length > 0;

  return (
    <article className="flex gap-3 motion-safe:animate-slide-up" aria-label="Assistant response">
      <AgentAvatar />
      <div
        className={clsx(
          'min-w-0 flex-1 overflow-hidden rounded-xl border bg-white shadow-xs transition-shadow dark:bg-zinc-900',
          selected
            ? 'border-indigo-300 ring-4 ring-indigo-500/10 dark:border-indigo-500/50 dark:ring-indigo-400/10'
            : 'border-zinc-200 dark:border-zinc-800',
        )}
      >
        <header className="flex flex-wrap items-center gap-2 px-4 pt-3.5">
          <StatusBadge status={response.status} />
          {showIntentChip(response.status, response.intent) && <IntentChip intent={response.intent} />}
          <span className="ml-auto text-[0.6875rem] text-zinc-400 dark:text-zinc-500">
            {formatClockTime(message.created_at)}
          </span>
        </header>

        <div className="space-y-3.5 px-4 pt-2.5 pb-4">
          {response.message && <Markdown>{response.message}</Markdown>}

          {clarification && (
            <div className="flex gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-sm text-amber-900 dark:border-amber-400/25 dark:bg-amber-400/10 dark:text-amber-200">
              <CircleQuestionMark aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
              <span>{clarification}</span>
            </div>
          )}

          {failedErrors.length > 0 && (
            <ul className="space-y-1 rounded-lg border border-red-200 bg-red-50/70 px-3 py-2 text-[0.8125rem] text-red-800 dark:border-red-400/25 dark:bg-red-400/10 dark:text-red-200">
              {failedErrors.map((error) => (
                <li key={error} className="flex gap-2">
                  <TriangleAlert aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
                  <span className="break-words">{error}</span>
                </li>
              ))}
            </ul>
          )}

          {response.sql && <SqlPreview sql={response.sql} dialect={response.dialect} />}

          {response.explanation && (
            <section aria-label="Explanation">
              <SectionLabel className="mb-1.5">Explanation</SectionLabel>
              <Markdown>{response.explanation}</Markdown>
            </section>
          )}
        </div>

        <footer className="flex flex-wrap items-center gap-1.5 border-t border-zinc-100 bg-zinc-50/60 px-3 py-2 dark:border-zinc-800 dark:bg-zinc-900/60">
          <ResponseChips response={response} onOpenDetails={onOpenDetails} />
          {rows.length > 0 && (
            <button
              type="button"
              onClick={() => setShowTrace((value) => !value)}
              aria-expanded={showTrace}
              className="inline-flex items-center gap-1 rounded-md px-1.5 py-1 text-xs text-zinc-500 hover:bg-zinc-100 hover:text-zinc-800 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-200"
            >
              <Clock aria-hidden="true" className="size-3.5" />
              {pluralize(rows.length, 'step')}
              {message.elapsed_ms !== null && <span>· {formatDuration(message.elapsed_ms)}</span>}
              <ChevronDown
                aria-hidden="true"
                className={clsx('size-3.5 transition-transform', showTrace && 'rotate-180')}
              />
            </button>
          )}
          {detailsAvailable && (
            <Button size="xs" variant="ghost" className="ml-auto" onClick={() => onOpenDetails()}>
              <PanelRightOpen aria-hidden="true" className="size-3.5" />
              Details
            </Button>
          )}
        </footer>

        {showTrace && (
          <div className="border-t border-zinc-100 px-4 py-3 dark:border-zinc-800">
            <StepTracker rows={rows} dense />
          </div>
        )}
      </div>
    </article>
  );
}

function SqlPreview({ sql, dialect }: { sql: string; dialect: string }) {
  return (
    <section
      aria-label="Generated SQL"
      className="overflow-hidden rounded-lg border border-zinc-200 bg-zinc-50/70 dark:border-zinc-800 dark:bg-zinc-950/60"
    >
      <div className="flex items-center gap-2 border-b border-zinc-200 px-3 py-1.5 dark:border-zinc-800">
        <span className="text-[0.6875rem] font-semibold tracking-wider text-zinc-500 uppercase dark:text-zinc-400">
          SQL
        </span>
        <span className="text-[0.6875rem] text-zinc-400 dark:text-zinc-500">{dialectLabel(dialect)}</span>
        <CopyButton text={sql} label="Copy SQL" variant="ghost" className="ml-auto" />
      </div>
      <SqlBlock sql={sql} className="max-h-64" />
    </section>
  );
}

function Chip({
  tone = 'neutral',
  icon: Icon,
  onClick,
  title,
  children,
}: {
  tone?: Tone;
  icon: typeof Table2;
  onClick: () => void;
  title: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className={clsx(
        'inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium whitespace-nowrap transition-colors hover:brightness-95 dark:hover:brightness-125',
        TONE_CLASSES[tone],
      )}
    >
      <Icon aria-hidden="true" className="size-3.5" />
      {children}
    </button>
  );
}

function ResponseChips({
  response,
  onOpenDetails,
}: {
  response: AgentResponse;
  onOpenDetails: (tab?: InspectorTab) => void;
}) {
  const { validation, execution, cost, attempts, issues } = response;
  return (
    <>
      {validation && (
        <Chip
          tone={validation.valid ? 'green' : 'red'}
          icon={validation.valid ? ShieldCheck : ShieldX}
          onClick={() => onOpenDetails('validation')}
          title="Show the validation checks"
        >
          {validation.valid ? 'Validated' : 'Validation failed'}
        </Chip>
      )}
      {execution &&
        (execution.error ? (
          <Chip tone="red" icon={TriangleAlert} onClick={() => onOpenDetails('results')} title="Show the error">
            Execution error
          </Chip>
        ) : (
          <Chip icon={Table2} onClick={() => onOpenDetails('results')} title="Show the result rows">
            {pluralize(execution.row_count, 'row')}
            {execution.truncated ? '+' : ''}
          </Chip>
        ))}
      {cost && (
        <Chip
          tone={COST_TONES[cost.level] ?? 'neutral'}
          icon={Gauge}
          onClick={() => onOpenDetails('cost')}
          title="Show the cost estimate"
        >
          {cost.level.charAt(0).toUpperCase() + cost.level.slice(1)} cost
        </Chip>
      )}
      {issues.length > 0 && (
        <Chip tone="violet" icon={Bug} onClick={() => onOpenDetails('explanation')} title="Show the issues found">
          {pluralize(issues.length, 'issue')} found
        </Chip>
      )}
      {attempts > 1 &&
        (response.status === 'success' ? (
          <Chip tone="amber" icon={Wrench} onClick={() => onOpenDetails('validation')} title="The validator caught a problem and the agent repaired it">
            Repaired after {attempts} attempts
          </Chip>
        ) : (
          <Chip icon={Wrench} onClick={() => onOpenDetails('validation')} title="The agent generated SQL several times, but no attempt produced a validated query">
            {attempts} attempts
          </Chip>
        ))}
    </>
  );
}
