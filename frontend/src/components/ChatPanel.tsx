import type { Ref } from 'react';
import type { PendingRequest } from '../hooks/useChat';
import type { ExamplePrompt } from '../lib/examples';
import { Composer, type ComposerHandle } from './Composer';
import { EmptyState } from './EmptyState';
import type { InspectorTab } from './inspector/tabs';
import { MessageList } from './MessageList';
import { QueryOptions, type QueryOptionsProps } from './QueryOptions';
import type { Conversation } from '../types';

export interface ChatPanelProps {
  composerRef: Ref<ComposerHandle>;
  conversation: Conversation | null;
  pending: PendingRequest | null;
  running: boolean;
  selectedId: string | null;
  /** Dialect and execute controls to show in the input toolbar (phones only). */
  toolbarOptions: QueryOptionsProps | null;
  onSend: (text: string) => void;
  onStop: () => void;
  onRetry: (messageId: string) => void;
  onOpenDetails: (messageId: string, tab?: InspectorTab) => void;
  onPickExample: (example: ExamplePrompt) => void;
}

/** The conversation (or the empty state) above the message input. */
export function ChatPanel({
  composerRef,
  conversation,
  pending,
  running,
  selectedId,
  toolbarOptions,
  onSend,
  onStop,
  onRetry,
  onOpenDetails,
  onPickExample,
}: ChatPanelProps) {
  const pendingHere = pending && conversation && pending.conversationId === conversation.id ? pending : null;
  const messages = conversation?.messages ?? [];
  const empty = messages.length === 0 && !pendingHere;

  return (
    <>
      {empty ? (
        <div className="@container min-h-0 flex-1 overflow-y-auto">
          <EmptyState onPick={onPickExample} disabled={running} />
        </div>
      ) : (
        <MessageList
          conversationId={conversation?.id ?? null}
          messages={messages}
          pending={pendingHere}
          selectedId={selectedId}
          busy={running}
          onOpenDetails={onOpenDetails}
          onRetry={onRetry}
        />
      )}
      <Composer
        ref={composerRef}
        running={running}
        onSend={onSend}
        onStop={onStop}
        compact={toolbarOptions !== null}
        toolbar={toolbarOptions ? <QueryOptions {...toolbarOptions} compact /> : undefined}
      />
    </>
  );
}
