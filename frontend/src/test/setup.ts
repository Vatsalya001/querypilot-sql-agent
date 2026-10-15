import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach } from 'vitest';

// jsdom has no matchMedia: emulate a 1440px-wide desktop with a light colour scheme and a fine pointer.
function matchMedia(query: string): MediaQueryList {
  const minWidth = /min-width:\s*(\d+)px/.exec(query);
  const matches = minWidth ? Number(minWidth[1]) <= 1440 : query.includes('pointer: fine');
  return {
    matches,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  };
}

beforeEach(() => {
  window.matchMedia = matchMedia;
  window.localStorage.clear();
});

afterEach(() => {
  cleanup();
});
