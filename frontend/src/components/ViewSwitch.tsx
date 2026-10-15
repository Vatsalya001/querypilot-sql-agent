import clsx from 'clsx';
import { MessagesSquare, PanelRight } from 'lucide-react';

export type MobileView = 'chat' | 'details';

/** The "Chat | Details" switch that replaces the side inspector below the lg breakpoint. */
export function ViewSwitch({
  value,
  onChange,
  hasDetails,
}: {
  value: MobileView;
  onChange: (view: MobileView) => void;
  hasDetails: boolean;
}) {
  const items = [
    { id: 'chat' as const, label: 'Chat', icon: MessagesSquare },
    { id: 'details' as const, label: 'Details', icon: PanelRight },
  ];
  return (
    <div className="flex shrink-0 justify-center border-b border-zinc-200 bg-white/85 px-3 py-2 backdrop-blur-md dark:border-zinc-800 dark:bg-zinc-950/85">
      <div role="tablist" aria-label="View" className="inline-flex w-full max-w-xs rounded-lg bg-zinc-100 p-0.5 dark:bg-zinc-800/80">
        {items.map((item) => {
          const selected = value === item.id;
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-controls={`view-${item.id}`}
              id={`view-tab-${item.id}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => onChange(item.id)}
              onKeyDown={(event) => {
                if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
                event.preventDefault();
                const next = item.id === 'chat' ? 'details' : 'chat';
                onChange(next);
                document.getElementById(`view-tab-${next}`)?.focus();
              }}
              className={clsx(
                'relative flex flex-1 items-center justify-center gap-1.5 rounded-md py-1.5 text-[0.8125rem] font-medium transition-colors',
                selected
                  ? 'bg-white text-zinc-900 shadow-xs dark:bg-zinc-700 dark:text-zinc-50'
                  : 'text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200',
              )}
            >
              <Icon aria-hidden="true" className="size-3.5" />
              {item.label}
              {item.id === 'details' && hasDetails && !selected && (
                <span aria-hidden="true" className="size-1.5 rounded-full bg-indigo-500" />
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}
