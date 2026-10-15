/**
 * localStorage persistence for conversations and UI settings.
 *
 * Everything read back is treated as untrusted: corrupt JSON or unexpected shapes are dropped instead
 * of crashing the app. Conversations are capped (30 conversations x 100 messages) and, when the
 * browser quota is exceeded, compacted step by step (result rows first, then the oldest chats).
 */
import type {
  AgentResponse,
  AssistantChatMessage,
  ChatError,
  ChatMessage,
  Conversation,
  CostEstimate,
  DebugIssue,
  Dialect,
  ExecutionResult,
  Intent,
  NodeName,
  OptimizationResult,
  OptimizationSuggestion,
  ResponseStatus,
  SqlValue,
  StepEvent,
  UserChatMessage,
  ValidationCheck,
  ValidationResult,
} from '../types';

export const CONVERSATIONS_KEY = 'querypilot.conversations.v1';
export const SETTINGS_KEY = 'querypilot.settings.v1';
export const THEME_KEY = 'querypilot.theme';
export const ACTIVE_CONVERSATION_KEY = 'querypilot.activeConversation.v1';
export const MAX_CONVERSATIONS = 30;
export const MAX_MESSAGES_PER_CONVERSATION = 100;
/** Result rows kept per message when the storage quota forces compaction. */
export const COMPACT_RESULT_ROWS = 50;

export type StorageLike = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

/** The browser's localStorage, or null when it is unavailable (privacy modes, sandboxed iframes). */
export function browserStorage(): StorageLike | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null;
  } catch {
    return null;
  }
}

/** Parse the JSON stored under `key`; null when missing, unreadable or corrupt. */
export function readJSON(key: string, storage: StorageLike | null = browserStorage()): unknown {
  try {
    const raw = storage?.getItem(key);
    return raw ? (JSON.parse(raw) as unknown) : null;
  } catch {
    return null;
  }
}

/** Store `value` as JSON; false when storage is unavailable or the quota is exceeded. */
export function writeJSON(key: string, value: unknown, storage: StorageLike | null = browserStorage()): boolean {
  if (!storage) return false;
  try {
    storage.setItem(key, JSON.stringify(value));
    return true;
  } catch {
    return false;
  }
}

// ---------------------------------------------------------------------------------------------
// Validation of stored data
// ---------------------------------------------------------------------------------------------

type JsonRecord = Record<string, unknown>;

