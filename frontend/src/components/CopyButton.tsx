import clsx from 'clsx';
import { Check, Copy } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { copyToClipboard } from '../lib/clipboard';
import { Button } from './ui';

export interface CopyButtonProps {
  text: string;
  /** Accessible name, e.g. "Copy SQL". */
  label?: string;
  /** Visible text (defaults to "Copy"); omit with `iconOnly`. */
  buttonText?: string;
  iconOnly?: boolean;
  variant?: 'secondary' | 'ghost';
  className?: string;
}

/** Copies `text` and confirms with a short-lived "Copied" state (also announced to screen readers). */
export function CopyButton({
  text,
  label = 'Copy',
  buttonText = 'Copy',
  iconOnly = false,
  variant = 'secondary',
  className,
}: CopyButtonProps) {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const onCopy = async () => {
    if (!(await copyToClipboard(text))) return;
    setCopied(true);
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setCopied(false), 2000);
  };

  const Icon = copied ? Check : Copy;
  return (
    <Button
      size="xs"
      variant={variant}
      aria-label={label}
      title={label}
      onClick={onCopy}
      className={clsx(iconOnly && 'w-7 px-0', className)}
    >
      <Icon aria-hidden="true" className={clsx('size-3.5', copied && 'text-emerald-500')} />
      {!iconOnly && <span>{copied ? 'Copied' : buttonText}</span>}
      <span className="sr-only" aria-live="polite">
        {copied ? 'Copied to clipboard' : ''}
      </span>
    </Button>
  );
}
