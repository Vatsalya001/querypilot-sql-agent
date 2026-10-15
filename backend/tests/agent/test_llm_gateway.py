"""LangChainGateway with scripted chat models: structured output, salvage, JSON fallback, streaming, errors."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from ollama import ResponseError

from app.agent import llm as llm_module
from app.agent.llm import (
    INVALID_OUTPUT_MESSAGE,
    OLLAMA_DOWN_MESSAGE,
    LangChainGateway,
    LLMError,
    build_gateway,
    llm_health,
    map_provider_error,
)
from app.agent.mock_llm import MockGateway
from app.agent.schemas import IntentDecision, SQLGeneration
from tests.agent.conftest import make_settings
from tests.agent.helpers import ScriptedChatModel

MESSAGES = [SystemMessage(content="You classify messages."), HumanMessage(content="<user_message>\nhi\n</user_message>")]
INTENT_ARGS = {"intent": "sql_help", "confidence": 0.9, "reasoning": "Greeting."}


class HTTPStatusError(Exception):
    """Mimics provider SDK errors that carry an HTTP status code."""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def gateway_with(
    models: dict[str, ScriptedChatModel], **overrides: Any
) -> tuple[LangChainGateway, list[tuple[str, int]]]:
    """A gateway whose chat models come from ``models`` (by model name); also returns the factory calls."""
    built: list[tuple[str, int]] = []

    def factory(name: str, max_tokens: int) -> ScriptedChatModel:
        built.append((name, max_tokens))
        return models[name]

    settings = make_settings(llm_provider="ollama", llm_model="main", llm_fast_model="fast", **overrides)
    return LangChainGateway(settings, model_factory=factory), built


def tool_call(name: str, args: dict[str, Any]) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": "call-1"}])


# ---- structured output ------------------------------------------------------------------------


async def test_tool_call_is_parsed_with_the_fast_model_and_intent_cap() -> None:
    fast = ScriptedChatModel(replies=[tool_call("IntentDecision", INTENT_ARGS)])
    gateway, built = gateway_with({"fast": fast})

    decision = await gateway.classify_intent(MESSAGES)

    assert decision.intent == "sql_help" and decision.confidence == 0.9
    assert built == [("fast", llm_module.INTENT_MAX_TOKENS)]
    assert fast.seen[0][0].content == "You classify messages."


async def test_generate_sql_uses_the_main_model_and_output_cap() -> None:
    main = ScriptedChatModel(replies=[tool_call("SQLGeneration", {"status": "ok", "sql": "SELECT 1", "summary": "x"})])
    gateway, built = gateway_with({"main": main}, llm_max_output_tokens=700)

    result = await gateway.generate_sql(MESSAGES)

    assert result.sql == "SELECT 1"
    assert built == [("main", 700)]


async def test_tool_call_under_another_name_is_salvaged() -> None:
    fast = ScriptedChatModel(replies=[tool_call("intent_decision", INTENT_ARGS)])
    gateway, _ = gateway_with({"fast": fast})

    assert (await gateway.classify_intent(MESSAGES)).intent == "sql_help"
    assert len(fast.seen) == 1  # no extra call needed


async def test_json_text_instead_of_a_tool_call_is_salvaged() -> None:
    reply = "<think>hmm</think>\n```json\n" + json.dumps(INTENT_ARGS) + "\n```"
    fast = ScriptedChatModel(replies=[AIMessage(content=reply)])
    gateway, _ = gateway_with({"fast": fast})

    assert (await gateway.classify_intent(MESSAGES)).intent == "sql_help"


async def test_unusable_reply_falls_back_to_json_with_one_corrective_retry() -> None:
    fast = ScriptedChatModel(
        replies=[
            AIMessage(content="I think this is a greeting."),  # structured call: no tool call, no JSON
            "Sure! The intent is sql_help.",  # JSON fallback: still no JSON
            json.dumps(INTENT_ARGS),  # corrective retry: valid
        ]
    )
    gateway, _ = gateway_with({"fast": fast})

    decision = await gateway.classify_intent(MESSAGES)

    assert decision.intent == "sql_help"
    fallback_prompt, retry_prompt = fast.seen[1], fast.seen[2]
    assert "Reply with ONLY one JSON object" in str(fallback_prompt[-1].content)
    assert "That reply was not valid" in str(retry_prompt[-1].content)
    assert retry_prompt[-2].content == "Sure! The intent is sql_help."


async def test_unsupported_tool_calling_goes_straight_to_the_json_fallback() -> None:
    fast = ScriptedChatModel(tools=False, replies=[json.dumps(INTENT_ARGS)])
    gateway, _ = gateway_with({"fast": fast})

    assert (await gateway.classify_intent(MESSAGES)).intent == "sql_help"
    assert len(fast.seen) == 1


async def test_output_still_invalid_after_the_retry_raises_llm_error() -> None:
    fast = ScriptedChatModel(replies=[AIMessage(content="no"), "still no", '{"intent": "dance"}'])
    gateway, _ = gateway_with({"fast": fast})

    with pytest.raises(LLMError) as caught:
        await gateway.classify_intent(MESSAGES)

    assert caught.value.code == "llm_error"
    assert caught.value.user_message == INVALID_OUTPUT_MESSAGE


async def test_fallback_model_answers_when_the_primary_is_rate_limited() -> None:
    primary = ScriptedChatModel(replies=[HTTPStatusError("Too Many Requests", 429)])
    backup = ScriptedChatModel(replies=[tool_call("SQLGeneration", {"status": "ok", "sql": "SELECT 2", "summary": ""})])
    gateway, built = gateway_with({"main": primary, "backup": backup}, llm_fallback_models="backup")

    result = await gateway.generate_sql(MESSAGES)

    assert result.sql == "SELECT 2"
    assert [name for name, _ in built] == ["main", "backup"]


async def test_rate_limit_without_fallback_is_reported_as_rate_limited() -> None:
    main = ScriptedChatModel(replies=[HTTPStatusError("Too Many Requests", 429)])
    gateway, _ = gateway_with({"main": main})

    with pytest.raises(LLMError) as caught:
        await gateway.generate_sql(MESSAGES)

    assert caught.value.code == "rate_limited"


async def test_ollama_not_running_is_reported_as_not_configured() -> None:
    fast = ScriptedChatModel(replies=[httpx.ConnectError("[Errno 61] Connection refused")])
    gateway, _ = gateway_with({"fast": fast})

    with pytest.raises(LLMError) as caught:
        await gateway.classify_intent(MESSAGES)

    assert caught.value.code == "llm_not_configured"
    assert caught.value.user_message == OLLAMA_DOWN_MESSAGE


async def test_missing_cloud_api_key_is_reported_before_any_call() -> None:
    gateway = LangChainGateway(make_settings(llm_provider="groq", groq_api_key=""))

    with pytest.raises(LLMError) as caught:
        await gateway.generate_sql(MESSAGES)

    assert caught.value.code == "llm_not_configured"
    assert "GROQ_API_KEY" in caught.value.user_message


# ---- streaming ----------------------------------------------------------------------------------


async def test_stream_text_hides_think_blocks_split_across_chunks() -> None:
    fast = ScriptedChatModel(replies=[["<thi", "nk>secret plan</th", "ink>This query ", "returns rows."]])
    gateway, built = gateway_with({"fast": fast})
    tokens: list[str] = []

    text = await gateway.stream_text(MESSAGES, on_token=tokens.append)

    assert text == "This query returns rows."
    assert "".join(tokens) == "This query returns rows."
    assert "secret" not in "".join(tokens)
    assert built == [("fast", llm_module.TEXT_MAX_TOKENS)]


async def test_stream_text_maps_provider_errors() -> None:
    fast = ScriptedChatModel(replies=[ResponseError("model 'fast' not found", 404)])
    gateway, _ = gateway_with({"fast": fast})

    with pytest.raises(LLMError) as caught:
        await gateway.stream_text(MESSAGES)

    assert caught.value.code == "llm_not_configured"
    assert caught.value.user_message == "Model 'fast' is not available. Run 'ollama pull fast'."


# ---- error mapping ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "provider", "code", "fragment"),
    [
        (ConnectionRefusedError("Connection refused"), "ollama", "llm_not_configured", "ollama serve"),
        (ConnectionRefusedError("Connection refused"), "groq", "llm_error", "Could not reach the groq API"),
        (HTTPStatusError("rate limit exceeded", 429), "groq", "rate_limited", "rate-limited"),
        (ResponseError("model 'qwen3.5:9b' not found", 404), "ollama", "llm_not_configured", "ollama pull qwen3.5:9b"),
        (HTTPStatusError("Invalid API Key", 401), "groq", "llm_not_configured", "GROQ_API_KEY"),
        (TimeoutError(), "ollama", "llm_error", "took too long"),
        (RuntimeError("boom /srv/secret/path"), "ollama", "llm_error", "request failed"),
    ],
)
def test_map_provider_error(error: Exception, provider: str, code: str, fragment: str) -> None:
    mapped = map_provider_error(error, provider=provider, model="m")

    assert mapped.code == code
    assert fragment in mapped.user_message
    assert "/srv/secret" not in mapped.user_message


# ---- gateway selection and health -------------------------------------------------------------


def test_build_gateway_selects_the_provider() -> None:
    assert isinstance(build_gateway(make_settings(llm_provider="mock")), MockGateway)
    gateway = build_gateway(make_settings(llm_provider="ollama", llm_model="qwen3.5:9b", llm_fast_model=""))
    assert isinstance(gateway, LangChainGateway)
    assert (gateway.provider, gateway.model, gateway.fast_model) == ("ollama", "qwen3.5:9b", "qwen3.5:9b")


def test_ollama_chat_model_settings() -> None:
    settings = make_settings(llm_provider="ollama", llm_model="qwen3.5:9b", llm_num_ctx=8192, ollama_keep_alive="30m")
    model = LangChainGateway(settings)._build_chat_model("qwen3.5:9b", 320)

    assert (model.model, model.num_ctx, model.num_predict, model.keep_alive) == ("qwen3.5:9b", 8192, 320, "30m")
    assert model.reasoning is False and model.temperature == 0.0


def tags_transport(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _clear_health_cache() -> None:
    llm_module._health_cache.clear()


async def test_health_ollama_configured_when_the_model_is_pulled() -> None:
    transport = tags_transport(lambda request: httpx.Response(200, json={"models": [{"name": "qwen3.5:9b"}]}))
    health = await llm_health(make_settings(llm_provider="ollama", llm_model="qwen3.5:9b"), transport=transport)

    assert health == {
        "provider": "ollama", "model": "qwen3.5:9b", "fast_model": "qwen3.5:9b", "fallback_models": [],
        "configured": True, "mock": False,
    }


async def test_health_ollama_not_configured_when_the_model_is_missing_or_server_down() -> None:
    settings = make_settings(llm_provider="ollama", llm_model="qwen3.5:9b")
    missing = tags_transport(lambda request: httpx.Response(200, json={"models": [{"name": "llama3:8b"}]}))
    assert (await llm_health(settings, transport=missing))["configured"] is False

    llm_module._health_cache.clear()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    assert (await llm_health(settings, transport=tags_transport(refuse)))["configured"] is False


async def test_health_cloud_and_mock_providers() -> None:
    groq = make_settings(llm_provider="groq", llm_model="openai/gpt-oss-120b", groq_api_key="", llm_fallback_models="a, b")
    assert (await llm_health(groq))["configured"] is False
    assert (await llm_health(groq))["fallback_models"] == ["a", "b"]
    keyed = make_settings(llm_provider="groq", groq_api_key="test-key-not-real")
    assert (await llm_health(keyed))["configured"] is True
    assert await llm_health(make_settings(llm_provider="mock")) == {
        "provider": "mock", "model": "mock", "fast_model": "mock", "fallback_models": [], "configured": True,
        "mock": True,
    }


def test_schemas_are_exposed_as_tools() -> None:
    from langchain_core.utils.function_calling import convert_to_openai_tool

    for schema in (IntentDecision, SQLGeneration):
        tool = convert_to_openai_tool(schema)
        assert tool["function"]["name"] == schema.__name__
        assert tool["function"]["description"]
        assert all(prop.get("description") for prop in tool["function"]["parameters"]["properties"].values())


# ---- prompt-cache warm-up -----------------------------------------------------------------------


async def test_warm_up_sends_two_one_token_probes_per_role() -> None:
    fast = ScriptedChatModel(replies=["x"] * 6)
    main = ScriptedChatModel(replies=["x"] * 2)
    gateway, built = gateway_with({"fast": fast, "main": main})

    assert await gateway.warm_up(intent_system="I", sql_system="S", text_systems=("E", "A")) is True

    assert [(m[0].content, m[1].content) for m in fast.seen] == [
        ("I", "1"), ("I", "2"), ("E", "1"), ("E", "2"), ("A", "1"), ("A", "2"),
    ]
    assert [(m[0].content, m[1].content) for m in main.seen] == [("S", "1"), ("S", "2")]
    assert set(built) == {("fast", 1), ("main", 1)}


async def test_warm_up_is_skipped_for_cloud_providers_and_never_raises() -> None:
    cloud = LangChainGateway(make_settings(llm_provider="groq", groq_api_key="test-key-not-real"))
    assert await cloud.warm_up(intent_system="I", sql_system="S") is False

    down = ScriptedChatModel(replies=[httpx.ConnectError("Connection refused")])
    gateway, _ = gateway_with({"fast": down, "main": down})
    assert await gateway.warm_up(intent_system="I", sql_system="S") is False
