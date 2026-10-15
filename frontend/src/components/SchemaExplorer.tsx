import clsx from 'clsx';
import { ChevronRight, KeyRound, Link2, RotateCcw, Search, Table2, TriangleAlert } from 'lucide-react';
import { useMemo, useState } from 'react';
import type { SchemaState } from '../hooks/useSchema';
import { formatNumber } from '../lib/format';
import { Button, SectionLabel } from './ui';
import type { SchemaColumn, SchemaTable } from '../types';

export interface SchemaExplorerProps {
  state: SchemaState;
  onReload: () => void;
  /** Insert a table or column name into the message input. */
  onInsert: (name: string) => void;
}

function columnTooltip(column: SchemaColumn): string {
  const parts = [`${column.name} · ${column.type}${column.nullable ? '' : ' · NOT NULL'}`];
  if (column.description) parts.push(column.description);
  if (column.foreign_key) parts.push(`References ${column.foreign_key.table}.${column.foreign_key.column}`);
  if (column.sample_values.length > 0) parts.push(`e.g. ${column.sample_values.slice(0, 3).join(', ')}`);
  parts.push('Click to insert into your message.');
  return parts.join('\n');
}

function matches(table: SchemaTable, query: string): { table: boolean; columns: SchemaColumn[] } {
  if (!query) return { table: true, columns: table.columns };
  const tableMatch = table.name.toLowerCase().includes(query);
  const columns = table.columns.filter((column) => column.name.toLowerCase().includes(query));
  return { table: tableMatch, columns: tableMatch ? table.columns : columns };
}

