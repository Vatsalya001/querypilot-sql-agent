import clsx from 'clsx';
import { CircleCheck, CircleDashed, CircleX, ListChecks, ShieldCheck, ShieldX, TriangleAlert } from 'lucide-react';
import { EmptyHint, SectionLabel } from '../ui';
import type { ValidationCheck, ValidationCheckName, ValidationResult } from '../../types';

/** The seven validator checks in pipeline order (docs/API.md). */
const CHECK_ORDER: readonly { name: ValidationCheckName; label: string }[] = [
  { name: 'syntax', label: 'Syntax' },
  { name: 'single_statement', label: 'Single statement' },
  { name: 'read_only', label: 'Read-only' },
  { name: 'tables', label: 'Tables exist' },
  { name: 'columns', label: 'Columns exist' },
  { name: 'joins', label: 'Join relationships' },
  { name: 'executable', label: 'Database dry-run' },
];

type CheckRow = ValidationCheck | { name: string; label: string; passed: null; message: string };

/** Canonical checks first (missing ones shown as "not run"), then any extra checks from the server. */
function checkRows(checks: readonly ValidationCheck[]): CheckRow[] {
  const byName = new Map(checks.map((check) => [check.name as string, check]));
  const rows: CheckRow[] = CHECK_ORDER.map(
    ({ name, label }) =>
      byName.get(name) ?? { name, label, passed: null, message: 'Not run: an earlier check failed.' },
  );
  const known = new Set<string>(CHECK_ORDER.map((check) => check.name));
  return [...rows, ...checks.filter((check) => !known.has(check.name))];
}

/** The 7-check validation checklist with errors, warnings and the referenced schema objects. */
export function ValidationPanel({ validation }: { validation: ValidationResult | null }) {
  if (!validation) {
    return (
      <EmptyHint icon={ListChecks} title="No validation for this response">
        Validation runs whenever the agent produces or receives SQL.
      </EmptyHint>
    );
  }

  const rows = checkRows(validation.checks);
  const passed = validation.checks.filter((check) => check.passed).length;
  // "read-only" is only claimed when that check actually passed (it is skipped for unparsable SQL).
  const readOnly = validation.checks.some((check) => check.name === 'read_only' && check.passed);
  const SummaryIcon = validation.valid ? ShieldCheck : ShieldX;

  return (
    <div className="space-y-5">
      <div
        className={clsx(
          'flex items-center gap-3 rounded-xl border p-3',
          validation.valid
            ? 'border-emerald-200 bg-emerald-50/70 text-emerald-900 dark:border-emerald-400/25 dark:bg-emerald-400/10 dark:text-emerald-200'
            : 'border-red-200 bg-red-50/70 text-red-900 dark:border-red-400/25 dark:bg-red-400/10 dark:text-red-200',
        )}
      >
        <SummaryIcon aria-hidden="true" className="size-6 shrink-0" />
        <div>
          <p className="text-sm font-semibold">{validation.valid ? 'All checks passed' : 'Validation failed'}</p>
          <p className="text-xs opacity-80">
            {passed} of {CHECK_ORDER.length} checks passed
            {validation.destructive ? ' · destructive statement detected' : readOnly ? ' · read-only' : ''}
          </p>
        </div>
      </div>

      <ul className="divide-y divide-zinc-100 overflow-hidden rounded-xl border border-zinc-200 dark:divide-zinc-800 dark:border-zinc-800">
        {rows.map((check) => {
          const Icon = check.passed === null ? CircleDashed : check.passed ? CircleCheck : CircleX;
          return (
            <li key={check.name} className="flex gap-3 px-3 py-2.5">
              <Icon
                aria-hidden="true"
                className={clsx(
                  'mt-0.5 size-4 shrink-0',
                  check.passed === null ? 'text-zinc-300 dark:text-zinc-600' : check.passed ? 'text-emerald-500' : 'text-red-500',
                )}
              />
              <div className="min-w-0">
                <p className="text-[0.8125rem] font-medium text-zinc-900 dark:text-zinc-100">
                  {check.label}
                  <span className="sr-only">
                    {check.passed === null ? ': not run' : check.passed ? ': passed' : ': failed'}
                  </span>
                </p>
                <p className="text-xs leading-relaxed break-words text-zinc-500 dark:text-zinc-400">{check.message}</p>
              </div>
            </li>
          );
        })}
      </ul>

      {validation.errors.length > 0 && (
        <section>
          <SectionLabel className="mb-2">Errors</SectionLabel>
          <ul className="space-y-1.5">
            {validation.errors.map((error) => (
              <li key={error} className="flex gap-2 text-[0.8125rem] text-red-700 dark:text-red-300">
                <CircleX aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
                <span className="min-w-0 break-words">{error}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {validation.warnings.length > 0 && (
        <section>
          <SectionLabel className="mb-2">Warnings</SectionLabel>
          <ul className="space-y-1.5">
            {validation.warnings.map((warning) => (
              <li key={warning} className="flex gap-2 text-[0.8125rem] text-amber-800 dark:text-amber-300">
                <TriangleAlert aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
                <span className="min-w-0 break-words">{warning}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {(validation.tables.length > 0 || validation.columns.length > 0) && (
        <section>
          <SectionLabel className="mb-2">Referenced schema</SectionLabel>
          <div className="flex flex-wrap gap-1.5">
            {validation.tables.map((table) => (
              <span
                key={`t-${table}`}
                className="rounded-md bg-indigo-50 px-1.5 py-0.5 font-mono text-[0.6875rem] font-medium text-indigo-700 dark:bg-indigo-400/10 dark:text-indigo-300"
              >
                {table}
              </span>
            ))}
            {validation.columns.map((column) => (
              <span
                key={`c-${column}`}
                className="rounded-md bg-zinc-100 px-1.5 py-0.5 font-mono text-[0.6875rem] text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
              >
                {column}
              </span>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}
