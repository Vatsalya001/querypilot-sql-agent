"""The node decorator: step events with duration and detail, and a writer that is safe outside a stream."""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from app.agent.events import DETAIL_KEY, NODE_LABELS, emit, emit_token, traced_node
from tests.agent.helpers import NODE_NAMES


class _State(TypedDict, total=False):
    value: int


@traced_node("respond")
async def _respond(state: _State) -> dict[str, Any]:
    emit_token("respond", "message", "hello ")
    emit_token("respond", "message", "")  # empty chunks are dropped
    return {"value": state.get("value", 0) + 1, DETAIL_KEY: "done"}


def _graph() -> Any:
    graph = StateGraph(_State)
    graph.add_node("respond", _respond)
    graph.add_edge(START, "respond")
    graph.add_edge("respond", END)
    return graph.compile()


def test_labels_cover_every_node_in_the_api_contract() -> None:
    assert list(NODE_LABELS) == NODE_NAMES


async def test_traced_node_streams_started_and_completed_steps() -> None:
    events = [chunk async for chunk in _graph().astream({"value": 1}, stream_mode="custom")]

    started, token, completed = events
    assert started == {"type": "step", "node": "respond", "label": "Preparing response", "status": "started",
                       "duration_ms": None, "detail": None}
    assert token == {"type": "token", "node": "respond", "target": "message", "text": "hello "}
    assert completed["status"] == "completed" and completed["detail"] == "done" and completed["duration_ms"] >= 0


async def test_detail_key_never_reaches_the_state_and_emit_is_safe_without_a_stream() -> None:
    assert await _graph().ainvoke({"value": 1}) == {"value": 2}  # not streamed in custom mode: emit is a no-op
    emit({"type": "step"})  # outside any graph run: silently ignored
    assert await _respond({"value": 5}) == {"value": 6}
