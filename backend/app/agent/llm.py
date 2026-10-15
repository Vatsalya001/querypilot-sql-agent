"""LLM gateway: the only way the agent talks to a language model.

The graph depends on the small :class:`LLMGateway` protocol, so tests and keyless demos can inject
:class:`~app.agent.mock_llm.MockGateway` (or any fake) instead of a real provider.

:class:`LangChainGateway` adapts LangChain chat models for Ollama (local default), Groq, Google and
any OpenAI-compatible server. Structured output is robust to small local models:

1. a native structured call -- tool calling for Ollama (its MLX runner rejects JSON-schema output),
   JSON schema first and tool calling second for hosted providers;
2. if the model answers without a valid tool call, the JSON object is salvaged from its text;
3. otherwise a plain completion with explicit JSON instructions is parsed (``<think>`` blocks and
   code fences stripped) and validated with Pydantic, with ONE corrective retry that quotes the
   validation error.

Provider exceptions are mapped onto :class:`LLMError` codes the API understands.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable, Sequence
from typing import Any, Literal, Protocol, TypeVar, runtime_checkable

import httpx
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import Runnable
from pydantic import BaseModel, ValidationError

from app.agent.parsing import ThinkStreamFilter, extract_json_object, json_instructions, strip_think
from app.agent.schemas import IntentDecision, SQLGeneration
from app.config import Settings

logger = logging.getLogger(__name__)

LLMErrorCode = Literal["llm_not_configured", "rate_limited", "llm_error"]
TokenCallback = Callable[[str], None]
ModelFactory = Callable[[str, int], BaseChatModel]
SchemaT = TypeVar("SchemaT", bound=BaseModel)

INTENT_MAX_TOKENS = 320
"""Output cap for intent classification on local models (a decision is ~60-120 tokens)."""
TEXT_MAX_TOKENS = 600
"""Output cap for explanations and prose answers on local models."""
WARM_UP_PROBES = ("1", "2")
"""Two user messages that differ in their first token (see :meth:`LangChainGateway.warm_up`)."""

OLLAMA_DOWN_MESSAGE = (
    "The local model server (Ollama) is not running. Start it with 'ollama serve' or "
    "'brew services start ollama'."
)
RATE_LIMITED_MESSAGE = "The language model is rate-limited right now. Please wait a moment and try again."
TIMEOUT_MESSAGE = "The language model took too long to respond. Please try again."
GENERIC_LLM_MESSAGE = "The language model request failed. Please try again."
INVALID_OUTPUT_MESSAGE = (
    "The language model returned a response I couldn't understand. Please try again or rephrase your request."
)
_API_KEY_ENV = {"groq": "GROQ_API_KEY", "google": "GOOGLE_API_KEY", "openai": "OPENAI_API_KEY"}


class LLMError(Exception):
    """A model call failed in a way the user should hear about.

    ``code`` is one of ``llm_not_configured``, ``rate_limited`` or ``llm_error``; ``user_message`` is
    safe to show in the UI (it never contains secrets or stack traces).
    """

    def __init__(self, code: LLMErrorCode, user_message: str, *, detail: str = "") -> None:
        super().__init__(detail or user_message)
        self.code: LLMErrorCode = code
        self.user_message = user_message


@runtime_checkable
class LLMGateway(Protocol):
    """What the graph needs from a language model."""

    provider: str
    model: str
    fast_model: str
    mock: bool

    async def classify_intent(self, messages: list[BaseMessage]) -> IntentDecision:
        """Classify the user's message (fast model)."""
        ...

    async def generate_sql(self, messages: list[BaseMessage]) -> SQLGeneration:
        """Write, modify, optimize or debug SQL (main model)."""
        ...

    async def stream_text(self, messages: list[BaseMessage], *, on_token: TokenCallback | None = None) -> str:
        """Stream prose (fast model); ``on_token`` receives every chunk. Returns the full text."""
        ...


# --------------------------------------------------------------------------------------------------
# Error mapping
# --------------------------------------------------------------------------------------------------


