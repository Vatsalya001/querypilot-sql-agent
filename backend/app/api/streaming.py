"""Server-Sent Events relay for ``POST /api/chat/stream`` (contract: docs/API.md).

The agent yields ``(event, data)`` pairs; each becomes one SSE message (``event:`` name plus one line of
JSON ``data:``). Two guarantees hold even when something goes wrong:

* the stream always ends with a terminal event (``final`` or ``error``) followed by ``done``;
* the agent run stops when the response ends. :class:`AgentEventStream` closes the event generator
  explicitly, so a client disconnect cancels the graph run (and its LLM call) right away instead of
  leaving a suspended generator for the garbage collector.
"""

import json
import logging
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any

from sse_starlette.sse import EventSourceResponse
from starlette.types import Receive, Scope, Send

from app.agent.service import ChatInput, SQLAgent

logger = logging.getLogger(__name__)

PING_SECONDS = 15
"""Interval of the SSE keep-alive comments (they stop proxies from closing an idle stream)."""

STREAM_FAILURE: dict[str, str] = {
    "message": "Something went wrong while answering. Please try again.",
    "code": "internal",
}
"""Payload of the ``error`` event sent when the agent stream breaks unexpectedly."""

SSEMessage = dict[str, str]


def encode_data(data: Any) -> str:
    """Serialize an event payload as one line of JSON."""
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"), default=str)


async def sse_messages(agent: SQLAgent, payload: ChatInput) -> AsyncGenerator[SSEMessage, None]:
    """Relay the agent's ``(event, data)`` pairs as SSE messages.

    If the agent stream raises, the client still receives a generic ``error`` (unless a terminal event
    was already sent) and ``done``; the traceback is logged, never sent. Closing this generator closes
    the agent stream too (``aclosing``), which cancels the graph run.
    """
    terminal_sent = done_sent = False
    try:
        async with aclosing(agent.stream(payload)) as events:
            async for event, data in events:
                yield {"event": event, "data": encode_data(data)}
                terminal_sent = terminal_sent or event in ("final", "error")
                done_sent = event == "done"
                if event == "final":
                    logger.info("Chat streamed: intent=%s status=%s", data.get("intent"), data.get("status"))
    except Exception:
        logger.exception("Chat stream failed")
        if not terminal_sent:
            yield {"event": "error", "data": encode_data(STREAM_FAILURE)}
        if not done_sent:
            yield {"event": "done", "data": "{}"}


class AgentEventStream(EventSourceResponse):
    """``EventSourceResponse`` that always closes its event generator when the response ends.

    sse-starlette cancels the response when the client disconnects. If that happens while an event is
    being written, rather than while the generator awaits the agent, the generator stays suspended at a
    ``yield``, and the graph run and the conversation's lock would live on until garbage collection.
    Closing the generator here makes the cancellation deterministic.
    """

    def __init__(self, events: AsyncGenerator[SSEMessage, None]) -> None:
        super().__init__(events, ping=PING_SECONDS, sep="\n")
        self._events = events

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                await self._events.aclose()
            except Exception:
                logger.exception("Closing the chat event stream failed")
