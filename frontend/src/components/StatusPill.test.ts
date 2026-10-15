import { describe, expect, it } from 'vitest';
import type { HealthResponse } from '../types';
import { describeHealth } from './StatusPill';

const health: HealthResponse = {
  status: 'ok',
  version: '1.0.0',
  llm: {
    provider: 'ollama',
    model: 'qwen3.5:9b',
    fast_model: 'qwen3.5:9b',
    fallback_models: [],
    configured: true,
    mock: false,
  },
  database: { dialect: 'sqlite', tables: 7 },
};

describe('describeHealth', () => {
  it('shows a pulsing warm-up pill while the local model primes its prompt cache', () => {
    const info = describeHealth({ status: 'ok', health: { ...health, llm: { ...health.llm, warmup: 'warming' } } });
    expect(info.tone).toBe('amber');
    expect(info.label).toContain('Warming up');
    expect(info.pulse).toBe(true);
  });

  it('shows the connected model once the warm-up is done or not applicable', () => {
    for (const warmup of ['ready', 'skipped', undefined] as const) {
      const info = describeHealth({ status: 'ok', health: { ...health, llm: { ...health.llm, warmup } } });
      expect(info.tone).toBe('green');
      expect(info.label).toContain('qwen3.5:9b');
      expect(info.pulse).toBeFalsy();
    }
  });
});
