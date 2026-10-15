import clsx from 'clsx';
import { useMemo } from 'react';
import { highlightSql } from '../lib/highlight';

export interface SqlBlockProps {
  sql: string;
  lineNumbers?: boolean;
  className?: string;
}

/** Syntax-highlighted SQL in a horizontally scrollable block, with optional line numbers. */
export function SqlBlock({ sql, lineNumbers = false, className }: SqlBlockProps) {
  const html = useMemo(() => highlightSql(sql), [sql]);
  const lineCount = useMemo(() => sql.split('\n').length, [sql]);

  return (
    <div className={clsx('flex overflow-auto font-mono text-[0.8125rem] leading-6', className)}>
      {lineNumbers && (
        <div
          aria-hidden="true"
          className="sticky left-0 shrink-0 border-r border-zinc-200 bg-zinc-50 py-3 pr-2.5 pl-3 text-right text-zinc-400 tabular-nums select-none dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-600"
        >
          {Array.from({ length: lineCount }, (_, index) => (
            <div key={index}>{index + 1}</div>
          ))}
        </div>
      )}
      <pre className="sql-code min-w-0 flex-1 px-4 py-3">
        <code dangerouslySetInnerHTML={{ __html: html }} />
      </pre>
    </div>
  );
}
