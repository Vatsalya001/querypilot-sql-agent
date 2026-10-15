import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { assistantMessage, makeResponse } from '../test/fixtures';
import { AssistantMessage } from './AssistantMessage';

function renderAnswer(response = makeResponse()) {
  render(
    <AssistantMessage
      message={assistantMessage({ response })}
      selected={false}
      busy={false}
      onOpenDetails={vi.fn()}
      onRetry={vi.fn()}
    />,
  );
}

describe('AssistantMessage', () => {
  it('credits the repair loop when a later attempt produced valid SQL', () => {
    renderAnswer(makeResponse({ attempts: 2 }));
    expect(screen.getByText('Repaired after 2 attempts')).toBeInTheDocument();
  });

  it('does not claim a repair when every attempt failed validation', () => {
    const invalid = makeResponse().validation!;
    renderAnswer(
      makeResponse({
        intent: 'debug_sql',
        status: 'failed',
        message: "I couldn't produce a valid SQL query for this request, even after automatic repair.",
        sql: null,
        sql_sqlite: null,
        explanation: null,
        validation: { ...invalid, valid: false, errors: ["Unknown table 'Employes'."] },
        optimization: null,
        execution: null,
        cost: null,
        attempts: 3,
      }),
    );
    expect(screen.getByText('Validation failed')).toBeInTheDocument();
    expect(screen.queryByText(/Repaired/)).not.toBeInTheDocument();
    expect(screen.getByText('3 attempts')).toBeInTheDocument();
  });
});
