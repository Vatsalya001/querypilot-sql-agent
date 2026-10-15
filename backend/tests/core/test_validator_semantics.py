"""Validator regressions: queries that SQLite runs without an error but that silently return wrong rows.

Each case below was found in review: the query compiled, every check passed, and the result was wrong
(for example all 300 customers instead of the 155 who paid by PayPal).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.catalog import SchemaCatalog
from app.sql.executor import execute_query
from app.sql.validator import validate_sql


def _failed(report, check: str) -> bool:
    return any(item.name == check and not item.passed for item in report.checks)


# ---- a hallucinated column in a value subquery binds to the outer query ---------------------------------

OUTER_BOUND = [
    # Payments has no CustomerID: SQLite reads Customers.CustomerID from the outer query instead, so the IN
    # condition is "CustomerID IN (CustomerID, ...)" and every customer matches.
    ("in", "SELECT * FROM Customers WHERE CustomerID IN "
           "(SELECT CustomerID FROM Payments WHERE PaymentMethod = 'PayPal')"),
    ("not_in", "SELECT * FROM Customers WHERE CustomerID NOT IN (SELECT CustomerID FROM Payments)"),
    ("aliased_projection", "SELECT * FROM Customers WHERE CustomerID IN (SELECT CustomerID AS id FROM Payments)"),
    ("scalar_comparison", "SELECT * FROM Orders WHERE CustomerID = (SELECT CustomerID FROM Payments WHERE "
                          "PaymentID = 1)"),
]


@pytest.mark.parametrize("sql", [case[1] for case in OUTER_BOUND], ids=[case[0] for case in OUTER_BOUND])
def test_subquery_column_that_binds_to_the_outer_query_is_rejected(
    catalog: SchemaCatalog, db_path: Path, sql: str
) -> None:
    report = validate_sql(sql, catalog, db_path=db_path)
    assert not report.valid
    assert _failed(report, "columns")
    assert any("Unknown column 'CustomerID' in table Payments" in error and "outer query" in error
               for error in report.errors), report.errors


def test_the_outer_bound_in_subquery_really_returns_wrong_rows(db_path: Path) -> None:
    """Why it matters: the query runs and returns every customer, not the 155 who paid by PayPal."""
    wrong = execute_query(OUTER_BOUND[0][1].replace("*", "COUNT(*)", 1), db_path)
    right = execute_query("SELECT COUNT(DISTINCT o.CustomerID) FROM Orders o JOIN Payments p "
                          "ON p.OrderID = o.OrderID WHERE p.PaymentMethod = 'PayPal'", db_path)
    assert wrong.error is None and right.error is None
    assert wrong.rows[0][0] == 300 and right.rows[0][0] < 300


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM Customers WHERE CustomerID IN (SELECT CustomerID FROM Orders)",
        "SELECT * FROM Orders o WHERE o.CustomerID IN (SELECT c.CustomerID FROM Customers c WHERE c.State = 'Texas')",
        # unqualified outer references in a WHERE clause are ordinary correlation
        "SELECT o.OrderID FROM Orders o WHERE EXISTS (SELECT 1 FROM Payments p WHERE p.OrderID = o.OrderID "
        "AND Amount < TotalAmount)",
        # the projection of EXISTS is never compared with anything
        "SELECT o.OrderID FROM Orders o WHERE EXISTS (SELECT CustomerID FROM Payments p WHERE p.OrderID = o.OrderID)",
        # an outer column inside an expression of a scalar subquery is a deliberate correlation
        "SELECT o.OrderID, (SELECT SUM(Amount) / TotalAmount FROM Payments p WHERE p.OrderID = o.OrderID) AS ratio "
        "FROM Orders o",
        # an explicitly qualified outer column is the author's choice
        "SELECT * FROM Customers c WHERE c.CustomerID IN (SELECT c.CustomerID FROM Payments p)",
        "SELECT e.FirstName FROM Employees e WHERE e.Salary > (SELECT AVG(Salary) FROM Employees "
        "WHERE DepartmentID = e.DepartmentID)",
    ],
)
def test_legitimate_correlated_subqueries_stay_valid(catalog: SchemaCatalog, db_path: Path, sql: str) -> None:
    report = validate_sql(sql, catalog, db_path=db_path)
    assert report.valid, report.errors


# ---- joins with CTEs, derived tables, bare JOINs and correlated subqueries --------------------------------

CLEAN_JOINS = [
    # a CTE / derived table on the left used to get "no column equality condition ... Cartesian product"
    ("cte_left", "WITH t AS (SELECT CustomerID, SUM(TotalAmount) AS total FROM Orders GROUP BY CustomerID) "
                 "SELECT c.FirstName, t.total FROM t JOIN Customers c ON c.CustomerID = t.CustomerID"),
    ("derived_left", "SELECT c.FirstName, t.total FROM (SELECT CustomerID, SUM(TotalAmount) AS total FROM Orders "
                     "GROUP BY CustomerID) t JOIN Customers c ON c.CustomerID = t.CustomerID ORDER BY t.total DESC"),
    ("cte_comma", "WITH top AS (SELECT CustomerID FROM Orders) SELECT c.FirstName FROM top, Customers c "
                  "WHERE c.CustomerID = top.CustomerID"),
    ("cte_renamed", "WITH x AS (SELECT o.CustomerID AS cid FROM Orders o) SELECT c.FirstName FROM x "
                    "JOIN Customers c ON c.CustomerID = x.cid"),
    ("cte_column_list", "WITH x(cid, n) AS (SELECT CustomerID, COUNT(*) FROM Orders GROUP BY CustomerID) "
                        "SELECT c.FirstName, x.n FROM x JOIN Customers c ON c.CustomerID = x.cid"),
    ("recursive_left", "WITH RECURSIVE chain(EmployeeID, ManagerID) AS (SELECT EmployeeID, ManagerID FROM Employees "
                       "WHERE ManagerID IS NULL UNION ALL SELECT e.EmployeeID, e.ManagerID FROM Employees e "
                       "JOIN chain c ON e.ManagerID = c.EmployeeID) SELECT e.FirstName FROM chain c "
                       "JOIN Employees e ON e.EmployeeID = c.EmployeeID"),
    ("star_cte", "WITH x AS (SELECT * FROM Orders WHERE Status = 'Shipped') SELECT c.FirstName FROM x "
                 "JOIN Customers c ON c.CustomerID = x.CustomerID"),
    # a bare JOIN with its condition in WHERE (sqlglot renders it as JOIN ... ON TRUE)
    ("bare_join_where", "SELECT c.FirstName, o.OrderID FROM Orders o JOIN Customers c "
                        "WHERE c.CustomerID = o.CustomerID"),
    # correlated key equalities that follow the foreign keys
    ("exists_fk", "SELECT * FROM Customers c WHERE EXISTS (SELECT 1 FROM Orders o WHERE o.CustomerID = c.CustomerID)"),
    ("correlated_same_key", "SELECT e.FirstName FROM Employees e WHERE e.Salary > (SELECT AVG(Salary) FROM Employees "
                            "WHERE DepartmentID = e.DepartmentID)"),
    ("exists_value_match", "SELECT * FROM Customers c WHERE EXISTS (SELECT 1 FROM Orders o WHERE "
                           "o.CustomerID = c.CustomerID AND o.ShippingState = c.State)"),
]


@pytest.mark.parametrize("sql", [case[1] for case in CLEAN_JOINS], ids=[case[0] for case in CLEAN_JOINS])
def test_valid_joins_get_no_false_warnings(catalog: SchemaCatalog, db_path: Path, sql: str) -> None:
    report = validate_sql(sql, catalog, db_path=db_path)
    assert report.valid, report.errors
    assert report.warnings == [], report.warnings


WRONG_KEYS = [
    # the CTE / derived column comes from Orders.CustomerID, which is not an order id
    ("cte_lineage", "WITH x AS (SELECT OrderID, CustomerID FROM Orders) SELECT p.Amount FROM x "
                    "JOIN Payments p ON p.OrderID = x.CustomerID"),
    ("derived_lineage", "SELECT p.Amount FROM (SELECT CustomerID FROM Orders) t JOIN Payments p "
                        "ON p.OrderID = t.CustomerID"),
    ("star_lineage", "WITH x AS (SELECT * FROM Orders) SELECT c.FirstName FROM x JOIN Customers c "
                     "ON c.CustomerID = x.OrderID"),
    # a bare JOIN whose condition sits in WHERE used to skip the key check
    ("bare_join_where", "SELECT * FROM Orders o JOIN Customers c WHERE c.CustomerID = o.OrderID"),
    # wrong keys inside EXISTS / IN semi-joins (300 customers instead of 258 for the first one)
    ("exists", "SELECT * FROM Customers c WHERE EXISTS (SELECT 1 FROM Orders o WHERE o.OrderID = c.CustomerID)"),
    ("in_correlated", "SELECT * FROM Customers c WHERE c.CustomerID IN (SELECT o.CustomerID FROM Orders o "
                      "WHERE o.EmployeeID = c.CustomerID)"),
    ("unqualified_inner", "SELECT * FROM Customers c WHERE EXISTS (SELECT 1 FROM Payments WHERE "
                          "PaymentID = c.CustomerID)"),
]


@pytest.mark.parametrize("sql", [case[1] for case in WRONG_KEYS], ids=[case[0] for case in WRONG_KEYS])
def test_wrong_join_keys_are_rejected_wherever_they_hide(catalog: SchemaCatalog, db_path: Path, sql: str) -> None:
    report = validate_sql(sql, catalog, db_path=db_path)
    assert not report.valid
    assert _failed(report, "joins")
    assert any("is not a valid relationship" in error for error in report.errors), report.errors


def test_a_real_cartesian_product_with_a_cte_is_still_reported(catalog: SchemaCatalog, db_path: Path) -> None:
    report = validate_sql("WITH t AS (SELECT CustomerID FROM Orders) SELECT * FROM t JOIN Customers c "
                          "ON c.State = 'Texas'", catalog, db_path=db_path)
    assert report.valid and any("no column equality condition" in warning for warning in report.warnings)
    bare = validate_sql("SELECT * FROM Orders o JOIN Customers c", catalog, db_path=db_path)
    assert any("without a join condition" in warning for warning in bare.warnings), bare.warnings
    assert not any("ON TRUE" in warning for warning in bare.warnings)


# ---- text compared with a number: always true or always false in SQLite ----------------------------------

TEXT_VS_NUMBER = [
    ("strftime_eq", "SELECT * FROM Orders WHERE strftime('%Y', OrderDate) = 2024", "sqlite"),  # 0 rows, not 494
    ("strftime_gt", "SELECT * FROM Orders WHERE strftime('%Y', OrderDate) > 2023", "sqlite"),  # every row
    ("strftime_in", "SELECT * FROM Orders WHERE strftime('%m', OrderDate) IN (1, 2, 3)", "sqlite"),
    ("strftime_between", "SELECT * FROM Orders WHERE strftime('%Y', OrderDate) BETWEEN 2023 AND 2024", "sqlite"),
    ("reversed", "SELECT * FROM Orders WHERE 2024 = strftime('%Y', OrderDate)", "sqlite"),
    ("having", "SELECT strftime('%Y', OrderDate) AS y, COUNT(*) FROM Orders GROUP BY y "
               "HAVING strftime('%Y', OrderDate) >= 2024", "sqlite"),
    ("date_function", "SELECT * FROM Orders WHERE date(OrderDate) > 2024", "sqlite"),
    ("substr", "SELECT * FROM Customers WHERE substr(Phone, 2, 3) = 213", "sqlite"),
    # correct in MySQL, but normalized to STRFTIME('%Y', HireDate) = 2024 for SQLite: silently 0 rows
    ("mysql_input", "SELECT * FROM Employees WHERE DATE_FORMAT(HireDate, '%Y') = 2024", "mysql"),
]


@pytest.mark.parametrize(("sql", "dialect"), [case[1:] for case in TEXT_VS_NUMBER], ids=[c[0] for c in TEXT_VS_NUMBER])
def test_text_compared_with_a_number_is_rejected(catalog: SchemaCatalog, db_path: Path, sql: str, dialect: str) -> None:
    report = validate_sql(sql, catalog, dialect=dialect, db_path=db_path)
    assert not report.valid
    assert any("compares" in error and "with a number" in error for error in report.errors), report.errors


def test_the_text_number_comparison_really_returns_no_rows(db_path: Path) -> None:
    wrong = execute_query("SELECT COUNT(*) FROM Orders WHERE strftime('%Y', OrderDate) = 2024", db_path)
    fixed = execute_query("SELECT COUNT(*) FROM Orders WHERE CAST(strftime('%Y', OrderDate) AS INTEGER) = 2024",
                          db_path)
    assert wrong.rows == [[0]] and fixed.rows[0][0] > 0


def test_the_error_suggests_a_working_comparison(catalog: SchemaCatalog, db_path: Path) -> None:
    report = validate_sql(TEXT_VS_NUMBER[0][1], catalog, db_path=db_path)
    assert any("CAST(STRFTIME('%Y', OrderDate) AS INTEGER) = 2024" in error for error in report.errors), report.errors
    dated = validate_sql("SELECT * FROM Orders WHERE date(OrderDate) > 2024", catalog, db_path=db_path)
    assert any("'2024-01-01'" in error for error in dated.errors), dated.errors


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM Orders WHERE strftime('%Y', OrderDate) = '2024'",
        "SELECT * FROM Orders WHERE CAST(strftime('%Y', OrderDate) AS INTEGER) = 2024",
        "SELECT * FROM Orders WHERE CAST(strftime('%m', OrderDate) AS INTEGER) BETWEEN 1 AND 3",
        "SELECT * FROM Orders WHERE strftime('%Y', OrderDate) + 0 = 2024",
        "SELECT * FROM Orders WHERE strftime('%m', OrderDate) IN ('01', '02', '03')",
        "SELECT * FROM Orders WHERE julianday(ShippedDate) - julianday(OrderDate) > 3",
        "SELECT * FROM Products WHERE length(ProductName) > 20",
        "SELECT * FROM Employees WHERE HireDate >= '2024-01-01'",
        "SELECT * FROM Orders WHERE strftime('%Y', OrderDate) IN (SELECT strftime('%Y', HireDate) FROM Employees)",
    ],
)
def test_matching_types_stay_valid(catalog: SchemaCatalog, db_path: Path, sql: str) -> None:
    report = validate_sql(sql, catalog, db_path=db_path)
    assert report.valid, report.errors


# ---- comma joins: the normalized SQL must keep the query planner's freedom ------------------------------


def test_comma_joins_are_not_rewritten_as_cross_join(catalog: SchemaCatalog, db_path: Path) -> None:
    """sqlglot reads ``FROM a, b`` as a CROSS JOIN; in SQLite that keyword also pins the join order.

    The normalized SQL is what runs, so writing CROSS JOIN back made SQLite scan 5,063 order lines instead of
    60 products here, and the optimizer (which reads the normalized SQL) never saw the comma join.
    """
    import sqlite3

    from app.sql.dialects import format_sql, transpile
    from app.sql.optimizer import analyze_query

    sql = ("SELECT SUM(oi.Quantity) FROM OrderItems oi, Products p "
           "WHERE p.ProductID = oi.ProductID AND p.Category = 'Software'")
    report = validate_sql(sql, catalog, db_path=db_path)
    assert report.valid and "CROSS JOIN" not in report.sql and ", Products AS p" in report.sql

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        plans = [[row[3] for row in conn.execute(f"EXPLAIN QUERY PLAN {query}")] for query in (sql, report.sql)]
    finally:
        conn.close()
    assert plans[0] == plans[1], plans
    assert "CROSS JOIN" not in format_sql(sql)
    assert any("comma-separated" in s.message for s in analyze_query(report.sql, catalog).suggestions)
    # PostgreSQL and MySQL keep the explicit form: there a comma binds looser than JOIN
    assert "CROSS JOIN" in transpile(sql, write="postgres", pretty=False)
