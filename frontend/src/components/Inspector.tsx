import clsx from 'clsx';
import { ChevronDown, Info, PanelRight, X } from 'lucide-react';
import { useMemo, useState } from 'react';
import { formatClockTime, formatDuration, formatNumber, pluralize } from '../lib/format';
import { ErrorCard, StoppedCard } from './ErrorCard';
import { CostPanel } from './inspector/CostPanel';
import { ExplanationPanel } from './inspector/ExplanationPanel';
import { OptimizationPanel } from './inspector/OptimizationPanel';
import { ResultsPanel } from './inspector/ResultsPanel';
import { SqlPanel } from './inspector/SqlPanel';
import { hasSqlDetails, type InspectorTab } from './inspector/tabs';
import { ValidationPanel } from './inspector/ValidationPanel';
import { Markdown } from './Markdown';
import { showIntentChip, STATUS_EXPLANATIONS } from './meta';
import { IntentChip, StatusBadge } from './StatusBadge';
import { buildStepRows, StepTracker } from './StepTracker';
import { Tabs, type TabItem } from './Tabs';
import { EmptyHint, IconButton } from './ui';
import type { AgentResponse, AssistantChatMessage } from '../types';

export interface InspectorProps {
  message: AssistantChatMessage | null;
  tab: InspectorTab;
  onTabChange: (tab: InspectorTab) => void;
  /** Shown on desktop, where the inspector is a closable side panel. */
  onClose?: () => void;
}

const DOT_TONES = { low: 'bg-emerald-500', medium: 'bg-amber-500', high: 'bg-red-500' } as const;

function tabItems(response: AgentResponse): TabItem<InspectorTab>[] {
  const { validation, execution, cost } = response;
  const passed = validation?.checks.filter((check) => check.passed).length ?? 0;
  return [
    { id: 'explanation', label: 'Explain' },
    {
      id: 'validation',
      label: 'Validation',
      badge: validation ? (
        <span
          className={clsx(
            'rounded-full px-1.5 text-[0.625rem] leading-4 font-semibold tabular-nums',
            validation.valid
              ? 'bg-emerald-50 text-emerald-700 dark:bg-emerald-400/10 dark:text-emerald-300'
              : 'bg-red-50 text-red-700 dark:bg-red-400/10 dark:text-red-300',
          )}
        >
          {passed}/{validation.checks.length}
        </span>
      ) : undefined,
    },
    { id: 'optimization', label: 'Optimize' },
    {
      id: 'results',
      label: 'Results',
      badge:
        execution && !execution.error ? (
          <span className="rounded-full bg-zinc-100 px-1.5 text-[0.625rem] leading-4 font-semibold text-zinc-500 tabular-nums dark:bg-zinc-800 dark:text-zinc-400">
            {formatNumber(execution.row_count)}
            {execution.truncated ? '+' : ''}
          </span>
        ) : undefined,
    },
    {
      id: 'cost',
      label: 'Cost',
      badge: cost ? (
        <span aria-hidden="true" className={clsx('size-1.5 rounded-full', DOT_TONES[cost.level] ?? 'bg-zinc-400')} />
      ) : undefined,
    },
  ];
}

function TabContent({ tab, response }: { tab: InspectorTab; response: AgentResponse }) {
  switch (tab) {
    case 'explanation':
      return <ExplanationPanel response={response} />;
    case 'validation':
      return <ValidationPanel validation={response.validation} />;
    case 'optimization':
      return <OptimizationPanel optimization={response.optimization} />;
    case 'results':
      return <ResultsPanel response={response} />;
    case 'cost':
      return <CostPanel cost={response.cost} />;
  }
}

