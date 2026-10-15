import { useCallback, useEffect, useState } from 'react';
import { loadSettings, saveSettings, type UiSettings } from '../lib/storage';

/** Persisted UI settings: output dialect, execute toggle and desktop sidebar state. */
export function useSettings(): [UiSettings, (patch: Partial<UiSettings>) => void] {
  const [settings, setSettings] = useState<UiSettings>(loadSettings);

  useEffect(() => {
    saveSettings(settings);
  }, [settings]);

  const update = useCallback((patch: Partial<UiSettings>) => {
    setSettings((current) => ({ ...current, ...patch }));
  }, []);

  return [settings, update];
}
