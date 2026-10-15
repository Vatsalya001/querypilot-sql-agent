import { CircleStop, RotateCcw, TriangleAlert } from 'lucide-react';
import { AgentAvatar } from './Avatar';
import { Button } from './ui';
import type { ChatError } from '../types';

const ERROR_TITLES: Record<string, string> = {
  llm_not_configured: 'The language model is not available',
  rate_limited: 'Rate limit reached',
  llm_error: 'The language model returned an error',
  internal: 'Something went wrong on the server',
  network_error: 'Cannot reach the QueryPilot API',
  stream_interrupted: 'The connection was interrupted',
  validation_error: 'The request was rejected',
};

export interface ErrorCardProps {
  error: ChatError;
  onRetry?: () => void;
  retryDisabled?: boolean;
  /** Show the agent avatar (in the transcript, not in the inspector). */
  showAvatar?: boolean;
}

/** Inline card for a failed request, with the server's message and a Retry action. */
export function ErrorCard({ error, onRetry, retryDisabled, showAvatar = true }: ErrorCardProps) {
  return (
    <article className="flex gap-3 motion-safe:animate-slide-up" aria-label="Error">
      {showAvatar && <AgentAvatar />}
      <div
        role="alert"
        className="min-w-0 flex-1 rounded-xl border border-red-200 bg-red-50/80 p-4 dark:border-red-400/25 dark:bg-red-400/10"
      >
        <div className="flex items-start gap-3">
          <TriangleAlert aria-hidden="true" className="mt-0.5 size-5 shrink-0 text-red-500 dark:text-red-400" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-semibold text-red-800 dark:text-red-200">
              {ERROR_TITLES[error.code] ?? 'The request failed'}
            </p>
            <p className="mt-1 text-sm break-words text-red-700 dark:text-red-300/90">{error.message}</p>
            {error.retry_after !== null && (
              <p className="mt-1 text-xs text-red-600/80 dark:text-red-300/70">
                You can try again in about {error.retry_after} s.
              </p>
            )}
            <div className="mt-3 flex flex-wrap items-center gap-2">
              {onRetry && (
                <Button size="xs" onClick={onRetry} disabled={retryDisabled}>
                  <RotateCcw aria-hidden="true" className="size-3.5" />
                  Retry
                </Button>
              )}
              <code className="rounded bg-red-100/80 px-1.5 py-0.5 font-mono text-[0.6875rem] text-red-700 dark:bg-red-400/10 dark:text-red-300">
                {error.code}
              </code>
            </div>
          </div>
        </div>
      </div>
    </article>
  );
}

/** Placeholder for a request the user stopped. */
export function StoppedCard({
  onRetry,
  retryDisabled,
  showAvatar = true,
}: {
  onRetry?: () => void;
  retryDisabled?: boolean;
  showAvatar?: boolean;
}) {
  return (
    <article className="flex gap-3" aria-label="Stopped response">
      {showAvatar && <AgentAvatar />}
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-3 rounded-xl border border-dashed border-zinc-300 px-4 py-3 text-sm text-zinc-500 dark:border-zinc-700 dark:text-zinc-400">
        <CircleStop aria-hidden="true" className="size-4 shrink-0" />
        <span className="flex-1">You stopped this response.</span>
        {onRetry && (
          <Button size="xs" onClick={onRetry} disabled={retryDisabled}>
            <RotateCcw aria-hidden="true" className="size-3.5" />
            Retry
          </Button>
        )}
      </div>
    </article>
  );
}
