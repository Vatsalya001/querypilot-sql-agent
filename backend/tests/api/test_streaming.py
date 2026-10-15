"""SSE tests for ``POST /api/chat/stream`` with a scripted FakeAgent."""

import asyncio
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi.testclient import TestClient
from starlette.types import Message

from app.agent.service import ChatInput
from app.api.streaming import AgentEventStream, sse_messages
from app.main import create_app
from tests.api.conftest import ClientFactory, make_settings
from tests.api.fakes import (
    AGENT_RESPONSE_KEYS,
    SECRET_ERROR_TEXT,
    STEP_KEYS,
    TOKEN_KEYS,
    FakeAgent,
    parse_sse,
)


def stream(client: TestClient, payload: dict[str, Any]) -> tuple[httpx.Response, list[tuple[str, Any]]]:
    response = client.post("/api/chat/stream", json=payload)
    return response, [(event, json.loads(data)) for event, data in parse_sse(response.text)]


def test_stream_emits_events_in_contract_order(make_client: ClientFactory) -> None:
    client, _ = make_client()

    response, events = stream(client, {"message": "Show all customers", "thread_id": "t-9"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    names = [event for event, _ in events]
    assert names[0] == "start"
    assert names[-2:] == ["final", "done"]
    assert set(names[1:-2]) == {"step", "token"}
    assert names.index("token") > names.index("step")
    assert events[0][1] == {"thread_id": "t-9", "message_id": "msg-1"}
    for event, data in events:
        if event == "step":
            assert set(data) == STEP_KEYS
        elif event == "token":
            assert set(data) == TOKEN_KEYS
    assert set(events[-2][1]) == AGENT_RESPONSE_KEYS
    assert events[-1][1] == {}


def test_stream_uses_lf_framing_and_single_line_json(make_client: ClientFactory) -> None:
    client, _ = make_client()

    response = client.post("/api/chat/stream", json={"message": "Show all customers"})

    assert "\r" not in response.text
    assert response.text.startswith("event: start\ndata: {")
    assert response.text.endswith("\n\n")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-accel-buffering"] == "no"


def test_stream_reports_agent_errors_as_an_error_event(make_client: ClientFactory) -> None:
    client, _ = make_client(agent_options={"error": ("llm_not_configured", "Start Ollama first.")})

    response, events = stream(client, {"message": "Show all customers"})

    assert response.status_code == 200
    names = [event for event, _ in events]
    assert names[0] == "start"
    assert names[-2:] == ["error", "done"]
    assert "final" not in names
    assert events[-2][1] == {"message": "Start Ollama first.", "code": "llm_not_configured"}


def test_stream_crash_still_ends_with_a_generic_error_and_done(make_client: ClientFactory) -> None:
    client, agent = make_client(agent_options={"explode": True})

    response, events = stream(client, {"message": "Show all customers"})

    assert response.status_code == 200
    assert [event for event, _ in events][-2:] == ["error", "done"]
    assert events[-2][1]["code"] == "internal"
    assert "boom" not in response.text
    assert SECRET_ERROR_TEXT not in response.text
    assert agent.stream_closed.is_set()


@pytest.fixture
def live_server(tmp_path: Path) -> Iterator[tuple[str, FakeAgent]]:
    """Run the app on a real uvicorn server (random port) with an agent that hangs after ``start``."""
    settings = make_settings(tmp_path)
    agent = FakeAgent(settings, hang=True)
    config = uvicorn.Config(
        create_app(settings, agent=agent),  # type: ignore[arg-type]
        host="127.0.0.1",
        port=0,
        log_level="warning",
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            pytest.fail("uvicorn did not start")
        time.sleep(0.02)
    port = server.servers[0].sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", agent
    server.should_exit = True
    thread.join(timeout=10)


def test_client_disconnect_cancels_the_agent_run(live_server: tuple[str, FakeAgent]) -> None:
    base_url, agent = live_server

    with (
        httpx.Client(base_url=base_url, timeout=10) as http,
        http.stream("POST", "/api/chat/stream", json={"message": "Show all customers"}) as response,
    ):
        assert response.status_code == 200
        assert next(response.iter_lines()) == "event: start"
        assert not agent.stream_closed.is_set()

    assert agent.stream_closed.wait(timeout=5), "the agent kept running after the client disconnected"


async def test_disconnect_while_an_event_is_being_sent_still_closes_the_agent_stream(tmp_path: Path) -> None:
    """The response is cancelled while it writes, not while it awaits the agent (a stalled socket)."""
    agent = FakeAgent(make_settings(tmp_path))
    response = AgentEventStream(sse_messages(agent, ChatInput(message="Show all customers")))  # type: ignore[arg-type]
    first_event_sent = asyncio.Event()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_event_sent.set()
            await asyncio.Event().wait()  # the client stopped reading

    async def receive() -> Message:
        await first_event_sent.wait()
        return {"type": "http.disconnect"}

    scope = {"type": "http", "method": "POST", "path": "/api/chat/stream", "headers": []}
    await asyncio.wait_for(response(scope, receive, send), timeout=5)

    assert agent.stream_closed.is_set(), "the agent stream was left suspended after the disconnect"
