import clsx from 'clsx';
import { Download, FileCode } from 'lucide-react';
import { useState } from 'react';
import { downloadSql } from '../../lib/download';
import { dialectLabel } from '../../lib/format';
import { CopyButton } from '../CopyButton';
import { SqlBlock } from '../SqlBlock';
import { Badge, Button } from '../ui';
import type { AgentResponse, Dialect } from '../../types';

/** The generated SQL with copy, download and (for PostgreSQL/MySQL) the executed SQLite version. */
export function SqlPanel({ response }: { response: AgentResponse }) {
  const [showSqlite, setShowSqlite] = useState(false);
  const canToggle = response.dialect !== 'sqlite' && Boolean(response.sql_sqlite);
  const identical = response.sql_sqlite === response.sql;
  const useSqlite = canToggle && showSqlite;
  const sql = (useSqlite ? response.sql_sqlite : response.sql) ?? '';
  const dialect: Dialect = useSqlite ? 'sqlite' : response.dialect;

  return (
    <section
      aria-label="SQL"
      className="overflow-hidden rounded-xl border border-zinc-200 bg-white dark:border-zinc-800 dark:bg-zinc-900"
    >
      <div className="flex flex-wrap items-center gap-2 border-b border-zinc-200 px-3 py-2 dark:border-zinc-800">
        <FileCode aria-hidden="true" className="size-4 text-zinc-400" />
        <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">SQL</h3>
        <Badge tone="indigo">{dialectLabel(dialect)}</Badge>
        <div className="ml-auto flex items-center gap-1.5">
          <CopyButton text={sql} label="Copy SQL" />
          <Button size="xs" onClick={() => downloadSql(sql, dialect)} aria-label="Download SQL file" title="Download .sql">
            <Download aria-hidden="true" className="size-3.5" />
            .sql
          </Button>
        </div>
      </div>
      {canToggle && (
        <div className="flex items-center gap-2 border-b border-zinc-200 px-3 py-1.5 text-xs dark:border-zinc-800">
          <span className="text-zinc-500 dark:text-zinc-400">Show:</span>
          <div className="inline-flex rounded-md bg-zinc-100 p-0.5 dark:bg-zinc-800" role="group" aria-label="SQL version">
            {[
              { id: false, label: dialectLabel(response.dialect) },
              { id: true, label: 'SQLite (executed)' },
            ].map((option) => (
              <button
                key={String(option.id)}
                type="button"
                aria-pressed={showSqlite === option.id}
                onClick={() => setShowSqlite(option.id)}
                className={clsx(
                  'rounded px-2 py-0.5 font-medium transition-colors',
                  showSqlite === option.id
                    ? 'bg-white text-zinc-900 shadow-xs dark:bg-zinc-700 dark:text-zinc-100'
                    : 'text-zinc-500 hover:text-zinc-800 dark:text-zinc-400 dark:hover:text-zinc-200',
                )}
              >
                {option.label}
              </button>
            ))}
          </div>
          {identical && <span className="text-zinc-400 dark:text-zinc-500">Identical in both dialects</span>}
        </div>
      )}
      <SqlBlock sql={sql} lineNumbers className="max-h-72 bg-zinc-50/60 dark:bg-zinc-950/40" />
    </section>
  );
}
