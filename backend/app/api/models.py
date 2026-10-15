"""Request and response models of the HTTP API (field for field with docs/API.md).

``AgentResponse`` stays a plain dict produced by the agent service; its shape is checked in the API tests.
The message length limit depends on runtime settings, so it is enforced in ``app.api.routes``.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_core import PydanticCustomError

from app.sql.dialects import SUPPORTED_DIALECTS, normalize_dialect

THREAD_ID_MAX_CHARS = 128
THREAD_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$"
MAX_HISTORY_ITEMS = 20
MAX_HISTORY_TEXT_CHARS = 10_000


class HistoryItem(BaseModel):
    """One earlier conversation message. Used only to rehydrate a thread the server no longer remembers."""

    role: Literal["user", "assistant"]
    content: str = Field(max_length=MAX_HISTORY_TEXT_CHARS)
    sql: str | None = Field(default=None, max_length=MAX_HISTORY_TEXT_CHARS)


class ChatRequest(BaseModel):
    """Body of ``POST /api/chat`` and ``POST /api/chat/stream``."""

    message: str = Field(description="The user's message; trimmed, 1..MAX_INPUT_CHARS characters.")
    thread_id: str | None = Field(
        default=None,
        max_length=THREAD_ID_MAX_CHARS,
        pattern=THREAD_ID_PATTERN,
        description="Conversation id (the client sends a UUID). Null or blank: the server creates one.",
    )
    dialect: str = Field(
        default="sqlite", description="Output dialect: sqlite (also null), postgres or mysql."
    )
    execute: bool = Field(default=True, description="Run the validated query against the sample database.")
    history: list[HistoryItem] | None = Field(
        default=None,
        max_length=MAX_HISTORY_ITEMS,
        description="Recent turns, used only when the server has no memory of thread_id.",
    )

    @field_validator("message")
    @classmethod
    def _strip_message(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise PydanticCustomError("message_empty", "must not be empty")
        return stripped

    @field_validator("thread_id", mode="before")
    @classmethod
    def _blank_thread_id_to_none(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("dialect", mode="before")
    @classmethod
    def _normalize_dialect(cls, value: Any) -> Any:
        """Map aliases (``postgresql``, ``pg``, ...) to the canonical name; null or blank means sqlite."""
        if value is not None and not isinstance(value, str):
            return value  # the str type check reports it
        try:
            return normalize_dialect(value)
        except ValueError:
            raise PydanticCustomError(
                "dialect_unsupported",
                "unsupported dialect '{dialect}'; use one of: {supported}",
                {"dialect": value[:40], "supported": ", ".join(SUPPORTED_DIALECTS)},
            ) from None


class LLMStatus(BaseModel):
    """The ``llm`` object of the health response (extra provider details pass through)."""

    model_config = ConfigDict(extra="allow")

    provider: str
    model: str
    fast_model: str
    fallback_models: list[str]
    configured: bool
    mock: bool
    warmup: Literal["warming", "ready", "skipped", "failed"] = "skipped"
    """Startup prompt-cache warm-up of the local model (Ollama only; "skipped" for other providers)."""


class DatabaseStatus(BaseModel):
    """The ``database`` object of the health response."""

    dialect: str
    tables: int


class HealthResponse(BaseModel):
    """Body of ``GET /api/health``."""

    status: Literal["ok"]
    version: str
    llm: LLMStatus
    database: DatabaseStatus


class GraphResponse(BaseModel):
    """Body of ``GET /api/graph``."""

    mermaid: str
    nodes: list[str]


class ThreadResetResponse(BaseModel):
    """Body of ``DELETE /api/threads/{thread_id}``."""

    ok: bool


class ErrorResponse(BaseModel):
    """Error body of every API failure (``retry_after`` is present on rate limits only)."""

    detail: str
    code: str
    retry_after: int | None = None
