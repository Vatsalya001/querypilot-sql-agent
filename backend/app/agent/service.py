"""Facade used by the API layer: runs one chat turn through the graph and yields API stream events.

Event sequence (docs/API.md): ``start`` -> ``step`` / ``token`` ... -> ``final`` (or ``error``) -> ``done``.
Conversation memory is an ``InMemorySaver`` checkpointer keyed by ``thread_id``; when the server has
no state for a thread (e.g. after a restart) the client's ``history`` rehydrates it.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from weakref import WeakValueDictionary

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.graph import NODE_NAMES, build_graph
from app.agent.llm import LLMError, LLMGateway, build_gateway
from app.agent.nodes import AgentDeps
from app.agent.prompts import OUT_OF_SCOPE_MESSAGE, PROMPT_INJECTION_MESSAGE, build_system_prompts
from app.agent.state import new_turn_input
from app.config import Settings
from app.db.catalog import SchemaCatalog, load_catalog
from app.db.database import ensure_database
from app.guardrails.input_guard import check_input
from app.sql.dialects import normalize_dialect
from app.sql.validator import is_read_only, strip_sql_fences

logger = logging.getLogger(__name__)

INTERNAL_ERROR_MESSAGE = "Something went wrong while processing your request. Please try again."
MAX_SEED_HISTORY = 20
"""At most this many client-supplied history items are used for rehydration."""
RECURSION_LIMIT = 40
"""Node executions allowed per turn (the longest default path needs 15); :func:`recursion_limit` raises it as needed."""
_REJECTION_MESSAGES = {OUT_OF_SCOPE_MESSAGE, PROMPT_INJECTION_MESSAGE}


@dataclass
class ChatInput:
    """One user turn. ``history`` items look like ``{"role": "user" | "assistant", "content": str, "sql": str | None}``."""

    message: str
    thread_id: str | None = None
    dialect: str = "sqlite"
    execute: bool = True
    history: list[dict[str, Any]] | None = None


class AgentRunError(Exception):
    """Raised by :meth:`SQLAgent.run` when the turn ended with an ``error`` event."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def recursion_limit(max_sql_attempts: int) -> int:
    """Node executions one turn may need: :data:`RECURSION_LIMIT`, raised for large ``MAX_SQL_ATTEMPTS``.

    The longest path runs 5 nodes before generation, 2 per attempt (generate + validate) and 4 after it
    (optimize, execute, explain, respond); a few steps of headroom are added.
    """
    return max(RECURSION_LIMIT, 2 * max_sql_attempts + 12)


def _history_field(item: Any, name: str) -> Any:
    """Read a field from a history item given as a dict or as an object (e.g. a Pydantic model)."""
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _is_injection(text: str) -> bool:
    """True when the input guard flags ``text`` as a prompt-injection attempt (length is not limited here)."""
    return bool(text.strip()) and check_input(text, max_chars=len(text) + 1).category == "prompt_injection"


def history_to_messages(history: Sequence[Any]) -> tuple[list[AnyMessage], str | None]:
    """Convert client-side history into messages plus the latest read-only SQL (for ``last_sql``)."""
    messages: list[AnyMessage] = []
    last_sql: str | None = None
    for item in list(history)[-MAX_SEED_HISTORY:]:
        role = _history_field(item, "role")
        content = str(_history_field(item, "content") or "")
        if content.strip() not in _REJECTION_MESSAGES and _is_injection(content):
            logger.warning("Ignoring a client history item flagged as prompt injection")
            continue
        raw_sql = _history_field(item, "sql")
        sql = strip_sql_fences(str(raw_sql)) if raw_sql else None
        if sql and not is_read_only(sql):
            logger.warning("Ignoring non-read-only SQL in client history")
            sql = None
        if sql and _is_injection(sql):  # e.g. an injection hidden in a string literal of a valid SELECT
            logger.warning("Ignoring client history SQL flagged as prompt injection")
            sql = None
        if role == "user":
            messages.append(HumanMessage(content=content))
        elif role == "assistant":
            status = "rejected" if content.strip() in _REJECTION_MESSAGES else ("success" if sql else None)
            messages.append(
                AIMessage(
                    content=f"{content}\nSQL: {sql}" if sql else content,
                    additional_kwargs={"sql": sql, "intent": None, "status": status},
                )
            )
            last_sql = sql or last_sql
    return messages, last_sql


def _as_event(chunk: Any) -> tuple[str, dict[str, Any]] | None:
    """Turn a custom stream payload into an ``(event, data)`` pair; ignore anything else."""
    if not isinstance(chunk, dict) or chunk.get("type") not in ("step", "token"):
        return None
    return str(chunk["type"]), {key: value for key, value in chunk.items() if key != "type"}


