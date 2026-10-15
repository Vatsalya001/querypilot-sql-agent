/** SQL syntax highlighting. Only the SQL grammar is registered to keep the bundle small. */
import hljs from 'highlight.js/lib/core';
import sql from 'highlight.js/lib/languages/sql';

hljs.registerLanguage('sql', sql);

/** Highlight SQL and return HTML. highlight.js escapes the source, so the result is safe to inject. */
export function highlightSql(code: string): string {
  return hljs.highlight(code, { language: 'sql', ignoreIllegals: true }).value;
}
