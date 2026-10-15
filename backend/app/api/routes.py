"""HTTP routes under ``/api`` (contract: docs/API.md)."""

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from fastapi.exceptions import RequestValidationError

from app import __version__
from app.agent.llm import llm_health
from app.agent.service import AgentRunError, ChatInput, SQLAgent
from app.api.errors import APIError, agent_error_status
from app.api.models import (
    THREAD_ID_MAX_CHARS,
    THREAD_ID_PATTERN,
    ChatRequest,
    DatabaseStatus,
    ErrorResponse,
    GraphResponse,
    HealthResponse,
    LLMStatus,
    ThreadResetResponse,
)
from app.api.rate_limit import enforce_rate_limit
from app.api.streaming import AgentEventStream, sse_messages
from app.config import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["api"])

_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    status: {"model": ErrorResponse} for status in (413, 422, 429, 500, 502, 503)
}
_SSE_RESPONSE: dict[int | str, dict[str, Any]] = {
    200: {
        "content": {"text/event-stream": {}},
        "description": "Events: start, step and token (repeated), then final or error, and always done.",
    }
}


def get_agent(request: Request) -> SQLAgent:
    """Dependency: the process-wide agent created at startup."""
    return request.app.state.agent


def get_app_settings(request: Request) -> Settings:
    """Dependency: the settings the app was created with."""
    return request.app.state.settings


AgentDep = Annotated[SQLAgent, Depends(get_agent)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


async def chat_input(body: ChatRequest, settings: SettingsDep) -> ChatInput:
    """Dependency: check the body against runtime limits and convert it to the agent's input."""
    if len(body.message) > settings.max_input_chars:
        raise RequestValidationError(
            [
                {
                    "type": "string_too_long",
                    "loc": ("body", "message"),
                    "msg": f"must be at most {settings.max_input_chars} characters (got {len(body.message)})",
                }
            ]
        )
    history = [item.model_dump() for item in body.history] if body.history else None
    return ChatInput(
        message=body.message,
        thread_id=body.thread_id,
        dialect=body.dialect,
        execute=body.execute,
        history=history,
    )


ChatInputDep = Annotated[ChatInput, Depends(chat_input)]


async def _llm_status(settings: Settings) -> dict[str, Any]:
    """Probe the LLM provider; a failing probe reports "not configured" instead of failing the health check."""
    try:
        return await llm_health(settings)
    except Exception:
        logger.exception("LLM health probe failed")
        return {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "fast_model": settings.fast_model,
            "fallback_models": settings.fallback_models,
            "configured": False,
            "mock": settings.llm_provider == "mock",
        }


def _warmup_status(request: Request) -> str:
    """State of the startup warm-up task: warming, ready, skipped (not applicable) or failed."""
    task = getattr(request.app.state, "warmup_task", None)
    if task is None:
        return "skipped"
    if not task.done():
        return "warming"
    if task.cancelled() or task.exception() is not None:
        return "failed"
    return "ready" if task.result() else "skipped"


@router.head("/health", include_in_schema=False)
@router.get("/health", response_model=HealthResponse)
async def health(request: Request, settings: SettingsDep, agent: AgentDep) -> HealthResponse:
    """Liveness, LLM configuration and database summary (drives the UI's status banners)."""
    llm = {**await _llm_status(settings), "warmup": _warmup_status(request)}
    return HealthResponse(
        status="ok",
        version=__version__,
        llm=LLMStatus.model_validate(llm),
        database=DatabaseStatus(dialect="sqlite", tables=len(agent.catalog.to_dict()["tables"])),
    )


@router.get("/schema")
async def schema(agent: AgentDep) -> dict[str, Any]:
    """The database schema for the schema explorer: tables, columns, indexes and relationships."""
    return agent.catalog.to_dict()


@router.get("/graph", response_model=GraphResponse)
async def graph(agent: AgentDep) -> GraphResponse:
    """Mermaid source and node names of the compiled LangGraph workflow."""
    return GraphResponse(mermaid=agent.mermaid(), nodes=agent.node_names())


@router.post("/chat", dependencies=[Depends(enforce_rate_limit)], responses=_ERROR_RESPONSES)
async def chat(payload: ChatInputDep, agent: AgentDep) -> dict[str, Any]:
    """Answer one message and return the complete ``AgentResponse`` (non-streaming)."""
    try:
        response = await agent.run(payload)
    except AgentRunError as exc:
        logger.warning("Chat failed with %s: %s", exc.code, exc.message)
        raise APIError(agent_error_status(exc.code), exc.code, exc.message) from exc
    logger.info("Chat answered: intent=%s status=%s", response.get("intent"), response.get("status"))
    return response


@router.post(
    "/chat/stream",
    dependencies=[Depends(enforce_rate_limit)],
    responses={**_ERROR_RESPONSES, **_SSE_RESPONSE},
)
async def chat_stream(payload: ChatInputDep, agent: AgentDep) -> AgentEventStream:
    """Answer one message as Server-Sent Events (the primary endpoint used by the UI)."""
    return AgentEventStream(sse_messages(agent, payload))


@router.delete("/threads/{thread_id}", response_model=ThreadResetResponse)
async def reset_thread(
    thread_id: Annotated[str, Path(max_length=THREAD_ID_MAX_CHARS, pattern=THREAD_ID_PATTERN)],
    agent: AgentDep,
) -> ThreadResetResponse:
    """Forget the server-side conversation memory of a thread."""
    await agent.reset_thread(thread_id)
    return ThreadResetResponse(ok=True)
