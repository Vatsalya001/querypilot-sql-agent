import { formatClockTime } from '../lib/format';
import type { UserChatMessage } from '../types';

export function UserMessage({ message }: { message: UserChatMessage }) {
  return (
    <div className="flex justify-end motion-safe:animate-slide-up">
      <div className="max-w-[88%] sm:max-w-[80%]">
        <p className="sr-only">You said:</p>
        <div className="rounded-2xl rounded-br-md bg-indigo-600 px-4 py-2.5 text-[0.9375rem] leading-relaxed break-words whitespace-pre-wrap text-white shadow-sm dark:bg-indigo-500">
          {message.content}
        </div>
        <p className="mt-1 text-right text-[0.6875rem] text-zinc-400 dark:text-zinc-500">
          {formatClockTime(message.created_at)}
        </p>
      </div>
    </div>
  );
}
