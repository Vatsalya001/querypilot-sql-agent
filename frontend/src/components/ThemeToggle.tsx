import { Moon, Sun } from 'lucide-react';
import type { Theme } from '../hooks/useTheme';
import { IconButton } from './ui';

export function ThemeToggle({ theme, onToggle }: { theme: Theme; onToggle: () => void }) {
  const dark = theme === 'dark';
  return (
    <IconButton label={dark ? 'Switch to light mode' : 'Switch to dark mode'} onClick={onToggle}>
      {dark ? <Sun aria-hidden="true" className="size-[18px]" /> : <Moon aria-hidden="true" className="size-[18px]" />}
    </IconButton>
  );
}