def _status_code(exc: BaseException) -> int | None:
    """HTTP status attached to a provider exception, if any."""
    for candidate in (getattr(exc, "status_code", None), getattr(getattr(exc, "response", None), "status_code", None)):
        if isinstance(candidate, int):
            return candidate
    return None


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """``exc`` followed by its causes/contexts (bounded, cycle-safe)."""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain and len(chain) < 5:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _is_connection_error(exc: BaseException) -> bool:
    for item in _exception_chain(exc):
        name = type(item).__name__
        text = str(item).lower()
        if isinstance(item, ConnectionError | httpx.ConnectError) or name in {"APIConnectionError", "ConnectError"}:
            return True
        if "connection refused" in text or "failed to connect" in text:
            return True
    return False


def _is_timeout(exc: BaseException) -> bool:
    return any(
        isinstance(item, TimeoutError | httpx.TimeoutException) or "timeout" in type(item).__name__.lower()
        for item in _exception_chain(exc)
    )


def _is_failed_generation(text: str) -> bool:
    """The model produced an invalid structured reply (Groq: HTTP 400 ``json_validate_failed`` or
    ``tool_use_failed``, with the bad output in ``failed_generation``). A one-off model mistake: the request
    itself is fine, and the quoted model output must not be mistaken for the provider's own message."""
    return any(marker in text for marker in ("failed_generation", "json_validate_failed", "tool_use_failed"))


def _is_missing_model(text: str) -> bool:
    """The provider says the model does not exist or was retired (Groq answers a decommissioned model with
    HTTP 400 ``model_decommissioned``, not 404)."""
    return "model" in text and any(marker in text for marker in ("not found", "does not exist", "decommissioned"))


def map_provider_error(exc: BaseException, *, provider: str, model: str) -> LLMError:
    """Translate any provider/client exception into an :class:`LLMError` with a user-safe message."""
    if isinstance(exc, LLMError):
        return exc
    detail = f"{type(exc).__name__}: {exc}"[:500]
    text = str(exc).lower()
    name = type(exc).__name__.lower()
    status = _status_code(exc)
    if _is_connection_error(exc):
        if provider == "ollama":
            return LLMError("llm_not_configured", OLLAMA_DOWN_MESSAGE, detail=detail)
        return LLMError("llm_error", f"Could not reach the {provider} API. Please try again.", detail=detail)
    if _is_timeout(exc):
        return LLMError("llm_error", TIMEOUT_MESSAGE, detail=detail)
    if status == 429 or "rate limit" in text or "ratelimit" in name or "resourceexhausted" in name:
        return LLMError("rate_limited", RATE_LIMITED_MESSAGE, detail=detail)
    if _is_failed_generation(text):
        return LLMError("llm_error", GENERIC_LLM_MESSAGE, detail=detail)
    if status == 404 or _is_missing_model(text):
        match = re.search(r"model [`'\"]([^`'\"]+)[`'\"]", str(exc))
        missing = match.group(1) if match else model
        if provider == "ollama":
            message = f"Model '{missing}' is not available. Run 'ollama pull {missing}'."
        else:
            message = (
                f"Model '{missing}' is not available from {provider} (it may have been retired). "
                "Check LLM_MODEL, LLM_FAST_MODEL and LLM_FALLBACK_MODELS."
            )
        return LLMError("llm_not_configured", message, detail=detail)
    if status in (401, 403) or "authentication" in name or "permissiondenied" in name or "api key" in text:
        env = _API_KEY_ENV.get(provider, "the API key")
        return LLMError("llm_not_configured", f"The {provider} API key is missing or invalid (set {env}).", detail=detail)
    return LLMError("llm_error", GENERIC_LLM_MESSAGE, detail=detail)


