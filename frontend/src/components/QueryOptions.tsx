import clsx from 'clsx';
import { ChevronDown } from 'lucide-react';
import { DIALECT_LABELS } from '../lib/format';
import { Switch } from './ui';
import type { Dialect } from '../types';

export interface QueryOptionsProps {
  dialect: Dialect;
  onDialectChange: (dialect: Dialect) => void;
  execute: boolean;
  onExecuteChange: (execute: boolean) => void;
  /** Smaller variant for the input toolbar on phones. */
  compact?: boolean;
}

const CONTROL =
  'h-8 rounded-lg border border-zinc-200 bg-white text-[0.8125rem] font-medium text-zinc-700 shadow-xs transition-colors hover:bg-zinc-50 dark:border-zinc-700/80 dark:bg-zinc-900 dark:text-zinc-200 dark:hover:bg-zinc-800';

/** Output dialect select and the Execute toggle. */
export function QueryOptions({ dialect, onDialectChange, execute, onExecuteChange, compact }: QueryOptionsProps) {
  return (
    <div className="flex items-center gap-1.5">
      <label className="relative flex items-center" title="Dialect of the returned SQL (validation and execution use SQLite)">
        <span className="sr-only">SQL dialect</span>
        <select
          value={dialect}
          onChange={(event) => onDialectChange(event.target.value as Dialect)}
          className={clsx(CONTROL, 'cursor-pointer appearance-none pr-7', compact ? 'pl-2' : 'pl-2.5')}
        >
          {(Object.keys(DIALECT_LABELS) as Dialect[]).map((value) => (
            <option key={value} value={value}>
              {DIALECT_LABELS[value]}
            </option>
          ))}
        </select>
        <ChevronDown aria-hidden="true" className="pointer-events-none absolute right-2 size-3.5 text-zinc-400" />
      </label>
      <label
        className={clsx(CONTROL, 'flex cursor-pointer items-center gap-2', compact ? 'px-2' : 'pr-2.5 pl-2')}
        title="Run validated queries against the read-only sample database"
      >
        <Switch checked={execute} onChange={onExecuteChange} label="Execute queries" />
        <span aria-hidden="true">Execute</span>
      </label>
    </div>
  );
}