function isRecord(value: unknown): value is JsonRecord {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

/** `value` if it is one of `options`, else undefined. */
function oneOf<T extends string>(options: readonly T[], value: unknown): T | undefined {
  return options.find((option) => option === value);
}

const DIALECTS: readonly Dialect[] = ['sqlite', 'postgres', 'mysql'];
const STATUSES: readonly ResponseStatus[] = ['success', 'answered', 'clarification', 'rejected', 'refused', 'failed'];
const INTENTS: readonly Intent[] = [
  'generate_sql',
  'modify_sql',
  'optimize_sql',
  'debug_sql',
  'explain_sql',
  'schema_info',
  'sql_help',
  'destructive',
  'out_of_scope',
  'ambiguous',
];
const COST_LEVELS: readonly CostEstimate['level'][] = ['low', 'medium', 'high'];
const STEP_STATUSES: readonly StepEvent['status'][] = ['started', 'completed'];

function sanitizeSteps(value: unknown): StepEvent[] {
  if (!Array.isArray(value)) return [];
  const steps: StepEvent[] = [];
  for (const step of value) {
    if (!isRecord(step) || typeof step.node !== 'string' || typeof step.label !== 'string') continue;
    steps.push({
      node: step.node as NodeName,
      label: step.label,
      status: oneOf(STEP_STATUSES, step.status) ?? 'completed',
      duration_ms: isFiniteNumber(step.duration_ms) ? step.duration_ms : null,
      detail: stringOrNull(step.detail),
    });
  }
  return steps;
}

function sanitizeValidation(value: unknown): ValidationResult | null {
  if (!isRecord(value) || typeof value.valid !== 'boolean') return null;
  return {
    valid: value.valid,
    checks: Array.isArray(value.checks)
      ? value.checks.filter(
          (check): check is ValidationCheck =>
            isRecord(check) &&
            typeof check.name === 'string' &&
            typeof check.label === 'string' &&
            typeof check.passed === 'boolean' &&
            typeof check.message === 'string',
        )
      : [],
    errors: stringArray(value.errors),
    warnings: stringArray(value.warnings),
    sql: typeof value.sql === 'string' ? value.sql : '',
    tables: stringArray(value.tables),
    columns: stringArray(value.columns),
    destructive: value.destructive === true,
  };
}

function sanitizeOptimization(value: unknown): OptimizationResult | null {
  if (!isRecord(value)) return null;
  return {
    formatted_sql: typeof value.formatted_sql === 'string' ? value.formatted_sql : '',
    suggestions: Array.isArray(value.suggestions)
      ? value.suggestions.filter(
          (item): item is OptimizationSuggestion =>
            isRecord(item) &&
            typeof item.category === 'string' &&
            typeof item.severity === 'string' &&
            typeof item.message === 'string',
        )
      : [],
    index_recommendations: stringArray(value.index_recommendations),
    unnecessary_joins: stringArray(value.unnecessary_joins),
  };
}

function isSqlValue(value: unknown): value is SqlValue {
  return value === null || typeof value === 'string' || typeof value === 'number';
}

function sanitizeExecution(value: unknown): ExecutionResult | null {
  if (!isRecord(value) || !Array.isArray(value.columns) || !Array.isArray(value.rows)) return null;
  const rows = value.rows.filter((row): row is SqlValue[] => Array.isArray(row) && row.every(isSqlValue));
  return {
    columns: stringArray(value.columns),
    rows,
    row_count: isFiniteNumber(value.row_count) ? value.row_count : rows.length,
    truncated: value.truncated === true,
    elapsed_ms: isFiniteNumber(value.elapsed_ms) ? value.elapsed_ms : 0,
    error: stringOrNull(value.error),
  };
}

function sanitizeCost(value: unknown): CostEstimate | null {
  if (!isRecord(value)) return null;
  const level = oneOf(COST_LEVELS, value.level);
  if (!level) return null;
  return {
    level,
    score: isFiniteNumber(value.score) ? value.score : 0,
    estimated_rows_scanned: isFiniteNumber(value.estimated_rows_scanned) ? value.estimated_rows_scanned : 0,
    plan: stringArray(value.plan),
    notes: stringArray(value.notes),
  };
}

/**
 * Rebuild a stored AgentResponse field by field, so render code can rely on every field's type: lists
 * that older or partial records lack become empty, malformed nested objects become null, and a response
 * with an unknown status (which no view can render) is dropped.
 */
function sanitizeResponse(value: unknown): AgentResponse | null {
  if (!isRecord(value)) return null;
  const status = oneOf(STATUSES, value.status);
  if (!status) return null;
  return {
    thread_id: typeof value.thread_id === 'string' ? value.thread_id : '',
    message_id: typeof value.message_id === 'string' ? value.message_id : '',
    intent: oneOf(INTENTS, value.intent) ?? null,
    status,
    message: typeof value.message === 'string' ? value.message : '',
    sql: stringOrNull(value.sql),
    sql_sqlite: stringOrNull(value.sql_sqlite),
    dialect: oneOf(DIALECTS, value.dialect) ?? 'sqlite',
    explanation: stringOrNull(value.explanation),
    validation: sanitizeValidation(value.validation),
    optimization: sanitizeOptimization(value.optimization),
    execution: sanitizeExecution(value.execution),
    cost: sanitizeCost(value.cost),
    issues: Array.isArray(value.issues)
      ? value.issues.filter(
          (issue): issue is DebugIssue =>
            isRecord(issue) && typeof issue.problem === 'string' && typeof issue.reason === 'string',
        )
      : [],
    changes: stringArray(value.changes),
    suggestions: stringArray(value.suggestions),
    assumptions: stringArray(value.assumptions),
    clarification_question: stringOrNull(value.clarification_question),
    schema_tables: stringArray(value.schema_tables),
    attempts: isFiniteNumber(value.attempts) ? value.attempts : 0,
    steps: sanitizeSteps(value.steps),
    model: stringOrNull(value.model),
  };
}

function sanitizeError(value: unknown): ChatError | null {
  if (!isRecord(value)) return null;
  return {
    message: typeof value.message === 'string' ? value.message : 'The request failed.',
    code: typeof value.code === 'string' ? value.code : 'unknown',
    retry_after: isFiniteNumber(value.retry_after) ? value.retry_after : null,
  };
}

const ASSISTANT_STATES: readonly AssistantChatMessage['state'][] = ['done', 'error', 'stopped'];

function sanitizeMessage(value: unknown): ChatMessage | null {
  if (!isRecord(value) || typeof value.id !== 'string' || !isFiniteNumber(value.created_at)) return null;

  if (value.role === 'user') {
    if (typeof value.content !== 'string') return null;
    const message: UserChatMessage = { id: value.id, role: 'user', content: value.content, created_at: value.created_at };
    return message;
  }

  if (value.role === 'assistant') {
    const response = sanitizeResponse(value.response);
    let error = sanitizeError(value.error);
    const storedState = ASSISTANT_STATES.find((candidate) => candidate === value.state);
    const state: AssistantChatMessage['state'] = storedState ?? (response ? 'done' : 'error');
    if (state === 'done' && !response) return null;
    if (state === 'error' && !error) error = { message: 'The request failed.', code: 'unknown', retry_after: null };
    const message: AssistantChatMessage = {
      id: value.id,
      role: 'assistant',
      created_at: value.created_at,
      prompt: typeof value.prompt === 'string' ? value.prompt : '',
      state,
      response: state === 'done' ? response : null,
      error: state === 'error' ? error : null,
      steps: sanitizeSteps(value.steps),
      elapsed_ms: isFiniteNumber(value.elapsed_ms) ? value.elapsed_ms : null,
    };
    return message;
  }
  return null;
}

function sanitizeConversation(value: unknown): Conversation | null {
  if (!isRecord(value) || typeof value.id !== 'string' || !value.id) return null;
  const messages = Array.isArray(value.messages)
    ? value.messages.map(sanitizeMessage).filter((message): message is ChatMessage => message !== null)
    : [];
  if (messages.length === 0) return null;
  const createdAt = isFiniteNumber(value.created_at) ? value.created_at : messages[0].created_at;
  const firstUser = messages.find((message): message is UserChatMessage => message.role === 'user');
  return {
    id: value.id,
    thread_id: typeof value.thread_id === 'string' && value.thread_id ? value.thread_id : value.id,
    title:
      typeof value.title === 'string' && value.title.trim() ? value.title : (firstUser?.content ?? 'Untitled chat'),
    created_at: createdAt,
    updated_at: isFiniteNumber(value.updated_at) ? value.updated_at : createdAt,
    messages,
  };
}

// ---------------------------------------------------------------------------------------------
// Conversations
// ---------------------------------------------------------------------------------------------

/** Newest first, at most MAX_CONVERSATIONS, each with its newest MAX_MESSAGES_PER_CONVERSATION messages. */
export function capConversations(conversations: readonly Conversation[]): Conversation[] {
  return [...conversations]
    .sort((a, b) => b.updated_at - a.updated_at)
    .slice(0, MAX_CONVERSATIONS)
    .map((conversation) =>
      conversation.messages.length > MAX_MESSAGES_PER_CONVERSATION
        ? { ...conversation, messages: conversation.messages.slice(-MAX_MESSAGES_PER_CONVERSATION) }
        : conversation,
    );
}

/** Load saved conversations; corrupt or invalid entries are skipped. */
export function loadConversations(storage: StorageLike | null = browserStorage()): Conversation[] {
  const raw = readJSON(CONVERSATIONS_KEY, storage);
  if (!Array.isArray(raw)) return [];
  const seen = new Set<string>();
  const conversations: Conversation[] = [];
  for (const item of raw) {
    const conversation = sanitizeConversation(item);
    if (conversation && !seen.has(conversation.id)) {
      seen.add(conversation.id);
      conversations.push(conversation);
    }
  }
  return capConversations(conversations);
}

/** Error shown for a question whose answer was still streaming when the page was closed or reloaded. */
export const INTERRUPTED_REQUEST_MESSAGE =
  'The page was closed or reloaded before this answer arrived. Retry to ask again.';

/**
 * Every question gets an assistant message when its request settles, so a conversation that ends with a
 * user message was interrupted by a page reload or close mid-request. Call this on startup (no request
 * is in flight yet): it appends a retryable error to such conversations instead of leaving a question
 * with no answer and no Retry.
 */
export function settleInterruptedRequests(conversations: readonly Conversation[]): Conversation[] {
  return conversations.map((conversation) => {
    const last = conversation.messages.at(-1);
    if (last?.role !== 'user') return conversation;
    const interrupted: AssistantChatMessage = {
      id: `${last.id}-interrupted`,
      role: 'assistant',
      created_at: last.created_at,
      prompt: last.content,
      state: 'error',
      response: null,
      error: { message: INTERRUPTED_REQUEST_MESSAGE, code: 'stream_interrupted', retry_after: null },
      steps: [],
      elapsed_ms: null,
    };
    return { ...conversation, messages: [...conversation.messages, interrupted] };
  });
}

/** Keep at most `maxRows` result rows on every assistant message (marking trimmed results truncated). */
function withTrimmedRows(conversation: Conversation, maxRows: number): Conversation {
  let changed = false;
  const messages = conversation.messages.map((message) => {
    const execution = message.role === 'assistant' ? message.response?.execution : null;
    if (message.role !== 'assistant' || !message.response || !execution || execution.rows.length <= maxRows) {
      return message;
    }
    changed = true;
    return {
      ...message,
      response: { ...message.response, execution: { ...execution, rows: execution.rows.slice(0, maxRows), truncated: true } },
    };
  });
  return changed ? { ...conversation, messages } : conversation;
}

/** Progressively smaller versions of `conversations` to try when the quota is exceeded. */
function* compactionCandidates(conversations: Conversation[]): Generator<Conversation[]> {
  yield conversations;
  const trimmed = conversations.map((conversation) => withTrimmedRows(conversation, COMPACT_RESULT_ROWS));
  yield trimmed;
  const lean = trimmed.map((conversation, index) => (index === 0 ? conversation : withTrimmedRows(conversation, 0)));
  yield lean;
  for (let count = lean.length - 1; count >= 1; count--) {
    yield lean.slice(0, count);
  }
}

/** Save conversations (capped). Returns false if even the most compact version does not fit. */
export function saveConversations(
  conversations: readonly Conversation[],
  storage: StorageLike | null = browserStorage(),
): boolean {
  if (!storage) return false;
  const capped = capConversations(conversations);
  if (capped.length === 0) {
    try {
      storage.removeItem(CONVERSATIONS_KEY);
      return true;
    } catch {
      return false;
    }
  }
  for (const candidate of compactionCandidates(capped)) {
    if (writeJSON(CONVERSATIONS_KEY, candidate, storage)) return true;
  }
  return false;
}

// ---------------------------------------------------------------------------------------------
// UI settings
// ---------------------------------------------------------------------------------------------

export interface UiSettings {
  dialect: Dialect;
  execute: boolean;
  /** Desktop sidebar expanded (the mobile drawer always starts closed). */
  sidebarOpen: boolean;
}

export const DEFAULT_SETTINGS: UiSettings = {
  dialect: 'sqlite',
  execute: true,
  sidebarOpen: true,
};

export function loadSettings(storage: StorageLike | null = browserStorage()): UiSettings {
  const raw = readJSON(SETTINGS_KEY, storage);
  if (!isRecord(raw)) return { ...DEFAULT_SETTINGS };
  return {
    dialect: DIALECTS.includes(raw.dialect as Dialect) ? (raw.dialect as Dialect) : DEFAULT_SETTINGS.dialect,
    execute: typeof raw.execute === 'boolean' ? raw.execute : DEFAULT_SETTINGS.execute,
    sidebarOpen: typeof raw.sidebarOpen === 'boolean' ? raw.sidebarOpen : DEFAULT_SETTINGS.sidebarOpen,
  };
}

export function saveSettings(settings: UiSettings, storage: StorageLike | null = browserStorage()): boolean {
  return writeJSON(SETTINGS_KEY, settings, storage);
}

/** Id of the conversation shown when the app was last open (null: a new, empty chat). */
export function loadActiveConversationId(storage: StorageLike | null = browserStorage()): string | null {
  const raw = readJSON(ACTIVE_CONVERSATION_KEY, storage);
  return typeof raw === 'string' && raw ? raw : null;
}

export function saveActiveConversationId(id: string | null, storage: StorageLike | null = browserStorage()): void {
  if (id === null) {
    try {
      storage?.removeItem(ACTIVE_CONVERSATION_KEY);
    } catch {
      // Storage unavailable: nothing to clear.
    }
    return;
  }
  writeJSON(ACTIVE_CONVERSATION_KEY, id, storage);
}
