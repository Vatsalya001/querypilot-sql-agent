"""Full-stack API tests: HTTP layer + real SQLAgent (LangGraph, validator, read-only executor, cost).

The app is built exactly as in production (``create_app`` without an injected agent, so the lifespan
creates the ``SQLAgent``) with ``LLM_PROVIDER=mock``: the deterministic offline ``MockGateway`` stands in
for the model, so no network or model server is needed. The sample database is the session one, seeded
into a temp directory by tests/conftest.py.
"""

import asyncio
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import BaseMessage

from app.agent.llm import LLMError
from app.agent.prompts import OUT_OF_SCOPE_MESSAGE
from app.agent.service import SQLAgent
from app.config import Settings
from app.main import create_app
from tests.api.conftest import make_settings
from tests.api.fakes import (
    AGENT_RESPONSE_KEYS,
    NODE_NAMES,
    STEP_KEYS,
    TOKEN_KEYS,
    assert_schema_contract,
    parse_sse,
)

HIRED_AFTER_JAN_2024 = "Show all employees hired after January 2024."
SERVER_DOWN = "The local model server (Ollama) is not running."


@pytest.fixture(scope="module")
def settings(tmp_path_factory: pytest.TempPathFactory, db_path: Path) -> Settings:
    """Mock-LLM settings on the session's sample database (``db_path`` from tests/conftest.py)."""
    import app.agent.mock_llm  # noqa: F401  (a broken mock gateway must fail these tests, not skip them)

    return make_settings(
        tmp_path_factory.mktemp("full_stack"), llm_provider="mock", database_path=str(db_path)
    )


@pytest.fixture(scope="module")
def stack(settings: Settings) -> Iterator[TestClient]:
    """One app with the real agent for the whole module (each test uses its own thread ids)."""
    with TestClient(create_app(settings)) as client:
        yield client


class UnreachableModelGateway:
    """A gateway whose model server is down: every call fails the way an unreachable Ollama does."""

    provider = "ollama"
    model = fast_model = "qwen3.5:9b"
    mock = False

    async def classify_intent(self, messages: list[BaseMessage]) -> Any:
        raise LLMError("llm_not_configured", SERVER_DOWN)

    async def generate_sql(self, messages: list[BaseMessage]) -> Any:
        raise LLMError("llm_not_configured", SERVER_DOWN)

    async def stream_text(self, messages: list[BaseMessage], *, on_token: Any = None) -> str:
        raise LLMError("llm_not_configured", SERVER_DOWN)


def new_thread() -> str:
    return str(uuid.uuid4())


def chat(client: TestClient, message: str, **fields: Any) -> dict[str, Any]:
    """POST /api/chat and return the AgentResponse after checking the contract keys."""
    response = client.post("/api/chat", json={"message": message, **fields})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == AGENT_RESPONSE_KEYS
    return body


def column_values(execution: dict[str, Any], column: str) -> list[Any]:
    """All values of ``column`` in an execution result."""
    index = execution["columns"].index(column)
    return [row[index] for row in execution["rows"]]


def assert_success(body: dict[str, Any], intent: str) -> None:
    assert body["status"] == "success", body["message"]
    assert body["intent"] == intent
    assert body["sql"] and body["sql_sqlite"]
    assert body["validation"]["valid"] is True
    assert all(check["passed"] for check in body["validation"]["checks"])
    assert body["optimization"] is not None
    assert body["cost"] is not None
    assert isinstance(body["explanation"], str) and body["explanation"].strip()
    assert body["model"]
    assert body["steps"], "completed steps are missing"
    for step in body["steps"]:
        assert set(step) == STEP_KEYS
        assert step["status"] == "completed"
        assert step["node"] in NODE_NAMES


def test_metadata_endpoints_describe_the_real_agent(stack: TestClient) -> None:
    schema = stack.get("/api/schema").json()
    assert_schema_contract(schema)
    table_names = {table["name"] for table in schema["tables"]}
    assert {"Customers", "Employees", "Orders"} <= table_names
    assert {
        "from_table": "Orders",
        "from_column": "CustomerID",
        "to_table": "Customers",
        "to_column": "CustomerID",
    } in schema["relationships"]

    health = stack.get("/api/health").json()
    assert health["status"] == "ok"
    assert health["database"] == {"dialect": "sqlite", "tables": len(schema["tables"])}
    assert health["llm"]["mock"] is True
    assert health["llm"]["configured"] is True

    graph = stack.get("/api/graph").json()
    assert graph["nodes"] == NODE_NAMES
    for node in NODE_NAMES:
        assert node in graph["mermaid"]


