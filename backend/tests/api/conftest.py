"""Fixtures for the API tests: deterministic settings and an app factory with an injectable agent."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from tests.api.fakes import FakeAgent

DEV_ORIGIN = "http://localhost:5173"


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    """Settings that ignore ``.env`` files and never auto-detect a built UI."""
    values: dict[str, Any] = {
        "llm_provider": "mock",
        "rate_limit_enabled": False,
        "max_input_chars": 2000,
        "cors_origins": f"{DEV_ORIGIN},http://127.0.0.1:5173",
        "static_dir": str(tmp_path / "no-ui"),
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


ClientFactory = Callable[..., tuple[TestClient, FakeAgent]]


@pytest.fixture
def make_client(tmp_path: Path) -> Iterator[ClientFactory]:
    """Build ``(TestClient, FakeAgent)`` pairs; ``agent_options`` go to ``FakeAgent``, the rest to settings."""
    with ExitStack() as stack:

        def factory(
            *,
            agent_options: dict[str, Any] | None = None,
            raise_server_exceptions: bool = True,
            **overrides: Any,
        ) -> tuple[TestClient, FakeAgent]:
            settings = make_settings(tmp_path, **overrides)
            agent = FakeAgent(settings, **(agent_options or {}))
            app = create_app(settings, agent=agent)  # type: ignore[arg-type]
            client = TestClient(app, raise_server_exceptions=raise_server_exceptions)
            return stack.enter_context(client), agent

        yield factory


@pytest.fixture
def client(make_client: ClientFactory) -> TestClient:
    """A client for an app backed by a successful ``FakeAgent``."""
    return make_client()[0]
