"""Fixtures for the offline tests of the hosted (Groq / OpenAI-compatible) deployment path.

``fake_api`` starts :class:`~tests.deploy.fake_chat_api.FakeChatAPI` and points the real ``groq`` client at
it through ``GROQ_API_BASE`` (the variable ``langchain-groq`` reads), so nothing ever reaches the network.
``groq_settings`` mirrors the configuration in ``render.yaml`` with a dummy key.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from app.config import Settings
from tests.deploy.fake_chat_api import FakeChatAPI

PRIMARY = "openai/gpt-oss-120b"
FAST = "openai/gpt-oss-20b"
FALLBACK = "qwen/qwen3.8-27b"
DUMMY_KEY = "gsk_offline_test_key_not_real"


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeChatAPI]:
    """A running fake chat API; ChatGroq instances built during the test talk to it."""
    api = FakeChatAPI().start()
    monkeypatch.setenv("GROQ_API_BASE", api.url)
    for name in ("GROQ_BASE_URL", "GROQ_PROXY", "GROQ_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    try:
        yield api
    finally:
        api.stop()


def groq_settings(**overrides: Any) -> Settings:
    """The Render configuration (``render.yaml``) with a dummy key; no ``.env`` files are read."""
    values: dict[str, Any] = {
        "llm_provider": "groq",
        "llm_model": PRIMARY,
        "llm_fast_model": FAST,
        "llm_fallback_models": FALLBACK,
        "groq_api_key": DUMMY_KEY,
        "llm_reasoning": False,
        "log_level": "WARNING",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)