def _is_unsupported_request(exc: BaseException) -> bool:
    """True when the provider rejected the *request shape* (e.g. an unsupported response format).

    Such a method is skipped for the rest of the process. A failed generation is also an HTTP 400 on Groq,
    but the next request may well succeed, so it never disables the method.
    """
    text = str(exc).lower()
    if _is_failed_generation(text):
        return False
    return (
        isinstance(exc, NotImplementedError)
        or _status_code(exc) in (400, 422)
        or "structured output is unavailable" in text
        or "does not support" in text
    )


# --------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------


def message_text(message: Any) -> str:
    """Plain text of a chat message or chunk (handles providers that return content blocks)."""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") in (None, "text"):
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return "" if content is None else str(content)


def parse_structured_text(schema: type[SchemaT], text: str) -> tuple[SchemaT | None, str]:
    """Extract and validate a JSON object from model text. Returns ``(model, "")`` or ``(None, error)``."""
    data = extract_json_object(text)
    if data is None:
        return None, "no JSON object found in the reply"
    try:
        return schema.model_validate(data), ""
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or 'object'}: {err['msg']}" for err in exc.errors()[:5]
        )
        return None, problems


def salvage_structured(schema: type[SchemaT], raw: Any) -> SchemaT | None:
    """Recover ``schema`` from a reply the structured parser rejected.

    Small models sometimes call the tool under another name (``intent_decision``) or answer with the
    JSON object as plain text instead of a tool call; both are still usable.
    """
    if raw is None:
        return None
    for call in getattr(raw, "tool_calls", None) or []:
        try:
            return schema.model_validate(call.get("args") or {})
        except ValidationError:
            continue
    parsed, _ = parse_structured_text(schema, message_text(raw))
    return parsed


# --------------------------------------------------------------------------------------------------
# LangChain-backed gateway
# --------------------------------------------------------------------------------------------------


