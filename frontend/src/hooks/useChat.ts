import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, deleteThread, isAbortError, isNetworkError, streamChat } from '../lib/api';
import { truncate } from '../lib/format';
import { buildHistory } from '../lib/history';
import { createId } from '../lib/ids';
import {
  loadActiveConversationId,
  loadConversations,
  saveActiveConversationId,
  saveConversations,
  settleInterruptedRequests,
} from '../lib/storage';
import type {
  AgentResponse,
  AssistantChatMessage,
  ChatError,
  Conversation,
  Dialect,
  StepEvent,
  UserChatMessage,
} from '../types';

/** Maximum message length accepted by the API. */
export const MAX_INPUT_CHARS = 2000;

const NETWORK_ERROR_MESSAGE = 'Could not reach the QueryPilot API. Check that the backend is running, then retry.';

/** The request currently streaming (never persisted). */
export interface PendingRequest {
  conversationId: string;
  prompt: string;
  startedAt: number;
  steps: StepEvent[];
  /** Explanation text streamed so far by `explain_sql`. */
  explanation: string;
  /** Answer text streamed so far by `answer_question`. */
  message: string;
}

export interface UseChatOptions {
  dialect: Dialect;
  execute: boolean;
  /** Called when a request fails because the API could not be reached. */
  onNetworkError?: (message: string) => void;
}

export interface ChatController {
  /** Newest first. */
  conversations: Conversation[];
  activeConversation: Conversation | null;
  pending: PendingRequest | null;
  isRunning: boolean;
  /** Send a message in the active conversation; resolves with the settled assistant message. */
  send: (text: string) => Promise<AssistantChatMessage | null>;
  stop: () => void;
  /** Re-send the prompt of a failed or stopped assistant message. */
  retry: (assistantMessageId: string) => void;
  newChat: () => void;
  selectConversation: (id: string) => void;
  deleteConversation: (id: string) => void;
}

function toChatError(error: unknown): ChatError {
  if (error instanceof ApiError) {
    return { message: error.message, code: error.code, retry_after: error.retryAfter };
  }
  if (isNetworkError(error)) {
    return { message: NETWORK_ERROR_MESSAGE, code: 'network_error', retry_after: null };
  }
  return {
    message: error instanceof Error && error.message ? error.message : 'Something went wrong. Please try again.',
    code: 'client_error',
    retry_after: null,
  };
}

