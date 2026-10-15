# API Contract — SQL Query AI Agent

This contract is shared by the FastAPI backend (`backend/app/api`) and the React frontend (`frontend/src/types.ts`, `frontend/src/lib/api.ts`). The two sides must match it field for field.

- Base path: `/api`. In production the UI and the API share one origin, because FastAPI serves the built SPA. In dev, Vite proxies `/api` to `http://localhost:8000`.
- JSON keys are `snake_case`.
- Nullable fields are always present and set to `null`. They are never omitted.

---

## `GET /api/health`

```json
{
  "status": "ok",
  "version": "1.0.0",
  "llm": {
    "provider": "groq",
    "model": "openai/gpt-oss-120b",
    "fast_model": "openai/gpt-oss-20b",
    "fallback_models": ["qwen/qwen3.8-27b"],
    "configured": true,
    "mock": false,
    "warmup": "ready"
  },
  "database": { "dialect": "sqlite", "tables": 7 }
}
```
- `configured: false` means the model cannot be used yet: for Ollama, the server is not reachable or a configured model is not pulled (probe cached for 10 s); for hosted providers, the API key is missing. The UI shows a warning banner.
- `mock: true` means `LLM_PROVIDER=mock`, the offline rule-based gateway used for tests and demos without a key. The UI shows an info banner.
- `warmup` reports the startup prompt-cache warm-up of the local model. The values are `warming`, `ready`, `skipped` (not applicable: hosted providers, mock, or `LLM_WARMUP=false`) and `failed`. While it is `warming`, the UI shows a "Warming up…" pill and polls faster, because the first local requests are slower until the cache is primed.

## `GET /api/schema`

Returns the database schema for the schema explorer.

```json
{
  "dialect": "sqlite",
  "tables": [
    {
      "name": "Customers",
      "description": "People and companies that place orders.",
      "row_count": 300,
      "columns": [
        { "name": "CustomerID", "type": "INTEGER", "primary_key": true, "nullable": false,
          "description": "Unique customer id.", "foreign_key": null, "sample_values": [] },
        { "name": "State", "type": "TEXT", "primary_key": false, "nullable": false,
          "description": "Full US state name, e.g. 'California'.", "foreign_key": null,
          "sample_values": ["California", "Texas", "New York"] }
      ],
      "indexes": [ { "name": "idx_customers_email", "columns": ["Email"], "unique": true } ]
    }
  ],
  "relationships": [
    { "from_table": "Orders", "from_column": "CustomerID", "to_table": "Customers", "to_column": "CustomerID" }
  ]
}
```
`foreign_key` is either `null` or `{ "table": "Customers", "column": "CustomerID" }`.

## `GET /api/graph`

```json
{ "mermaid": "graph TD; ...", "nodes": ["guard_input", "detect_intent", "..."] }
```
Returns the Mermaid source of the compiled LangGraph, used by the docs and the "How it works" view.

## `DELETE /api/threads/{thread_id}`

Clears the server-side conversation memory for a thread. Returns `{ "ok": true }`.

---

## `POST /api/chat/stream` (Server-Sent Events) — primary endpoint

### Request body (`ChatRequest`)
```json
{
  "message": "Show all customers",
  "thread_id": "8f6a1c2e-…",
  "dialect": "sqlite",
  "execute": true,
  "history": [
    { "role": "user", "content": "Show all customers", "sql": null },
    { "role": "assistant", "content": "Here are all customers.", "sql": "SELECT * FROM Customers;" }
  ]
}
```
| field | type | notes |
|---|---|---|
| `message` | string, 1..2000 chars | required |
| `thread_id` | string \| null | Conversation id. The client generates a UUID per conversation. If null, the server creates one and returns it in `start`. |
| `dialect` | `"sqlite" \| "postgres" \| "mysql"` | Default `"sqlite"`. Output dialect of the returned SQL. Validation and execution always run on SQLite. |
| `execute` | bool | Default `true`. Runs the validated query against the sample DB. |
| `history` | array \| null | Optional. The UI sends the last ≤ 10 messages (user and assistant items); the server accepts at most 20 items of at most 10,000 characters each. The server uses it **only** to rehydrate memory when it has no state for `thread_id`, for example after a cold start. |

Errors raised before streaming starts are returned as normal JSON with an HTTP status. Every error body has `{ "detail": "...", "code": "..." }`:

| HTTP | `code` | when |
|---|---|---|
| 413 | `payload_too_large` | request body over 2 MiB (with or without `Content-Length`) |
| 422 | `validation_error` | empty or too-long message, unknown dialect, malformed JSON. Includes `errors: [{field, message, type}]`. |
| 429 | `rate_limited` | per-client limit exceeded. Includes `retry_after` (seconds) and a `Retry-After` header. |

### Response: `text/event-stream`
Each SSE message has an `event:` name and a JSON `data:` payload.

| event | data | when |
|---|---|---|
| `start` | `{ "thread_id": str, "message_id": str }` | first event |
| `step` | `{ "node": str, "label": str, "status": "started" \| "completed", "duration_ms": number \| null, "detail": str \| null }` | as each LangGraph node starts and finishes |
| `token` | `{ "node": str, "target": "explanation" \| "message", "text": str }` | streamed LLM tokens. `explanation` tokens come from `explain_sql`. `message` tokens come from `answer_question`. |
| `final` | `AgentResponse` (below) | exactly once on success |
| `error` | `{ "message": str, "code": "llm_not_configured" \| "rate_limited" \| "llm_error" \| "internal" }` | on failure, sent instead of `final` |
| `done` | `{}` | always last |

