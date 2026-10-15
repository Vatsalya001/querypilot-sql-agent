"""A local fake of an OpenAI-compatible chat-completions API (Groq, OpenAI, OpenRouter, ...).

The hosted deployment talks to Groq through ``langchain-groq`` and the ``groq`` SDK. Pointing those real
clients at this server (``GROQ_API_BASE`` or ``OPENAI_BASE_URL``) exercises the production code path
over real HTTP -- request building, the SDK's retries, SSE parsing and error handling -- without a
network connection or an API key. Payloads mirror what Groq returns for ``openai/gpt-oss-*`` models
(a separate ``reasoning`` field, ``x_groq`` metadata, Groq's error bodies).

The server records every request. Replies are queued per test (``queue``) or produced by a
``responder`` callback when the queue is empty.
"""

from __future__ import annotations

import json
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

GROQ_CHAT_PATH = "/openai/v1/chat/completions"
"""Where the ``groq`` SDK posts chat completions, relative to its base URL."""


@dataclass
class RecordedRequest:
    """One request received by the fake server."""

    path: str
    headers: dict[str, str]
    """Header names lower-cased."""
    body: dict[str, Any]

    @property
    def model(self) -> str:
        return str(self.body.get("model"))

    @property
    def schema_name(self) -> str | None:
        """Name of the structured-output schema requested (JSON schema or forced tool), if any."""
        response_format = self.body.get("response_format") or {}
        if response_format.get("type") == "json_schema":
            return response_format["json_schema"]["name"]
        tool_choice = self.body.get("tool_choice")
        if isinstance(tool_choice, dict):
            return tool_choice["function"]["name"]
        return None

    @property
    def kind(self) -> str:
        """``json_schema``, ``tools``, ``stream`` or ``text``."""
        if (self.body.get("response_format") or {}).get("type") == "json_schema":
            return "json_schema"
        if self.body.get("tools"):
            return "tools"
        return "stream" if self.body.get("stream") else "text"


Reply = tuple[int, dict[str, str], Any]
"""``(status, headers, payload)``; the payload is a JSON object, or a list of chunks for a stream."""
Responder = Callable[[RecordedRequest], Reply]


# ---- payload builders ------------------------------------------------------------------------------


def completion(
    model: str,
    *,
    content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    reasoning: str | None = "The user wants data from the schema.",
) -> dict[str, Any]:
    """A non-streamed chat completion shaped like Groq's (gpt-oss models return ``reasoning`` separately)."""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if reasoning:
        message["reasoning"] = reasoning
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "created": 1_760_000_000,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": "tool_calls" if tool_calls else "stop",
            }
        ],
        "usage": {
            "queue_time": 0.01, "prompt_tokens": 1200, "prompt_time": 0.02, "completion_tokens": 90,
            "completion_time": 0.05, "total_tokens": 1290, "total_time": 0.07,
        },
        "system_fingerprint": "fp_fake",
        "x_groq": {"id": "req_fake"},
        "service_tier": "on_demand",
    }


def tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """An OpenAI-style tool call (arguments JSON-encoded, as the API sends them)."""
    return {"id": "call_fake", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}


def stream_chunks(model: str, pieces: list[str], *, reasoning: str | None = "Explaining the query.") -> list[Any]:
    """SSE chunks of a streamed completion: reasoning deltas first (gpt-oss), then content, then usage."""

    def chunk(delta: dict[str, Any], finish_reason: str | None = None, **extra: Any) -> dict[str, Any]:
        return {
            "id": "chatcmpl-fake", "object": "chat.completion.chunk", "created": 1_760_000_000, "model": model,
            "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish_reason}], **extra,
        }

    chunks = [chunk({"role": "assistant", "content": ""}, x_groq={"id": "req_fake"})]
    if reasoning:
        chunks.append(chunk({"reasoning": reasoning}))
    chunks += [chunk({"content": piece}) for piece in pieces]
    chunks.append(
        chunk({}, "stop", x_groq={"id": "req_fake", "usage": {"prompt_tokens": 300, "completion_tokens": 20,
                                                             "total_tokens": 320}})
    )
    return chunks


def error(status: int, message: str, code: str, error_type: str = "invalid_request_error", **extra: Any) -> Reply:
    """An error reply with Groq's body shape. 429s carry a tiny ``retry-after-ms`` so SDK retries are fast."""
    headers = {"retry-after-ms": "5"} if status == 429 else {}
    return status, headers, {"error": {"message": message, "type": error_type, "code": code, **extra}}


def structured(request: RecordedRequest, data: dict[str, Any]) -> Reply:
    """Answer ``data`` in whatever form the request asked for (JSON schema, forced tool call or text)."""
    if request.kind == "tools":
        return 200, {}, completion(request.model, tool_calls=[tool_call(request.schema_name or "tool", data)])
    return 200, {}, completion(request.model, content=json.dumps(data))


def rate_limited(model: str) -> Reply:
    """Groq's 429 for an exhausted per-minute token budget."""
    return error(
        429,
        f"Rate limit reached for model `{model}` in organization `org_fake` service tier `on_demand` on tokens "
        "per minute (TPM): Limit 8000, Used 7600, Requested 2900. Please try again in 18.75s.",
        "rate_limit_exceeded",
        "tokens",
    )


INVALID_KEY = error(401, "Invalid API Key", "invalid_api_key")


# ---- server ----------------------------------------------------------------------------------------


class FakeChatAPI:
    """Threaded HTTP server on an ephemeral 127.0.0.1 port. Use :meth:`start` / :meth:`stop`."""

    def __init__(self, responder: Responder | None = None) -> None:
        self.requests: list[RecordedRequest] = []
        self.responder = responder
        self._queue: deque[Reply | Responder] = deque()
        self._lock = threading.Lock()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._server.daemon_threads = True
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.02}, name="fake-chat-api", daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> FakeChatAPI:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def queue(self, *replies: Reply | Responder) -> None:
        """Replies for the next requests, in order (a callable builds its reply from the request)."""
        with self._lock:
            self._queue.extend(replies)

    def models(self) -> list[str]:
        return [request.model for request in self.requests]

    def _reply(self, request: RecordedRequest) -> Reply:
        with self._lock:
            self.requests.append(request)
            item = self._queue.popleft() if self._queue else self.responder
        if item is None:
            return error(500, "fake server: no reply queued", "no_reply", "internal_server_error")
        return item(request) if callable(item) else item

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - keep test output quiet
                return

            def do_POST(self) -> None:  # noqa: N802 - http.server naming
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                headers = {name.lower(): value for name, value in self.headers.items()}
                status, extra_headers, payload = api._reply(RecordedRequest(self.path, headers, body))
                if body.get("stream") and status == 200:
                    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in payload] + ["data: [DONE]\n\n"]
                    raw, content_type = "".join(lines).encode(), "text/event-stream"
                else:
                    raw, content_type = json.dumps(payload).encode(), "application/json"
                self.send_response(status)
                self.send_header("content-type", content_type)
                for name, value in extra_headers.items():
                    self.send_header(name, value)
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return Handler
