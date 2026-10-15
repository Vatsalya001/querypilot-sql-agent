import { Menu, PanelLeftClose, PanelLeftOpen, Workflow } from 'lucide-react';
import type { HealthState } from '../hooks/useHealth';
import type { Theme } from '../hooks/useTheme';
import { LogoMark } from './Logo';
import { QueryOptions, type QueryOptionsProps } from './QueryOptions';
import { StatusPill } from './StatusPill';
import { ThemeToggle } from './ThemeToggle';
import { Button, IconButton } from './ui';

export interface HeaderProps {
  health: HealthState;
  theme: Theme;
  onToggleTheme: () => void;
  /** Dialect and execute controls; omitted on phones, where they live in the input toolbar. */
  queryOptions: QueryOptionsProps | null;
  /** The sidebar is a docked column (wide screens) rather than a drawer. */
  sidebarDocked: boolean;
  sidebarOpen: boolean;
  onToggleSidebar: () => void;
  onOpenHowItWorks: () => void;
  compact: boolean;
}

export function Header({
  health,
  theme,
  onToggleTheme,
  queryOptions,
  sidebarDocked,
  sidebarOpen,
  onToggleSidebar,
  onOpenHowItWorks,
  compact,
}: HeaderProps) {
  const SidebarIcon = sidebarDocked ? (sidebarOpen ? PanelLeftClose : PanelLeftOpen) : Menu;
  return (
    <header className="relative z-30 flex h-14 shrink-0 items-center gap-2 border-b border-zinc-200 bg-white/85 px-2.5 backdrop-blur-md sm:gap-3 sm:px-4 dark:border-zinc-800 dark:bg-zinc-950/85">
      <IconButton
        label={sidebarOpen ? 'Hide sidebar' : 'Show sidebar'}
        aria-expanded={sidebarOpen}
        onClick={onToggleSidebar}
      >
        <SidebarIcon aria-hidden="true" className="size-[18px]" />
      </IconButton>

      <div className="flex min-w-0 items-center gap-2.5">
        <LogoMark className="size-7 shrink-0" />
        <div className="min-w-0 leading-tight">
          <p className="truncate text-[0.9375rem] font-semibold tracking-tight text-zinc-900 dark:text-zinc-50">
            QueryPilot
          </p>
          <p className="hidden text-[0.6875rem] text-zinc-500 sm:block dark:text-zinc-400">SQL AI Agent</p>
        </div>
      </div>

      <div className="mx-1 hidden h-6 w-px bg-zinc-200 sm:block dark:bg-zinc-800" aria-hidden="true" />
      <StatusPill state={health} compact={compact} />

      <div className="ml-auto flex items-center gap-1 sm:gap-2">
        {queryOptions && <QueryOptions {...queryOptions} />}
        {compact ? (
          <IconButton label="How it works" onClick={onOpenHowItWorks}>
            <Workflow aria-hidden="true" className="size-[18px]" />
          </IconButton>
        ) : (
          <Button variant="ghost" size="sm" onClick={onOpenHowItWorks}>
            <Workflow aria-hidden="true" className="size-4" />
            <span className="hidden xl:inline">How it works</span>
            <span className="sr-only xl:hidden">How it works</span>
          </Button>
        )}
        <ThemeToggle theme={theme} onToggle={onToggleTheme} />
      </div>
    </header>
  );
}
