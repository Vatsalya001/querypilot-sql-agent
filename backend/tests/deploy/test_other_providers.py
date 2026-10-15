"""Sanity checks for the other hosted providers, offline.

``LLM_PROVIDER=openai`` (any OpenAI-compatible server) runs against the local fake API over HTTP;
``LLM_PROVIDER=google`` is checked for construction only (no Gemini request is ever sent).
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.llm import LangChainGateway, LLMError, build_gateway, llm_health
from app.agent.schemas import IntentDecision, SQLGeneration
from app.config import Settings
from tests.deploy.fake_chat_api import FakeChatAPI, error, stream_chunks, structured

MESSAGES = [SystemMessage(content="You are QueryPilot."), HumanMessage(content="Which tables exist?")]


def openai_gateway(base_url: str, **overrides: object) -> LangChainGateway:
    values: dict[str, object] = {"llm_provider": "openai", "llm_model": "local-model", "openai_base_url": base_url}
    values.update(overrides)
    gateway = build_gateway(Settings(_env_file=None, **values))
    assert isinstance(gateway, LangChainGateway)
    return gateway


async def test_openai_compatible_server_without_a_key(fake_api: FakeChatAPI) -> None:
    gateway = openai_gateway(f"{fake_api.url}/v1")
    fake_api.queue(
        lambda request: structured(request, {"intent": "schema_info", "confidence": 0.8}),
        (200, {}, stream_chunks("local-model", ["Seven ", "tables."], reasoning=None)),
    )

    decision = await gateway.classify_intent(MESSAGES)
    text = await gateway.stream_text(MESSAGES)

    assert decision.intent == "schema_info" and text == "Seven tables."
    structured_request, stream_request = fake_api.requests
    assert structured_request.path == "/v1/chat/completions"
    assert structured_request.headers["authorization"] == "Bearer not-needed"  # local servers need no key
    assert (structured_request.model, structured_request.kind) == ("local-model", "json_schema")
    assert structured_request.schema_name == "IntentDecision"
    assert stream_request.kind == "stream"
    for request in fake_api.requests:  # no Groq- or Ollama-specific parameters
        assert not {"reasoning_effort", "reasoning_format", "num_ctx", "keep_alive", "options"} & set(request.body)
    assert await gateway.warm_up(intent_system="I", sql_system="S") is False
    assert len(fake_api.requests) == 2


async def test_openai_auth_failure_names_the_key_variable(fake_api: FakeChatAPI) -> None:
    gateway = openai_gateway(f"{fake_api.url}/v1", openai_api_key="sk-offline-not-real")
    fake_api.responder = lambda request: error(401, "Incorrect API key provided.", "invalid_api_key")

    with pytest.raises(LLMError) as caught:
        await gateway.generate_sql(MESSAGES)

    assert caught.value.code == "llm_not_configured" and "OPENAI_API_KEY" in caught.value.user_message
    assert fake_api.requests[0].headers["authorization"] == "Bearer sk-offline-not-real"


async def test_openai_health_is_configured_by_a_key_or_a_base_url() -> None:
    assert (await llm_health(Settings(_env_file=None, llm_provider="openai")))["configured"] is False
    with_url = Settings(_env_file=None, llm_provider="openai", openai_base_url="http://127.0.0.1:1234/v1")
    assert (await llm_health(with_url))["configured"] is True


async def test_google_gateway_builds_gemini_models_offline() -> None:
    from langchain_google_genai import ChatGoogleGenerativeAI

    settings = Settings(
        _env_file=None, llm_provider="google", llm_model="gemini-flash-latest",
        llm_fast_model="gemini-flash-lite-latest", google_api_key="AIza-offline-test-key",
    )
    gateway = build_gateway(settings)
    assert isinstance(gateway, LangChainGateway)

    model = gateway._build_chat_model("gemini-flash-latest", 700)

    assert isinstance(model, ChatGoogleGenerativeAI)
    assert (model.max_output_tokens, model.temperature, model.max_retries, model.timeout) == (700, 0.0, 1, 120.0)
    for schema in (IntentDecision, SQLGeneration):
        for method in ("json_schema", "function_calling"):
            gateway._runnable(
                fast=schema is IntentDecision,
                max_tokens=700,
                transform=lambda llm, s=schema, m=method: llm.with_structured_output(s, method=m, include_raw=True),
            )
    assert await gateway.warm_up(intent_system="I", sql_system="S") is False
    assert (await llm_health(settings))["configured"] is True


async def test_google_without_a_key_fails_before_any_request() -> None:
    gateway = build_gateway(Settings(_env_file=None, llm_provider="google", llm_model="gemini-flash-latest"))

    with pytest.raises(LLMError) as caught:
        await gateway.classify_intent(MESSAGES)

    assert caught.value.code == "llm_not_configured" and "GOOGLE_API_KEY" in caught.value.user_message
