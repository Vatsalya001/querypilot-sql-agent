/** LangGraph pipeline metadata for the step tracker and the "How it works" view (docs/API.md). */
import type { NodeName } from '../types';

export type PipelinePhase = 'safety' | 'sql' | 'outcome' | 'response';

export interface PipelineNodeInfo {
  node: NodeName;
  label: string;
  description: string;
  phase: PipelinePhase;
}

export const PIPELINE_NODES: readonly PipelineNodeInfo[] = [
  {
    node: 'guard_input',
    label: 'Checking input safety',
    description: 'Screens the raw message for prompt injection, destructive SQL and size limits before any LLM call.',
    phase: 'safety',
  },
  {
    node: 'detect_intent',
    label: 'Detecting intent',
    description: 'Classifies the request: generate, modify, optimize, debug or explain SQL, a schema question, or out of scope.',
    phase: 'safety',
  },
  {
    node: 'validate_scope',
    label: 'Validating scope',
    description: 'Routes out-of-scope, destructive and ambiguous requests away from SQL generation.',
    phase: 'safety',
  },
  {
    node: 'retrieve_schema',
    label: 'Retrieving schema',
    description: 'Selects the tables, columns and relationships relevant to the question to ground the prompt.',
    phase: 'sql',
  },
  {
    node: 'diagnose_sql',
    label: 'Analyzing your SQL',
    description: 'Validates SQL you pasted to pinpoint concrete problems before debugging or optimizing it.',
    phase: 'sql',
  },
  {
    node: 'generate_sql',
    label: 'Generating SQL',
    description: 'Writes a read-only query from the schema only; retries with validator feedback when a check fails.',
    phase: 'sql',
  },
  {
    node: 'validate_sql',
    label: 'Validating SQL',
    description: 'Seven deterministic checks: syntax, single statement, read-only, tables, columns, joins and a dry-run.',
    phase: 'sql',
  },
  {
    node: 'optimize_sql',
    label: 'Optimizing SQL',
    description: 'Formats the query and suggests indexes, cheaper patterns and joins that can be removed.',
    phase: 'sql',
  },
  {
    node: 'execute_sql',
    label: 'Executing query',
    description: 'Runs the query on a read-only SQLite connection with a row limit and estimates its cost.',
    phase: 'sql',
  },
  {
    node: 'explain_sql',
    label: 'Explaining query',
    description: 'Describes in plain English what the query returns, streamed while it is written.',
    phase: 'sql',
  },
  {
    node: 'answer_question',
    label: 'Answering question',
    description: 'Answers questions about the schema or SQL concepts in prose.',
    phase: 'outcome',
  },
  {
    node: 'ask_clarification',
    label: 'Asking for clarification',
    description: 'Asks one focused question when the request is ambiguous.',
    phase: 'outcome',
  },
  {
    node: 'reject_request',
    label: 'Rejecting out-of-scope request',
    description: 'Politely declines requests unrelated to SQL or this database, and prompt-injection attempts.',
    phase: 'outcome',
  },
  {
    node: 'refuse_destructive',
    label: 'Blocking destructive request',
    description: 'Refuses DELETE, UPDATE, INSERT, DROP, ALTER and TRUNCATE: the agent is strictly read-only.',
    phase: 'outcome',
  },
  {
    node: 'handle_failure',
    label: 'Handling failure',
    description: 'Explains why no valid SQL could be produced after the repair attempts.',
    phase: 'outcome',
  },
  {
    node: 'respond',
    label: 'Preparing response',
    description: 'Assembles the final response and saves the turn in conversation memory.',
    phase: 'response',
  },
];

export const PHASE_TITLES: Record<PipelinePhase, string> = {
  safety: 'Safety and routing',
  sql: 'SQL pipeline',
  outcome: 'Other outcomes',
  response: 'Response',
};

const NODE_INFO = new Map<string, PipelineNodeInfo>(PIPELINE_NODES.map((info) => [info.node, info]));

export function nodeInfo(node: string): PipelineNodeInfo | undefined {
  return NODE_INFO.get(node);
}

/** Nodes that the self-correction loop can run more than once per request. */
export const REPAIR_LOOP_NODES: ReadonlySet<string> = new Set(['generate_sql', 'validate_sql']);
