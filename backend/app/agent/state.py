"""LangGraph state of the SQL agent.

Two kinds of keys live in :class:`AgentState`:

* **persistent** across the turns of a thread (saved by the checkpointer): ``messages`` (reduced with
  ``add_messages``) and ``last_sql`` (the latest validated SQLite query, used by follow-ups);
* **per turn**: everything else. :func:`new_turn_input` resets all of them at the start of every
  turn so nothing leaks from one request into the next.

Only JSON-safe values (plus LangChain messages) are stored, so any checkpointer can serialize them.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage, HumanMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    """State shared by all graph nodes."""

    # --- persistent across turns ---
    messages: Annotated[list[AnyMessage], add_messages]
    last_sql: str | None

    # --- request ---
    user_input: str
    dialect: str
    execute: bool

    # --- per-turn working state ---
    guard: dict[str, Any] | None
    intent: str | None
    intent_confidence: float | None
    user_sql: str | None
    prompt_injection: bool
    clarification_question: str | None
    schema_context: dict[str, Any] | None
    diagnosis: dict[str, Any] | None
    candidate_sql: str | None
    attempts: int
    generation: dict[str, Any] | None
    validation: dict[str, Any] | None
    optimization: dict[str, Any] | None
    execution: dict[str, Any] | None
    cost: dict[str, Any] | None
    explanation: str | None
    status: str | None
    response_message: str | None
    final: dict[str, Any] | None
    error: str | None


PER_TURN_DEFAULTS: dict[str, Any] = {
    "guard": None,
    "intent": None,
    "intent_confidence": None,
    "user_sql": None,
    "prompt_injection": False,
    "clarification_question": None,
    "schema_context": None,
    "diagnosis": None,
    "candidate_sql": None,
    "attempts": 0,
    "generation": None,
    "validation": None,
    "optimization": None,
    "execution": None,
    "cost": None,
    "explanation": None,
    "status": None,
    "response_message": None,
    "final": None,
    "error": None,
}


def new_turn_input(
    message: str,
    *,
    dialect: str,
    execute: bool,
    seed_messages: list[AnyMessage] | None = None,
    seed_last_sql: str | None = None,
) -> dict[str, Any]:
    """Build the graph input for one user turn.

    Resets every per-turn key, appends the user's message to ``messages`` and, when the thread is
    being rehydrated from client-side history, prepends ``seed_messages`` and sets ``last_sql``.
    ``last_sql`` is left untouched otherwise, so follow-ups keep working on an existing thread.
    """
    data: dict[str, Any] = dict(PER_TURN_DEFAULTS)
    data.update(user_input=message, dialect=dialect, execute=execute)
    data["messages"] = [*(seed_messages or []), HumanMessage(content=message)]
    if seed_messages is not None or seed_last_sql is not None:
        data["last_sql"] = seed_last_sql
    return data
