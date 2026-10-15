import clsx from 'clsx';
import { ArrowUp, Square } from 'lucide-react';
import {
  useCallback,
  useEffect,
  useImperativeHandle,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
  type Ref,
} from 'react';
import { MAX_INPUT_CHARS } from '../hooks/useChat';
import { Button, Kbd } from './ui';

export interface ComposerHandle {
  /** Insert text at the cursor (used by the schema explorer). */
  insertText: (text: string) => void;
  /** Replace the whole draft. */
  setText: (text: string) => void;
  focus: () => void;
}

export interface ComposerProps {
  ref?: Ref<ComposerHandle>;
  running: boolean;
  onSend: (text: string) => void;
  onStop: () => void;
  /** Extra controls shown in the input toolbar (dialect and execute on small screens). */
  toolbar?: ReactNode;
  /** Phone layout: shorter placeholder. */
  compact?: boolean;
}

const MAX_HEIGHT_PX = 220;
const COUNTER_WARNING_AT = MAX_INPUT_CHARS - 200;

/** Auto-growing message input. Enter sends, Shift+Enter inserts a new line. */
export function Composer({ ref, running, onSend, onStop, toolbar, compact = false }: ComposerProps) {
  const [value, setValue] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const caretRef = useRef<number | null>(null);

  useLayoutEffect(() => {
    const textarea = textareaRef.current;
    if (!textarea) return;
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, MAX_HEIGHT_PX)}px`;
    if (caretRef.current !== null) {
      textarea.focus();
      textarea.setSelectionRange(caretRef.current, caretRef.current);
      caretRef.current = null;
    }
  }, [value]);

  // Give focus back to the input when an answer finishes (desktop only: avoid popping mobile keyboards).
  const wasRunning = useRef(running);
  useEffect(() => {
    const finished = wasRunning.current && !running;
    wasRunning.current = running;
    if (!finished) return;
    const idle = !document.activeElement || document.activeElement === document.body;
    const finePointer = typeof window.matchMedia === 'function' && window.matchMedia('(pointer: fine)').matches;
    if (idle && finePointer) textareaRef.current?.focus();
  }, [running]);

  const insertText = useCallback(
    (text: string) => {
      const textarea = textareaRef.current;
      const start = textarea?.selectionStart ?? value.length;
      const end = textarea?.selectionEnd ?? value.length;
      const before = value.slice(0, start);
      const after = value.slice(end);
      const spaceBefore = before.length > 0 && !/[\s(]$/.test(before) ? ' ' : '';
      const spaceAfter = after.length > 0 && !/^[\s),.;]/.test(after) ? ' ' : '';
      const insertion = `${spaceBefore}${text}${spaceAfter}`;
      const next = `${before}${insertion}${after}`.slice(0, MAX_INPUT_CHARS);
      if (next === value) return; // input is full
      caretRef.current = Math.min(before.length + insertion.length, next.length);
      setValue(next);
    },
    [value],
  );

  useImperativeHandle(
    ref,
    () => ({
      insertText,
      setText: (text: string) => {
        const next = text.slice(0, MAX_INPUT_CHARS);
        if (next === value) {
          textareaRef.current?.focus();
          return;
        }
        caretRef.current = next.length;
        setValue(next);
      },
      focus: () => textareaRef.current?.focus(),
    }),
    [insertText, value],
  );

  const trimmed = value.trim();

  const submit = () => {
    if (!trimmed || running) return;
    onSend(trimmed);
    setValue('');
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  const count = value.length;

  return (
    <div className="@container shrink-0 px-3 pt-2 pb-3 sm:px-6 sm:pb-4">
      <div className="mx-auto max-w-3xl">
        <div
          className={clsx(
            'rounded-2xl border bg-white shadow-sm transition-[border-color,box-shadow] dark:bg-zinc-900',
            'border-zinc-200 focus-within:border-indigo-400 focus-within:ring-4 focus-within:ring-indigo-500/10',
            'dark:border-zinc-700/80 dark:focus-within:border-indigo-500/70 dark:focus-within:ring-indigo-400/10',
          )}
        >
          <label htmlFor="composer-input" className="sr-only">
            Message QueryPilot
          </label>
          <textarea
            id="composer-input"
            ref={textareaRef}
            rows={1}
            value={value}
            maxLength={MAX_INPUT_CHARS}
            disabled={running}
            onChange={(event) => setValue(event.target.value)}
            onKeyDown={onKeyDown}
            placeholder={
              running
                ? 'Waiting for the answer…'
                : compact
                  ? 'Ask a question or paste SQL…'
                  : 'Ask about the data, or paste SQL to optimize, debug or explain…'
            }
            aria-describedby="composer-hint composer-count"
            className="block max-h-[220px] w-full resize-none bg-transparent px-4 pt-3.5 pb-1.5 text-[0.9375rem] leading-relaxed text-zinc-900 placeholder:text-zinc-400 focus:outline-none focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-60 dark:text-zinc-100 dark:placeholder:text-zinc-500"
          />
          <div className="flex items-center gap-2 px-2.5 pb-2.5">
            {toolbar}
            <span
              id="composer-count"
              className={clsx(
                'ml-auto text-[0.6875rem] tabular-nums',
                count >= MAX_INPUT_CHARS
                  ? 'font-medium text-red-600 dark:text-red-400'
                  : count >= COUNTER_WARNING_AT
                    ? 'text-amber-600 dark:text-amber-400'
                    : 'text-zinc-400 dark:text-zinc-500',
              )}
            >
              <span className="sr-only">Characters used: </span>
              {count}/{MAX_INPUT_CHARS}
            </span>
            {running ? (
              <Button size="sm" onClick={onStop} aria-label="Stop generating">
                <Square aria-hidden="true" className="size-3 fill-current" />
                Stop
              </Button>
            ) : (
              <Button
                variant="primary"
                size="sm"
                className="w-8 px-0"
                onClick={submit}
                disabled={!trimmed}
                aria-label="Send message"
                title="Send (Enter)"
              >
                <ArrowUp aria-hidden="true" className="size-4" />
              </Button>
            )}
          </div>
        </div>
        <p
          id="composer-hint"
          className="mt-2 hidden items-center justify-center gap-1.5 text-[0.6875rem] whitespace-nowrap text-zinc-400 @md:flex dark:text-zinc-500"
        >
          <Kbd>Enter</Kbd> to send <span aria-hidden="true">·</span> <Kbd>Shift</Kbd>+<Kbd>Enter</Kbd> for a new line
          <span className="hidden @2xl:inline" aria-hidden="true">
            ·
          </span>
          <span className="hidden @2xl:inline">Read-only: QueryPilot never modifies data</span>
        </p>
      </div>
    </div>
  );
}
