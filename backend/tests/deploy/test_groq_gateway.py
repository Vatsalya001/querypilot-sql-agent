"""The hosted LLM path, offline: the real gateway builds ChatGroq, which talks HTTP to a local fake Groq API.

Covers what the public deployment depends on but no live test can check without a key: the requests the
gateway sends (model per role, reasoning settings, structured-output format, no Ollama-only options), how
replies are parsed into ``IntentDecision`` / ``SQLGeneration``, streaming, the fallback chain on HTTP 429,
and the mapping of provider errors onto ``LLMError`` codes.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from app.agent.llm import RATE_LIMITED_MESSAGE, LangChainGateway, LLMError, build_gateway
from app.agent.schemas import IntentDecision, SQLGeneration
from tests.deploy.conftest import DUMMY_KEY, FALLBACK, FAST, PRIMARY, groq_settings
from tests.deploy.fake_chat_api import (
    GROQ_CHAT_PATH,
    INVALID_KEY,
    FakeChatAPI,
    completion,
    error,
    rate_limited,
    stream_chunks,
    structured,
)

MESSAGES = [SystemMessage(content="You are QueryPilot."), HumanMessage(content="Show all customers.")]
INTENT = {"intent": "generate_sql", "confidence": 0.94, "reasoning": "asks for customer rows"}
GENERATION = {
    "status": "ok",
    "sql": "SELECT CustomerID, FirstName, LastName FROM Customers",
    "assumptions": ["All customers, no filter."],
    "summary": "Lists every customer.",
}
OLLAMA_ONLY_KEYS = {"num_ctx", "num_predict", "keep_alive", "options", "think", "format"}
JSON_VALIDATE_FAILED = error(
    400,
    "Generated JSON does not match the expected schema. Please adjust your prompt. See 'failed_generation' for "
    "more details.",
    "json_validate_failed",
    failed_generation='{"status": "ok", "sql": "SELECT',
)
TOOL_USE_FAILED = error(
    400,
    "Failed to call a function. Please adjust your prompt. See 'failed_generation' for more details.",
    "tool_use_failed",
    failed_generation='<|channel|>commentary to=SQLGeneration',
)


def gateway(**overrides: object) -> LangChainGateway:
    built = build_gateway(groq_settings(**overrides))
    assert isinstance(built, LangChainGateway)
    return built


# ---- request shape and parsing ---------------------------------------------------------------------


async def test_intent_uses_the_fast_model_with_json_schema_output(fake_api: FakeChatAPI) -> None:
    fake_api.queue(lambda request: structured(request, INTENT))

    decision = await gateway().classify_intent(MESSAGES)

    assert isinstance(decision, IntentDecision)
    assert (decision.intent, decision.confidence) == ("generate_sql", 0.94)
    [request] = fake_api.requests
    assert request.path == GROQ_CHAT_PATH
    assert request.headers["authorization"] == f"Bearer {DUMMY_KEY}"
    assert request.model == FAST
    assert request.kind == "json_schema" and request.schema_name == "IntentDecision"
    assert request.body["response_format"]["json_schema"]["schema"]["properties"]["intent"]["enum"]
    assert request.body["reasoning_effort"] == "low"  # gpt-oss: low effort while LLM_REASONING is off
    assert request.body.get("reasoning_format") is None  # not supported by gpt-oss models on Groq
    assert request.body["max_tokens"] == 1024 and request.body["stream"] is False
    assert request.body["temperature"] < 1e-6
    assert not OLLAMA_ONLY_KEYS & set(request.body)
    assert [message["role"] for message in request.body["messages"]] == ["system", "user"]


async def test_sql_generation_uses_the_main_model_and_parses_every_field(fake_api: FakeChatAPI) -> None:
    fake_api.queue(lambda request: structured(request, GENERATION))

    result = await gateway(llm_max_output_tokens=900).generate_sql(MESSAGES)

    assert isinstance(result, SQLGeneration)
    assert result.sql == GENERATION["sql"] and result.assumptions == GENERATION["assumptions"]
    [request] = fake_api.requests
    assert (request.model, request.kind, request.schema_name) == (PRIMARY, "json_schema", "SQLGeneration")
    assert request.body["max_tokens"] == 900


async def test_reasoning_mode_raises_the_effort_of_gpt_oss_models(fake_api: FakeChatAPI) -> None:
    fake_api.queue(lambda request: structured(request, GENERATION))

    await gateway(llm_reasoning=True).generate_sql(MESSAGES)

    assert fake_api.requests[0].body["reasoning_effort"] == "medium"


async def test_stream_text_streams_the_answer_and_hides_the_reasoning(fake_api: FakeChatAPI) -> None:
    fake_api.queue((200, {}, stream_chunks(FAST, ["This query ", "lists every ", "customer."])))
    tokens: list[str] = []

    text = await gateway().stream_text(MESSAGES, on_token=tokens.append)

    assert text == "This query lists every customer."
    assert tokens == ["This query ", "lists every ", "customer."]
    [request] = fake_api.requests
    assert (request.model, request.kind) == (FAST, "stream")
    assert request.body["reasoning_effort"] == "low"
    assert "response_format" not in request.body and "tools" not in request.body


async def test_a_model_without_json_schema_support_gets_a_forced_tool_call(fake_api: FakeChatAPI) -> None:
    unsupported = error(400, "This model does not support response format `json_schema`.", "invalid_request_error")
    fake_api.queue(unsupported, lambda request: structured(request, GENERATION))
    llm = gateway(llm_fallback_models="")

    result = await llm.generate_sql(MESSAGES)

    assert result.sql == GENERATION["sql"]
    first, second = fake_api.requests
    assert (first.kind, second.kind) == ("json_schema", "tools")
    assert second.body["tool_choice"] == {"type": "function", "function": {"name": "SQLGeneration"}}
    assert [tool["function"]["name"] for tool in second.body["tools"]] == ["SQLGeneration"]

    # A genuinely unsupported format is remembered: the next call goes straight to tool calling.
    fake_api.queue(lambda request: structured(request, GENERATION))
    await llm.generate_sql(MESSAGES)
    assert fake_api.requests[-1].kind == "tools"


async def test_warm_up_is_skipped_for_groq(fake_api: FakeChatAPI) -> None:
    assert await gateway().warm_up(intent_system="I", sql_system="S", text_systems=("E",)) is False
    assert fake_api.requests == []


# ---- fallback chain and error mapping --------------------------------------------------------------


async def test_rate_limited_primary_falls_back_to_the_next_model(fake_api: FakeChatAPI) -> None:
    fake_api.responder = lambda request: (
        rate_limited(request.model) if request.model == PRIMARY else structured(request, GENERATION)
    )

    result = await gateway().generate_sql(MESSAGES)

    assert result.sql == GENERATION["sql"]
    models = fake_api.models()
    assert models[-1] == FALLBACK
    assert set(models[:-1]) == {PRIMARY} and 1 <= len(models[:-1]) <= 2  # the SDK retries a 429 at most once
    fallback_request = fake_api.requests[-1]
    assert fallback_request.kind == "json_schema"  # Groq supports JSON schema output for the Qwen fallback too
    assert fallback_request.body["reasoning_effort"] == "none"  # Qwen: thinking off


async def test_rate_limit_on_every_model_is_reported_as_rate_limited(fake_api: FakeChatAPI) -> None:
    fake_api.responder = lambda request: rate_limited(request.model)

    with pytest.raises(LLMError) as caught:
        await gateway().classify_intent(MESSAGES)

    assert caught.value.code == "rate_limited"
    assert caught.value.user_message == RATE_LIMITED_MESSAGE
    assert set(fake_api.models()) == {FAST, FALLBACK}
    assert {request.kind for request in fake_api.requests} == {"json_schema"}  # no pointless method retries


async def test_invalid_api_key_is_reported_as_not_configured(fake_api: FakeChatAPI) -> None:
    fake_api.responder = lambda request: INVALID_KEY

    with pytest.raises(LLMError) as caught:
        await gateway().generate_sql(MESSAGES)

    assert caught.value.code == "llm_not_configured"
    assert "GROQ_API_KEY" in caught.value.user_message
    assert DUMMY_KEY not in caught.value.user_message
    assert len(fake_api.requests) <= 2  # primary and fallback, never the other structured methods


async def test_unknown_model_is_reported_as_not_configured(fake_api: FakeChatAPI) -> None:
    fake_api.responder = lambda request: error(
        404, f"The model `{request.model}` does not exist or you do not have access to it.", "model_not_found"
    )

    with pytest.raises(LLMError) as caught:
        await gateway(llm_fallback_models="").classify_intent(MESSAGES)

    assert caught.value.code == "llm_not_configured"
    assert f"Model '{FAST}' is not available from groq" in caught.value.user_message
    assert len(fake_api.requests) == 1


async def test_retired_model_is_reported_as_not_configured(fake_api: FakeChatAPI) -> None:
    # Groq answers requests for a decommissioned model with HTTP 400 (not 404).
    fake_api.responder = lambda request: error(
        400,
        f"The model `{request.model}` has been decommissioned and is no longer supported. Please refer to "
        "https://console.groq.com/docs/deprecations for a recommendation on which model to use instead.",
        "model_decommissioned",
    )

    with pytest.raises(LLMError) as caught:
        await gateway(llm_fallback_models="").generate_sql(MESSAGES)

    assert caught.value.code == "llm_not_configured"
    assert f"Model '{PRIMARY}' is not available from groq" in caught.value.user_message
    assert "LLM_MODEL" in caught.value.user_message
    assert len(fake_api.requests) == 1  # no further structured-output methods tried against a retired model


async def test_a_failed_generation_does_not_disable_json_schema_output(fake_api: FakeChatAPI) -> None:
    # A 400 json_validate_failed is one bad generation, not an unsupported request: the next call must
    # still use JSON schema output instead of being downgraded for the rest of the process lifetime.
    fake_api.queue(JSON_VALIDATE_FAILED, lambda request: structured(request, GENERATION))
    llm = gateway(llm_fallback_models="")

    assert (await llm.generate_sql(MESSAGES)).sql == GENERATION["sql"]
    assert [request.kind for request in fake_api.requests] == ["json_schema", "tools"]

    fake_api.queue(lambda request: structured(request, GENERATION))
    await llm.generate_sql(MESSAGES)
    assert fake_api.requests[-1].kind == "json_schema"


async def test_a_failed_tool_call_does_not_disable_tool_calling(fake_api: FakeChatAPI) -> None:
    fake_api.queue(
        JSON_VALIDATE_FAILED,
        TOOL_USE_FAILED,
        lambda request: (200, {}, completion(request.model, content=json.dumps(GENERATION))),
    )
    llm = gateway(llm_fallback_models="")

    assert (await llm.generate_sql(MESSAGES)).sql == GENERATION["sql"]
    assert [request.kind for request in fake_api.requests] == ["json_schema", "tools", "text"]

    fake_api.queue(JSON_VALIDATE_FAILED, lambda request: structured(request, GENERATION))
    await llm.generate_sql(MESSAGES)
    assert [request.kind for request in fake_api.requests[3:]] == ["json_schema", "tools"]


async def test_model_output_quoted_in_a_failed_generation_is_not_read_as_a_missing_model(
    fake_api: FakeChatAPI,
) -> None:
    # Groq quotes the bad output in `failed_generation`; text like "model ... not found" in there is the
    # model's own words, not a provider message about LLM_MODEL.
    quoted = error(
        400,
        "Generated JSON does not match the expected schema. Please adjust your prompt. See 'failed_generation' "
        "for more details.",
        "json_validate_failed",
        failed_generation='{"status": "cannot_answer", "summary": "The model column was not found in Products"}',
    )
    fake_api.queue(quoted, lambda request: structured(request, GENERATION))

    result = await gateway(llm_fallback_models="").generate_sql(MESSAGES)

    assert result.sql == GENERATION["sql"]
    assert [request.kind for request in fake_api.requests] == ["json_schema", "tools"]


async def test_api_key_pasted_with_surrounding_whitespace_still_works(fake_api: FakeChatAPI) -> None:
    # A key copied with a trailing newline must not turn into an illegal HTTP header (which surfaced as
    # "Could not reach the groq API" while /api/health reported the provider as configured).
    fake_api.queue(lambda request: structured(request, INTENT))

    decision = await gateway(groq_api_key=f"  {DUMMY_KEY}\n").classify_intent(MESSAGES)

    assert decision.intent == "generate_sql"
    assert fake_api.requests[0].headers["authorization"] == f"Bearer {DUMMY_KEY}"


def test_groq_requests_never_carry_ollama_options() -> None:
    model = gateway()._build_chat_model(PRIMARY, 512)
    params = model._default_params

    assert params["model"] == PRIMARY and params["max_tokens"] == 512
    assert params["reasoning_effort"] == "low" and params["reasoning_format"] is None
    assert not OLLAMA_ONLY_KEYS & set(params) and not params.get("model_kwargs")
    assert model.max_retries == 1 and model.request_timeout == 120.0


def test_qwen_models_get_qwen_reasoning_settings() -> None:
    off = gateway()._build_chat_model(FALLBACK, 512)
    on = gateway(llm_reasoning=True)._build_chat_model(FALLBACK, 512)

    assert (off.reasoning_effort, off.reasoning_format) == ("none", None)
    assert (on.reasoning_effort, on.reasoning_format) == (None, "parsed")
