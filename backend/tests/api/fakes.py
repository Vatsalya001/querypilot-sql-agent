"""Test doubles for the API layer: a scripted stand-in for ``SQLAgent`` plus contract key sets."""

import asyncio
import copy
import threading
from collections.abc import AsyncIterator
from typing import Any

from app.agent.service import AgentRunError, ChatInput
from app.config import Settings

NODE_NAMES = [
    "guard_input",
    "detect_intent",
    "validate_scope",
    "retrieve_schema",
    "diagnose_sql",
    "generate_sql",
    "validate_sql",
    "optimize_sql",
    "execute_sql",
    "explain_sql",
    "answer_question",
    "ask_clarification",
    "reject_request",
    "refuse_destructive",
    "handle_failure",
    "respond",
]
"""Node names from docs/API.md."""

AGENT_RESPONSE_KEYS = {
    "thread_id",
    "message_id",
    "intent",
    "status",
    "message",
    "sql",
    "sql_sqlite",
    "dialect",
    "explanation",
    "validation",
    "optimization",
    "execution",
    "cost",
    "issues",
    "changes",
    "suggestions",
    "assumptions",
    "clarification_question",
    "schema_tables",
    "attempts",
    "steps",
    "model",
}
STEP_KEYS = {"node", "label", "status", "duration_ms", "detail"}
TOKEN_KEYS = {"node", "target", "text"}
SCHEMA_KEYS = {"dialect", "tables", "relationships"}
TABLE_KEYS = {"name", "description", "row_count", "columns", "indexes"}
COLUMN_KEYS = {"name", "type", "primary_key", "nullable", "description", "foreign_key", "sample_values"}
INDEX_KEYS = {"name", "columns", "unique"}
RELATIONSHIP_KEYS = {"from_table", "from_column", "to_table", "to_column"}

SAMPLE_SCHEMA: dict[str, Any] = {
    "dialect": "sqlite",
    "tables": [
        {
            "name": "Customers",
            "description": "People and companies that place orders.",
            "row_count": 300,
            "columns": [
                {
                    "name": "CustomerID",
                    "type": "INTEGER",
                    "primary_key": True,
                    "nullable": False,
                    "description": "Unique customer id.",
                    "foreign_key": None,
                    "sample_values": [],
                },
                {
                    "name": "State",
                    "type": "TEXT",
                    "primary_key": False,
                    "nullable": False,
                    "description": "Full US state name.",
                    "foreign_key": None,
                    "sample_values": ["California", "Texas"],
                },
            ],
            "indexes": [{"name": "idx_customers_state", "columns": ["State"], "unique": False}],
        },
        {
            "name": "Orders",
            "description": "Customer orders.",
            "row_count": 1200,
            "columns": [
                {
                    "name": "OrderID",
                    "type": "INTEGER",
                    "primary_key": True,
                    "nullable": False,
                    "description": "Unique order id.",
                    "foreign_key": None,
                    "sample_values": [],
                },
                {
                    "name": "CustomerID",
                    "type": "INTEGER",
                    "primary_key": False,
                    "nullable": False,
                    "description": "Who placed the order.",
                    "foreign_key": {"table": "Customers", "column": "CustomerID"},
                    "sample_values": [],
                },
            ],
            "indexes": [],
        },
    ],
    "relationships": [
        {
            "from_table": "Orders",
            "from_column": "CustomerID",
            "to_table": "Customers",
            "to_column": "CustomerID",
        }
    ],
}

_SQL = "SELECT\n  *\nFROM Customers\nWHERE\n  State = 'California'"

SAMPLE_RESPONSE: dict[str, Any] = {
    "thread_id": "",
    "message_id": "msg-1",
    "intent": "generate_sql",
    "status": "success",
    "message": "Here are all customers located in California.",
    "sql": _SQL,
    "sql_sqlite": _SQL,
    "dialect": "sqlite",
    "explanation": "This query retrieves every customer whose state is California.",
    "validation": {
        "valid": True,
        "checks": [{"name": "syntax", "label": "Syntax", "passed": True, "message": "Valid SQLite syntax."}],
        "errors": [],
        "warnings": [],
        "sql": "SELECT * FROM Customers WHERE State = 'California'",
        "tables": ["Customers"],
        "columns": ["Customers.State"],
        "destructive": False,
    },
    "optimization": {
        "formatted_sql": _SQL,
        "suggestions": [],
        "index_recommendations": [],
        "unnecessary_joins": [],
    },
    "execution": {
        "columns": ["CustomerID", "State"],
        "rows": [[1, "California"]],
        "row_count": 1,
        "truncated": False,
        "elapsed_ms": 1.7,
        "error": None,
    },
    "cost": {
        "level": "low",
        "score": 300,
        "estimated_rows_scanned": 300,
        "plan": ["SCAN Customers"],
        "notes": [],
    },
    "issues": [],
    "changes": [],
    "suggestions": [],
    "assumptions": [],
    "clarification_question": None,
    "schema_tables": ["Customers"],
    "attempts": 1,
    "steps": [],
    "model": "fake-model",
}

