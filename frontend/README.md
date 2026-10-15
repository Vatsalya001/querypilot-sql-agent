# QueryPilot web UI

React 19 + Vite + TypeScript + Tailwind CSS v4. It talks to the FastAPI backend through the contract in
[`docs/API.md`](../docs/API.md); `src/types.ts` mirrors that contract field for field.

## Scripts

| command | what it does |
|---|---|
| `npm run dev` | Vite dev server on http://localhost:5173, proxying `/api` to `http://localhost:8000` |
| `npm run build` | type-check (`tsc -b`) and build into `dist/` (FastAPI serves it in production) |
| `npm run preview` | serve the production build locally (same `/api` proxy) |
| `npm test` | unit and component tests (Vitest + jsdom, offline, `fetch` is mocked) |
| `npm run typecheck` | TypeScript only |

Environment variables (all optional):

- `VITE_API_TARGET`: where the dev and preview servers proxy `/api` (default `http://localhost:8000`),
  e.g. `VITE_API_TARGET=http://localhost:8012 npm run dev`.
- `VITE_API_BASE_URL`: build-time origin of the API when the UI is hosted separately (default: same origin).

## Structure

```
src/
  App.tsx              layout: header, sidebar (drawer below 1280 px), chat, inspector ("Chat | Details" tabs below 1024 px)
  types.ts             API types (docs/API.md) plus the persisted conversation model
  lib/                 framework-free logic, unit tested
    api.ts             fetch client; POST /api/chat/stream with typed SSE events
    sse.ts             spec-following text/event-stream parser (chunk boundaries, CRLF, multi-line data, pings)
    storage.ts         localStorage persistence with validation, caps (30 chats x 100 messages) and quota compaction
    history.ts         the `history` sent with each request (last 10 turns, SQLite form of the SQL)
    csv.ts, download.ts, format.ts, highlight.ts, pipeline.ts, examples.ts, ids.ts, clipboard.ts
  hooks/               useChat (conversations + streaming), useTheme, useHealth, useSchema, useSettings, ...
  components/          small presentational components; inspector/ holds the SQL, explanation,
                       validation, optimization, results and cost panels
```

## Behaviour notes

- Each conversation has a UUID `thread_id`. Every request also sends the last 10 turns as `history`, so the
  server can rehydrate a thread after a restart. Deleting a chat calls `DELETE /api/threads/{id}`.
- Streaming: `step` events drive the live pipeline tracker (repeated `generate_sql` / `validate_sql` runs are
  labelled as repair attempts), `token` events stream the explanation or answer, `final` renders the answer.
  Stop aborts the request with an `AbortController`.
- Errors before streaming (422, 429, 5xx) show the JSON `detail` (and `retry_after`) in an inline error card
  with Retry; network failures also raise a toast. A question whose answer was still streaming when the page
  was closed or reloaded comes back with a "connection was interrupted" card and Retry.
- Saved chats are rebuilt field by field on load, so corrupt or outdated `localStorage` data is repaired or
  dropped instead of crashing the app.
- Theme: class-based dark mode, persisted, defaulting to `prefers-color-scheme`; `index.html` applies it before
  the first paint. Animations respect `prefers-reduced-motion`.
