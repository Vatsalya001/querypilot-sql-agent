import clsx from 'clsx';
import type { LucideIcon } from 'lucide-react';
import { useRef, type KeyboardEvent, type ReactNode } from 'react';

export interface TabItem<T extends string> {
  id: T;
  label: string;
  icon?: LucideIcon;
  /** Small trailing indicator, e.g. a count. */
  badge?: ReactNode;
}

export interface TabsProps<T extends string> {
  items: readonly TabItem<T>[];
  value: T;
  onChange: (value: T) => void;
  /** Accessible name of the tab list. */
  label: string;
  /** Prefix for tab and panel element ids (`${idPrefix}-tab-${id}`, `${idPrefix}-panel-${id}`). */
  idPrefix: string;
  /** Spread the tabs evenly across the full width. */
  stretch?: boolean;
  className?: string;
}

/** WAI-ARIA tabs with roving focus: arrow keys, Home and End move between tabs. */
export function Tabs<T extends string>({
  items,
  value,
  onChange,
  label,
  idPrefix,
  stretch = false,
  className,
}: TabsProps<T>) {
  const buttons = useRef(new Map<T, HTMLButtonElement>());

  const onKeyDown = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next: number | null = null;
    if (event.key === 'ArrowRight') next = (index + 1) % items.length;
    else if (event.key === 'ArrowLeft') next = (index - 1 + items.length) % items.length;
    else if (event.key === 'Home') next = 0;
    else if (event.key === 'End') next = items.length - 1;
    if (next === null) return;
    event.preventDefault();
    const target = items[next];
    onChange(target.id);
    buttons.current.get(target.id)?.focus();
  };

  return (
    <div role="tablist" aria-label={label} className={clsx('flex overflow-x-auto scrollbar-none', className)}>
      {items.map((item, index) => {
        const selected = item.id === value;
        const Icon = item.icon;
        return (
          <button
            key={item.id}
            ref={(element) => {
              if (element) buttons.current.set(item.id, element);
              else buttons.current.delete(item.id);
            }}
            type="button"
            role="tab"
            id={`${idPrefix}-tab-${item.id}`}
            aria-selected={selected}
            aria-controls={`${idPrefix}-panel-${item.id}`}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(item.id)}
            onKeyDown={(event) => onKeyDown(event, index)}
            className={clsx(
              'relative flex shrink-0 items-center gap-1.5 py-2.5 text-xs font-medium whitespace-nowrap transition-colors sm:text-[0.8125rem]',
              stretch ? 'flex-1 justify-center px-1.5' : 'px-2.5',
              'after:absolute after:inset-x-1.5 after:-bottom-px after:h-0.5 after:rounded-full after:transition-colors',
              selected
                ? 'text-indigo-600 after:bg-indigo-600 dark:text-indigo-300 dark:after:bg-indigo-400'
                : 'text-zinc-500 after:bg-transparent hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200',
            )}
          >
            {Icon && <Icon aria-hidden="true" className="size-3.5" />}
            {item.label}
            {item.badge}
          </button>
        );
      })}
    </div>
  );
}
