import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import App from './App';
import { ToastProvider } from './components/Toaster';
import { ACTIVE_CONVERSATION_KEY, CONVERSATIONS_KEY } from './lib/storage';
import { assistantMessage, conversation, makeResponse, SQL, userMessage } from './test/fixtures';
import { controllableSSE, jsonResponse, sseResponse } from './test/sse';
import type { ChatRequest, HealthResponse, SchemaResponse } from './types';

const HEALTH: HealthResponse = {
  status: 'ok',
  version: '1.0.0',
  llm: { provider: 'ollama', model: 'qwen3.5:9b', fast_model: 'qwen3.5:9b', fallback_models: [], configured: true, mock: false },
  database: { dialect: 'sqlite', tables: 2 },
};

const SCHEMA: SchemaResponse = {
  dialect: 'sqlite',
  tables: [
    {
      name: 'Customers',
      description: 'People and companies that place orders.',
      row_count: 300,
      columns: [
        { name: 'CustomerID', type: 'INTEGER', primary_key: true, nullable: false, description: 'Unique customer id.', foreign_key: null, sample_values: [] },
        { name: 'State', type: 'TEXT', primary_key: false, nullable: false, description: "Full US state name, e.g. 'California'.", foreign_key: null, sample_values: ['California', 'Texas'] },
      ],
      indexes: [],
    },
    {
      name: 'Orders',
      description: 'Customer orders.',
      row_count: 2000,
      columns: [
        { name: 'OrderID', type: 'INTEGER', primary_key: true, nullable: false, description: 'Unique order id.', foreign_key: null, sample_values: [] },
        { name: 'CustomerID', type: 'INTEGER', primary_key: false, nullable: false, description: 'Who placed the order.', foreign_key: { table: 'Customers', column: 'CustomerID' }, sample_values: [] },
      ],
      indexes: [],
    },
  ],
  relationships: [{ from_table: 'Orders', from_column: 'CustomerID', to_table: 'Customers', to_column: 'CustomerID' }],
};

const START = { thread_id: 'thread-1', message_id: 'message-1' };

/** A complete answer stream whose `start` event echoes the client's thread id, like the real server. */
function answer(body: ChatRequest, response = makeResponse()): Response {
  return sseResponse([
    ['start', { thread_id: body.thread_id, message_id: 'message-1' }],
    ['final', response],
    ['done', {}],
  ]);
}

type ChatHandler = (body: ChatRequest, init: RequestInit) => Response | Promise<Response>;

