"""Custom stream events emitted by graph nodes (LangGraph ``stream_mode="custom"``).

Every node is wrapped by :func:`traced_node`, which emits ``step`` events when the node starts and
finishes. LLM-backed nodes also emit ``token`` events while text streams in. The writer is a safe
no-op when the graph runs without the custom stream mode (or outside a graph, e.g. in unit tests).
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langgraph.config import get_stream_writer

logger = logging.getLogger(__name__)

NODE_LABELS: dict[str, str] = {
    "guard_input": "Checking input safety",
    "detect_intent": "Detecting intent",
    "validate_scope": "Validating scope",
    "retrieve_schema": "Retrieving schema",
    "diagnose_sql": "Analyzing your SQL",
    "generate_sql": "Generating SQL",
    "validate_sql": "Validating SQL",
    "optimize_sql": "Optimizing SQL",
    "execute_sql": "Executing query",
    "explain_sql": "Explaining query",
    "answer_question": "Answering question",
    "ask_clarification": "Asking for clarification",
    "reject_request": "Rejecting out-of-scope request",
    "refuse_destructive": "Blocking destructive request",
    "handle_failure": "Handling failure",
    "respond": "Preparing response",
}
"""Node name -> default label shown in ``step`` events (docs/API.md)."""

DETAIL_KEY = "__detail__"
"""Reserved key a node may put in its returned update; popped by :func:`traced_node` and sent as ``detail``."""

NodeFn = Callable[..., Awaitable[dict[str, Any]]]


def emit(event: dict[str, Any]) -> None:
    """Send a custom stream event; silently does nothing when no custom stream is attached."""
    try:
        writer = get_stream_writer()
    except RuntimeError:  # called outside a LangGraph run
        return
    writer(event)


def emit_token(node: str, target: Literal["explanation", "message"], text: str) -> None:
    """Emit one streamed LLM token chunk."""
    if text:
        emit({"type": "token", "node": node, "target": target, "text": text})


def traced_node(name: str) -> Callable[[NodeFn], NodeFn]:
    """Decorate an async node: emit started/completed ``step`` events with duration and detail."""
    label = NODE_LABELS[name]

    def decorator(fn: NodeFn) -> NodeFn:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> dict[str, Any]:
            emit({"type": "step", "node": name, "label": label, "status": "started",
                  "duration_ms": None, "detail": None})
            started = time.perf_counter()
            update = dict(await fn(*args, **kwargs) or {})
            detail = update.pop(DETAIL_KEY, None)
            duration_ms = round((time.perf_counter() - started) * 1000, 1)
            logger.debug("node %s finished in %.1f ms (%s)", name, duration_ms, detail)
            emit({"type": "step", "node": name, "label": label, "status": "completed",
                  "duration_ms": duration_ms, "detail": detail})
            return update

        return wrapper

    return decorator
