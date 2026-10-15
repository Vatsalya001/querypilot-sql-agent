import clsx from 'clsx';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChatPanel } from './components/ChatPanel';
import type { ComposerHandle } from './components/Composer';
import { Drawer } from './components/Drawer';
import { Header } from './components/Header';
import { HealthBanner } from './components/HealthBanner';
import { HowItWorksModal } from './components/HowItWorksModal';
import { Inspector } from './components/Inspector';
import { preferredTab, type InspectorTab } from './components/inspector/tabs';
import type { QueryOptionsProps } from './components/QueryOptions';
import { Sidebar } from './components/Sidebar';
import { useToast } from './components/Toaster';
import { ViewSwitch, type MobileView } from './components/ViewSwitch';
import { useChat } from './hooks/useChat';
import { useHealth } from './hooks/useHealth';
import { useMediaQuery } from './hooks/useMediaQuery';
import { useSchema } from './hooks/useSchema';
import { useSettings } from './hooks/useSettings';
import { useTheme } from './hooks/useTheme';
import type { ExamplePrompt } from './lib/examples';
import type { AssistantChatMessage } from './types';

export default function App() {
  const { theme, toggleTheme } = useTheme();
  const { state: health, refresh: refreshHealth } = useHealth();
  const { state: schemaState, reload: reloadSchema } = useSchema();
  const [settings, updateSettings] = useSettings();
  const toast = useToast();
  // lg+: the inspector is a side panel; xl+: the sidebar is docked (below xl it is a drawer).
  const isDesktop = useMediaQuery('(min-width: 1024px)');
  const isWide = useMediaQuery('(min-width: 1280px)');
  const isTabletUp = useMediaQuery('(min-width: 768px)');

  const onNetworkError = useCallback(
    (message: string) => toast({ title: 'Network error', description: message, tone: 'error' }),
    [toast],
  );
  const chat = useChat({ dialect: settings.dialect, execute: settings.execute, onNetworkError });

  const composerRef = useRef<ComposerHandle>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [howItWorksOpen, setHowItWorksOpen] = useState(false);
  const [mobileView, setMobileView] = useState<MobileView>('chat');
  const [inspectorOpen, setInspectorOpen] = useState(true);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>('results');

  const conversation = chat.activeConversation;
  const messages = useMemo(() => conversation?.messages ?? [], [conversation]);
  const lastAssistant = useMemo(
    () => messages.findLast((message): message is AssistantChatMessage => message.role === 'assistant') ?? null,
    [messages],
  );

  // Follow the newest answer: select it when it arrives and when another conversation is opened.
  useEffect(() => {
    setSelectedId(lastAssistant?.id ?? null);
    setInspectorTab(preferredTab(lastAssistant?.response ?? null));
  }, [lastAssistant]);

  const selectedMessage = useMemo(
    () =>
      messages.find(
        (message): message is AssistantChatMessage => message.role === 'assistant' && message.id === selectedId,
      ) ?? null,
    [messages, selectedId],
  );

  // When the API comes back after an outage, retry the schema once (only on the transition to ok).
  const previousHealth = useRef(health.status);
  useEffect(() => {
    const recovered = previousHealth.current !== 'ok' && health.status === 'ok';
    previousHealth.current = health.status;
    if (recovered && schemaState.status === 'error') reloadSchema();
  }, [health.status, schemaState.status, reloadSchema]);

  useEffect(() => {
    document.title = conversation ? `${conversation.title} · QueryPilot` : 'QueryPilot · SQL AI Agent';
  }, [conversation]);

  const openDetails = useCallback(
    (messageId: string, tab?: InspectorTab) => {
      const message = messages.find((item) => item.id === messageId);
      setSelectedId(messageId);
      setInspectorTab(tab ?? preferredTab(message?.role === 'assistant' ? message.response : null));
      setInspectorOpen(true);
      if (!isDesktop) setMobileView('details');
    },
    [isDesktop, messages],
  );

  const closeDrawer = useCallback(() => setDrawerOpen(false), []);
  const closeHowItWorks = useCallback(() => setHowItWorksOpen(false), []);

  const toggleSidebar = () => {
    if (isWide) updateSettings({ sidebarOpen: !settings.sidebarOpen });
    else setDrawerOpen((open) => !open);
  };

  const showChat = () => {
    setDrawerOpen(false);
    setMobileView('chat');
  };

  const newChat = () => {
    chat.newChat();
    showChat();
    composerRef.current?.focus();
  };

  const selectConversation = (id: string) => {
    chat.selectConversation(id);
    showChat();
  };

  const insertIntoMessage = (name: string) => {
    showChat();
    composerRef.current?.insertText(name);
  };

  const pickExample = async (example: ExamplePrompt) => {
    const answer = await chat.send(example.prompt);
    if (example.followUp && answer?.state === 'done') {
      composerRef.current?.setText(example.followUp);
      toast({ title: 'Try the follow-up', description: `Press Enter to send “${example.followUp}”.`, tone: 'info' });
    }
  };

  const queryOptions: QueryOptionsProps = {
    dialect: settings.dialect,
    onDialectChange: (dialect) => updateSettings({ dialect }),
    execute: settings.execute,
    onExecuteChange: (execute) => updateSettings({ execute }),
  };

  const sidebarProps = {
    conversations: chat.conversations,
    activeId: conversation?.id ?? null,
    runningId: chat.pending?.conversationId ?? null,
    onNewChat: newChat,
    onSelect: selectConversation,
    onDelete: chat.deleteConversation,
    schemaState,
    onReloadSchema: reloadSchema,
    onInsert: insertIntoMessage,
  };

  const sidebarOpen = isWide ? settings.sidebarOpen : drawerOpen;
  const hasMessages =
    messages.length > 0 || (chat.pending !== null && chat.pending.conversationId === conversation?.id);
  const showDetailsView = !isDesktop && hasMessages && mobileView === 'details';

  return (
    <div className="flex h-dvh flex-col overflow-hidden">
      <a
        href="#composer-input"
        className="sr-only focus:not-sr-only focus:fixed focus:top-2 focus:left-2 focus:z-[70] focus:rounded-lg focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:font-medium focus:shadow-lg dark:focus:bg-zinc-800"
      >
        Skip to message input
      </a>
      <Header
        health={health}
        theme={theme}
        onToggleTheme={toggleTheme}
        queryOptions={isTabletUp ? queryOptions : null}
        sidebarDocked={isWide}
        sidebarOpen={sidebarOpen}
        onToggleSidebar={toggleSidebar}
        onOpenHowItWorks={() => setHowItWorksOpen(true)}
        compact={!isTabletUp}
      />
      <HealthBanner state={health} onRetry={refreshHealth} />

      <div className="flex min-h-0 flex-1">
        {isWide && settings.sidebarOpen && (
          <aside
            aria-label="Conversations and schema"
            className="flex w-72 shrink-0 flex-col border-r border-zinc-200 bg-zinc-50/80 dark:border-zinc-800 dark:bg-zinc-900/40"
          >
            <Sidebar {...sidebarProps} />
          </aside>
        )}

        <main className="flex min-w-0 flex-1 flex-col">
          {!isDesktop && hasMessages && (
            <ViewSwitch value={mobileView} onChange={setMobileView} hasDetails={selectedMessage !== null} />
          )}
          <div
            id="view-chat"
            role={isDesktop ? undefined : 'tabpanel'}
            aria-labelledby={isDesktop ? undefined : 'view-tab-chat'}
            className={clsx('flex min-h-0 flex-1 flex-col', showDetailsView && 'hidden')}
          >
            <ChatPanel
              composerRef={composerRef}
              conversation={conversation}
              pending={chat.pending}
              running={chat.isRunning}
              selectedId={selectedId}
              toolbarOptions={isTabletUp ? null : queryOptions}
              onSend={(text) => void chat.send(text)}
              onStop={chat.stop}
              onRetry={chat.retry}
              onOpenDetails={openDetails}
              onPickExample={(example) => void pickExample(example)}
            />
          </div>
          {!isDesktop && (
            <div
              id="view-details"
              role="tabpanel"
              aria-labelledby="view-tab-details"
              className={clsx('min-h-0 flex-1', !showDetailsView && 'hidden')}
            >
              <Inspector message={selectedMessage} tab={inspectorTab} onTabChange={setInspectorTab} />
            </div>
          )}
        </main>

        {isDesktop && inspectorOpen && selectedMessage && (
          <aside
            aria-label="Response details"
            className="flex w-[400px] shrink-0 flex-col border-l border-zinc-200 motion-safe:animate-fade-in xl:w-[440px] 2xl:w-[520px] dark:border-zinc-800"
          >
            <Inspector
              message={selectedMessage}
              tab={inspectorTab}
              onTabChange={setInspectorTab}
              onClose={() => setInspectorOpen(false)}
            />
          </aside>
        )}
      </div>

      {!isWide && (
        <Drawer open={drawerOpen} onClose={closeDrawer} label="Conversations and schema">
          <Sidebar {...sidebarProps} onClose={closeDrawer} />
        </Drawer>
      )}
      <HowItWorksModal open={howItWorksOpen} onClose={closeHowItWorks} />
    </div>
  );
}
