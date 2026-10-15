"""Test doubles and helpers for the agent tests (never call a real model or the network).

* :class:`FakeGateway` - scripted ``LLMGateway``: queued results per role, delegating to the rule-based
  ``MockGateway`` when a queue is empty, and recording every prompt it receives.
* :class:`ScriptedChatModel` - a LangChain chat model with queued replies, used to test the real
  ``LangChainGateway`` (structured output, salvage, JSON fallback, streaming, error mapping).
* :func:`run_turn` / :class:`TurnResult` - run one chat turn through ``SQLAgent.stream`` and inspect it.
"""

from __future__ import annotations

from collections import deque
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from app.agent.llm import TokenCallback
from app.agent.mock_llm import MockGateway
from app.agent.schemas import IntentDecision, SQLGeneration

if TYPE_CHECKING:  # the service pulls in the whole graph; gateway-only tests do not need it
    from app.agent.service import SQLAgent

AGENT_RESPONSE_KEYS = frozenset(
    {
        "thread_id", "message_id", "intent", "status", "message", "sql", "sql_sqlite", "dialect", "explanation",
        "validation", "optimization", "execution", "cost", "issues", "changes", "suggestions", "assumptions",
        "clarification_question", "schema_tables", "attempts", "steps", "model",
    }
)
"""Keys of ``AgentResponse`` in docs/API.md."""

NODE_NAMES = [
    "guard_input", "detect_intent", "validate_scope", "retrieve_schema", "diagnose_sql", "generate_sql",
    "validate_sql", "optimize_sql", "execute_sql", "explain_sql", "answer_question", "ask_clarification",
    "reject_request", "refuse_destructive", "handle_failure", "respond",
]
"""Node names in docs/API.md."""

OUT_OF_SCOPE_TEXT = (
    "I'm designed to assist only with SQL and database-related tasks. "
    "Please ask a question related to the provided database schema."
)
"""The exact refusal text required by the brief (section 3.7)."""


def intent(name: str, confidence: float = 0.95, **extra: Any) -> IntentDecision:
    """Shorthand for a scripted intent decision."""
    return IntentDecision.model_validate({"intent": name, "confidence": confidence, "reasoning": "scripted", **extra})


def generation(sql: str | None, status: str = "ok", **extra: Any) -> SQLGeneration:
    """Shorthand for a scripted SQL generation."""
    return SQLGeneration.model_validate({"status": status, "sql": sql, "summary": extra.pop("summary", ""), **extra})


class FakeGateway:
    """Scripted ``LLMGateway``. Queue an ``Exception`` instance to make that call raise it."""

    provider = "fake"
    model = "fake-main"
    fast_model = "fake-fast"
    mock = False

    def __init__(
        self,
        *,
        intents: Iterable[IntentDecision | BaseException] = (),
        generations: Iterable[SQLGeneration | BaseException] = (),
        texts: Iterable[str | BaseException] = (),
    ) -> None:
        self.intents: deque[Any] = deque(intents)
        self.generations: deque[Any] = deque(generations)
        self.texts: deque[Any] = deque(texts)
        self.calls: list[tuple[str, list[BaseMessage]]] = []
        self._mock = MockGateway()

    def prompts(self, role: str) -> list[list[BaseMessage]]:
        """Message lists received for ``role`` (``intent``, ``sql`` or ``text``), in call order."""
        return [messages for name, messages in self.calls if name == role]

    async def classify_intent(self, messages: list[BaseMessage]) -> IntentDecision:
        self.calls.append(("intent", list(messages)))
        if self.intents:
            return _unwrap(self.intents.popleft())
        return await self._mock.classify_intent(messages)

    async def generate_sql(self, messages: list[BaseMessage]) -> SQLGeneration:
        self.calls.append(("sql", list(messages)))
        if self.generations:
            return _unwrap(self.generations.popleft())
        return await self._mock.generate_sql(messages)

    async def stream_text(self, messages: list[BaseMessage], *, on_token: TokenCallback | None = None) -> str:
        self.calls.append(("text", list(messages)))
        if not self.texts:
            return await self._mock.stream_text(messages, on_token=on_token)
        text = _unwrap(self.texts.popleft())
        for word in text.split(" "):
            if on_token is not None:
                on_token(word + " ")
        return text


def _unwrap(item: Any) -> Any:
    if isinstance(item, BaseException):
        raise item
    return item


class ScriptedChatModel(BaseChatModel):
    """Chat model that returns queued replies and records every input.

    A reply can be an ``AIMessage``, a string (plain text), a list of strings (streamed as chunks) or an
    exception instance (raised). ``tools=False`` makes ``with_structured_output`` unsupported.
    """

    replies: list[Any] = Field(default_factory=list)
    seen: list[list[BaseMessage]] = Field(default_factory=list)
    tools: bool = True

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, *, tool_choice: Any = None, **kwargs: Any) -> Any:
        if not self.tools:
            raise NotImplementedError("tool calling is not supported by this model")
        return self

    def _next(self, messages: list[BaseMessage]) -> Any:
        self.seen.append(list(messages))
        if not self.replies:
            raise AssertionError("ScriptedChatModel ran out of replies")
        return _unwrap(self.replies.pop(0))

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        reply = self._next(messages)
        if isinstance(reply, list):
            reply = "".join(reply)
        message = reply if isinstance(reply, AIMessage) else AIMessage(content=str(reply))
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        reply = self._next(messages)
        for part in reply if isinstance(reply, list) else [str(reply)]:
            yield ChatGenerationChunk(message=AIMessageChunk(content=part))

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._stream(messages, stop=stop, **kwargs):
            yield chunk


@dataclass
class TurnResult:
    """Every ``(event, data)`` pair of one streamed turn."""

    events: list[tuple[str, dict[str, Any]]]

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.events]

    @property
    def final(self) -> dict[str, Any]:
        finals = [data for name, data in self.events if name == "final"]
        assert len(finals) == 1, f"expected exactly one final event, got {self.names}"
        return finals[0]

    @property
    def error(self) -> dict[str, Any] | None:
        return next((data for name, data in self.events if name == "error"), None)

    def nodes(self, status: str = "completed") -> list[str]:
        """Node names of the ``step`` events with the given status, in order."""
        return [data["node"] for name, data in self.events if name == "step" and data["status"] == status]

    def tokens(self, target: str | None = None) -> str:
        return "".join(
            data["text"] for name, data in self.events if name == "token" and target in (None, data["target"])
        )


async def run_turn(agent: SQLAgent, message: str, **options: Any) -> TurnResult:
    """Stream one turn through the agent and collect its events."""
    from app.agent.service import ChatInput

    return TurnResult([event async for event in agent.stream(ChatInput(message=message, **options))])
