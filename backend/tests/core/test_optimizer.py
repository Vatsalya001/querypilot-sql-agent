"""Rule-based optimizer: every rule fires on a targeted query and stays quiet on a clean one."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.catalog import SchemaCatalog
from app.sql.executor import execute_query
from app.sql.optimizer import OptimizationReport, analyze_query, expand_select_star

RULES = [
    # (id, sql, category, message fragment)
    ("select_star", "SELECT * FROM Customers WHERE CustomerID = 1", "readability", "instead of SELECT *"),
    ("missing_index", "SELECT CustomerID FROM Customers WHERE State = 'California'", "index",
     "Customers.State is filtered but not indexed."),
    ("composite_index", "SELECT OrderID FROM Orders WHERE Status = 'Shipped' AND ShippingState = 'Texas'", "index",
     "Orders.Status, Orders.ShippingState are filtered together"),
    ("join_index", "SELECT c.CustomerID FROM Customers c JOIN Orders o ON o.ShippingState = c.State "
                   "WHERE o.OrderID = 1", "index", "used in a join"),
    ("group_index", "SELECT Status, COUNT(*) FROM Orders GROUP BY Status", "index", "used for sorting or grouping"),
    ("non_sargable", "SELECT OrderID FROM Orders WHERE strftime('%Y', OrderDate) = '2024'", "performance",
     "OrderDate >= '2024-01-01' AND OrderDate < '2025-01-01'"),
    ("non_sargable_lower", "SELECT CustomerID FROM Customers WHERE lower(LastName) = 'smith'", "performance",
     "prevents index use"),
    ("leading_wildcard", "SELECT CustomerID FROM Customers WHERE LastName LIKE '%son'", "performance",
     "starts with a wildcard"),
    ("unnecessary_left_join", "SELECT o.OrderID FROM Orders o LEFT JOIN Customers c ON c.CustomerID = o.CustomerID",
     "join", "unnecessary"),
    ("maybe_unnecessary_inner_join", "SELECT o.OrderID FROM Orders o JOIN Customers c "
                                     "ON c.CustomerID = o.CustomerID", "join", "may be removable"),
    ("distinct_group_by", "SELECT DISTINCT Status, COUNT(*) FROM Orders GROUP BY Status", "readability",
     "DISTINCT is redundant"),
    ("not_in_subquery", "SELECT CustomerID FROM Customers WHERE CustomerID NOT IN (SELECT CustomerID FROM Orders)",
     "correctness", "NOT EXISTS"),
    ("correlated_scalar", "SELECT o.OrderID, (SELECT SUM(p.Amount) FROM Payments p WHERE p.OrderID = o.OrderID) "
                          "AS paid FROM Orders o", "performance", "JOIN to a grouped"),
    ("comma_join", "SELECT c.FirstName, o.OrderID FROM Customers c, Orders o WHERE c.CustomerID = o.CustomerID",
     "readability", "explicit JOIN"),
    ("order_without_limit", "SELECT OrderID, OrderDate FROM Orders ORDER BY OrderDate", "performance",
     "add LIMIT"),
    ("or_across_columns", "SELECT OrderID FROM Orders WHERE Status = 'Shipped' OR ShippingState = 'Texas'",
     "performance", "OR across different columns"),
]


@pytest.mark.parametrize(("sql", "category", "fragment"), [case[1:] for case in RULES], ids=[c[0] for c in RULES])
def test_rule_fires(catalog: SchemaCatalog, sql: str, category: str, fragment: str) -> None:
    report = analyze_query(sql, catalog)
    matches = [s for s in report.suggestions if s.category == category and fragment in s.message]
    assert matches, [s.message for s in report.suggestions]


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT FirstName, LastName FROM Customers WHERE CustomerID = 3",
        "SELECT o.OrderID, c.FirstName FROM Orders o JOIN Customers c ON c.CustomerID = o.CustomerID "
        "WHERE o.OrderDate >= '2025-01-01' ORDER BY o.OrderDate DESC LIMIT 20",
        "SELECT e.FirstName, d.DepartmentName FROM Employees e JOIN Departments d ON d.DepartmentID = e.DepartmentID",
    ],
)
def test_clean_queries_have_no_findings(catalog: SchemaCatalog, sql: str) -> None:
    report = analyze_query(sql, catalog)
    assert report.suggestions == [] and report.index_recommendations == [] and report.unnecessary_joins == []


def test_api_example(catalog: SchemaCatalog) -> None:
    report = analyze_query("SELECT * FROM Customers WHERE State = 'California'", catalog)
    payload = report.to_dict()
    assert set(payload) == {"formatted_sql", "suggestions", "index_recommendations", "unnecessary_joins"}
    assert payload["formatted_sql"] == "SELECT\n  *\nFROM Customers\nWHERE\n  State = 'California'"
    assert payload["index_recommendations"] == ["CREATE INDEX idx_customers_state ON Customers (State);"]
    assert {"category": "index", "severity": "info", "message": "Customers.State is filtered but not indexed."} in (
        payload["suggestions"]
    )


def test_index_recommendations_are_deterministic_and_deduplicated(catalog: SchemaCatalog) -> None:
    sql = ("SELECT OrderID FROM Orders WHERE Status = 'Shipped' AND ShippingState = 'Texas' AND TotalAmount > 100 "
           "AND Status = 'Shipped'")
    first = analyze_query(sql, catalog)
    assert first.index_recommendations == [
        "CREATE INDEX idx_orders_status_shippingstate_totalamount ON Orders (Status, ShippingState, TotalAmount);"
    ]
    assert first.to_dict() == analyze_query(sql, catalog).to_dict()
    assert len({s.message for s in first.suggestions}) == len(first.suggestions)


def test_no_index_advice_for_existing_indexes_keys_or_small_tables(catalog: SchemaCatalog) -> None:
    assert analyze_query("SELECT OrderID FROM Orders WHERE CustomerID = 5", catalog).index_recommendations == []
    assert analyze_query("SELECT OrderID FROM Orders WHERE OrderID = 5", catalog).index_recommendations == []
    small = analyze_query("SELECT EmployeeID FROM Employees WHERE HireDate >= '2024-01-01'", catalog)
    assert small.index_recommendations == []  # 80 rows: below the 100-row threshold


def test_unnecessary_joins_are_listed(catalog: SchemaCatalog) -> None:
    report = analyze_query("SELECT o.OrderID FROM Orders o LEFT JOIN Customers c ON c.CustomerID = o.CustomerID",
                           catalog)
    assert report.unnecessary_joins == ["Customers AS c"]
    used = analyze_query("SELECT o.OrderID, c.State FROM Orders o LEFT JOIN Customers c "
                         "ON c.CustomerID = o.CustomerID", catalog)
    assert used.unnecessary_joins == []


@pytest.mark.parametrize("sql", ["", "not sql (((", "SELECT 1; SELECT 2", "DELETE FROM", "SELECT * FROM Nope WHERE x"])
def test_never_raises(catalog: SchemaCatalog, sql: str) -> None:
    report = analyze_query(sql, catalog)
    assert isinstance(report, OptimizationReport)
    assert isinstance(report.to_dict()["suggestions"], list)


def test_garbage_returns_the_input_text(catalog: SchemaCatalog) -> None:
    report = analyze_query("not sql (((", catalog)
    assert report.formatted_sql == "not sql (((" and report.suggestions == []


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM Departments WHERE Budget > 0",
         "SELECT DepartmentID, DepartmentName, Location, Budget FROM Departments WHERE Budget > 0"),
        ("SELECT DISTINCT *, 1 AS one FROM departments LIMIT 3",
         "SELECT DISTINCT DepartmentID, DepartmentName, Location, Budget, 1 AS one FROM departments LIMIT 3"),
        ("SELECT d.*, e.FirstName FROM Departments d JOIN Employees e ON e.DepartmentID = d.DepartmentID",
         "SELECT d.DepartmentID, d.DepartmentName, d.Location, d.Budget, e.FirstName FROM Departments AS d "
         "JOIN Employees AS e ON e.DepartmentID = d.DepartmentID"),
    ],
)
def test_expand_select_star(catalog: SchemaCatalog, sql: str, expected: str) -> None:
    assert expand_select_star(sql, catalog) == expected


def test_expanded_star_returns_the_same_result(catalog: SchemaCatalog, db_path: Path) -> None:
    sql = "SELECT * FROM Orders o JOIN Customers c ON o.CustomerID = c.CustomerID ORDER BY o.OrderID"
    expanded = expand_select_star(sql, catalog)

    assert expanded is not None and "*" not in expanded and expanded.startswith("SELECT o.OrderID, o.CustomerID")
    before, after = execute_query(sql, db_path, max_rows=20), execute_query(expanded, db_path, max_rows=20)
    assert (before.columns, before.rows) == (after.columns, after.rows)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT CustomerID FROM Customers",
        "SELECT COUNT(*) FROM Orders",
        "WITH t AS (SELECT CustomerID FROM Orders) SELECT * FROM t",
        "SELECT * FROM (SELECT State FROM Customers) AS x",
        "SELECT * FROM Orders JOIN OrderItems USING (OrderID)",
        "SELECT * FROM Orders NATURAL JOIN OrderItems",
        "SELECT x.* FROM Orders AS o",
        "SELECT * FROM Suppliers",
        "SELECT * FROM Orders; SELECT * FROM Customers",
        "not sql at all (",
    ],
)
def test_expand_select_star_leaves_other_queries_alone(catalog: SchemaCatalog, sql: str) -> None:
    assert expand_select_star(sql, catalog) is None


# ---- review regressions: advice that would change the result or cannot help -----------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # Orders is joined on its foreign key, not its key: every customer row repeats once per order
        "SELECT c.FirstName FROM Customers c LEFT JOIN Orders o ON o.CustomerID = c.CustomerID",
        "SELECT c.FirstName FROM Customers c JOIN Orders o ON o.CustomerID = c.CustomerID",
        "SELECT d.DepartmentName FROM Departments d LEFT JOIN Employees e ON e.DepartmentID = d.DepartmentID",
    ],
)
def test_one_to_many_joins_are_not_called_unnecessary(catalog: SchemaCatalog, db_path: Path, sql: str) -> None:
    report = analyze_query(sql, catalog)
    assert report.unnecessary_joins == []
    assert not any("unnecessary" in s.message or "may be removable" in s.message for s in report.suggestions)
    assert any(s.category == "join" and "once per matching" in s.message for s in report.suggestions), (
        [s.message for s in report.suggestions]
    )
    # the advice is right to hold back: removing the join changes the number of rows
    without = sql.split(" LEFT JOIN ")[0].split(" JOIN ")[0]
    assert execute_query(sql, db_path, max_rows=10_000).row_count != execute_query(without, db_path).row_count


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT o.OrderID FROM Orders o LEFT JOIN Customers c ON c.CustomerID = o.CustomerID",
        "SELECT o.OrderID FROM Orders o LEFT JOIN Customers c ON c.customerid = o.CustomerID",  # any case
        "SELECT o.OrderID FROM Orders o LEFT JOIN Customers c ON o.CustomerID = c.CustomerID",
    ],
)
def test_listed_unnecessary_joins_really_do_not_change_the_result(
    catalog: SchemaCatalog, db_path: Path, sql: str
) -> None:
    report = analyze_query(sql, catalog)
    assert report.unnecessary_joins == ["Customers AS c"]
    assert any("is unnecessary" in s.message for s in report.suggestions), [s.message for s in report.suggestions]
    without = "SELECT o.OrderID FROM Orders o"
    assert execute_query(sql, db_path, max_rows=10_000).rows == execute_query(without, db_path, max_rows=10_000).rows


@pytest.mark.parametrize(
    ("sql", "unhelpful"),
    [
        # LIKE is case-insensitive in SQLite: a plain (BINARY) index is never used for LIKE 'Sm%'
        ("SELECT CustomerID FROM Customers WHERE LastName LIKE 'Sm%'", "ON Customers (LastName);"),
        # an index on LastName cannot order by LOWER(LastName) or group by strftime() of a date
        ("SELECT CustomerID FROM Customers ORDER BY LOWER(LastName) LIMIT 10", "ON Customers (LastName);"),
        ("SELECT strftime('%Y', ShippedDate) AS y, COUNT(*) FROM Orders GROUP BY strftime('%Y', ShippedDate)",
         "ON Orders (ShippedDate);"),
        # the right-hand side changes with every row, so there is no range to search
        ("SELECT OrderItemID FROM OrderItems WHERE UnitPrice > Quantity * 10", "ON OrderItems (UnitPrice);"),
    ],
)
def test_no_index_advice_that_sqlite_cannot_use(catalog: SchemaCatalog, sql: str, unhelpful: str) -> None:
    report = analyze_query(sql, catalog)
    assert not any(statement.endswith(unhelpful) for statement in report.index_recommendations), (
        report.index_recommendations
    )


def test_like_prefix_gets_a_nocase_index_that_sqlite_uses(catalog: SchemaCatalog, tmp_path: Path, db_path: Path) -> None:
    import shutil
    import sqlite3

    sql = "SELECT CustomerID FROM Customers WHERE LastName LIKE 'Sm%'"
    report = analyze_query(sql, catalog)
    assert report.index_recommendations == [
        "CREATE INDEX idx_customers_lastname_nocase ON Customers (LastName COLLATE NOCASE);"
    ]
    copy = tmp_path / "copy.db"
    shutil.copy(db_path, copy)
    conn = sqlite3.connect(copy)
    try:
        conn.execute(report.index_recommendations[0])
        plan = " ".join(str(row[3]) for row in conn.execute(f"EXPLAIN QUERY PLAN {sql}"))
    finally:
        conn.close()
    assert plan.startswith("SEARCH Customers USING") and "INDEX idx_customers_lastname_nocase" in plan, plan
    wildcard = analyze_query("SELECT CustomerID FROM Customers WHERE LastName LIKE '%son'", catalog)
    assert any("COLLATE NOCASE" in s.message for s in wildcard.suggestions if "wildcard" in s.message)