class SQLAgent:
    """The QueryPilot SQL agent: a compiled LangGraph plus its dependencies and conversation memory."""

    def __init__(
        self,
        settings: Settings,
        *,
        gateway: LLMGateway | None = None,
        catalog: SchemaCatalog | None = None,
        checkpointer: Any = None,
        db_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.db_path = Path(ensure_database(db_path or settings.resolved_database_path))
        self.catalog = catalog if catalog is not None else load_catalog(self.db_path)
        self.gateway = gateway if gateway is not None else build_gateway(settings)
        self.checkpointer = checkpointer if checkpointer is not None else InMemorySaver()
        self.prompts = build_system_prompts(self.catalog)
        deps = AgentDeps(
            settings=settings, gateway=self.gateway, catalog=self.catalog, db_path=self.db_path, prompts=self.prompts
        )
        self.graph = build_graph(deps, checkpointer=self.checkpointer)
        self._recursion_limit = recursion_limit(settings.max_sql_attempts)
        self._thread_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def stream(self, req: ChatInput) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        """Run one turn and yield ``(event, data)`` pairs exactly as the SSE endpoint sends them.

        Raises ``ValueError`` (before any event) for an unsupported dialect.
        """
        dialect = normalize_dialect(req.dialect)
        thread_id = (req.thread_id or "").strip() or str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        yield "start", {"thread_id": thread_id, "message_id": message_id}

        config: RunnableConfig = {
            "configurable": {"thread_id": thread_id, "message_id": message_id},
            "recursion_limit": self._recursion_limit,
        }
        steps: list[dict[str, Any]] = []
        lock = self._lock_for(thread_id)
        async with lock:  # one turn at a time per conversation
            try:
                payload = await self._turn_input(req, dialect, config)
                # aclosing: if the consumer goes away (client disconnect), the graph run is closed at once.
                async with aclosing(self.graph.astream(payload, config, stream_mode="custom")) as chunks:
                    async for chunk in chunks:
                        event = _as_event(chunk)
                        if event is None:
                            continue
                        if event[0] == "step" and event[1].get("status") == "completed":
                            steps.append(event[1])
                        yield event
                snapshot = await self.graph.aget_state(config)
                final = (snapshot.values or {}).get("final")
                if not final:
                    raise RuntimeError("The graph finished without producing a response.")
                yield "final", {**final, "thread_id": thread_id, "message_id": message_id, "steps": steps}
            except LLMError as exc:
                logger.warning("LLM error on thread %s (%s): %s", thread_id, exc.code, exc)
                yield "error", {"message": exc.user_message, "code": exc.code}
            except Exception:
                logger.exception("Agent run failed on thread %s", thread_id)
                yield "error", {"message": INTERNAL_ERROR_MESSAGE, "code": "internal"}
        yield "done", {}

    async def run(self, req: ChatInput) -> dict[str, Any]:
        """Run one turn to completion and return the AgentResponse (raises :class:`AgentRunError`)."""
        final: dict[str, Any] | None = None
        async for event, data in self.stream(req):
            if event == "final":
                final = data
            elif event == "error":
                raise AgentRunError(str(data.get("code", "internal")), str(data.get("message", "")))
        if final is None:
            raise AgentRunError("internal", INTERNAL_ERROR_MESSAGE)
        return final

    async def warm_up(self) -> bool:
        """Prime the local model's prompt cache with the static system prompts (Ollama only).

        Meant to run as a background task at startup (``asyncio.create_task(agent.warm_up())``) so that
        the first user requests are as fast as later ones. Disabled with ``LLM_WARMUP=false``.
        """
        warm_up = getattr(self.gateway, "warm_up", None)
        if not self.settings.llm_warmup or warm_up is None:
            return False
        return await warm_up(
            intent_system=self.prompts.intent,
            sql_system=self.prompts.sql,
            text_systems=(self.prompts.explain, self.prompts.answer),
        )

    async def reset_thread(self, thread_id: str) -> None:
        """Forget the server-side memory of a conversation.

        Waits for a turn running on the thread (it holds the thread's lock): deleting mid-turn would be
        undone by the checkpoints the turn writes afterwards.
        """
        async with self._lock_for(thread_id):
            await self.checkpointer.adelete_thread(thread_id)

    def mermaid(self) -> str:
        """Mermaid source of the compiled graph."""
        return self.graph.get_graph().draw_mermaid()

    def node_names(self) -> list[str]:
        """Names of all graph nodes in pipeline order."""
        return list(NODE_NAMES)

    async def _turn_input(self, req: ChatInput, dialect: str, config: RunnableConfig) -> dict[str, Any]:
        """Graph input for this turn, rehydrating memory from ``req.history`` if the thread is unknown."""
        seed_messages: list[AnyMessage] | None = None
        seed_last_sql: str | None = None
        if req.history:
            snapshot = await self.graph.aget_state(config)
            if not (snapshot.values or {}).get("messages"):
                seed_messages, seed_last_sql = history_to_messages(req.history)
        return new_turn_input(
            req.message, dialect=dialect, execute=req.execute, seed_messages=seed_messages, seed_last_sql=seed_last_sql
        )

    def _lock_for(self, thread_id: str) -> asyncio.Lock:
        lock = self._thread_locks.get(thread_id)
        if lock is None:
            lock = asyncio.Lock()
            self._thread_locks[thread_id] = lock
        return lock
