import { useCallback, useEffect, useState } from 'react';
import { THEME_KEY, browserStorage } from '../lib/storage';

export type Theme = 'light' | 'dark';

const DARK_QUERY = '(prefers-color-scheme: dark)';

function systemPrefersDark(): boolean {
  return typeof window !== 'undefined' && typeof window.matchMedia === 'function'
    ? window.matchMedia(DARK_QUERY).matches
    : false;
}

function readStoredTheme(): Theme | null {
  try {
    const value = browserStorage()?.getItem(THEME_KEY);
    return value === 'light' || value === 'dark' ? value : null;
  } catch {
    return null;
  }
}

/**
 * Light/dark theme using Tailwind's class strategy. Follows the system preference until the user
 * picks a theme, which is then persisted (index.html applies it before the first paint).
 */
export function useTheme(): { theme: Theme; setTheme: (theme: Theme) => void; toggleTheme: () => void } {
  const [storedTheme, setStoredTheme] = useState<Theme | null>(readStoredTheme);
  const [systemDark, setSystemDark] = useState(systemPrefersDark);

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia(DARK_QUERY);
    const onChange = (event: MediaQueryListEvent) => setSystemDark(event.matches);
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  const theme: Theme = storedTheme ?? (systemDark ? 'dark' : 'light');

  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle('dark', theme === 'dark');
    root.style.colorScheme = theme;
  }, [theme]);

  const setTheme = useCallback((next: Theme) => {
    setStoredTheme(next);
    try {
      browserStorage()?.setItem(THEME_KEY, next);
    } catch {
      // Storage unavailable: the choice lasts for this session only.
    }
  }, []);

  const toggleTheme = useCallback(() => setTheme(theme === 'dark' ? 'light' : 'dark'), [setTheme, theme]);

  return { theme, setTheme, toggleTheme };
}