class LangChainGateway:
    """Gateway backed by LangChain chat models (Ollama, Groq, Google or OpenAI-compatible)."""

    mock = False

    def __init__(self, settings: Settings, *, model_factory: ModelFactory | None = None) -> None:
        self.settings = settings
        self.provider: str = settings.llm_provider
        self.model: str = settings.llm_model
        self.fast_model: str = settings.fast_model
        self._factory: ModelFactory = model_factory or self._build_chat_model
        self._models: dict[tuple[str, int], BaseChatModel] = {}
        self._unsupported: set[tuple[str, str]] = set()

    # ---- public API ----------------------------------------------------------------------------

    async def classify_intent(self, messages: list[BaseMessage]) -> IntentDecision:
        """Classify the user's message with the fast model."""
        return await self._structured(IntentDecision, messages, fast=True, max_tokens=self._cap(INTENT_MAX_TOKENS))

    async def generate_sql(self, messages: list[BaseMessage]) -> SQLGeneration:
        """Produce SQL with the main model."""
        return await self._structured(
            SQLGeneration, messages, fast=False, max_tokens=self.settings.llm_max_output_tokens
        )

    async def stream_text(self, messages: list[BaseMessage], *, on_token: TokenCallback | None = None) -> str:
        """Stream prose from the fast model, hiding ``<think>`` blocks; returns the full cleaned text."""
        self._ensure_configured()
        runnable = self._runnable(fast=True, max_tokens=self._cap(TEXT_MAX_TOKENS))
        think_filter = ThinkStreamFilter()
        parts: list[str] = []

        def emit(text: str) -> None:
            if text:
                parts.append(text)
                if on_token is not None:
                    on_token(text)

        async def consume() -> None:
            async for chunk in runnable.astream(messages):
                emit(think_filter.feed(message_text(chunk)))
            emit(think_filter.flush())

        try:
            await asyncio.wait_for(consume(), timeout=self.settings.llm_timeout_s)
        except Exception as exc:
            raise map_provider_error(exc, provider=self.provider, model=self.fast_model) from exc
        return strip_think("".join(parts))

    async def warm_up(self, *, intent_system: str, sql_system: str, text_systems: Sequence[str] = ()) -> bool:
        """Prime a local Ollama server's prompt cache with each role's static system prompt.

        Ollama's runner for hybrid models such as qwen3.5 can only resume from saved cache checkpoints
        and saves one where two requests diverge. Two one-token requests per role whose user messages
        differ in their first token therefore leave a checkpoint exactly at the end of the static
        prefix (tools + system prompt), and every later request skips re-reading it (measured: the
        2,700-token SQL prompt drops from ~15 s to ~0.5 s of prompt processing). Requests are sent
        exactly like real calls (same tools, context size and thinking mode). Returns False when the
        provider is not Ollama or the server is unavailable; never raises.
        """
        if self.provider != "ollama":
            return False
        jobs: list[tuple[str, type[BaseModel] | None, bool]] = [
            (intent_system, IntentDecision, True),
            (sql_system, SQLGeneration, False),
            *((system, None, True) for system in text_systems),
        ]
        started = time.perf_counter()
        for system, schema, fast in jobs:
            name = self.fast_model if fast else self.model
            try:
                model = self._chat_model(name, 1)
                runnable = model.bind_tools([schema]) if schema is not None else model
                for probe in WARM_UP_PROBES:
                    await self._invoke(runnable, [SystemMessage(content=system), HumanMessage(content=probe)])
            except Exception as exc:
                error = map_provider_error(exc, provider=self.provider, model=name)
                logger.warning("LLM warm-up skipped: %s", error.user_message)
                return False
        logger.info("LLM prompt cache warmed for %d roles in %.1f s", len(jobs), time.perf_counter() - started)
        return True

    # ---- structured output ---------------------------------------------------------------------

    def _structured_methods(self) -> tuple[str, ...]:
        if self.provider == "ollama":
            return ("function_calling",)
        return ("json_schema", "function_calling")

    async def _structured(
        self, schema: type[SchemaT], messages: list[BaseMessage], *, fast: bool, max_tokens: int
    ) -> SchemaT:
        self._ensure_configured()
        model_name = self.fast_model if fast else self.model
        problem = "the model returned no structured output"
        for method in self._structured_methods():
            if (model_name, method) in self._unsupported:
                continue
            try:  # building the runnable can already fail (e.g. a model without tool calling)
                runnable = self._runnable(
                    fast=fast,
                    max_tokens=max_tokens,
                    transform=lambda llm, m=method: llm.with_structured_output(schema, method=m, include_raw=True),
                )
                result = await self._invoke(runnable, messages)
            except Exception as exc:
                error = map_provider_error(exc, provider=self.provider, model=model_name)
                if error.code != "llm_error" or _is_timeout(exc):
                    raise error from exc
                if _is_unsupported_request(exc):
                    self._unsupported.add((model_name, method))
                logger.warning("Structured call (%s) on %s failed: %s", method, model_name, error)
                problem = f"{method} call failed"
                continue
            parsed = result.get("parsed") if isinstance(result, dict) else None
            if isinstance(parsed, schema):
                return parsed
            salvaged = salvage_structured(schema, result.get("raw") if isinstance(result, dict) else None)
            if salvaged is not None:
                logger.info("Recovered %s from an imperfect model reply", schema.__name__)
                return salvaged
            parsing_error = result.get("parsing_error") if isinstance(result, dict) else None
            problem = str(parsing_error or "the model did not call the tool")
            logger.warning("Structured output for %s unusable (%s); using JSON fallback", schema.__name__, problem)
            break
        return await self._json_fallback(schema, messages, fast=fast, max_tokens=max_tokens, problem=problem)

    async def _json_fallback(
        self, schema: type[SchemaT], messages: list[BaseMessage], *, fast: bool, max_tokens: int, problem: str
    ) -> SchemaT:
        """Plain completion parsed as JSON, with one corrective retry that quotes the validation error."""
        model_name = self.fast_model if fast else self.model
        conversation: list[BaseMessage] = [*messages, HumanMessage(content=json_instructions(schema))]
        text = await self._complete(conversation, fast=fast, max_tokens=max_tokens)
        parsed, error = parse_structured_text(schema, text)
        if parsed is not None:
            return parsed
        logger.warning("JSON fallback for %s invalid (%s); retrying once", schema.__name__, error)
        conversation += [
            AIMessage(content=text[:4000]),
            HumanMessage(
                content=f"That reply was not valid ({error}). Reply again with ONLY the corrected JSON object."
            ),
        ]
        text = await self._complete(conversation, fast=fast, max_tokens=max_tokens)
        parsed, error = parse_structured_text(schema, text)
        if parsed is not None:
            return parsed
        raise LLMError(
            "llm_error",
            INVALID_OUTPUT_MESSAGE,
            detail=f"{model_name}: {schema.__name__} unparseable after retry ({problem}; {error})",
        )

    async def _complete(self, messages: list[BaseMessage], *, fast: bool, max_tokens: int) -> str:
        runnable = self._runnable(fast=fast, max_tokens=max_tokens)
        try:
            reply = await self._invoke(runnable, messages)
        except Exception as exc:
            raise map_provider_error(
                exc, provider=self.provider, model=self.fast_model if fast else self.model
            ) from exc
        return strip_think(message_text(reply))

    async def _invoke(self, runnable: Runnable, messages: Sequence[BaseMessage]) -> Any:
        return await asyncio.wait_for(runnable.ainvoke(list(messages)), timeout=self.settings.llm_timeout_s)

    # ---- model construction --------------------------------------------------------------------

    def _cap(self, role_cap: int) -> int:
        """Per-role output cap; only tightened for local models without thinking (latency matters most)."""
        limit = self.settings.llm_max_output_tokens
        if self.provider == "ollama" and not self.settings.llm_reasoning:
            return min(role_cap, limit)
        return limit

    def _runnable(
        self,
        *,
        fast: bool,
        max_tokens: int,
        transform: Callable[[BaseChatModel], Runnable] | None = None,
    ) -> Runnable:
        """Primary model (optionally transformed) with ``.with_fallbacks`` over LLM_FALLBACK_MODELS."""
        primary = self.fast_model if fast else self.model
        names = [primary, *(name for name in self.settings.fallback_models if name != primary)]
        runnables = [
            transform(self._chat_model(name, max_tokens)) if transform else self._chat_model(name, max_tokens)
            for name in names
        ]
        return runnables[0].with_fallbacks(runnables[1:]) if len(runnables) > 1 else runnables[0]

    def _chat_model(self, name: str, max_tokens: int) -> BaseChatModel:
        key = (name, max_tokens)
        if key not in self._models:
            self._models[key] = self._factory(name, max_tokens)
        return self._models[key]

    def _ensure_configured(self) -> None:
        settings = self.settings
        missing = (
            (self.provider == "groq" and not settings.groq_api_key)
            or (self.provider == "google" and not settings.google_api_key)
            or (self.provider == "openai" and not (settings.openai_api_key or settings.openai_base_url))
        )
        if missing:
            env = _API_KEY_ENV[self.provider]
            raise LLMError(
                "llm_not_configured",
                f"The {self.provider} provider needs an API key: set {env}, or use LLM_PROVIDER=ollama "
                "(local) or LLM_PROVIDER=mock (offline demo).",
            )

    def _build_chat_model(self, name: str, max_tokens: int) -> BaseChatModel:
        """Instantiate the LangChain chat model for ``name`` (imports are lazy per provider)."""
        s = self.settings
        if self.provider == "ollama":
            from langchain_ollama import ChatOllama

            return ChatOllama(
                model=name,
                base_url=s.ollama_base_url,
                temperature=s.llm_temperature,
                num_ctx=s.llm_num_ctx,
                num_predict=max_tokens,
                keep_alive=s.ollama_keep_alive,
                reasoning=s.llm_reasoning,
                client_kwargs={"timeout": s.llm_timeout_s},
            )
        if self.provider == "groq":
            from langchain_groq import ChatGroq

            extra: dict[str, Any] = {}
            if "gpt-oss" in name:
                extra["reasoning_effort"] = "medium" if s.llm_reasoning else "low"
            elif "qwen" in name.lower() and s.llm_reasoning:
                extra["reasoning_format"] = "parsed"
            elif "qwen" in name.lower():
                extra["reasoning_effort"] = "none"
            return ChatGroq(
                model=name,
                api_key=s.groq_api_key,
                temperature=s.llm_temperature,
                max_tokens=max_tokens,
                timeout=s.llm_timeout_s,
                max_retries=1,
                **extra,
            )
        if self.provider == "google":
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(
                model=name,
                google_api_key=s.google_api_key,
                temperature=s.llm_temperature,
                max_output_tokens=max_tokens,
                timeout=s.llm_timeout_s,
                max_retries=1,
            )
        if self.provider == "openai":
            from langchain_openai import ChatOpenAI

            return ChatOpenAI(
                model=name,
                api_key=s.openai_api_key or "not-needed",
                base_url=s.openai_base_url or None,
                temperature=s.llm_temperature,
                max_tokens=max_tokens,
                timeout=s.llm_timeout_s,
                max_retries=1,
            )
        raise ValueError(f"Unsupported LLM provider: {self.provider}")