SECRET_ERROR_TEXT = "boom: secret internals at /srv/app/agent.py"


def step(node: str, label: str, status: str, detail: str | None = None) -> dict[str, Any]:
    """A ``step`` event payload."""
    duration_ms = 1.5 if status == "completed" else None
    return {"node": node, "label": label, "status": status, "duration_ms": duration_ms, "detail": detail}


class FakeCatalog:
    """Stands in for the schema catalog; only ``to_dict`` is used by the API."""

    def __init__(self, schema: dict[str, Any]) -> None:
        self._schema = schema

    def to_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._schema)


class FakeAgent:
    """Scripted implementation of the ``SQLAgent`` facade used by the API layer.

    Modes: default (success), ``error=(code, message)`` (error event), ``explode=True`` (raises an
    unexpected exception mid-stream) and ``hang=True`` (waits forever after ``start``, like a slow LLM).
    """

    def __init__(
        self,
        settings: Settings,
        *,
        error: tuple[str, str] | None = None,
        explode: bool = False,
        hang: bool = False,
    ) -> None:
        self.settings = settings
        self.catalog = FakeCatalog(SAMPLE_SCHEMA)
        self.error = error
        self.explode = explode
        self.hang = hang
        self.requests: list[ChatInput] = []
        self.reset_calls: list[str] = []
        self.stream_closed = threading.Event()
        """Set when the stream generator finishes, fails or is cancelled (observable from any thread)."""

    async def stream(self, req: ChatInput) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        self.requests.append(req)
        thread_id = req.thread_id or "server-made-thread"
        try:
            yield "start", {"thread_id": thread_id, "message_id": "msg-1"}
            if self.hang:
                await asyncio.Event().wait()
            yield "step", step("guard_input", "Checking input safety", "started")
            yield "step", step("guard_input", "Checking input safety", "completed", "ok")
            if self.explode:
                raise RuntimeError(SECRET_ERROR_TEXT)
            yield "step", step("explain_sql", "Explaining query", "started")
            for text in ("This query ", "retrieves ", "customers."):
                yield "token", {"node": "explain_sql", "target": "explanation", "text": text}
            yield "step", step("explain_sql", "Explaining query", "completed", "3 tokens")
            if self.error is not None:
                code, message = self.error
                yield "error", {"message": message, "code": code}
            else:
                yield "final", {**SAMPLE_RESPONSE, "thread_id": thread_id, "dialect": req.dialect}
            yield "done", {}
        finally:
            self.stream_closed.set()

    async def run(self, req: ChatInput) -> dict[str, Any]:
        final: dict[str, Any] | None = None
        async for event, data in self.stream(req):
            if event == "error":
                raise AgentRunError(code=data["code"], message=data["message"])
            if event == "final":
                final = data
        assert final is not None
        return final

    async def reset_thread(self, thread_id: str) -> None:
        self.reset_calls.append(thread_id)

    def mermaid(self) -> str:
        return "graph TD;\n\t__start__ --> guard_input;\n\trespond --> __end__;\n"

    def node_names(self) -> list[str]:
        return list(NODE_NAMES)


def parse_sse(body: str) -> list[tuple[str, str]]:
    """Parse a raw ``text/event-stream`` body into ``(event, data)`` pairs (comments are skipped)."""
    messages: list[tuple[str, str]] = []
    for block in body.replace("\r\n", "\n").split("\n\n"):
        event, data_lines = "message", []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:") :].strip())
        if data_lines:
            messages.append((event, "\n".join(data_lines)))
    return messages


def assert_schema_contract(schema: dict[str, Any]) -> None:
    """Assert a ``GET /api/schema`` body has exactly the keys of docs/API.md."""
    assert set(schema) == SCHEMA_KEYS
    assert schema["dialect"] == "sqlite"
    assert schema["tables"], "the schema has no tables"
    for table in schema["tables"]:
        assert set(table) == TABLE_KEYS, table["name"]
        for column in table["columns"]:
            assert set(column) == COLUMN_KEYS, (table["name"], column)
            assert column["foreign_key"] is None or set(column["foreign_key"]) == {"table", "column"}
        for index in table["indexes"]:
            assert set(index) == INDEX_KEYS
    for relationship in schema["relationships"]:
        assert set(relationship) == RELATIONSHIP_KEYS
