"""The production app on the Render configuration (Groq), offline.

``create_app`` with its lifespan builds the real ``SQLAgent`` and gateway, exactly as in the container;
only the Groq endpoint is the local fake. This checks the public deployment end to end: health, a full
chat turn (intent on the fast model, SQL on the main model, a streamed explanation), SSE, and how
provider failures reach the HTTP client.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agent.llm import RATE_LIMITED_MESSAGE
from app.main import create_app
from tests.deploy.conftest import FALLBACK, FAST, PRIMARY, groq_settings
from tests.deploy.fake_chat_api import (
    INVALID_KEY,
    FakeChatAPI,
    RecordedRequest,
    Reply,
    rate_limited,
    stream_chunks,
    structured,
)

SQL = "SELECT CustomerID, FirstName, LastName, State FROM Customers"
EXPLANATION = ["This query ", "lists every ", "customer."]


def pipeline_responder(request: RecordedRequest) -> Reply:
    """Plausible replies per role: intent, SQL generation, streamed explanation."""
    if request.kind == "stream":
        return 200, {}, stream_chunks(request.model, EXPLANATION)
    if request.schema_name == "IntentDecision":
        return structured(request, {"intent": "generate_sql", "confidence": 0.95, "reasoning": "customer rows"})
    if request.schema_name == "SQLGeneration":
        return structured(request, {"status": "ok", "sql": SQL, "summary": "All customers."})
    raise AssertionError(f"unexpected request: {request.body}")


HostedClient = Callable[..., TestClient]


@pytest.fixture
def hosted_client(fake_api: FakeChatAPI, db_path: Path, tmp_path: Path) -> Iterator[HostedClient]:
    """Start the app (with lifespan) on the Groq settings; keyword arguments override settings."""
    clients: list[TestClient] = []

    def factory(**overrides: Any) -> TestClient:
        values: dict[str, Any] = {
            "database_path": str(db_path),
            "static_dir": str(tmp_path / "no-ui"),
            "rate_limit_enabled": False,
            "cors_origins": "",
        }
        values.update(overrides)
        client = TestClient(create_app(groq_settings(**values)))
        clients.append(client.__enter__())
        return client

    fake_api.responder = pipeline_responder
    try:
        yield factory
    finally:
        for client in clients:
            client.__exit__(None, None, None)


def _health(client: TestClient) -> dict[str, Any]:
    deadline = time.monotonic() + 5
    while True:  # the warm-up task finishes at once for hosted providers; give the event loop a moment
        health = client.get("/api/health").json()
        if health["llm"]["warmup"] != "warming" or time.monotonic() > deadline:
            return health
        time.sleep(0.02)


def test_health_reports_groq_as_configured_without_calling_it(hosted_client: HostedClient, fake_api: FakeChatAPI) -> None:
    health = _health(hosted_client())

    assert health["status"] == "ok"
    assert health["llm"] == {
        "provider": "groq", "model": PRIMARY, "fast_model": FAST, "fallback_models": [FALLBACK],
        "configured": True, "mock": False, "warmup": "skipped",
    }
    assert health["database"] == {"dialect": "sqlite", "tables": 7}
    assert fake_api.requests == []  # no warm-up and no probe: health never spends Groq quota


def test_chat_runs_the_whole_pipeline_on_groq(hosted_client: HostedClient, fake_api: FakeChatAPI) -> None:
    response = hosted_client().post("/api/chat", json={"message": "Show all customers."})

    assert response.status_code == 200
    body = response.json()
    assert (body["status"], body["intent"], body["model"]) == ("success", "generate_sql", PRIMARY)
    assert body["validation"]["valid"] is True
    assert body["execution"]["row_count"] == 300
    assert body["explanation"] == "".join(EXPLANATION)
    assert [(request.model, request.kind, request.schema_name) for request in fake_api.requests] == [
        (FAST, "json_schema", "IntentDecision"),
        (PRIMARY, "json_schema", "SQLGeneration"),
        (FAST, "stream", None),
    ]


def test_chat_stream_relays_groq_tokens(hosted_client: HostedClient, fake_api: FakeChatAPI) -> None:
    with hosted_client().stream("POST", "/api/chat/stream", json={"message": "Show all customers."}) as response:
        assert response.status_code == 200
        lines = list(response.iter_lines())

    events = [line.removeprefix("event: ") for line in lines if line.startswith("event: ")]
    payloads = [json.loads(line.removeprefix("data: ")) for line in lines if line.startswith("data: ")]
    assert events[0] == "start" and events[-2:] == ["final", "done"]
    tokens = [data["text"] for name, data in zip(events, payloads, strict=True) if name == "token"]
    assert "".join(tokens) == "".join(EXPLANATION)
    final = payloads[events.index("final")]
    assert final["status"] == "success" and final["sql"]


def test_groq_rate_limit_reaches_the_client_as_http_429(hosted_client: HostedClient, fake_api: FakeChatAPI) -> None:
    client = hosted_client()
    fake_api.responder = lambda request: rate_limited(request.model)

    response = client.post("/api/chat", json={"message": "Show all customers."})

    assert response.status_code == 429
    assert response.json() == {"detail": RATE_LIMITED_MESSAGE, "code": "rate_limited"}
    assert set(fake_api.models()) == {FAST, FALLBACK}  # the fallback model was tried before giving up


def test_invalid_groq_key_reaches_the_client_as_http_503(hosted_client: HostedClient, fake_api: FakeChatAPI) -> None:
    client = hosted_client()
    fake_api.responder = lambda request: INVALID_KEY

    response = client.post("/api/chat", json={"message": "Show all customers."})

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "llm_not_configured" and "GROQ_API_KEY" in body["detail"]


def test_missing_groq_key_is_visible_in_health_and_chat(hosted_client: HostedClient, fake_api: FakeChatAPI) -> None:
    client = hosted_client(groq_api_key="")

    assert _health(client)["llm"]["configured"] is False
    response = client.post("/api/chat", json={"message": "Show all customers."})
    assert response.status_code == 503
    assert response.json()["code"] == "llm_not_configured" and "GROQ_API_KEY" in response.json()["detail"]
    assert fake_api.requests == []
