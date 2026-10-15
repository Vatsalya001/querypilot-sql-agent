"""Read-only executor: rows, truncation, time limit, safety and JSON-safe values."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.sql.executor import ExecutionResult, UnsafeQueryError, execute_query, json_safe


def test_returns_columns_and_rows(db_path: Path) -> None:
    result = execute_query("SELECT CustomerID, FirstName, State FROM Customers ORDER BY CustomerID LIMIT 3", db_path)
    assert result.error is None
    assert result.columns == ["CustomerID", "FirstName", "State"]
    assert len(result.rows) == result.row_count == 3
    assert [row[0] for row in result.rows] == [1, 2, 3]
    assert result.truncated is False
    assert result.elapsed_ms >= 0


def test_truncates_at_max_rows(db_path: Path) -> None:
    result = execute_query("SELECT OrderID FROM Orders", db_path, max_rows=5)
    assert result.row_count == 5 and len(result.rows) == 5
    assert result.truncated is True


def test_exactly_max_rows_is_not_truncated(db_path: Path) -> None:
    result = execute_query("SELECT DepartmentID FROM Departments", db_path, max_rows=8)
    assert result.row_count == 8 and result.truncated is False


def test_time_limit_interrupts_runaway_queries(db_path: Path) -> None:
    sql = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT COUNT(*) FROM n"
    result = execute_query(sql, db_path, timeout_s=0.3)
    assert result.error == "Query exceeded the 0.3 s time limit."
    assert result.rows == [] and result.elapsed_ms < 3000


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM Orders",
        "DROP TABLE Customers",
        "UPDATE Employees SET Salary = 0",
        "SELECT 1; DROP TABLE Orders",
        "PRAGMA table_info(Orders)",
        "ATTACH DATABASE ':memory:' AS x",
        "SELECT * INTO Backup FROM Orders",
        "not sql at all",
    ],
)
def test_refuses_anything_but_one_read_only_query(db_path: Path, sql: str) -> None:
    with pytest.raises(UnsafeQueryError):
        execute_query(sql, db_path)


def test_database_errors_are_captured(db_path: Path) -> None:
    result = execute_query("SELECT * FROM Nope", db_path)
    assert result.error == "no such table: Nope"
    assert result.rows == [] and result.row_count == 0


def test_values_are_json_safe(db_path: Path) -> None:
    result = execute_query("SELECT x'CAFE' AS blob, 1.5 AS f, NULL AS n, 9e999 AS inf, 'text' AS s", db_path)
    payload = result.to_dict()
    assert payload["rows"] == [["cafe", 1.5, None, "inf", "text"]]
    json.dumps(payload, allow_nan=False)  # must not raise


def test_to_dict_shape(db_path: Path) -> None:
    payload = execute_query("```sql\nSELECT 1 AS one;\n```", db_path).to_dict()
    assert payload == {"columns": ["one"], "rows": [[1]], "row_count": 1, "truncated": False,
                       "elapsed_ms": payload["elapsed_ms"], "error": None}


def test_json_safe_helpers() -> None:
    from decimal import Decimal

    assert json_safe(Decimal("1.25")) == 1.25
    assert json_safe(memoryview(b"\x01\x02")) == "0102"
    assert json_safe(float("nan")) == "nan"
    assert isinstance(ExecutionResult([], [], 0, False, 0.0).to_dict(), dict)
