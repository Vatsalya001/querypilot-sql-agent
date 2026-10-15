"""Pure helpers of the graph nodes and the agent service."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.agent.nodes import clarified_request, pasted_sql
from app.agent.service import RECURSION_LIMIT, recursion_limit


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Why does this fail? SELECT Nmae FROM Customers", "SELECT Nmae FROM Customers"),
        ("what does this do? select status, count(*) from orders group by status",
         "select status, count(*) from orders group by status"),
        ("explain select name from customers", "select name from customers"),  # parses: plain lowercase SQL
        ("Fix it:\n```sql\nselect name form customers\n```", "select name form customers"),  # fenced: always SQL
        ("Optimize: WITH t AS (SELECT * FROM Orders) SELECT COUNT(*) FROM t",
         "WITH t AS (SELECT * FROM Orders) SELECT COUNT(*) FROM t"),
        ("Fix this: SELECT Name, FROM Customers", "SELECT Name, FROM Customers"),  # broken, but code
        ("Explain this query: SELECT length(zeroblob(1000)) AS n", "SELECT length(zeroblob(1000)) AS n"),
        ("Explain the select statement you wrote", None),
        ("Explain the SELECT statement you wrote", None),
        ("Why does this select return duplicates?", None),
        ("Optimize the select query above", None),
        ("Optimize the SELECT query above", None),
        ("Why does my query select so few rows from customers?", None),
        ("Why doesn't the select work? It's slow", None),
        ("Explain this query", None),
    ],
)
def test_pasted_sql_ignores_english_that_merely_says_select(text: str, expected: str | None) -> None:
    assert pasted_sql(text) == expected


def test_clarified_request_is_the_message_the_clarification_was_about() -> None:
    asked = [
        HumanMessage(content="Show the best customers"),
        AIMessage(content="How should I rank them?", additional_kwargs={"status": "clarification"}),
    ]
    answered = [*asked, HumanMessage(content="by revenue"),
                AIMessage(content="Here.", additional_kwargs={"status": "success"})]

    assert clarified_request(asked) == "Show the best customers"
    assert clarified_request(answered) is None
    assert clarified_request(asked[1:]) is None
    assert clarified_request([]) is None
    missing_sql = [
        HumanMessage(content="Explain this query"),
        AIMessage(content="Please paste the SQL query you'd like me to explain.",
                  additional_kwargs={"status": "clarification", "intent": "explain_sql"}),
    ]
    assert clarified_request(missing_sql) is None  # it asked for SQL, not for details of a data request


def test_recursion_limit_covers_the_longest_repair_loop() -> None:
    assert recursion_limit(3) == RECURSION_LIMIT == 40
    longest_path = 5 + 2 * 25 + 4  # nodes before generation, the repair loop, nodes after validation
    assert recursion_limit(25) > longest_path