def test_generates_validates_and_executes_sql(stack: TestClient) -> None:
    body = chat(stack, HIRED_AFTER_JAN_2024, thread_id=new_thread())

    assert_success(body, "generate_sql")
    assert "HireDate" in body["sql"]
    assert "2024-01-01" in body["sql"]
    assert body["dialect"] == "sqlite"
    execution = body["execution"]
    assert execution["error"] is None
    assert execution["row_count"] == len(execution["rows"]) > 0
    assert all(hire_date >= "2024-01-01" for hire_date in column_values(execution, "HireDate"))
    nodes = [step["node"] for step in body["steps"]]
    for node in ("guard_input", "detect_intent", "generate_sql", "validate_sql", "execute_sql", "respond"):
        assert node in nodes
    assert nodes[0] == "guard_input"
    assert nodes[-1] == "respond"


def test_follow_up_modifies_the_previous_query_on_the_same_thread(stack: TestClient) -> None:
    thread_id = new_thread()
    first = chat(stack, "Show all customers.", thread_id=thread_id)
    assert_success(first, "generate_sql")
    assert "California" not in first["sql"]

    follow_up = chat(stack, "Only those from California.", thread_id=thread_id)

    assert_success(follow_up, "modify_sql")
    assert follow_up["thread_id"] == thread_id
    assert "Customers" in follow_up["sql"]
    assert "'California'" in follow_up["sql"]
    states = column_values(follow_up["execution"], "State")
    assert states, "the sample data should contain Californian customers"
    assert set(states) == {"California"}
    assert follow_up["execution"]["row_count"] < first["execution"]["row_count"]


def test_client_history_rehydrates_an_unknown_thread(stack: TestClient) -> None:
    history = [
        {"role": "user", "content": "Show all customers.", "sql": None},
        {"role": "assistant", "content": "Here are all customers.", "sql": "SELECT * FROM Customers"},
    ]

    body = chat(stack, "Only those from Texas.", thread_id=new_thread(), history=history)

    assert_success(body, "modify_sql")
    assert "'Texas'" in body["sql"]
    assert set(column_values(body["execution"], "State")) == {"Texas"}


def test_thread_reset_clears_server_memory(stack: TestClient) -> None:
    agent = stack.app.state.agent  # type: ignore[attr-defined]
    thread_id = new_thread()
    config = {"configurable": {"thread_id": thread_id}}
    chat(stack, "Show all customers.", thread_id=thread_id)
    assert agent.graph.get_state(config).values.get("last_sql")

    response = stack.delete(f"/api/threads/{thread_id}")

    assert response.json() == {"ok": True}
    assert not agent.graph.get_state(config).values


@pytest.mark.parametrize("message", ["Delete all customers from Texas.", "DROP TABLE Customers;"])
def test_destructive_requests_are_refused(stack: TestClient, message: str) -> None:
    body = chat(stack, message, thread_id=new_thread())

    assert body["status"] == "refused"
    assert body["intent"] in ("destructive", None)
    assert body["sql"] is None
    assert body["execution"] is None
    assert "read-only" in body["message"].lower()
    customers = chat(stack, "Show all customers.", thread_id=new_thread())
    assert customers["execution"]["row_count"] > 0


def test_out_of_scope_requests_are_rejected(stack: TestClient) -> None:
    body = chat(stack, "Who won the FIFA World Cup in 2022?", thread_id=new_thread())

    assert body["status"] == "rejected"
    assert body["intent"] == "out_of_scope"
    assert body["message"] == OUT_OF_SCOPE_MESSAGE
    assert body["sql"] is None


def test_debugging_fixes_misspelled_identifiers(stack: TestClient) -> None:
    body = chat(
        stack,
        "This query fails, please fix it: SELECT FirstName, Stat FROM Customer WHERE Stat = 'California'",
        thread_id=new_thread(),
    )

    assert_success(body, "debug_sql")
    assert "Customers" in body["sql"]
    assert "State" in body["sql"]
    assert body["issues"], "a debug answer lists the problems it fixed"


