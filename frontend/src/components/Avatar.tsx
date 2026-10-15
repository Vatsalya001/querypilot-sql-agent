import clsx from 'clsx';
import { LogoMark } from './Logo';

/** The assistant's avatar next to its messages. */
export function AgentAvatar({ className }: { className?: string }) {
  return (
    <div className={clsx('hidden shrink-0 pt-0.5 sm:block', className)} aria-hidden="true">
      <LogoMark className="size-7" />
    </div>
  );
}
