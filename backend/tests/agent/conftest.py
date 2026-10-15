"""Fixtures for the agent tests.

``make_agent(gateway=None, **settings_overrides)`` builds an :class:`~app.agent.service.SQLAgent` on the
session database (``db_path`` / ``catalog`` from ``tests/conftest.py``) with the offline ``MockGateway``
unless another gateway (e.g. ``tests.agent.helpers.FakeGateway``) is given.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from app.agent.llm import LLMGateway
from app.agent.mock_llm import MockGateway
from app.config import Settings

if TYPE_CHECKING:
    from app.agent.service import SQLAgent
    from app.db.catalog import SchemaCatalog

AgentFactory = Callable[..., "SQLAgent"]


def make_settings(**overrides: Any) -> Settings:
    """Deterministic settings: no ``.env`` files, the mock provider and the documented defaults."""
    values: dict[str, Any] = {
        "llm_provider": "mock",
        "llm_model": "qwen3.5:9b",
        "llm_fast_model": "",
        "llm_fallback_models": "",
        "max_sql_attempts": 3,
        "history_window": 8,
        "max_input_chars": 2000,
        "max_result_rows": 500,
        "query_timeout_s": 5.0,
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def make_agent(db_path: Path, catalog: SchemaCatalog) -> AgentFactory:
    """Factory for agents on the session database; keyword arguments override settings."""
    from app.agent.service import SQLAgent

    def factory(gateway: LLMGateway | None = None, **overrides: Any) -> SQLAgent:
        settings = make_settings(database_path=str(db_path), **overrides)
        return SQLAgent(settings, gateway=gateway or MockGateway(), catalog=catalog, db_path=db_path)

    return factory


@pytest.fixture
def agent(make_agent: AgentFactory) -> SQLAgent:
    """An agent backed by the rule-based mock gateway."""
    return make_agent()