def test_optimizing_user_sql_recommends_an_index(stack: TestClient) -> None:
    body = chat(
        stack,
        "Optimize this query: SELECT * FROM Customers WHERE State = 'California'",
        thread_id=new_thread(),
    )

    assert_success(body, "optimize_sql")
    assert "SELECT *" not in body["sql"].upper().replace("\n", " ")
    assert any("State" in index for index in body["optimization"]["index_recommendations"])


def test_dialect_and_execute_options(stack: TestClient) -> None:
    body = chat(stack, HIRED_AFTER_JAN_2024, thread_id=new_thread(), dialect="postgres", execute=False)

    assert body["status"] == "success"
    assert body["dialect"] == "postgres"
    assert "HireDate" in body["sql"] and "HireDate" in body["sql_sqlite"]
    assert body["execution"] is None
    assert body["cost"] is not None


def test_stream_runs_the_real_graph_in_contract_order(stack: TestClient) -> None:
    thread_id = new_thread()

    response = stack.post("/api/chat/stream", json={"message": HIRED_AFTER_JAN_2024, "thread_id": thread_id})

    assert response.status_code == 200
    events = [(event, json.loads(data)) for event, data in parse_sse(response.text)]
    names = [event for event, _ in events]
    assert names[0] == "start"
    assert names[-2:] == ["final", "done"]
    assert set(names[1:-2]) == {"step", "token"}
    assert events[0][1]["thread_id"] == thread_id
    steps = [data for event, data in events if event == "step"]
    tokens = [data for event, data in events if event == "token"]
    for step in steps:
        assert set(step) == STEP_KEYS
        assert step["node"] in NODE_NAMES
    for token in tokens:
        assert set(token) == TOKEN_KEYS
        assert (token["node"], token["target"]) == ("explain_sql", "explanation")
    final = events[-2][1]
    assert set(final) == AGENT_RESPONSE_KEYS
    assert final["status"] == "success"
    assert final["message_id"] == events[0][1]["message_id"]
    assert final["steps"] == [step for step in steps if step["status"] == "completed"]
    assert "".join(token["text"] for token in tokens).strip() == final["explanation"].strip()


def test_model_outage_becomes_a_503_and_an_error_event(settings: Settings) -> None:
    agent = SQLAgent(settings, gateway=UnreachableModelGateway())  # type: ignore[arg-type]

    with TestClient(create_app(settings, agent=agent)) as client:
        response = client.post("/api/chat", json={"message": HIRED_AFTER_JAN_2024})
        streamed = client.post("/api/chat/stream", json={"message": HIRED_AFTER_JAN_2024})

    assert response.status_code == 503
    assert response.json() == {"detail": SERVER_DOWN, "code": "llm_not_configured"}
    events = [(event, json.loads(data)) for event, data in parse_sse(streamed.text)]
    names = [event for event, _ in events]
    assert names[0] == "start"
    assert names[-2:] == ["error", "done"]
    assert "final" not in names
    assert events[-2][1] == {"message": SERVER_DOWN, "code": "llm_not_configured"}


def test_startup_warms_the_model_in_the_background(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[SQLAgent] = []

    async def fake_warm_up(self: SQLAgent) -> bool:
        calls.append(self)
        return True

    monkeypatch.setattr(SQLAgent, "warm_up", fake_warm_up)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        task = app.state.warmup_task
    assert calls == [app.state.agent]
    assert task.done() and task.result() is True


def test_shutdown_cancels_an_unfinished_warm_up(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_warm_up(self: SQLAgent) -> bool:
        await asyncio.sleep(60)
        return True

    monkeypatch.setattr(SQLAgent, "warm_up", slow_warm_up)
    app = create_app(settings)
    with TestClient(app):
        task = app.state.warmup_task
        assert not task.done()
    assert task.cancelled()


def test_an_injected_agent_is_not_warmed_up(settings: Settings) -> None:
    agent = SQLAgent(settings)
    app = create_app(settings, agent=agent)
    with TestClient(app):
        assert app.state.warmup_task is None
