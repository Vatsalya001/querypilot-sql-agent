/** Shared test data shaped exactly like docs/API.md. */
import type {
  AgentResponse,
  AssistantChatMessage,
  ChatMessage,
  Conversation,
  StepEvent,
  UserChatMessage,
} from '../types';

export const SQL = "SELECT\n  *\nFROM Customers\nWHERE\n  State = 'California'";

export function makeSteps(): StepEvent[] {
  return [
    { node: 'guard_input', label: 'Checking input safety', status: 'completed', duration_ms: 1.2, detail: null },
    { node: 'detect_intent', label: 'Detecting intent', status: 'completed', duration_ms: 412.3, detail: 'modify_sql (0.97)' },
    { node: 'generate_sql', label: 'Generating SQL', status: 'completed', duration_ms: 1520.4, detail: null },
    { node: 'validate_sql', label: 'Validating SQL', status: 'completed', duration_ms: 8.1, detail: 'valid' },
  ];
}

export function makeResponse(overrides: Partial<AgentResponse> = {}): AgentResponse {
  return {
    thread_id: 'thread-1',
    message_id: 'message-1',
    intent: 'modify_sql',
    status: 'success',
    message: 'Here are all customers located in California.',
    sql: SQL,
    sql_sqlite: SQL,
    dialect: 'sqlite',
    explanation: 'This query retrieves every customer whose state is California.',
    validation: {
      valid: true,
      checks: [
        { name: 'syntax', label: 'Syntax', passed: true, message: 'Valid SQLite syntax.' },
        { name: 'single_statement', label: 'Single statement', passed: true, message: 'Exactly one statement.' },
        { name: 'read_only', label: 'Read-only', passed: true, message: 'SELECT only.' },
        { name: 'tables', label: 'Tables exist', passed: true, message: 'Customers' },
        { name: 'columns', label: 'Columns exist', passed: true, message: 'All 1 referenced columns exist.' },
        { name: 'joins', label: 'Join relationships', passed: true, message: 'No joins.' },
        { name: 'executable', label: 'Database dry-run', passed: true, message: 'Query plan compiled.' },
      ],
      errors: [],
      warnings: [],
      sql: "SELECT * FROM Customers WHERE State = 'California'",
      tables: ['Customers'],
      columns: ['Customers.State'],
      destructive: false,
    },
    optimization: {
      formatted_sql: SQL,
      suggestions: [
        { category: 'readability', severity: 'info', message: 'Select only the columns you need instead of SELECT *.' },
      ],
      index_recommendations: ['CREATE INDEX idx_customers_state ON Customers (State);'],
      unnecessary_joins: [],
    },
    execution: {
      columns: ['CustomerID', 'FirstName', 'State'],
      rows: [
        [1, 'Ava', 'California'],
        [2, 'Noah', null],
      ],
      row_count: 2,
      truncated: false,
      elapsed_ms: 1.7,
      error: null,
    },
    cost: {
      level: 'low',
      score: 300,
      estimated_rows_scanned: 300,
      plan: ['SCAN Customers'],
      notes: ['Full table scan on Customers (300 rows).'],
    },
    issues: [],
    changes: [],
    suggestions: [],
    assumptions: [],
    clarification_question: null,
    schema_tables: ['Customers'],
    attempts: 1,
    steps: makeSteps(),
    model: 'qwen3.5:9b',
    ...overrides,
  };
}

let counter = 0;

export function userMessage(content: string, createdAt = 1_000): UserChatMessage {
  counter += 1;
  return { id: `u-${counter}`, role: 'user', content, created_at: createdAt };
}

export function assistantMessage(
  overrides: Partial<AssistantChatMessage> = {},
  createdAt = 2_000,
): AssistantChatMessage {
  counter += 1;
  return {
    id: `a-${counter}`,
    role: 'assistant',
    created_at: createdAt,
    prompt: 'Only those from California',
    state: 'done',
    response: makeResponse(),
    error: null,
    steps: makeSteps(),
    elapsed_ms: 2300,
    ...overrides,
  };
}

export function conversation(
  id: string,
  updatedAt: number,
  messages: ChatMessage[] = [userMessage('Show all customers')],
): Conversation {
  return { id, thread_id: id, title: `Chat ${id}`, created_at: updatedAt, updated_at: updatedAt, messages };
}
