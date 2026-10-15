/** Client-side file downloads (SQL scripts and CSV exports). */
import { toCsv } from './csv';
import type { Dialect, SqlValue } from '../types';

/** `20261015-213045`: a sortable local timestamp for file names. */
export function fileTimestamp(date: Date = new Date()): string {
  const pad = (value: number) => String(value).padStart(2, '0');
  return (
    `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}` +
    `-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`
  );
}

/** Save `content` as a file named `filename`. */
export function downloadText(filename: string, content: string, mimeType: string): void {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = filename;
  link.rel = 'noopener';
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Download a query as a `.sql` file (terminated with a semicolon and a newline). */
export function downloadSql(sql: string, dialect: Dialect): void {
  const body = sql.trim().replace(/;?\s*$/, ';\n');
  downloadText(`querypilot-${dialect}-${fileTimestamp()}.sql`, body, 'application/sql;charset=utf-8');
}

/** Download a result set as CSV. A UTF-8 byte-order mark lets spreadsheet apps detect the encoding. */
export function downloadCsv(columns: readonly string[], rows: readonly (readonly SqlValue[])[]): void {
  downloadText(`querypilot-results-${fileTimestamp()}.csv`, `\uFEFF${toCsv(columns, rows)}`, 'text/csv;charset=utf-8');
}
