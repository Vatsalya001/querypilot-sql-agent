/**
 * Types shared with the backend. They mirror docs/API.md field for field (snake_case keys,
 * nullable fields always present as `null`).
 */

// ---------------------------------------------------------------------------------------------
// Enumerations
// ---------------------------------------------------------------------------------------------

/** Output dialect of the returned SQL. Validation and execution always run on SQLite. */
export type Dialect = 'sqlite' | 'postgres' | 'mysql';

export type Intent =
  | 'generate_sql'
  | 'modify_sql'
  | 'optimize_sql'
  | 'debug_sql'
  | 'explain_sql'
  | 'schema_info'
  | 'sql_help'
  | 'destructive'
  | 'out_of_scope'
  | 'ambiguous';

export type ResponseStatus = 'success' | 'answered' | 'clarification' | 'rejected' | 'refused' | 'failed';

/** LangGraph node names, in pipeline order. */
export type NodeName =
  | 'guard_input'
  | 'detect_intent'
  | 'validate_scope'
  | 'retrieve_schema'
  | 'diagnose_sql'
  | 'generate_sql'
  | 'validate_sql'
  | 'optimize_sql'
  | 'execute_sql'
  | 'explain_sql'
  | 'answer_question'
  | 'ask_clarification'
  | 'reject_request'
  | 'refuse_destructive'
  | 'handle_failure'
  | 'respond';

export type StepStatus = 'started' | 'completed';

export type TokenTarget = 'explanation' | 'message';

export type StreamErrorCode = 'llm_not_configured' | 'rate_limited' | 'llm_error' | 'internal';

export type ValidationCheckName =
  | 'syntax'
  | 'single_statement'
  | 'read_only'
  | 'tables'
  | 'columns'
  | 'joins'
  | 'executable';

export type CostLevel = 'low' | 'medium' | 'high';

// ---------------------------------------------------------------------------------------------
// GET /api/health
// ---------------------------------------------------------------------------------------------

export interface LLMStatus {
  provider: string;
  model: string;
  fast_model: string;
  fallback_models: string[];
  /** false: the provider API key is missing or the local model is unreachable. */
  configured: boolean;
  /** true: LLM_PROVIDER=mock, the offline rule-based gateway. */
  mock: boolean;
  /** Startup prompt-cache warm-up of the local model ("skipped" for hosted providers and mock). */
  warmup?: 'warming' | 'ready' | 'skipped' | 'failed';
}

export interface DatabaseStatus {
  dialect: string;
  tables: number;
}

export interface HealthResponse {
  status: 'ok';
  version: string;
  llm: LLMStatus;
  database: DatabaseStatus;
}

// ---------------------------------------------------------------------------------------------
// GET /api/schema
// ---------------------------------------------------------------------------------------------

export interface ForeignKeyRef {
  table: string;
  column: string;
}

export interface SchemaColumn {
  name: string;
  type: string;
  primary_key: boolean;
  nullable: boolean;
  description: string;
  foreign_key: ForeignKeyRef | null;
  sample_values: SqlValue[];
}

export interface SchemaIndex {
  name: string;
  columns: string[];
  unique: boolean;
}

export interface SchemaTable {
  name: string;
  description: string;
  row_count: number;
  columns: SchemaColumn[];
  indexes: SchemaIndex[];
}

export interface Relationship {
  from_table: string;
  from_column: string;
  to_table: string;
  to_column: string;
}

export interface SchemaResponse {
  dialect: string;
  tables: SchemaTable[];
  relationships: Relationship[];
}

// ---------------------------------------------------------------------------------------------
// GET /api/graph, DELETE /api/threads/{thread_id}
// ---------------------------------------------------------------------------------------------

export interface GraphResponse {
  mermaid: string;
  nodes: string[];
}

export interface ThreadResetResponse {
  ok: boolean;
}

// ---------------------------------------------------------------------------------------------
// POST /api/chat/stream and POST /api/chat
// ---------------------------------------------------------------------------------------------

export interface HistoryItem {
  role: 'user' | 'assistant';
  content: string;
  sql: string | null;
}

export interface ChatRequest {
  /** 1..2000 characters. */
  message: string;
  thread_id: string | null;
  dialect: Dialect;
  execute: boolean;
  /** The last <= 10 turns; used by the server only to rehydrate an unknown thread. */
  history: HistoryItem[] | null;
}

