"""Sample database: deterministic build, constraints, data shape and the read-only connection."""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from app.db.database import connect_readonly, ensure_database

TABLES = ("Departments", "Employees", "Customers", "Products", "Orders", "OrderItems", "Payments")


def fingerprint(path: Path) -> tuple[dict[str, int], str]:
    """Row counts plus a checksum of every row of every table."""
    conn = sqlite3.connect(path)
    try:
        digest = hashlib.sha256()
        counts = {}
        for table in TABLES:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            counts[table] = len(rows)
            digest.update(repr(rows).encode())
        return counts, digest.hexdigest()
    finally:
        conn.close()


def scalar(path: Path, sql: str) -> object:
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_build_is_deterministic(tmp_path: Path, db_path: Path) -> None:
    rebuilt = ensure_database(tmp_path / "again.db")
    assert fingerprint(rebuilt) == fingerprint(db_path)


def test_ensure_database_is_idempotent_and_resettable(tmp_path: Path) -> None:
    path = ensure_database(tmp_path / "company.db")
    first = path.stat().st_mtime_ns
    assert ensure_database(path) == path
    assert path.stat().st_mtime_ns == first, "an up-to-date database must not be rebuilt"
    before = fingerprint(path)
    ensure_database(path, reset=True)
    assert fingerprint(path) == before
    assert not list(tmp_path.glob("*.building")), "temporary build files must be cleaned up"


def test_outdated_database_is_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "stale.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE Leftover (x INTEGER)")
    conn.commit()
    conn.close()
    ensure_database(path)
    assert scalar(path, "SELECT COUNT(*) FROM sqlite_master WHERE name = 'Leftover'") == 0
    assert scalar(path, "SELECT COUNT(*) FROM Orders") == 2000


def test_row_volumes_and_size(db_path: Path) -> None:
    counts, _ = fingerprint(db_path)
    assert counts["Departments"] == 8
    assert 70 <= counts["Employees"] <= 90
    assert counts["Customers"] == 300
    assert counts["Products"] == 60
    assert counts["Orders"] == 2000
    assert 4500 <= counts["OrderItems"] <= 5500
    assert counts["Payments"] >= 1800
    assert db_path.stat().st_size < 2 * 1024 * 1024


@pytest.mark.parametrize(
    ("table", "column", "ref_table", "ref_column"),
    [
        ("Employees", "DepartmentID", "Departments", "DepartmentID"),
        ("Employees", "ManagerID", "Employees", "EmployeeID"),
        ("Orders", "CustomerID", "Customers", "CustomerID"),
        ("Orders", "EmployeeID", "Employees", "EmployeeID"),
        ("OrderItems", "OrderID", "Orders", "OrderID"),
        ("OrderItems", "ProductID", "Products", "ProductID"),
        ("Payments", "OrderID", "Orders", "OrderID"),
    ],
)
def test_foreign_keys_are_declared(db_path: Path, table: str, column: str, ref_table: str, ref_column: str) -> None:
    conn = sqlite3.connect(db_path)
    try:
        keys = {(row[3], row[2], row[4]) for row in conn.execute(f"PRAGMA foreign_key_list({table})")}
        assert (column, ref_table, ref_column) in keys
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_data_shape_supports_the_brief_examples(db_path: Path) -> None:
    assert scalar(db_path, "SELECT COUNT(*) FROM Employees WHERE HireDate >= '2024-01-01'") >= 12
    top_state = scalar(db_path, "SELECT State FROM Customers GROUP BY State ORDER BY COUNT(*) DESC LIMIT 1")
    assert top_state == "California"
    california = scalar(db_path, "SELECT COUNT(*) FROM Customers WHERE State = 'California'")
    assert 0.12 <= california / 300 <= 0.18
    assert scalar(db_path, "SELECT COUNT(DISTINCT State) FROM Customers") >= 20
    assert scalar(db_path, "SELECT COUNT(*) FROM Employees WHERE ManagerID IS NULL") == 1


def test_data_is_internally_consistent(db_path: Path) -> None:
    mismatched_totals = scalar(db_path, """
        SELECT COUNT(*) FROM Orders o
        WHERE ABS(o.TotalAmount - (SELECT ROUND(SUM(i.Quantity * i.UnitPrice * (1 - i.Discount)), 2)
                                   FROM OrderItems i WHERE i.OrderID = o.OrderID)) > 0.011""")
    assert mismatched_totals == 0
    assert scalar(db_path, "SELECT COUNT(*) FROM Payments p JOIN Orders o USING (OrderID) "
                           "WHERE o.Status = 'Cancelled'") == 0
    assert scalar(db_path, "SELECT MIN(OrderDate) >= '2023-01-01' AND MAX(OrderDate) <= '2026-09-30' FROM Orders") == 1
    assert scalar(db_path, "SELECT COUNT(*) FROM Orders WHERE ShippedDate < OrderDate") == 0


def test_indexes_left_out_on_purpose(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        leading = set()
        for table in TABLES:
            for index in conn.execute(f"PRAGMA index_list({table})"):
                first = conn.execute(f"PRAGMA index_info({index[1]})").fetchone()
                leading.add((table, first[2]))
    finally:
        conn.close()
    assert ("Orders", "OrderDate") in leading
    for missing in [("Customers", "State"), ("Employees", "HireDate"), ("Products", "Category"), ("Orders", "Status")]:
        assert missing not in leading


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO Departments VALUES (99, 'Legal', 'Boston', 1)",
        "UPDATE Orders SET Status = 'Pending'",
        "DELETE FROM Payments",
        "DROP TABLE Orders",
        "CREATE TABLE Scratch (x INTEGER)",
        "CREATE TEMP TABLE Scratch (x INTEGER)",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA query_only = OFF",
        "PRAGMA table_info(Orders)",
        "SELECT * FROM pragma_table_info('Orders')",
        "SELECT load_extension('evil')",
        "SELECT name FROM sqlite_master",
    ],
)
def test_connect_readonly_rejects_writes_and_admin(db_path: Path, sql: str) -> None:
    conn = connect_readonly(db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql).fetchall()
        assert conn.execute("SELECT COUNT(*) FROM Departments").fetchone()[0] == 8
    finally:
        conn.close()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT COUNT(*) FROM Orders",
        "WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 5) SELECT SUM(x) FROM n",
        "SELECT RANK() OVER (ORDER BY Salary DESC) FROM Employees",
        "EXPLAIN QUERY PLAN SELECT * FROM Orders WHERE CustomerID = 1",
        "SELECT strftime('%Y', OrderDate), date('now') FROM Orders LIMIT 1",
    ],
)
def test_connect_readonly_allows_reads(db_path: Path, sql: str) -> None:
    conn = connect_readonly(db_path)
    try:
        assert conn.execute(sql).fetchall()
    finally:
        conn.close()
