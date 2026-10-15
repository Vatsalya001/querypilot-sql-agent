/** CSV serialization following RFC 4180 (CRLF line breaks, quotes doubled inside quoted fields). */
import type { SqlValue } from '../types';

const NEEDS_QUOTES = /[",\r\n]|^\s|\s$/;
/** Leading characters that make spreadsheet apps evaluate a cell as a formula (CSV injection, OWASP). */
const FORMULA_TRIGGER = /^[=+\-@\t\r]/;

/**
 * Serialize one value as a CSV field. `null` and `undefined` become empty fields. Text that a spreadsheet
 * would run as a formula gets a leading apostrophe; numbers (including negatives) are left untouched.
 */
export function escapeCsvField(value: SqlValue | boolean | undefined): string {
  if (value === null || value === undefined) return '';
  const raw = String(value);
  const text = typeof value === 'string' && FORMULA_TRIGGER.test(raw) ? `'${raw}` : raw;
  return NEEDS_QUOTES.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

/** Serialize a header row plus data rows. Records are separated by CRLF, without a trailing break. */
export function toCsv(columns: readonly string[], rows: readonly (readonly SqlValue[])[]): string {
  const lines = [columns.map(escapeCsvField).join(',')];
  for (const row of rows) {
    lines.push(row.map(escapeCsvField).join(','));
  }
  return lines.join('\r\n');
}
