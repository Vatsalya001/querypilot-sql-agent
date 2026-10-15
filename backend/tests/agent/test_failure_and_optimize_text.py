"""User-facing failure text and the optimize-mode rules."""

from app.agent.prompts import OPTIMIZE_INSTRUCTIONS, failure_message


def test_failure_message_has_no_double_punctuation() -> None:
    message = failure_message(["Unknown column 'Region' in table Customers. Did you mean 'State'?"])
    assert "?." not in message and ".." not in message
    assert "Did you mean 'State'?" in message


def test_failure_message_without_errors() -> None:
    assert "Remaining problems" not in failure_message([])


def test_optimize_keeps_where_filters_in_place() -> None:
    assert "do not move them into JOIN ... ON" in OPTIMIZE_INSTRUCTIONS
