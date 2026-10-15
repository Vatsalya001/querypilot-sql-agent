import { describe, expect, it } from 'vitest';
import { buildStepRows } from './StepTracker';
import type { NodeName, StepEvent } from '../types';

const started = (node: NodeName, label: string): StepEvent => ({ node, label, status: 'started', duration_ms: null, detail: null });
const completed = (node: NodeName, label: string, duration_ms: number, detail: string | null = null): StepEvent => ({
  node,
  label,
  status: 'completed',
  duration_ms,
  detail,
});

describe('buildStepRows', () => {
  it('pairs started and completed events and numbers repair attempts', () => {
    const rows = buildStepRows([
      started('generate_sql', 'Generating SQL'),
      completed('generate_sql', 'Generating SQL', 900),
      started('validate_sql', 'Validating SQL'),
      completed('validate_sql', 'Validating SQL', 4, 'invalid: no such column'),
      started('generate_sql', 'Generating SQL'),
      completed('generate_sql', 'Generating SQL', 700),
      started('validate_sql', 'Validating SQL'),
    ]);
    expect(rows.map((row) => [row.node, row.status, row.attempt])).toEqual([
      ['generate_sql', 'done', null],
      ['validate_sql', 'done', null],
      ['generate_sql', 'done', 1],
      ['validate_sql', 'running', 1],
    ]);
    expect(rows[1]).toMatchObject({ duration_ms: 4, detail: 'invalid: no such column' });
  });

  it('turns completed-only events (AgentResponse.steps) into finished rows', () => {
    const rows = buildStepRows([completed('guard_input', 'Checking input safety', 1), completed('respond', 'Preparing response', 2)]);
    expect(rows.map((row) => row.status)).toEqual(['done', 'done']);
  });

  it('marks unfinished steps as interrupted once the request has settled', () => {
    const steps = [started('detect_intent', 'Detecting intent')];
    expect(buildStepRows(steps)[0].status).toBe('running');
    expect(buildStepRows(steps, true)[0].status).toBe('interrupted');
  });
});
