import clsx from 'clsx';
import { ChevronRight, ExternalLink, Repeat, RotateCcw } from 'lucide-react';
import { useEffect, useState } from 'react';
import { getGraph, isAbortError } from '../lib/api';
import { PHASE_TITLES, PIPELINE_NODES, type PipelinePhase } from '../lib/pipeline';
import { CopyButton } from './CopyButton';
import { NODE_ICONS } from './meta';
import { Modal } from './Modal';
import { Button, SectionLabel, Spinner } from './ui';
import type { GraphResponse } from '../types';

type GraphState =
  | { status: 'idle' | 'loading' }
  | { status: 'ready'; graph: GraphResponse }
  | { status: 'error'; error: string };

const PHASES: readonly PipelinePhase[] = ['safety', 'sql', 'outcome', 'response'];

const PHASE_ACCENTS: Record<PipelinePhase, string> = {
  safety: 'text-emerald-600 dark:text-emerald-400',
  sql: 'text-indigo-600 dark:text-indigo-400',
  outcome: 'text-amber-600 dark:text-amber-400',
  response: 'text-sky-600 dark:text-sky-400',
};

/** A mermaid.live link that opens the diagram source in the online editor. */
function mermaidLiveUrl(source: string): string {
  const state = JSON.stringify({ code: source, mermaid: JSON.stringify({ theme: 'default' }) });
  const bytes = new TextEncoder().encode(state);
  let binary = '';
  bytes.forEach((byte) => {
    binary += String.fromCharCode(byte);
  });
  return `https://mermaid.live/edit#base64:${btoa(binary)}`;
}

export function HowItWorksModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [graph, setGraph] = useState<GraphState>({ status: 'idle' });
  const [attempt, setAttempt] = useState(0);
  const loaded = graph.status === 'ready';

  useEffect(() => {
    if (!open || loaded) return;
    const controller = new AbortController();
    setGraph({ status: 'loading' });
    getGraph(controller.signal)
      .then((data) => setGraph({ status: 'ready', graph: data }))
      .catch((error: unknown) => {
        if (isAbortError(error)) return;
        setGraph({ status: 'error', error: error instanceof Error ? error.message : 'Could not load the graph.' });
      });
    return () => controller.abort();
  }, [open, loaded, attempt]);

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="How QueryPilot works"
      description="Every message runs through a LangGraph state machine with deterministic guardrails around the LLM."
    >
      <div className="space-y-6">
        <div className="grid gap-2 text-[0.8125rem] text-zinc-600 sm:grid-cols-3 dark:text-zinc-400">
          {[
            ['Grounded', 'Prompts include only the real schema; unknown tables and columns are rejected.'],
            ['Validated', 'Seven deterministic checks run before any SQL is shown or executed.'],
            ['Self-correcting', 'Failed checks are fed back to the model, which repairs its query.'],
          ].map(([title, text]) => (
            <div key={title} className="rounded-lg bg-zinc-50 p-3 dark:bg-zinc-800/50">
              <p className="font-semibold text-zinc-900 dark:text-zinc-100">{title}</p>
              <p className="mt-0.5 leading-relaxed">{text}</p>
            </div>
          ))}
        </div>

        {PHASES.map((phase) => (
          <section key={phase}>
            <SectionLabel className={clsx('mb-3', PHASE_ACCENTS[phase])}>{PHASE_TITLES[phase]}</SectionLabel>
            <ol className="space-y-3">
              {PIPELINE_NODES.filter((node) => node.phase === phase).map((node) => {
                const Icon = NODE_ICONS[node.node];
                return (
                  <li key={node.node} className="flex gap-3">
                    <span className="flex size-8 shrink-0 items-center justify-center rounded-lg border border-zinc-200 bg-white text-zinc-500 shadow-xs dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-300">
                      {Icon && <Icon aria-hidden="true" className="size-4" />}
                    </span>
                    <div className="min-w-0">
                      <p className="flex flex-wrap items-baseline gap-x-2 text-sm font-medium text-zinc-900 dark:text-zinc-100">
                        {node.label}
                        <code className="font-mono text-[0.6875rem] font-normal text-zinc-400 dark:text-zinc-500">
                          {node.node}
                        </code>
                      </p>
                      <p className="text-[0.8125rem] leading-relaxed text-zinc-600 dark:text-zinc-400">
                        {node.description}
                      </p>
                    </div>
                  </li>
                );
              })}
            </ol>
            {phase === 'sql' && (
              <p className="mt-3 flex items-center gap-2 rounded-lg border border-dashed border-amber-300 px-3 py-2 text-xs text-amber-800 dark:border-amber-400/30 dark:text-amber-300">
                <Repeat aria-hidden="true" className="size-3.5 shrink-0" />
                generate_sql and validate_sql form a repair loop: validator errors go back to the model until the query
                passes or the attempts run out.
              </p>
            )}
          </section>
        ))}

        <details className="group rounded-xl border border-zinc-200 dark:border-zinc-800">
          <summary className="flex list-none items-center gap-2 px-4 py-3 text-sm font-medium text-zinc-900 [&::-webkit-details-marker]:hidden dark:text-zinc-100">
            <ChevronRight aria-hidden="true" className="size-4 text-zinc-400 transition-transform group-open:rotate-90" />
            Mermaid source of the compiled graph
            <code className="ml-auto font-mono text-[0.6875rem] font-normal text-zinc-400">GET /api/graph</code>
          </summary>
          <div className="border-t border-zinc-200 p-4 dark:border-zinc-800">
            {(graph.status === 'idle' || graph.status === 'loading') && (
              <p className="flex items-center gap-2 text-sm text-zinc-500">
                <Spinner className="size-4" /> Loading the graph…
              </p>
            )}
            {graph.status === 'error' && (
              <div className="flex flex-wrap items-center gap-3 text-sm text-red-700 dark:text-red-300">
                <span className="min-w-0 flex-1 break-words">{graph.error}</span>
                <Button size="xs" onClick={() => setAttempt((value) => value + 1)}>
                  <RotateCcw aria-hidden="true" className="size-3.5" />
                  Retry
                </Button>
              </div>
            )}
            {graph.status === 'ready' && (
              <>
                <div className="mb-2 flex flex-wrap items-center gap-2">
                  <span className="text-xs text-zinc-500 dark:text-zinc-400">
                    {graph.graph.nodes.length} nodes
                  </span>
                  <div className="ml-auto flex items-center gap-1.5">
                    <CopyButton text={graph.graph.mermaid} label="Copy Mermaid source" />
                    <a
                      href={mermaidLiveUrl(graph.graph.mermaid)}
                      target="_blank"
                      rel="noreferrer noopener"
                      className="inline-flex h-7 items-center gap-1 rounded-md border border-zinc-200 bg-white px-2 text-xs font-medium text-zinc-700 shadow-xs hover:bg-zinc-50 dark:border-zinc-700/80 dark:bg-zinc-900 dark:text-zinc-200 dark:hover:bg-zinc-800"
                    >
                      <ExternalLink aria-hidden="true" className="size-3.5" />
                      Mermaid Live
                    </a>
                  </div>
                </div>
                <pre className="max-h-72 overflow-auto rounded-lg bg-zinc-50 p-3 font-mono text-xs leading-relaxed text-zinc-700 dark:bg-zinc-950/60 dark:text-zinc-300">
                  {graph.graph.mermaid}
                </pre>
              </>
            )}
          </div>
        </details>
      </div>
    </Modal>
  );
}
