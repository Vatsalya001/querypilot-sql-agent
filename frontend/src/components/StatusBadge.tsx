import { INTENT_META, STATUS_META, type DisplayStatus } from './meta';
import { Badge } from './ui';
import type { Intent } from '../types';

export function StatusBadge({ status }: { status: DisplayStatus }) {
  const meta = STATUS_META[status];
  return (
    <Badge tone={meta.tone} icon={meta.icon} title={meta.hint}>
      {meta.label}
    </Badge>
  );
}

export function IntentChip({ intent }: { intent: Intent }) {
  const meta = INTENT_META[intent];
  if (!meta) return null;
  const Icon = meta.icon;
  return (
    <span
      title={`Detected intent: ${intent}`}
      className="inline-flex items-center gap-1 rounded-md bg-zinc-100 px-1.5 py-0.5 text-[0.6875rem] font-medium text-zinc-600 dark:bg-zinc-800 dark:text-zinc-400"
    >
      <Icon aria-hidden="true" className="size-3" />
      {meta.label}
    </span>
  );
}
