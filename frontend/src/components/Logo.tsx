import { useId } from 'react';

/** The QueryPilot mark: a database with a spark, on an indigo tile. */
export function LogoMark({ className }: { className?: string }) {
  const gradientId = useId();
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true" focusable="false">
      <defs>
        <linearGradient id={gradientId} x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#6366f1" />
          <stop offset="1" stopColor="#4338ca" />
        </linearGradient>
      </defs>
      <rect width="32" height="32" rx="8" fill={`url(#${gradientId})`} />
      <g fill="none" stroke="#fff" strokeWidth="2" strokeLinecap="round">
        <ellipse cx="14" cy="9.5" rx="7" ry="2.75" />
        <path d="M7 9.5v11c0 1.52 3.13 2.75 7 2.75" />
        <path d="M21 9.5v4.5" />
        <path d="M7 15c0 1.52 3.13 2.75 7 2.75" />
      </g>
      <path d="M23.5 16.5l1.25 2.75 2.75 1.25-2.75 1.25-1.25 2.75-1.25-2.75-2.75-1.25 2.75-1.25z" fill="#fff" />
    </svg>
  );
}
