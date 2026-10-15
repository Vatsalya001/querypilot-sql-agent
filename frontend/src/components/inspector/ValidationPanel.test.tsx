import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { makeResponse } from '../../test/fixtures';
import { ValidationPanel } from './ValidationPanel';
import type { ValidationResult } from '../../types';

const VALID = makeResponse().validation!;

/** What the validator returns for unparsable SQL: syntax fails and every later check is skipped. */
const UNPARSABLE: ValidationResult = {
  ...VALID,
  valid: false,
  checks: VALID.checks.map((check) =>
    check.name === 'syntax'
      ? { ...check, passed: false, message: "Syntax error near 'WHERE'." }
      : { ...check, passed: false, message: 'Skipped: syntax error.' },
  ),
  errors: ["Syntax error near 'WHERE'."],
  sql: '',
  tables: [],
  columns: [],
  destructive: false,
};

describe('ValidationPanel', () => {
  it('summarises a passing query as read-only', () => {
    render(<ValidationPanel validation={VALID} />);
    expect(screen.getByText('All checks passed')).toBeInTheDocument();
    expect(screen.getByText(/7 of 7 checks passed · read-only/)).toBeInTheDocument();
  });

  it('does not call a query read-only when the read-only check did not pass', () => {
    render(<ValidationPanel validation={UNPARSABLE} />);
    expect(screen.getByText('Validation failed')).toBeInTheDocument();
    const summary = screen.getByText(/0 of 7 checks passed/);
    expect(summary).not.toHaveTextContent('read-only');
  });

  it('flags destructive statements', () => {
    render(<ValidationPanel validation={{ ...UNPARSABLE, destructive: true }} />);
    expect(screen.getByText(/destructive statement detected/)).toBeInTheDocument();
  });
});