def build_gateway(settings: Settings) -> LLMGateway:
    """Create the gateway selected by ``LLM_PROVIDER``."""
    if settings.llm_provider == "mock":
        from app.agent.mock_llm import MockGateway

        return MockGateway()
    return LangChainGateway(settings)


# --------------------------------------------------------------------------------------------------
# Health
# --------------------------------------------------------------------------------------------------

HEALTH_CACHE_TTL_S = 10.0
_OLLAMA_PROBE_TIMEOUT_S = 1.5
_health_cache: dict[tuple[str, ...], tuple[float, bool]] = {}


async def _ollama_models_ready(settings: Settings, transport: httpx.AsyncBaseTransport | None) -> bool:
    """True when the Ollama server answers and every configured model is pulled (cached 10 s)."""
    wanted = tuple(dict.fromkeys([settings.llm_model, settings.fast_model]))
    key = (settings.ollama_base_url, *wanted)
    cached = _health_cache.get(key)
    now = time.monotonic()
    if cached and cached[0] > now:
        return cached[1]
    ready = False
    try:
        async with httpx.AsyncClient(timeout=_OLLAMA_PROBE_TIMEOUT_S, transport=transport) as client:
            response = await client.get(f"{settings.ollama_base_url.rstrip('/')}/api/tags")
            response.raise_for_status()
            available: set[str] = set()
            for entry in response.json().get("models", []):
                for field_name in ("name", "model"):
                    value = str(entry.get(field_name) or "")
                    if value:
                        available.update({value, value.removesuffix(":latest")})
            ready = all(model in available for model in wanted)
    except (httpx.HTTPError, ValueError) as exc:
        logger.debug("Ollama health probe failed: %s", exc)
    _health_cache[key] = (now + HEALTH_CACHE_TTL_S, ready)
    return ready


async def llm_health(settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    """The ``llm`` object of ``GET /api/health``.

    ``configured`` means: Ollama reachable with the models pulled; a cloud provider with its API key
    set; always true for the mock provider. ``transport`` lets tests stub the Ollama probe.
    """
    provider = settings.llm_provider
    if provider == "mock":
        return {"provider": "mock", "model": "mock", "fast_model": "mock", "fallback_models": [],
                "configured": True, "mock": True}
    if provider == "ollama":
        configured = await _ollama_models_ready(settings, transport)
    elif provider == "groq":
        configured = bool(settings.groq_api_key)
    elif provider == "google":
        configured = bool(settings.google_api_key)
    else:
        configured = bool(settings.openai_api_key or settings.openai_base_url)
    return {
        "provider": provider,
        "model": settings.llm_model,
        "fast_model": settings.fast_model,
        "fallback_models": settings.fallback_models,
        "configured": configured,
        "mock": False,
    }
