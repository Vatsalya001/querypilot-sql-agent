/** Labels, icons and colours for statuses, intents and pipeline nodes. */
import {
  Ban,
  BookOpen,
  Bug,
  CircleCheck,
  CircleQuestionMark,
  CircleStop,
  CircleX,
  Compass,
  Database,
  Gauge,
  LifeBuoy,
  ListChecks,
  MessageSquareText,
  Pencil,
  Play,
  Route,
  Send,
  ShieldAlert,
  ShieldCheck,
  ShieldX,
  Sparkles,
  Stethoscope,
  TriangleAlert,
  Zap,
  type LucideIcon,
} from 'lucide-react';
import type { Tone } from './ui';
import type { Intent, ResponseStatus } from '../types';

/** Response statuses plus the client-side states of a failed or stopped request. */
export type DisplayStatus = ResponseStatus | 'error' | 'stopped';

export interface StatusMeta {
  label: string;
  tone: Tone;
  icon: LucideIcon;
  /** One-line explanation used as a tooltip. */
  hint: string;
}

export const STATUS_META: Record<DisplayStatus, StatusMeta> = {
  success: { label: 'Success', tone: 'green', icon: CircleCheck, hint: 'Validated SQL was produced.' },
  answered: { label: 'Answered', tone: 'sky', icon: MessageSquareText, hint: 'Answered in prose.' },
  clarification: {
    label: 'Needs clarification',
    tone: 'amber',
    icon: CircleQuestionMark,
    hint: 'The agent needs more detail.',
  },
  rejected: { label: 'Out of scope', tone: 'neutral', icon: Ban, hint: 'Not a SQL or database request.' },
  refused: { label: 'Blocked', tone: 'red', icon: ShieldX, hint: 'Destructive operations are never generated.' },
  failed: { label: 'Failed', tone: 'red', icon: CircleX, hint: 'No valid SQL could be produced.' },
  error: { label: 'Error', tone: 'red', icon: TriangleAlert, hint: 'The request did not complete.' },
  stopped: { label: 'Stopped', tone: 'neutral', icon: CircleStop, hint: 'You stopped this request.' },
};

/** Why a response has no SQL, shown in the inspector. */
export const STATUS_EXPLANATIONS: Partial<Record<DisplayStatus, string>> = {
  answered: 'Schema and SQL-concept questions are answered in prose, so no query was needed.',
  clarification: 'The request was ambiguous, so the agent asked a question instead of guessing.',
  rejected:
    'Requests unrelated to SQL or this database (and prompt-injection attempts) are declined before any SQL is generated.',
  refused:
    'Destructive operations are blocked. QueryPilot only returns read-only SELECT queries, and the database connection itself is read-only.',
  failed: 'No query passed validation, so nothing was executed.',
};

export interface IntentMeta {
  label: string;
  icon: LucideIcon;
}

export const INTENT_META: Record<Intent, IntentMeta> = {
  generate_sql: { label: 'Generate', icon: Sparkles },
  modify_sql: { label: 'Modify', icon: Pencil },
  optimize_sql: { label: 'Optimize', icon: Zap },
  debug_sql: { label: 'Debug', icon: Bug },
  explain_sql: { label: 'Explain', icon: BookOpen },
  schema_info: { label: 'Schema info', icon: Database },
  sql_help: { label: 'SQL help', icon: LifeBuoy },
  destructive: { label: 'Destructive', icon: ShieldAlert },
  out_of_scope: { label: 'Out of scope', icon: Ban },
  ambiguous: { label: 'Ambiguous', icon: CircleQuestionMark },
};

export const NODE_ICONS: Record<string, LucideIcon> = {
  guard_input: ShieldCheck,
  detect_intent: Compass,
  validate_scope: Route,
  retrieve_schema: Database,
  diagnose_sql: Stethoscope,
  generate_sql: Sparkles,
  validate_sql: ListChecks,
  optimize_sql: Gauge,
  execute_sql: Play,
  explain_sql: BookOpen,
  answer_question: MessageSquareText,
  ask_clarification: CircleQuestionMark,
  reject_request: Ban,
  refuse_destructive: ShieldX,
  handle_failure: TriangleAlert,
  respond: Send,
};

export const COST_TONES: Record<string, Tone> = { low: 'green', medium: 'amber', high: 'red' };

export const SEVERITY_TONES: Record<string, Tone> = {
  info: 'sky',
  low: 'sky',
  suggestion: 'sky',
  warning: 'amber',
  medium: 'amber',
  high: 'red',
  critical: 'red',
  error: 'red',
};

/** The intent chip is hidden when it would repeat the status badge (e.g. "Out of scope" twice). */
export function showIntentChip(status: DisplayStatus, intent: Intent | null): intent is Intent {
  return intent !== null && INTENT_META[intent]?.label !== STATUS_META[status].label;
}
