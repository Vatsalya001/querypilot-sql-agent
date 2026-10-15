"""Resource bounds on untrusted read-only SQL: value size, memory, temp storage, wall-clock time and result size.

Pasted SQL is executed in explain mode, so these limits must hold even when the query is valid and read-only.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from app.db.database import MAX_VALUE_BYTES, connect_readonly
from app.sql import executor
from app.sql.executor import MAX_RESULT_BYTES, execute_query


def test_values_larger_than_the_limit_fail_fast(db_path: Path) -> None:
    started = time.perf_counter()
    result = execute_query("SELECT length(zeroblob(1000000000)) AS n", db_path, timeout_s=5)
    assert result.error is not None and "limit" in result.error
    assert result.rows == []
    assert time.perf_counter() - started < 1.0


def test_values_within_the_limit_still_work(db_path: Path) -> None:
    result = execute_query(f"SELECT length(zeroblob({MAX_VALUE_BYTES // 2})) AS n", db_path)
    assert result.error is None and result.rows == [[MAX_VALUE_BYTES // 2]]


def test_aggregated_strings_are_bounded(db_path: Path) -> None:
    sql = "SELECT length(group_concat(a.Email || b.Email)) FROM Customers a, Customers b"
    result = execute_query(sql, db_path, timeout_s=5)
    assert result.error is not None and "limit" in result.error


def test_large_sorts_hit_the_memory_cap_instead_of_spilling_to_disk(db_path: Path) -> None:
    sql = "SELECT a.OrderItemID FROM OrderItems a, OrderItems b ORDER BY random()"
    started = time.perf_counter()
    result = execute_query(sql, db_path, max_rows=5, timeout_s=10)
    assert result.error is not None and "memory" in result.error
    assert time.perf_counter() - started < 5.0


def test_the_time_limit_is_wall_clock(db_path: Path) -> None:
    sql = "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n) SELECT COUNT(*) FROM n"
    started = time.perf_counter()
    result = execute_query(sql, db_path, timeout_s=0.5)
    assert result.error == "Query exceeded the 0.5 s time limit."
    assert time.perf_counter() - started < 2.0


def test_the_time_limit_holds_when_each_step_is_slow(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A step-counting progress handler would only check the clock after thousands of slow steps."""

    def connect_with_slow_function(path: str | Path) -> sqlite3.Connection:
        conn = connect_readonly(path)
        conn.create_function("slow_step", 1, lambda value: time.sleep(0.02) or value)
        return conn

    monkeypatch.setattr(executor, "connect_readonly", connect_with_slow_function)
    started = time.perf_counter()
    result = execute_query("SELECT slow_step(OrderID) FROM Orders", db_path, max_rows=5_000, timeout_s=0.3)
    assert result.error == "Query exceeded the 0.3 s time limit."
    assert time.perf_counter() - started < 1.5  # 2,000 rows x 20 ms would take 40 s


def test_result_payload_is_capped(db_path: Path) -> None:
    sql = "SELECT replace(hex(zeroblob(40000)), '0', 'x') AS big FROM OrderItems"
    result = execute_query(sql, db_path, max_rows=500)
    assert result.error is None
    assert result.truncated is True
    assert sum(len(str(value)) for row in result.rows for value in row) <= MAX_RESULT_BYTES + 100_000


@pytest.mark.parametrize("sql", ["ATTACH DATABASE ':memory:' AS extra", "PRAGMA temp_store = FILE"])
def test_connection_settings_cannot_be_changed_by_queries(db_path: Path, sql: str) -> None:
    conn = connect_readonly(db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    finally:
        conn.close()


def test_wide_results_are_rejected(db_path: Path) -> None:
    columns = ", ".join(f"{i} AS c{i}" for i in range(150))
    result = execute_query(f"SELECT {columns}", db_path)
    assert result.error is not None and "columns" in result.error
