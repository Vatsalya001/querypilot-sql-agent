"""Conditional-edge routers of the agent graph.

Each router is a pure function of the state that returns the name of the next node. Keeping them
separate from the nodes makes every branch of the workflow readable in one place and unit-testable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.agent.state import AgentState

SQL_INPUT_INTENTS = frozenset({"optimize_sql", "debug_sql", "explain_sql"})
"""Intents that work on SQL supplied by the user."""

ANSWER_INTENTS = frozenset({"schema_info", "sql_help"})
"""Intents answered in prose without generating SQL."""

MIN_INTENT_CONFIDENCE = 0.5
"""Below this classifier confidence the agent asks for clarification instead of guessing."""


def route_after_guard(state: AgentState) -> str:
    """Blocked input never reaches the LLM: destructive SQL is refused, anything else rejected."""
    guard = state.get("guard") or {}
    if guard.get("allowed", True):
        return "detect_intent"
    if guard.get("category") == "destructive_sql":
        return "refuse_destructive"
    return "reject_request"


def route_after_scope(state: AgentState) -> str:
    """Scope policy applied to the classified intent."""
    intent = state.get("intent")
    if state.get("prompt_injection") or intent == "out_of_scope":
        return "reject_request"
    if intent == "destructive":
        return "refuse_destructive"
    if intent == "ambiguous" or (state.get("intent_confidence") or 0.0) < MIN_INTENT_CONFIDENCE:
        return "ask_clarification"
    if intent in ANSWER_INTENTS:
        return "answer_question"
    if intent in SQL_INPUT_INTENTS and not state.get("user_sql"):
        return "ask_clarification"
    return "retrieve_schema"


def route_after_retrieve(state: AgentState) -> str:
    """User-supplied SQL is diagnosed first; everything else goes straight to generation."""
    if state.get("intent") in SQL_INPUT_INTENTS and state.get("user_sql"):
        return "diagnose_sql"
    return "generate_sql"


def route_after_diagnose(state: AgentState) -> str:
    """Valid SQL to explain skips generation; destructive SQL is refused; the rest is (re)written."""
    diagnosis: dict[str, Any] = state.get("diagnosis") or {}
    if (diagnosis.get("validation") or {}).get("destructive"):
        return "refuse_destructive"
    if state.get("intent") == "explain_sql" and (state.get("validation") or {}).get("valid"):
        return "optimize_sql"
    return "generate_sql"


def route_after_generate(state: AgentState) -> str:
    """Follow the generator's status."""
    status = (state.get("generation") or {}).get("status")
    if status == "needs_clarification":
        return "ask_clarification"
    if status == "cannot_answer":
        return "handle_failure"
    return "validate_sql"


def make_route_after_validate(max_attempts: int) -> Callable[[AgentState], str]:
    """Build the self-correction router: repair until ``max_attempts`` generations, then give up."""

    def route_after_validate(state: AgentState) -> str:
        if (state.get("validation") or {}).get("valid"):
            return "optimize_sql"
        if (state.get("attempts") or 0) < max_attempts:
            return "generate_sql"
        return "handle_failure"

    return route_after_validate
