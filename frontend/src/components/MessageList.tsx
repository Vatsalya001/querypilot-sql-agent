import { ArrowDown } from 'lucide-react';
import { useCallback, useLayoutEffect, useRef, useState } from 'react';
import type { PendingRequest } from '../hooks/useChat';
import { AssistantMessage } from './AssistantMessage';
import type { InspectorTab } from './inspector/tabs';
import { PendingMessage } from './PendingMessage';
import { UserMessage } from './UserMessage';
import type { ChatMessage } from '../types';

/** Within this distance from the bottom the list keeps following new content. */
const STICK_THRESHOLD_PX = 96;

export interface MessageListProps {
  conversationId: string | null;
  messages: readonly ChatMessage[];
  pending: PendingRequest | null;
  selectedId: string | null;
  busy: boolean;
  onOpenDetails: (messageId: string, tab?: InspectorTab) => void;
  onRetry: (messageId: string) => void;
}

/** The scrollable transcript. Follows new content while the user is at the bottom. */
export function MessageList({
  conversationId,
  messages,
  pending,
  selectedId,
  busy,
  onOpenDetails,
  onRetry,
}: MessageListProps) {
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickRef = useRef(true);
  const lastScrollTop = useRef(0);
  const [showJump, setShowJump] = useState(false);

  const scrollToBottom = useCallback(() => {
    const element = scrollRef.current;
    if (!element) return;
    element.scrollTop = element.scrollHeight;
    lastScrollTop.current = element.scrollTop;
    stickRef.current = true;
    setShowJump(false);
  }, []);

  // Only an upward scroll by the user stops following; content growing below never does.
  const onScroll = () => {
    const element = scrollRef.current;
    if (!element) return;
    const distance = element.scrollHeight - element.scrollTop - element.clientHeight;
    if (distance < STICK_THRESHOLD_PX) stickRef.current = true;
    else if (element.scrollTop < lastScrollTop.current) stickRef.current = false;
    lastScrollTop.current = element.scrollTop;
    setShowJump(!stickRef.current);
  };

  // Layout effects run before paint, so new content never flashes above the fold.
  useLayoutEffect(() => {
    scrollToBottom();
  }, [conversationId, scrollToBottom]);

  // A new user message always scrolls into view; other updates only while following.
  const lastMessage = messages.at(-1);
  useLayoutEffect(() => {
    if (lastMessage?.role === 'user') scrollToBottom();
  }, [lastMessage, scrollToBottom]);

  useLayoutEffect(() => {
    if (stickRef.current) scrollToBottom();
  }, [messages.length, pending?.steps.length, pending?.explanation, pending?.message, scrollToBottom]);

  return (
    <div className="relative min-h-0 flex-1">
      <div
        ref={scrollRef}
        onScroll={onScroll}
        className="h-full overflow-y-auto overscroll-contain"
        role="log"
        aria-label="Conversation"
        aria-live="polite"
        aria-relevant="additions"
      >
        <div className="mx-auto max-w-3xl space-y-6 px-3 py-6 sm:px-6">
          {messages.map((message) =>
            message.role === 'user' ? (
              <UserMessage key={message.id} message={message} />
            ) : (
              <AssistantMessage
                key={message.id}
                message={message}
                selected={message.id === selectedId}
                busy={busy}
                onOpenDetails={onOpenDetails}
                onRetry={onRetry}
              />
            ),
          )}
          {pending && <PendingMessage pending={pending} />}
        </div>
      </div>
      {showJump && (
        <button
          type="button"
          onClick={scrollToBottom}
          aria-label="Scroll to the latest message"
          className="absolute bottom-3 left-1/2 flex size-9 -translate-x-1/2 items-center justify-center rounded-full border border-zinc-200 bg-white text-zinc-600 shadow-md transition-colors hover:text-zinc-900 motion-safe:animate-fade-in dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-300 dark:hover:text-white"
        >
          <ArrowDown aria-hidden="true" className="size-4" />
        </button>
      )}
    </div>
  );
}