/** Details of one assistant message: SQL, explanation, validation, optimization, results and cost. */
export function Inspector({ message, tab, onTabChange, onClose }: InspectorProps) {
  return (
    <div className="flex h-full min-h-0 flex-col bg-white dark:bg-zinc-900/40">
      <div className="flex h-12 shrink-0 items-center gap-2 border-b border-zinc-200 px-4 dark:border-zinc-800">
        <PanelRight aria-hidden="true" className="size-4 text-zinc-400" />
        <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Response details</h2>
        {message && (
          <span className="text-xs text-zinc-400 dark:text-zinc-500">{formatClockTime(message.created_at)}</span>
        )}
        {onClose && (
          <IconButton label="Close details" size="sm" className="ml-auto" onClick={onClose}>
            <X aria-hidden="true" className="size-4" />
          </IconButton>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {message ? (
          <InspectorBody message={message} tab={tab} onTabChange={onTabChange} />
        ) : (
          <EmptyHint icon={PanelRight} title="Nothing selected yet">
            Ask a question, then select <strong className="font-medium">Details</strong> on a response to inspect its
            SQL, validation, results and cost.
          </EmptyHint>
        )}
      </div>
    </div>
  );
}

function InspectorBody({
  message,
  tab,
  onTabChange,
}: {
  message: AssistantChatMessage;
  tab: InspectorTab;
  onTabChange: (tab: InspectorTab) => void;
}) {
  const response = message.response;
  const steps = response && response.steps.length > 0 ? response.steps : message.steps;
  const detailed = response !== null && hasSqlDetails(response);
  const trace = <PipelineTrace steps={steps} elapsedMs={message.elapsed_ms} defaultOpen={!detailed} />;

  if (!response) {
    return (
      <div className="space-y-4 p-4">
        {message.state === 'error' && message.error ? (
          <ErrorCard error={message.error} showAvatar={false} />
        ) : (
          <StoppedCard showAvatar={false} />
        )}
        {trace}
      </div>
    );
  }

  const explanation = STATUS_EXPLANATIONS[response.status];

  return (
    <div>
      <div className="space-y-4 p-4">
        <div className="flex flex-wrap items-center gap-2">
          <StatusBadge status={response.status} />
          {showIntentChip(response.status, response.intent) && <IntentChip intent={response.intent} />}
          {response.attempts > 1 && (
            <span className="text-xs text-amber-700 dark:text-amber-300">
              {pluralize(response.attempts, 'attempt')}
            </span>
          )}
          {response.model && (
            <span className="ml-auto truncate font-mono text-[0.6875rem] text-zinc-400 dark:text-zinc-500">
              {response.model}
            </span>
          )}
        </div>

        {!detailed && (
          <div className="space-y-3 rounded-xl border border-zinc-200 p-4 dark:border-zinc-800">
            {response.message ? (
              <Markdown>{response.message}</Markdown>
            ) : (
              <p className="text-sm text-zinc-500">This response has no SQL to inspect.</p>
            )}
          </div>
        )}

        {explanation && (
          <p className="flex gap-2 rounded-lg bg-zinc-50 px-3 py-2.5 text-[0.8125rem] leading-relaxed text-zinc-600 dark:bg-zinc-800/50 dark:text-zinc-400">
            <Info aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-zinc-400" />
            {explanation}
          </p>
        )}

        {response.sql && <SqlPanel key={message.id} response={response} />}
      </div>

      {detailed && (
        <>
          <div className="sticky top-0 z-10 border-y border-zinc-200 bg-white/95 backdrop-blur-sm dark:border-zinc-800 dark:bg-zinc-900/95">
            <Tabs
              items={tabItems(response)}
              value={tab}
              onChange={onTabChange}
              label="Response details"
              idPrefix="inspector"
              stretch
              className="px-2"
            />
          </div>
          <div
            role="tabpanel"
            id={`inspector-panel-${tab}`}
            aria-labelledby={`inspector-tab-${tab}`}
            tabIndex={0}
            className="p-4 focus-visible:outline-offset-[-2px]"
          >
            <TabContent tab={tab} response={response} />
          </div>
        </>
      )}

      <div className="px-4 pb-4">{trace}</div>
    </div>
  );
}

function PipelineTrace({
  steps,
  elapsedMs,
  defaultOpen,
}: {
  steps: AssistantChatMessage['steps'];
  elapsedMs: number | null;
  defaultOpen: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const rows = useMemo(() => buildStepRows(steps, true), [steps]);
  if (rows.length === 0) return null;

  return (
    <section className="rounded-xl border border-zinc-200 dark:border-zinc-800">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 rounded-xl px-4 py-3 text-left"
      >
        <span className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">Pipeline trace</span>
        <span className="text-xs text-zinc-500 dark:text-zinc-400">
          {pluralize(rows.length, 'step')}
          {elapsedMs !== null && ` · ${formatDuration(elapsedMs)}`}
        </span>
        <ChevronDown
          aria-hidden="true"
          className={clsx('ml-auto size-4 text-zinc-400 transition-transform', open && 'rotate-180')}
        />
      </button>
      {open && (
        <div className="border-t border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <StepTracker rows={rows} dense />
        </div>
      )}
    </section>
  );
}
