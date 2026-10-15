import type { AgentResponse } from '../../types';

export type InspectorTab = 'explanation' | 'validation' | 'optimization' | 'results' | 'cost';

/** The most useful tab for a response: results when it ran, validation when it failed, else explanation. */
export function preferredTab(response: AgentResponse | null): InspectorTab {
  if (!response) return 'explanation';
  if (response.status === 'failed' || response.status === 'refused') return 'validation';
  if (response.execution) return 'results';
  return 'explanation';
}

/** True when the response carries SQL artefacts worth inspecting (SQL, validation, results, ...). */
export function hasSqlDetails(response: AgentResponse): boolean {
  return Boolean(
    response.sql || response.validation || response.optimization || response.execution || response.cost,
  );
}
