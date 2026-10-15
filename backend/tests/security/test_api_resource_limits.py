"""End to end: SQL pasted for explanation is executed, so its resource limits must hold through the API.

Uses the real agent with the offline mock LLM (no network), like tests/api/test_full_stack.py.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from tests.api.conftest import make_settings


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory, db_path: Path) -> Iterator[TestClient]:
    settings = make_settings(tmp_path_factory.mktemp("security_stack"), llm_provider="mock",
                             database_path=str(db_path))
    with TestClient(create_app(settings)) as client:
        yield client


@pytest.mark.parametrize(
    "sql",
    ["SELECT length(zeroblob(1000000000)) AS n", "SELECT zeroblob(50000000) AS z"],
)
def test_oversized_values_in_pasted_sql_are_refused_by_the_database(stack: TestClient, sql: str) -> None:
    response = stack.post("/api/chat", json={"message": f"Explain this query: {sql}"})
    assert response.status_code == 200
    assert len(response.content) < 200_000  # previously a 100 MB body for the 50 MB BLOB
    body = response.json()
    assert body["execution"] is not None
    assert "limit" in (body["execution"]["error"] or "")
    assert body["execution"]["rows"] == []