/** Tables with row counts and expandable columns (types, PK/FK badges, descriptions). */
export function SchemaExplorer({ state, onReload, onInsert }: SchemaExplorerProps) {
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set());
  const [filter, setFilter] = useState('');
  const query = filter.trim().toLowerCase();

  const tables = useMemo(() => {
    if (state.status !== 'ready') return [];
    return state.schema.tables
      .map((table) => ({ table, match: matches(table, query) }))
      .filter(({ match }) => match.table || match.columns.length > 0);
  }, [state, query]);

  const toggle = (name: string) => {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between px-4 pt-1 pb-2">
        <SectionLabel>
          <span id="schema-heading">Schema</span>
        </SectionLabel>
        {state.status === 'ready' && (
          <span className="text-[0.6875rem] text-zinc-400 dark:text-zinc-500">
            {state.schema.dialect === 'sqlite' ? 'SQLite' : state.schema.dialect} · {state.schema.tables.length} tables
          </span>
        )}
      </div>

      {state.status === 'ready' && (
        <div className="px-3 pb-2">
          <label className="relative block">
            <span className="sr-only">Filter tables and columns</span>
            <Search
              aria-hidden="true"
              className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-zinc-400"
            />
            <input
              type="search"
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              placeholder="Filter tables and columns"
              className="h-8 w-full rounded-lg border border-zinc-200 bg-white pr-2 pl-8 text-xs text-zinc-800 placeholder:text-zinc-400 focus:border-indigo-400 focus:outline-none focus-visible:outline-none focus:ring-2 focus:ring-indigo-500/15 dark:border-zinc-700/80 dark:bg-zinc-900 dark:text-zinc-200 dark:placeholder:text-zinc-500"
            />
          </label>
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-3">
        {state.status === 'loading' && (
          <div className="space-y-2 px-2 py-1" aria-label="Loading schema">
            {[70, 55, 80, 60, 50].map((width) => (
              <div key={width} className="skeleton h-5" style={{ width: `${width}%` }} />
            ))}
          </div>
        )}

        {state.status === 'error' && (
          <div className="mx-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900 dark:border-amber-400/25 dark:bg-amber-400/10 dark:text-amber-200">
            <p className="flex items-center gap-1.5 font-medium">
              <TriangleAlert aria-hidden="true" className="size-3.5" />
              Schema unavailable
            </p>
            <p className="mt-1 break-words opacity-80">{state.error}</p>
            <Button size="xs" className="mt-2" onClick={onReload}>
              <RotateCcw aria-hidden="true" className="size-3" />
              Retry
            </Button>
          </div>
        )}

        {state.status === 'ready' && tables.length === 0 && (
          <p className="px-3 py-2 text-xs text-zinc-400 dark:text-zinc-500">No tables or columns match “{filter}”.</p>
        )}

        {state.status === 'ready' && (
          <ul aria-labelledby="schema-heading" className="space-y-0.5">
            {tables.map(({ table, match }) => {
              const open = expanded.has(table.name) || (query !== '' && !match.table);
              const listId = `schema-columns-${table.name}`;
              return (
                <li key={table.name}>
                  <div className="group flex items-center rounded-lg hover:bg-zinc-100 dark:hover:bg-zinc-800/70">
                    <button
                      type="button"
                      onClick={() => toggle(table.name)}
                      aria-expanded={open}
                      aria-controls={listId}
                      aria-label={`${open ? 'Collapse' : 'Expand'} ${table.name} columns`}
                      className="flex size-7 shrink-0 items-center justify-center rounded-md text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-200"
                    >
                      <ChevronRight
                        aria-hidden="true"
                        className={clsx('size-3.5 transition-transform', open && 'rotate-90')}
                      />
                    </button>
                    <button
                      type="button"
                      onClick={() => onInsert(table.name)}
                      title={`${table.description}\n${formatNumber(table.row_count)} rows · click to insert`}
                      aria-label={`Insert table ${table.name} (${formatNumber(table.row_count)} rows)`}
                      className="flex min-w-0 flex-1 items-center gap-2 rounded-md py-1.5 pr-2 text-left"
                    >
                      <Table2 aria-hidden="true" className="size-3.5 shrink-0 text-indigo-500 dark:text-indigo-400" />
                      <span className="truncate text-[0.8125rem] font-medium text-zinc-800 dark:text-zinc-200">
                        {table.name}
                      </span>
                      <span className="ml-auto shrink-0 text-[0.6875rem] text-zinc-400 tabular-nums dark:text-zinc-500">
                        {formatNumber(table.row_count)}
                      </span>
                    </button>
                  </div>
                  {open && (
                    <ul
                      id={listId}
                      className="mt-0.5 mb-1.5 ml-[13px] space-y-px border-l border-zinc-200 pl-2 dark:border-zinc-800"
                    >
                      {match.columns.map((column) => (
                        <li key={column.name}>
                          <button
                            type="button"
                            onClick={() => onInsert(column.name)}
                            title={columnTooltip(column)}
                            aria-label={`Insert column ${table.name}.${column.name} (${column.type})`}
                            className="flex w-full min-w-0 items-center gap-1.5 rounded-md px-2 py-1 text-left hover:bg-zinc-100 dark:hover:bg-zinc-800/70"
                          >
                            <span className="truncate font-mono text-xs text-zinc-700 dark:text-zinc-300">
                              {column.name}
                            </span>
                            {column.primary_key && (
                              <span className="inline-flex shrink-0 items-center gap-0.5 rounded bg-amber-50 px-1 text-[0.625rem] font-semibold text-amber-700 dark:bg-amber-400/10 dark:text-amber-300">
                                <KeyRound aria-hidden="true" className="size-2.5" />
                                PK
                              </span>
                            )}
                            {column.foreign_key && (
                              <span className="inline-flex shrink-0 items-center gap-0.5 rounded bg-indigo-50 px-1 text-[0.625rem] font-semibold text-indigo-700 dark:bg-indigo-400/10 dark:text-indigo-300">
                                <Link2 aria-hidden="true" className="size-2.5" />
                                FK
                              </span>
                            )}
                            <span className="ml-auto shrink-0 pl-1 font-mono text-[0.625rem] text-zinc-400 uppercase dark:text-zinc-500">
                              {column.type}
                            </span>
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}
