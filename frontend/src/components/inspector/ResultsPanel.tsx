import clsx from 'clsx';
import { Download, PlayCircle, Table2, TriangleAlert } from 'lucide-react';
import { downloadCsv } from '../../lib/download';
import { formatDuration, formatNumber, pluralize } from '../../lib/format';
import { Button, EmptyHint } from '../ui';
import type { AgentResponse, SqlValue } from '../../types';

function Cell({ value }: { value: SqlValue }) {
  if (value === null) {
    return (
      <span className="rounded bg-zinc-100 px-1 py-px font-mono text-[0.625rem] font-medium tracking-wide text-zinc-400 dark:bg-zinc-800 dark:text-zinc-500">
        NULL
      </span>
    );
  }
  return <>{String(value)}</>;
}

/** Query results as a scrollable table with a sticky header, plus CSV export. */
export function ResultsPanel({ response }: { response: AgentResponse }) {
  const { execution } = response;

  if (!execution) {
    return response.status === 'success' ? (
      <EmptyHint icon={PlayCircle} title="Execution disabled">
        Turn on <strong className="font-medium">Execute</strong> to run validated queries against the read-only sample
        database.
      </EmptyHint>
    ) : (
      <EmptyHint icon={Table2} title="No results for this response">
        Only validated queries are executed.
      </EmptyHint>
    );
  }

  if (execution.error) {
    return (
      <div
        role="alert"
        className="flex gap-3 rounded-xl border border-red-200 bg-red-50/70 p-3 text-red-900 dark:border-red-400/25 dark:bg-red-400/10 dark:text-red-200"
      >
        <TriangleAlert aria-hidden="true" className="mt-0.5 size-5 shrink-0" />
        <div className="min-w-0">
          <p className="text-sm font-semibold">The query failed at runtime</p>
          <p className="mt-1 font-mono text-xs break-words opacity-90">{execution.error}</p>
          <p className="mt-2 text-xs opacity-75">The SQL passed validation and is still shown above.</p>
        </div>
      </div>
    );
  }

  const numericColumns = execution.columns.map((_, index) =>
    execution.rows.some((row) => typeof row[index] === 'number') &&
    execution.rows.every((row) => row[index] === null || typeof row[index] === 'number'),
  );

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <p className="text-sm font-medium text-zinc-900 dark:text-zinc-100">
          {pluralize(execution.row_count, 'row')}
          {execution.truncated && <span className="text-zinc-500 dark:text-zinc-400">+</span>}
        </p>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">{formatDuration(execution.elapsed_ms)}</p>
        <Button
          size="xs"
          className="ml-auto"
          onClick={() => downloadCsv(execution.columns, execution.rows)}
          disabled={execution.columns.length === 0}
          aria-label="Download results as CSV"
        >
          <Download aria-hidden="true" className="size-3.5" />
          CSV
        </Button>
      </div>

      {execution.truncated && (
        <p className="rounded-lg bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:bg-amber-400/10 dark:text-amber-300">
          Showing the first {formatNumber(execution.rows.length)} rows. More rows match; add filters or a LIMIT to
          narrow the result.
        </p>
      )}

      <div className="max-h-[min(60vh,560px)] overflow-auto rounded-xl border border-zinc-200 dark:border-zinc-800">
        <table className="min-w-full border-separate border-spacing-0 text-[0.8125rem]">
          <caption className="sr-only">Query results</caption>
          <thead>
            <tr>
              <th
                scope="col"
                className="sticky top-0 left-0 z-20 w-10 border-b border-zinc-200 bg-zinc-50 px-2 py-2 text-right text-[0.6875rem] font-medium text-zinc-400 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-500"
              >
                #
              </th>
              {execution.columns.map((column, index) => (
                <th
                  key={`${index}-${column}`}
                  scope="col"
                  className={clsx(
                    'sticky top-0 z-10 border-b border-zinc-200 bg-zinc-50 px-3 py-2 font-semibold whitespace-nowrap text-zinc-700 dark:border-zinc-800 dark:bg-zinc-900 dark:text-zinc-200',
                    numericColumns[index] ? 'text-right' : 'text-left',
                  )}
                >
                  {column}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {execution.rows.length === 0 ? (
              <tr>
                <td
                  colSpan={execution.columns.length + 1}
                  className="px-3 py-8 text-center text-sm text-zinc-500 dark:text-zinc-400"
                >
                  The query returned no rows.
                </td>
              </tr>
            ) : (
              execution.rows.map((row, rowIndex) => (
                <tr key={rowIndex} className="group">
                  <td className="sticky left-0 border-b border-zinc-100 bg-white px-2 py-1.5 text-right text-[0.6875rem] text-zinc-400 tabular-nums group-hover:bg-indigo-50/60 dark:border-zinc-800/70 dark:bg-zinc-900 dark:text-zinc-500 dark:group-hover:bg-zinc-800">
                    {rowIndex + 1}
                  </td>
                  {row.map((value, columnIndex) => (
                    <td
                      key={columnIndex}
                      title={value === null ? 'NULL' : String(value)}
                      className={clsx(
                        'max-w-[18rem] truncate border-b border-zinc-100 px-3 py-1.5 whitespace-nowrap text-zinc-700 group-hover:bg-indigo-50/60 dark:border-zinc-800/70 dark:text-zinc-300 dark:group-hover:bg-zinc-800/60',
                        numericColumns[columnIndex] && 'text-right font-mono text-xs tabular-nums',
                      )}
                    >
                      <Cell value={value} />
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
