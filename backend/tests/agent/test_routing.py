"""Conditional-edge routers are pure functions of the state."""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.routing import (
    make_route_after_validate,
    route_after_diagnose,
    route_after_generate,
    route_after_guard,
    route_after_retrieve,
    route_after_scope,
)


@pytest.mark.parametrize(
    ("guard", "expected"),
    [
        ({"allowed": True, "category": "ok"}, "detect_intent"),
        ({"allowed": False, "category": "destructive_sql"}, "refuse_destructive"),
        ({"allowed": False, "category": "prompt_injection"}, "reject_request"),
        ({"allowed": False, "category": "empty"}, "reject_request"),
        ({"allowed": False, "category": "too_long"}, "reject_request"),
    ],
)
def test_route_after_guard(guard: dict[str, Any], expected: str) -> None:
    assert route_after_guard({"guard": guard}) == expected


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"intent": "out_of_scope", "intent_confidence": 0.9}, "reject_request"),
        ({"intent": "generate_sql", "intent_confidence": 0.9, "prompt_injection": True}, "reject_request"),
        ({"intent": "destructive", "intent_confidence": 0.9}, "refuse_destructive"),
        ({"intent": "ambiguous", "intent_confidence": 0.9}, "ask_clarification"),
        ({"intent": "generate_sql", "intent_confidence": 0.3}, "ask_clarification"),
        ({"intent": "schema_info", "intent_confidence": 0.9}, "answer_question"),
        ({"intent": "sql_help", "intent_confidence": 0.9}, "answer_question"),
        ({"intent": "debug_sql", "intent_confidence": 0.9, "user_sql": None}, "ask_clarification"),
        ({"intent": "debug_sql", "intent_confidence": 0.9, "user_sql": "SELECT 1"}, "retrieve_schema"),
        ({"intent": "generate_sql", "intent_confidence": 0.9}, "retrieve_schema"),
        ({"intent": "modify_sql", "intent_confidence": 0.9}, "retrieve_schema"),
    ],
)
def test_route_after_scope(state: dict[str, Any], expected: str) -> None:
    assert route_after_scope(state) == expected


def test_route_after_retrieve() -> None:
    assert route_after_retrieve({"intent": "optimize_sql", "user_sql": "SELECT 1"}) == "diagnose_sql"
    assert route_after_retrieve({"intent": "generate_sql"}) == "generate_sql"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({"intent": "explain_sql", "validation": {"valid": True}, "diagnosis": {"validation": {}}}, "optimize_sql"),
        ({"intent": "debug_sql", "diagnosis": {"validation": {"valid": False}}}, "generate_sql"),
        ({"intent": "optimize_sql", "diagnosis": {"validation": {"valid": True}}}, "generate_sql"),
        ({"intent": "explain_sql", "diagnosis": {"validation": {"destructive": True}}}, "refuse_destructive"),
    ],
)
def test_route_after_diagnose(state: dict[str, Any], expected: str) -> None:
    assert route_after_diagnose(state) == expected


@pytest.mark.parametrize(
    ("status", "expected"),
    [("ok", "validate_sql"), ("needs_clarification", "ask_clarification"), ("cannot_answer", "handle_failure")],
)
def test_route_after_generate(status: str, expected: str) -> None:
    assert route_after_generate({"generation": {"status": status}}) == expected


def test_route_after_validate_repairs_until_the_attempt_budget_is_spent() -> None:
    route = make_route_after_validate(3)
    assert route({"validation": {"valid": True}, "attempts": 1}) == "optimize_sql"
    assert route({"validation": {"valid": False}, "attempts": 1}) == "generate_sql"
    assert route({"validation": {"valid": False}, "attempts": 2}) == "generate_sql"
    assert route({"validation": {"valid": False}, "attempts": 3}) == "handle_failure"