Node names and default labels (`label` in `step` events):

| node | label |
|---|---|
| `guard_input` | Checking input safety |
| `detect_intent` | Detecting intent |
| `validate_scope` | Validating scope |
| `retrieve_schema` | Retrieving schema |
| `diagnose_sql` | Analyzing your SQL |
| `generate_sql` | Generating SQL |
| `validate_sql` | Validating SQL |
| `optimize_sql` | Optimizing SQL |
| `execute_sql` | Executing query |
| `explain_sql` | Explaining query |
| `answer_question` | Answering question |
| `ask_clarification` | Asking for clarification |
| `reject_request` | Rejecting out-of-scope request |
| `refuse_destructive` | Blocking destructive request |
| `handle_failure` | Handling failure |
| `respond` | Preparing response |

`generate_sql` and `validate_sql` can appear more than once in one run, because of the self-correction loop.

## `POST /api/chat` — non-streaming

The request body is the same as for `/api/chat/stream`. The response is the `AgentResponse` JSON. It is used by tests, evals and simple clients.

Where the stream would send an `error` event, this endpoint returns an HTTP error with `{ "detail", "code" }` instead:

| HTTP | `code` |
|---|---|
| 503 | `llm_not_configured` (model server down or API key missing) |
| 429 | `rate_limited` |
| 502 | `llm_error` |
| 500 | `internal` |

---

## `AgentResponse`
```json
{
  "thread_id": "8f6a…",
  "message_id": "b1c2…",
  "intent": "generate_sql",
  "status": "success",
  "message": "Here are all customers located in California.",
  "sql": "SELECT\n  *\nFROM Customers\nWHERE\n  State = 'California'",
  "sql_sqlite": "SELECT\n  *\nFROM Customers\nWHERE\n  State = 'California'",
  "dialect": "sqlite",
  "explanation": "This query retrieves every customer whose state is California.",
  "validation": {
    "valid": true,
    "checks": [
      { "name": "syntax", "label": "Syntax", "passed": true, "message": "Valid SQLite syntax." },
      { "name": "single_statement", "label": "Single statement", "passed": true, "message": "Exactly one statement." },
      { "name": "read_only", "label": "Read-only", "passed": true, "message": "SELECT only." },
      { "name": "tables", "label": "Tables exist", "passed": true, "message": "Customers" },
      { "name": "columns", "label": "Columns exist", "passed": true, "message": "All 1 referenced columns exist." },
      { "name": "joins", "label": "Join relationships", "passed": true, "message": "No joins." },
      { "name": "executable", "label": "Database dry-run", "passed": true, "message": "Query plan compiled." }
    ],
    "errors": [],
    "warnings": [],
    "sql": "SELECT * FROM Customers WHERE State = 'California'",
    "tables": ["Customers"],
    "columns": ["Customers.State"],
    "destructive": false
  },
  "optimization": {
    "formatted_sql": "SELECT\n  *\nFROM Customers\nWHERE\n  State = 'California'",
    "suggestions": [
      { "category": "readability", "severity": "info", "message": "Select only the columns you need instead of SELECT *." },
      { "category": "index", "severity": "info", "message": "Customers.State is filtered but not indexed." }
    ],
    "index_recommendations": ["CREATE INDEX idx_customers_state ON Customers (State);"],
    "unnecessary_joins": []
  },
  "execution": {
    "columns": ["CustomerID", "FirstName", "State"],
    "rows": [[1, "Ava", "California"]],
    "row_count": 1,
    "truncated": false,
    "elapsed_ms": 1.7,
    "error": null
  },
  "cost": {
    "level": "low",
    "score": 300,
    "estimated_rows_scanned": 300,
    "plan": ["SCAN Customers"],
    "notes": ["Full table scan on Customers (300 rows)."]
  },
  "issues": [],
  "changes": [],
  "suggestions": [],
  "assumptions": [],
  "clarification_question": null,
  "schema_tables": ["Customers"],
  "attempts": 1,
  "steps": [ { "node": "detect_intent", "label": "Detecting intent", "status": "completed", "duration_ms": 412.3, "detail": "generate_sql (0.97)" } ],
  "model": "openai/gpt-oss-120b"
}
```

### Enumerations
- `intent`: `generate_sql`, `modify_sql`, `optimize_sql`, `debug_sql`, `explain_sql`, `schema_info`, `sql_help`, `destructive`, `out_of_scope`, `ambiguous`, or `null` if blocked before classification.
- `status`:

  | status | meaning | UI treatment |
  |---|---|---|
  | `success` | validated SQL produced, and executed if requested | SQL, explanation and results panels |
  | `answered` | schema or SQL-concept question answered in prose | chat text only |
  | `clarification` | agent needs more detail. `clarification_question` is set. | amber "needs clarification" badge |
  | `rejected` | out of scope or prompt-injection attempt | grey "out of scope" badge |
  | `refused` | destructive operation requested | red "blocked" badge |
  | `failed` | could not produce valid SQL after retries, or the data does not exist in the schema | red badge plus `validation.errors` |
- `issues` (debug): `[{ "problem": str, "reason": str }]`. `changes` (optimize/modify): `[str]`. `suggestions` (from the LLM): `[str]`. `assumptions`: `[str]`.
- `validation`, `optimization`, `execution` and `cost` are `null` when they don't apply. `execution` is `null` when `execute=false`. `execution.error` holds a runtime DB error, and the query is still returned.
- `execution.rows` hold JSON scalars only: numbers, strings or null. At most `MAX_RESULT_ROWS` rows are returned (default 500), and `truncated: true` means more rows exist.
