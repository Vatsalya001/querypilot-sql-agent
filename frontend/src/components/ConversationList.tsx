import clsx from 'clsx';
import { MessageSquare, Trash } from 'lucide-react';
import { useState } from 'react';
import { useNow } from '../hooks/useNow';
import { formatRelativeTime } from '../lib/format';
import { Button, Spinner } from './ui';
import type { Conversation } from '../types';

export interface ConversationListProps {
  conversations: readonly Conversation[];
  activeId: string | null;
  runningId: string | null;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
}

/** Saved conversations, newest first, with an inline delete confirmation. */
export function ConversationList({ conversations, activeId, runningId, onSelect, onDelete }: ConversationListProps) {
  const now = useNow();
  const [confirmingId, setConfirmingId] = useState<string | null>(null);

  if (conversations.length === 0) {
    return (
      <p className="flex items-center gap-2 px-4 py-2 text-xs text-zinc-400 dark:text-zinc-500">
        <MessageSquare aria-hidden="true" className="size-3.5" />
        Your conversations will appear here.
      </p>
    );
  }

  return (
    <ul className="space-y-0.5 px-2" aria-label="Conversations">
      {conversations.map((conversation) => {
        const active = conversation.id === activeId;
        if (confirmingId === conversation.id) {
          return (
            <li
              key={conversation.id}
              className="flex items-center gap-1.5 rounded-lg bg-red-50 py-1.5 pr-1.5 pl-2.5 dark:bg-red-400/10"
            >
              <span className="min-w-0 flex-1 truncate text-xs font-medium text-red-700 dark:text-red-300">
                Delete this chat?
              </span>
              <Button
                size="xs"
                variant="danger"
                autoFocus
                onClick={() => {
                  setConfirmingId(null);
                  onDelete(conversation.id);
                }}
              >
                Delete
              </Button>
              <Button size="xs" variant="ghost" onClick={() => setConfirmingId(null)}>
                Cancel
              </Button>
            </li>
          );
        }
        return (
          <li
            key={conversation.id}
            className={clsx(
              'group flex items-center rounded-lg transition-colors',
              active
                ? 'bg-indigo-50 text-indigo-950 dark:bg-indigo-400/10 dark:text-indigo-100'
                : 'text-zinc-700 hover:bg-zinc-100 dark:text-zinc-300 dark:hover:bg-zinc-800/70',
            )}
          >
            <button
              type="button"
              onClick={() => onSelect(conversation.id)}
              aria-current={active ? 'page' : undefined}
              className="flex min-w-0 flex-1 flex-col items-start rounded-lg py-2 pr-1 pl-2.5 text-left"
            >
              <span className="w-full truncate text-[0.8125rem] font-medium">{conversation.title}</span>
              <span className="flex items-center gap-1 text-[0.6875rem] text-zinc-400 dark:text-zinc-500">
                {runningId === conversation.id && <Spinner className="size-3 text-indigo-500" />}
                {formatRelativeTime(conversation.updated_at, now)}
              </span>
            </button>
            <button
              type="button"
              onClick={() => setConfirmingId(conversation.id)}
              aria-label={`Delete chat: ${conversation.title}`}
              title="Delete chat"
              className="mr-1 flex size-7 shrink-0 items-center justify-center rounded-md text-zinc-400 opacity-0 transition hover:bg-zinc-200 hover:text-red-600 group-hover:opacity-100 focus-visible:opacity-100 pointer-coarse:opacity-100 dark:hover:bg-zinc-700 dark:hover:text-red-400"
            >
              <Trash aria-hidden="true" className="size-3.5" />
            </button>
          </li>
        );
      })}
    </ul>
  );
}