/** Route fetch calls like the real API; chat requests go to `chat`. */
function mockApi(chat: ChatHandler) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    if (url.endsWith('/api/health')) return jsonResponse(HEALTH);
    if (url.endsWith('/api/schema')) return jsonResponse(SCHEMA);
    if (url.endsWith('/api/graph')) return jsonResponse({ mermaid: 'graph TD;', nodes: [] });
    if (url.includes('/api/threads/')) return jsonResponse({ ok: true });
    if (url.endsWith('/api/chat/stream')) return chat(JSON.parse(String(init.body)) as ChatRequest, init);
    return jsonResponse({ detail: 'Not Found', code: 'not_found' }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function chatBodies(fetchMock: ReturnType<typeof mockApi>): ChatRequest[] {
  return fetchMock.mock.calls
    .filter(([url]) => String(url).endsWith('/api/chat/stream'))
    .map(([, init]) => JSON.parse(String(init?.body)) as ChatRequest);
}

async function renderApp() {
  const view = render(
    <ToastProvider>
      <App />
    </ToastProvider>,
  );
  await screen.findByText('Ollama · qwen3.5:9b');
  return view;
}

const input = () => screen.getByLabelText('Message QueryPilot');
const transcript = () => screen.getByRole('log', { name: 'Conversation' });

describe('App', () => {
  it('shows example prompts grouped by capability on an empty chat', async () => {
    mockApi(() => sseResponse([]));
    await renderApp();

    expect(screen.getByRole('heading', { name: 'Ask your database anything' })).toBeInTheDocument();
    for (const group of ['Generate', 'Follow-up', 'Optimize', 'Debug', 'Explain', 'Guardrails']) {
      expect(screen.getByRole('heading', { name: group })).toBeInTheDocument();
    }
    for (const prompt of [
      'Show all employees hired after January 2024',
      'Top 5 products by total revenue in 2025',
      'Average salary by department',
      'Delete all orders from 2023',
      'Who won the FIFA World Cup?',
    ]) {
      expect(screen.getByRole('button', { name: prompt })).toBeEnabled();
    }
    expect(screen.getByRole('button', { name: /Show all customers.*Only those from California/ })).toBeInTheDocument();
    expect(await screen.findByRole('button', { name: /Insert table Customers/ })).toBeInTheDocument();
  });

  it('streams pipeline steps and the explanation, then shows the final SQL with a working Copy button', async () => {
    const user = userEvent.setup();
    const sse = controllableSSE();
    const fetchMock = mockApi(() => sse.response);
    await renderApp();

    await user.type(input(), 'Only those from California{Enter}');
    expect(input()).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Stop generating' })).toBeInTheDocument();

    sse.send('start', START);
    sse.send('step', { node: 'guard_input', label: 'Checking input safety', status: 'started', duration_ms: null, detail: null });
    sse.send('step', { node: 'guard_input', label: 'Checking input safety', status: 'completed', duration_ms: 1.2, detail: null });
    sse.send('step', { node: 'generate_sql', label: 'Generating SQL', status: 'started', duration_ms: null, detail: null });
    sse.send('step', { node: 'generate_sql', label: 'Generating SQL', status: 'completed', duration_ms: 900, detail: null });
    sse.send('step', { node: 'validate_sql', label: 'Validating SQL', status: 'started', duration_ms: null, detail: null });
    sse.send('step', { node: 'validate_sql', label: 'Validating SQL', status: 'completed', duration_ms: 5, detail: 'invalid' });
    sse.send('step', { node: 'generate_sql', label: 'Generating SQL', status: 'started', duration_ms: null, detail: null });

    const pending = await screen.findByRole('article', { name: 'Response in progress' });
    await within(pending).findByText('repair attempt 1');
    expect(within(pending).getByText('Checking input safety')).toBeInTheDocument();
    expect(within(pending).getByText('Validating SQL')).toBeInTheDocument();
    expect(within(pending).getByRole('status')).toHaveTextContent('Generating SQL');

    sse.send('token', { node: 'explain_sql', target: 'explanation', text: 'This query retrieves ' });
    sse.send('token', { node: 'explain_sql', target: 'explanation', text: 'every customer' });
    expect(await within(pending).findByText(/This query retrieves every customer/)).toBeInTheDocument();

    sse.send('final', makeResponse({ attempts: 2 }));
    sse.send('done', {});
    sse.close();

    const answer = await screen.findByRole('article', { name: 'Assistant response' });
    expect(screen.queryByRole('article', { name: 'Response in progress' })).not.toBeInTheDocument();
    expect(within(answer).getByRole('region', { name: 'Generated SQL' })).toHaveTextContent("State = 'California'");
    expect(within(answer).getByText('Repaired after 2 attempts')).toBeInTheDocument();
    expect(within(answer).getByText('2 rows')).toBeInTheDocument();
    expect(input()).toBeEnabled();

    await user.click(within(answer).getByRole('button', { name: 'Copy SQL' }));
    expect(await within(answer).findByText('Copied')).toBeInTheDocument();
    await expect(navigator.clipboard.readText()).resolves.toBe(SQL);

    const [body] = chatBodies(fetchMock);
    expect(body).toMatchObject({ message: 'Only those from California', dialect: 'sqlite', execute: true, history: null });
    expect(body.thread_id).toMatch(/^[0-9a-f-]{36}$/);
  });

  it('opens the inspector on the results and validation of the selected answer', async () => {
    const user = userEvent.setup();
    mockApi(() => sseResponse([['start', START], ['final', makeResponse()], ['done', {}]]));
    await renderApp();

    await user.type(input(), 'Only those from California{Enter}');
    const details = await screen.findByRole('complementary', { name: 'Response details' });
    const table = await within(details).findByRole('table', { name: 'Query results' });
    expect(within(table).getByRole('columnheader', { name: 'FirstName' })).toBeInTheDocument();
    expect(within(table).getByText('NULL')).toBeInTheDocument();

    await user.click(within(details).getByRole('tab', { name: /Validation/ }));
    expect(within(details).getByText('All checks passed')).toBeInTheDocument();
    expect(within(details).getByText('Database dry-run')).toBeInTheDocument();
  });

  it('renders an error card with Retry when the stream reports an error', async () => {
    const user = userEvent.setup();
    let call = 0;
    const fetchMock = mockApi(() => {
      call += 1;
      return call === 1
        ? sseResponse([
            ['start', START],
            ['step', { node: 'detect_intent', label: 'Detecting intent', status: 'started', duration_ms: null, detail: null }],
            ['error', { message: 'The language model is not available.', code: 'llm_not_configured' }],
            ['done', {}],
          ])
        : sseResponse([['start', START], ['final', makeResponse()], ['done', {}]]);
    });
    await renderApp();

    await user.type(input(), 'Show all customers{Enter}');
    const alert = await within(transcript()).findByRole('alert');
    expect(alert).toHaveTextContent('The language model is not available');
    expect(alert).toHaveTextContent('The language model is not available.');
    expect(alert).toHaveTextContent('llm_not_configured');

    await user.click(within(alert).getByRole('button', { name: 'Retry' }));
    expect(await within(transcript()).findByRole('article', { name: 'Assistant response' })).toBeInTheDocument();
    expect(within(transcript()).queryByRole('alert')).not.toBeInTheDocument();
    expect(within(transcript()).getAllByText('Show all customers')).toHaveLength(1);
    expect(chatBodies(fetchMock).map((body) => body.message)).toEqual(['Show all customers', 'Show all customers']);
  });

  it('shows the JSON detail of an HTTP error raised before streaming', async () => {
    const user = userEvent.setup();
    mockApi(() => jsonResponse({ detail: 'Rate limit exceeded: 10 requests per minute.', code: 'rate_limited', retry_after: 30 }, 429));
    await renderApp();

    await user.type(input(), 'Show all customers{Enter}');
    const alert = await within(transcript()).findByRole('alert');
    expect(alert).toHaveTextContent('Rate limit reached');
    expect(alert).toHaveTextContent('Rate limit exceeded: 10 requests per minute.');
    expect(alert).toHaveTextContent('try again in about 30 s');
  });

  it('shows a toast when the API cannot be reached', async () => {
    const user = userEvent.setup();
    mockApi(() => {
      throw new TypeError('Failed to fetch');
    });
    await renderApp();

    await user.type(input(), 'Show all customers{Enter}');
    expect(await screen.findByText('Network error')).toBeInTheDocument();
    expect(await within(transcript()).findByRole('alert')).toHaveTextContent('Cannot reach the QueryPilot API');
  });

  it('stops a running request', async () => {
    const user = userEvent.setup();
    const sse = controllableSSE();
    mockApi((_body, init) => {
      init.signal?.addEventListener('abort', () => sse.fail(new DOMException('The operation was aborted.', 'AbortError')));
      return sse.response;
    });
    await renderApp();

    await user.type(input(), 'Show all customers{Enter}');
    sse.send('start', START);
    await screen.findByRole('article', { name: 'Response in progress' });
    await user.click(screen.getByRole('button', { name: 'Stop generating' }));
    expect(await within(transcript()).findByText('You stopped this response.')).toBeInTheDocument();
    expect(input()).toBeEnabled();
  });

  it('renders guardrail refusals with a Blocked badge and no SQL', async () => {
    const user = userEvent.setup();
    const refused = makeResponse({
      intent: 'destructive',
      status: 'refused',
      message: "I can't run that. QueryPilot is strictly read-only.",
      sql: null,
      sql_sqlite: null,
      explanation: null,
      validation: null,
      optimization: null,
      execution: null,
      cost: null,
    });
    mockApi(() => sseResponse([['start', START], ['final', refused], ['done', {}]]));
    await renderApp();

    await user.type(input(), 'Delete all orders from 2023{Enter}');
    const answer = await within(transcript()).findByRole('article', { name: 'Assistant response' });
    expect(within(answer).getByText('Blocked')).toBeInTheDocument();
    expect(within(answer).getByText(/strictly read-only/)).toBeInTheDocument();
    expect(within(answer).queryByRole('region', { name: 'Generated SQL' })).not.toBeInTheDocument();
  });

  it('sends earlier turns as history and keeps one thread per conversation', async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi((body) => answer(body, makeResponse({ sql_sqlite: 'SELECT * FROM Customers' })));
    await renderApp();

    await user.type(input(), 'Show all customers{Enter}');
    await within(transcript()).findByRole('article', { name: 'Assistant response' });
    await user.type(input(), 'Only those from California{Enter}');
    await waitFor(() => expect(within(transcript()).getAllByRole('article', { name: 'Assistant response' })).toHaveLength(2));

    const [first, second] = chatBodies(fetchMock);
    expect(second.thread_id).toBe(first.thread_id);
    expect(second.history).toEqual([
      { role: 'user', content: 'Show all customers', sql: null },
      { role: 'assistant', content: 'Here are all customers located in California.', sql: 'SELECT * FROM Customers' },
    ]);
  });

  it('inserts schema names at the cursor from the schema explorer', async () => {
    const user = userEvent.setup();
    mockApi(() => sseResponse([]));
    await renderApp();

    await user.type(input(), 'How many rows are in');
    await user.click(await screen.findByRole('button', { name: /Insert table Customers/ }));
    expect(input()).toHaveValue('How many rows are in Customers');

    await user.click(screen.getByRole('button', { name: 'Expand Customers columns' }));
    await user.click(screen.getByRole('button', { name: /Insert column Customers\.State/ }));
    expect(input()).toHaveValue('How many rows are in Customers State');
  });

  it('renders saved chats whose stored response data is corrupt instead of crashing', async () => {
    const corrupt = { ...makeResponse(), cost: {}, validation: { valid: true }, execution: { columns: ['a'] }, model: {} };
    window.localStorage.setItem(
      CONVERSATIONS_KEY,
      JSON.stringify([conversation('saved', 1, [userMessage('Show all customers'), assistantMessage({ response: corrupt as never })])]),
    );
    window.localStorage.setItem(ACTIVE_CONVERSATION_KEY, JSON.stringify('saved'));
    mockApi((body) => answer(body));
    await renderApp();

    const saved = within(transcript()).getByRole('article', { name: 'Assistant response' });
    expect(saved).toHaveTextContent('Here are all customers located in California.');
    expect(screen.getByRole('complementary', { name: 'Response details' })).toBeInTheDocument();
  });

  it('offers Retry for a question whose answer never arrived because the page was reloaded', async () => {
    const user = userEvent.setup();
    window.localStorage.setItem(
      CONVERSATIONS_KEY,
      JSON.stringify([conversation('saved', 1, [userMessage('Show all customers')])]),
    );
    window.localStorage.setItem(ACTIVE_CONVERSATION_KEY, JSON.stringify('saved'));
    const fetchMock = mockApi((body) => answer(body));
    await renderApp();

    const alert = within(transcript()).getByRole('alert');
    expect(alert).toHaveTextContent('The connection was interrupted');
    await user.click(within(alert).getByRole('button', { name: 'Retry' }));

    expect(await within(transcript()).findByRole('article', { name: 'Assistant response' })).toBeInTheDocument();
    expect(within(transcript()).queryByRole('alert')).not.toBeInTheDocument();
    expect(within(transcript()).getAllByText('Show all customers')).toHaveLength(1);
    expect(chatBodies(fetchMock).map((body) => [body.message, body.thread_id])).toEqual([['Show all customers', 'saved']]);
  });

  it('deletes a conversation after confirmation and clears its server memory', async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi((body) => answer(body));
    await renderApp();

    await user.type(input(), 'Show all customers{Enter}');
    await within(transcript()).findByRole('article', { name: 'Assistant response' });
    const [{ thread_id: threadId }] = chatBodies(fetchMock);

    await user.click(screen.getByRole('button', { name: 'Delete chat: Show all customers' }));
    await user.click(screen.getByRole('button', { name: 'Delete' }));

    expect(screen.getByText('Your conversations will appear here.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Ask your database anything' })).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(`/api/threads/${threadId}`, expect.objectContaining({ method: 'DELETE' }));
  });
});
