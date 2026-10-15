import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { makeResponse } from '../../test/fixtures';
import { SqlPanel } from './SqlPanel';

describe('SqlPanel', () => {
  it('shows the requested dialect and can switch to the executed SQLite query', async () => {
    const user = userEvent.setup();
    render(
      <SqlPanel
        response={makeResponse({
          dialect: 'postgres',
          sql: "SELECT TO_CHAR(CAST(OrderDate AS TIMESTAMP), 'YYYY-MM') AS Month FROM Orders",
          sql_sqlite: "SELECT STRFTIME('%Y-%m', OrderDate) AS Month FROM Orders",
        })}
      />,
    );

    const panel = screen.getByRole('region', { name: 'SQL' });
    expect(panel).toHaveTextContent('TO_CHAR');
    expect(screen.getAllByText('PostgreSQL').length).toBeGreaterThan(0);

    await user.click(screen.getByRole('button', { name: 'SQLite (executed)' }));
    expect(panel).toHaveTextContent('STRFTIME');
    expect(screen.getByRole('button', { name: 'SQLite (executed)' })).toHaveAttribute('aria-pressed', 'true');

    await user.click(screen.getByRole('button', { name: 'Copy SQL' }));
    await expect(navigator.clipboard.readText()).resolves.toContain('STRFTIME');
  });

  it('has no dialect switch for SQLite output', () => {
    render(<SqlPanel response={makeResponse()} />);
    expect(screen.queryByRole('button', { name: 'SQLite (executed)' })).not.toBeInTheDocument();
  });
});
