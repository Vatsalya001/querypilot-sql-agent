import { SquarePen, X } from 'lucide-react';
import type { SchemaState } from '../hooks/useSchema';
import { ConversationList } from './ConversationList';
import { SchemaExplorer } from './SchemaExplorer';
import { Button, IconButton, SectionLabel } from './ui';
import type { Conversation } from '../types';

export interface SidebarProps {
  conversations: readonly Conversation[];
  activeId: string | null;
  runningId: string | null;
  onNewChat: () => void;
  onSelect: (id: string) => void;
  onDelete: (id: string) => void;
  schemaState: SchemaState;
  onReloadSchema: () => void;
  onInsert: (name: string) => void;
  /** Present in the mobile drawer. */
  onClose?: () => void;
}

/** New chat, conversation history and the schema explorer. */
export function Sidebar({
  conversations,
  activeId,
  runningId,
  onNewChat,
  onSelect,
  onDelete,
  schemaState,
  onReloadSchema,
  onInsert,
  onClose,
}: SidebarProps) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 p-3">
        <Button variant="primary" size="md" className="flex-1" onClick={onNewChat}>
          <SquarePen aria-hidden="true" className="size-4" />
          New chat
        </Button>
        {onClose && (
          <IconButton label="Close sidebar" onClick={onClose}>
            <X aria-hidden="true" className="size-4" />
          </IconButton>
        )}
      </div>

      <section aria-labelledby="history-heading" className="flex max-h-[42%] shrink-0 flex-col">
        <div className="flex items-center justify-between px-4 pt-1 pb-1.5">
          <SectionLabel>
            <span id="history-heading">History</span>
          </SectionLabel>
          {conversations.length > 0 && (
            <span className="text-[0.6875rem] text-zinc-400 tabular-nums dark:text-zinc-500">{conversations.length}</span>
          )}
        </div>
        <div className="min-h-0 overflow-y-auto pb-2">
          <ConversationList
            conversations={conversations}
            activeId={activeId}
            runningId={runningId}
            onSelect={onSelect}
            onDelete={onDelete}
          />
        </div>
      </section>

      <div className="mx-4 border-t border-zinc-200 dark:border-zinc-800" />

      <section aria-labelledby="schema-heading" className="flex min-h-0 flex-1 flex-col pt-3">
        <SchemaExplorer state={schemaState} onReload={onReloadSchema} onInsert={onInsert} />
      </section>
    </div>
  );
}
