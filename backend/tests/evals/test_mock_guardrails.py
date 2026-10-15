"""The offline mock gateway (keyless demos) must handle every golden guardrail case like the real agent."""

from __future__ import annotations

import pytest

from app.agent.mock_llm import classify_message
from app.agent.routing import route_after_guard, route_after_scope
from app.guardrails.input_guard import check_input
from evals.golden import GoldenCase, load_golden

GUARDRAIL_CASES = [case for case in load_golden() if case.category == "guardrail"]
_EXPECTED_ROUTE = {"refused": "refuse_destructive", "rejected": "reject_request", "clarification": "ask_clarification"}


@pytest.mark.parametrize("case", GUARDRAIL_CASES, ids=[case.id for case in GUARDRAIL_CASES])
def test_mock_routes_golden_guardrail_cases(case: GoldenCase) -> None:
    guard = check_input(case.question).to_dict()
    if not guard["allowed"]:
        route = route_after_guard({"guard": guard})
    else:
        decision = classify_message(case.question)
        route = route_after_scope({
            "intent": decision.intent,
            "intent_confidence": decision.confidence,
            "prompt_injection": decision.prompt_injection,
            "user_sql": decision.user_sql,
        })
    assert route == _EXPECTED_ROUTE[case.expected_status]


@pytest.mark.parametrize(
    ("message", "intent"),
    [
        ("Which team has won the most NBA championships?", "out_of_scope"),
        ("Write a Python script that scrapes product prices.", "out_of_scope"),
        ("Which team has the biggest budget?", "generate_sql"),
        ("How many products are in the Software category?", "generate_sql"),
    ],
)
def test_mock_off_topic_rule_keeps_data_questions(message: str, intent: str) -> None:
    assert classify_message(message).intent == intent