/** Error body returned before streaming starts (422, 429, 5xx). */
export interface ApiErrorBody {
  detail: string;
  code: string;
  retry_after?: number | null;
}

// --- SSE event payloads -----------------------------------------------------------------------

export interface StartEvent {
  thread_id: string;
  message_id: string;
}

export interface StepEvent {
  node: NodeName;
  label: string;
  status: StepStatus;
  duration_ms: number | null;
  detail: string | null;
}

export interface TokenEvent {
  node: NodeName;
  target: TokenTarget;
  text: string;
}

export interface ErrorEvent {
  message: string;
  code: StreamErrorCode;
}

export type DoneEvent = Record<string, never>;

export type StreamEvent =
  | { event: 'start'; data: StartEvent }
  | { event: 'step'; data: StepEvent }
  | { event: 'token'; data: TokenEvent }
  | { event: 'final'; data: AgentResponse }
  | { event: 'error'; data: ErrorEvent }
  | { event: 'done'; data: DoneEvent };

// ---------------------------------------------------------------------------------------------
// AgentResponse
// ---------------------------------------------------------------------------------------------

/** A JSON scalar from a result row (or a sample value). */
export type SqlValue = string | number | null;

export interface ValidationCheck {
  name: ValidationCheckName;
  label: string;
  passed: boolean;
  message: string;
}

export interface ValidationResult {
  valid: boolean;
  checks: ValidationCheck[];
  errors: string[];
  warnings: string[];
  sql: string;
  tables: string[];
  columns: string[];
  destructive: boolean;
}

export interface OptimizationSuggestion {
  category: string;
  severity: string;
  message: string;
}

export interface OptimizationResult {
  formatted_sql: string;
  suggestions: OptimizationSuggestion[];
  index_recommendations: string[];
  unnecessary_joins: string[];
}

export interface ExecutionResult {
  columns: string[];
  rows: SqlValue[][];
  row_count: number;
  truncated: boolean;
  elapsed_ms: number;
  /** Runtime database error; the query is still returned. */
  error: string | null;
}

export interface CostEstimate {
  level: CostLevel;
  score: number;
  estimated_rows_scanned: number;
  plan: string[];
  notes: string[];
}

export interface DebugIssue {
  problem: string;
  reason: string;
}

export interface AgentResponse {
  thread_id: string;
  message_id: string;
  /** null when the request was blocked before classification. */
  intent: Intent | null;
  status: ResponseStatus;
  message: string;
  sql: string | null;
  sql_sqlite: string | null;
  dialect: Dialect;
  explanation: string | null;
  validation: ValidationResult | null;
  optimization: OptimizationResult | null;
  /** null when execute=false or when the response has no SQL. */
  execution: ExecutionResult | null;
  cost: CostEstimate | null;
  issues: DebugIssue[];
  changes: string[];
  suggestions: string[];
  assumptions: string[];
  clarification_question: string | null;
  schema_tables: string[];
  attempts: number;
  steps: StepEvent[];
  model: string | null;
}

// ---------------------------------------------------------------------------------------------
// Client-side conversation model (persisted in localStorage, not part of the HTTP contract)
// ---------------------------------------------------------------------------------------------

export interface UserChatMessage {
  id: string;
  role: 'user';
  content: string;
  created_at: number;
}

/** A failed request: an SSE `error` event, an HTTP error, or a network failure. */
export interface ChatError {
  message: string;
  code: string;
  retry_after: number | null;
}

export interface AssistantChatMessage {
  id: string;
  role: 'assistant';
  created_at: number;
  /** The user prompt this message answers (used by Retry). */
  prompt: string;
  /** done: `response` is set; error: `error` is set; stopped: the user pressed Stop. */
  state: 'done' | 'error' | 'stopped';
  response: AgentResponse | null;
  error: ChatError | null;
  /** Pipeline steps as streamed (started and completed events). */
  steps: StepEvent[];
  /** Wall-clock time of the request in milliseconds. */
  elapsed_ms: number | null;
}

export type ChatMessage = UserChatMessage | AssistantChatMessage;

export interface Conversation {
  id: string;
  /** Server-side conversation id (a UUID generated by the client). */
  thread_id: string;
  title: string;
  created_at: number;
  updated_at: number;
  messages: ChatMessage[];
}
