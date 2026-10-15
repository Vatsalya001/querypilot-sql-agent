"""Model-output parsing helpers and the tolerant structured-output schemas."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.parsing import ThinkStreamFilter, extract_json_object, json_instructions, strip_think
from app.agent.schemas import IntentDecision, SQLGeneration


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("<think>plan</think>Answer.", "Answer."),
        ("<THINK>a\nb</THINK>\n  Answer.  ", "Answer."),
        ("reasoning without an opening tag</think>Answer.", "Answer."),
        ("Answer.<think>cut off by the token limit", "Answer."),
        ("No tags at all.", "No tags at all."),
    ],
)
def test_strip_think(raw: str, clean: str) -> None:
    assert strip_think(raw) == clean


def test_think_stream_filter_handles_tags_split_across_chunks() -> None:
    stream = ThinkStreamFilter()
    chunks = ["Hello <", "thi", "nk>hidden", " text</thi", "nk> world", " <", "b>"]
    emitted = "".join(stream.feed(chunk) for chunk in chunks) + stream.flush()
    assert emitted == "Hello  world <b>"


def test_think_stream_filter_drops_an_unterminated_block() -> None:
    stream = ThinkStreamFilter()
    assert stream.feed("Visible <think>never closed") == "Visible "
    assert stream.flush() == ""


@pytest.mark.parametrize(
    "text",
    [
        '{"intent": "sql_help", "confidence": 0.9}',
        'Sure:\n```json\n{"intent": "sql_help", "confidence": 0.9}\n```',
        '<think>{"intent": "wrong"}</think> {"intent": "sql_help", "confidence": 0.9} trailing',
        '{"name": "IntentDecision", "arguments": {"intent": "sql_help", "confidence": 0.9}}',
        '{"name": "IntentDecision", "arguments": "{\\"intent\\": \\"sql_help\\", \\"confidence\\": 0.9}"}',
    ],
)
def test_extract_json_object(text: str) -> None:
    assert extract_json_object(text) == {"intent": "sql_help", "confidence": 0.9}


def test_extract_json_object_without_json() -> None:
    assert extract_json_object("no braces here") is None
    assert extract_json_object("{not json}") is None


def test_json_instructions_list_every_field() -> None:
    text = json_instructions(SQLGeneration)
    for field in SQLGeneration.model_fields:
        assert f'"{field}"' in text
    assert '"ok", "needs_clarification", "cannot_answer"' in text


@pytest.mark.parametrize(
    ("label", "expected"),
    [("Generate SQL", "generate_sql"), ("out of scope", "out_of_scope"), ("MODIFY", "modify_sql"),
     ("follow-up", "modify_sql"), ("greeting", "sql_help")],
)
def test_intent_labels_are_normalized(label: str, expected: str) -> None:
    assert IntentDecision.model_validate({"intent": label, "confidence": 0.9}).intent == expected


def test_intent_decision_tolerates_small_format_slips() -> None:
    decision = IntentDecision.model_validate(
        {"intent": "explain_sql", "confidence": "95%", "user_sql": "", "clarification_question": "null",
         "prompt_injection": None, "reasoning": None}
    )
    assert decision.confidence == 0.95
    assert decision.user_sql is None and decision.clarification_question is None
    assert decision.prompt_injection is False and decision.reasoning == ""
    assert IntentDecision.model_validate({"intent": "sql_help"}).confidence == 0.75


def test_intent_decision_rejects_unknown_intents() -> None:
    with pytest.raises(ValidationError):
        IntentDecision.model_validate({"intent": "dance", "confidence": 0.9})


def test_sql_generation_coerces_lists_and_issues() -> None:
    result = SQLGeneration.model_validate(
        {"status": "success", "sql": "SELECT 1", "assumptions": "one assumption", "changes": None,
         "issues": ["Typo in FROM", {"issue": "Unknown column", "fix": "Use State"}]}
    )
    assert result.status == "ok" and result.summary == ""
    assert result.assumptions == ["one assumption"] and result.changes == []
    assert [(issue.problem, issue.reason) for issue in result.issues] == [
        ("Typo in FROM", ""),
        ("Unknown column", "Use State"),
    ]


def test_textual_nulls_from_small_models_become_empty() -> None:
    result = SQLGeneration.model_validate(
        {"status": "ok", "sql": "SELECT 1", "clarification_question": "None", "assumptions": "None",
         "issues": "null", "changes": ["None", "Added a filter."], "suggestions": None, "summary": "x"}
    )
    assert result.clarification_question is None
    assert (result.assumptions, result.issues, result.changes, result.suggestions) == ([], [], ["Added a filter."], [])
    decision = IntentDecision.model_validate({"intent": "generate_sql", "confidence": 1, "user_sql": "None"})
    assert decision.user_sql is None