/** Conversations, the streaming request and the actions that drive them. */
export function useChat({ dialect, execute, onNetworkError }: UseChatOptions): ChatController {
  // Nothing is in flight on startup, so a saved question without an answer was cut off by a reload.
  const [conversations, setConversations] = useState<Conversation[]>(() =>
    settleInterruptedRequests(loadConversations()),
  );
  const [activeId, setActiveId] = useState<string | null>(loadActiveConversationId);
  const [pending, setPending] = useState<PendingRequest | null>(null);

  const conversationsRef = useRef(conversations);
  const activeIdRef = useRef(activeId);
  const runningRef = useRef(false);
  const runningConversationRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const optionsRef = useRef({ dialect, execute, onNetworkError });

  useEffect(() => {
    conversationsRef.current = conversations;
    saveConversations(conversations);
  }, [conversations]);

  useEffect(() => {
    activeIdRef.current = activeId;
    saveActiveConversationId(activeId);
  }, [activeId]);

  useEffect(() => {
    optionsRef.current = { dialect, execute, onNetworkError };
  }, [dialect, execute, onNetworkError]);

  useEffect(() => () => abortRef.current?.abort(), []);

  const send = useCallback(async (text: string): Promise<AssistantChatMessage | null> => {
    const prompt = text.trim().slice(0, MAX_INPUT_CHARS);
    if (!prompt || runningRef.current) return null;
    runningRef.current = true;

    const startedAt = Date.now();
    const existing = conversationsRef.current.find((conversation) => conversation.id === activeIdRef.current);
    const conversationId = existing?.id ?? createId();
    const threadId = existing?.thread_id ?? conversationId;
    const history = buildHistory(existing?.messages ?? []);
    const userMessage: UserChatMessage = { id: createId(), role: 'user', content: prompt, created_at: startedAt };

    const withUserMessage = existing
      ? conversationsRef.current.map((conversation) =>
          conversation.id === conversationId
            ? { ...conversation, updated_at: startedAt, messages: [...conversation.messages, userMessage] }
            : conversation,
        )
      : [
          {
            id: conversationId,
            thread_id: threadId,
            title: truncate(prompt, 80),
            created_at: startedAt,
            updated_at: startedAt,
            messages: [userMessage],
          },
          ...conversationsRef.current,
        ];
    conversationsRef.current = withUserMessage;
    activeIdRef.current = conversationId;
    setConversations(withUserMessage);
    setActiveId(conversationId);
    setPending({ conversationId, prompt, startedAt, steps: [], explanation: '', message: '' });
    runningConversationRef.current = conversationId;

    const controller = new AbortController();
    abortRef.current = controller;
    const steps: StepEvent[] = [];
    let response: AgentResponse | null = null;
    let failure: ChatError | null = null;
    let stopped = false;
    let serverThreadId = threadId;
    const { dialect: requestDialect, execute: requestExecute } = optionsRef.current;

    try {
      await streamChat(
        {
          message: prompt,
          thread_id: threadId,
          dialect: requestDialect,
          execute: requestExecute,
          history: history.length > 0 ? history : null,
        },
        {
          onStart: (data) => {
            if (data.thread_id) serverThreadId = data.thread_id;
          },
          onStep: (step) => {
            steps.push(step);
            setPending((current) => (current ? { ...current, steps: [...current.steps, step] } : current));
          },
          onToken: (token) => {
            setPending((current) => {
              if (!current) return current;
              return token.target === 'explanation'
                ? { ...current, explanation: current.explanation + token.text }
                : { ...current, message: current.message + token.text };
            });
          },
          onFinal: (data) => {
            response = data;
          },
          onError: (data) => {
            failure = { message: data.message, code: data.code, retry_after: null };
          },
        },
        controller.signal,
      );
    } catch (error) {
      if (response === null) {
        if (isAbortError(error)) {
          stopped = true;
        } else {
          failure = toChatError(error);
          if (isNetworkError(error)) optionsRef.current.onNetworkError?.(NETWORK_ERROR_MESSAGE);
        }
      }
    }

    const finalResponse: AgentResponse | null = response;
    const assistant: AssistantChatMessage = {
      id: createId(),
      role: 'assistant',
      created_at: Date.now(),
      prompt,
      state: finalResponse ? 'done' : stopped ? 'stopped' : 'error',
      response: finalResponse,
      error: finalResponse || stopped ? null : (failure ?? toChatError(null)),
      steps,
      elapsed_ms: Date.now() - startedAt,
    };

    const settled = conversationsRef.current.map((conversation) =>
      conversation.id === conversationId
        ? {
            ...conversation,
            thread_id: serverThreadId,
            updated_at: Date.now(),
            messages: [...conversation.messages, assistant],
          }
        : conversation,
    );
    conversationsRef.current = settled;
    setConversations(settled);
    setPending(null);
    abortRef.current = null;
    runningConversationRef.current = null;
    runningRef.current = false;
    return assistant;
  }, []);

  const stop = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  const retry = useCallback(
    (assistantMessageId: string) => {
      if (runningRef.current) return;
      const conversation = conversationsRef.current.find((item) => item.id === activeIdRef.current);
      if (!conversation) return;
      const index = conversation.messages.findIndex((message) => message.id === assistantMessageId);
      const target = conversation.messages[index];
      if (!target || target.role !== 'assistant' || !target.prompt) return;

      // Drop the failed answer and the prompt it answered; send() appends them again.
      const previous = conversation.messages[index - 1];
      const start = previous?.role === 'user' && previous.content === target.prompt ? index - 1 : index;
      const messages = [...conversation.messages.slice(0, start), ...conversation.messages.slice(index + 1)];
      const updated = conversationsRef.current.map((item) =>
        item.id === conversation.id ? { ...item, messages } : item,
      );
      conversationsRef.current = updated;
      setConversations(updated);
      void send(target.prompt);
    },
    [send],
  );

  const newChat = useCallback(() => {
    activeIdRef.current = null;
    setActiveId(null);
  }, []);

  const selectConversation = useCallback((id: string) => {
    activeIdRef.current = id;
    setActiveId(id);
  }, []);

  const deleteConversation = useCallback((id: string) => {
    const conversation = conversationsRef.current.find((item) => item.id === id);
    if (!conversation) return;
    if (runningConversationRef.current === id) abortRef.current?.abort();
    const remaining = conversationsRef.current.filter((item) => item.id !== id);
    conversationsRef.current = remaining;
    setConversations(remaining);
    if (activeIdRef.current === id) {
      activeIdRef.current = null;
      setActiveId(null);
    }
    deleteThread(conversation.thread_id);
  }, []);

  const sorted = useMemo(
    () => [...conversations].sort((a, b) => b.updated_at - a.updated_at),
    [conversations],
  );
  const activeConversation = useMemo(
    () => conversations.find((conversation) => conversation.id === activeId) ?? null,
    [conversations, activeId],
  );

  return {
    conversations: sorted,
    activeConversation,
    pending,
    isRunning: pending !== null,
    send,
    stop,
    retry,
    newChat,
    selectConversation,
    deleteConversation,
  };
}
