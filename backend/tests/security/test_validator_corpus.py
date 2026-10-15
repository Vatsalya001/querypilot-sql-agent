"""Validator corpus: no false rejections of valid analytic SQL, no false acceptance of bad or unsafe SQL."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.db.catalog import SchemaCatalog
from app.db.database import connect_readonly
from app.sql.executor import UnsafeQueryError, execute_query
from app.sql.validator import is_read_only, validate_sql

VALID = [
    "SELECT * FROM Employees WHERE HireDate >= '2024-01-01'",
    "SELECT COUNT(*) FROM Orders",
    "SELECT Status, COUNT(*) AS n FROM Orders GROUP BY Status ORDER BY n DESC",
    "SELECT e.FirstName, m.FirstName AS ManagerFirst FROM Employees e LEFT JOIN Employees m "
    "ON e.ManagerID = m.EmployeeID",
    "SELECT d.DepartmentName, COUNT(e.EmployeeID) AS headcount FROM Departments d "
    "LEFT JOIN Employees e ON e.DepartmentID = d.DepartmentID GROUP BY d.DepartmentName",
    "SELECT c.CustomerID FROM Customers c LEFT JOIN Orders o ON o.CustomerID = c.CustomerID WHERE o.OrderID IS NULL",
    "SELECT ProductName, RANK() OVER (ORDER BY UnitPrice DESC) AS price_rank FROM Products",
    "SELECT ProductName, ROW_NUMBER() OVER (PARTITION BY Category ORDER BY UnitPrice DESC) AS rn FROM Products",
    "SELECT OrderID, SUM(TotalAmount) OVER (ORDER BY OrderDate ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) "
    "AS running FROM Orders",
    "SELECT CASE WHEN Salary > 100000 THEN 'high' WHEN Salary > 60000 THEN 'mid' ELSE 'low' END AS band, "
    "COUNT(*) FROM Employees GROUP BY band",
    "SELECT COALESCE(ShippedDate, 'not shipped') AS shipped FROM Orders",
    "SELECT strftime('%Y-%m', OrderDate) AS month, SUM(TotalAmount) AS revenue FROM Orders GROUP BY month",
    "SELECT date(OrderDate, '+7 days') AS due FROM Orders",
    "SELECT julianday(ShippedDate) - julianday(OrderDate) AS days_to_ship FROM Orders WHERE ShippedDate IS NOT NULL",
    "SELECT State FROM Customers UNION SELECT ShippingState FROM Orders",
    "SELECT City FROM Customers UNION ALL SELECT Location FROM Departments ORDER BY 1",
    "SELECT FirstName FROM Customers INTERSECT SELECT FirstName FROM Employees",
    "SELECT FirstName FROM Customers EXCEPT SELECT FirstName FROM Employees",
    "SELECT * FROM Products WHERE UnitPrice > (SELECT AVG(UnitPrice) FROM Products)",
    "SELECT * FROM Customers WHERE CustomerID IN (SELECT CustomerID FROM Orders WHERE TotalAmount > 1000)",
    "SELECT * FROM Customers c WHERE EXISTS (SELECT 1 FROM Orders o WHERE o.CustomerID = c.CustomerID "
    "AND o.Status = 'Returned')",
    "SELECT * FROM Customers c WHERE NOT EXISTS (SELECT 1 FROM Orders o WHERE o.CustomerID = c.CustomerID)",
    "SELECT FirstName || ' ' || LastName AS full_name FROM Employees ORDER BY full_name",
    "SELECT Category, AVG(UnitPrice) AS avg_price FROM Products GROUP BY Category HAVING AVG(UnitPrice) > 100",
    "SELECT Category, COUNT(*) AS n FROM Products GROUP BY Category HAVING n > 5",
    "SELECT * FROM Orders WHERE Status IN ('Shipped', 'Delivered') AND OrderDate BETWEEN '2025-01-01' AND '2025-12-31'",
    "SELECT * FROM Customers WHERE Email LIKE '%@example.com'",
    "SELECT CAST(Salary AS INTEGER) AS salary_int FROM Employees",
    "SELECT * FROM Employees WHERE ManagerID IS NULL",
    "SELECT * FROM Orders WHERE ShippedDate IS NULL AND Status <> 'Cancelled'",
    "WITH revenue AS (SELECT CustomerID, SUM(TotalAmount) AS total FROM Orders GROUP BY CustomerID) "
    "SELECT c.FirstName, r.total FROM revenue r JOIN Customers c ON c.CustomerID = r.CustomerID "
    "ORDER BY r.total DESC LIMIT 10",
    "WITH RECURSIVE chain(EmployeeID, ManagerID, depth) AS (SELECT EmployeeID, ManagerID, 0 FROM Employees "
    "WHERE ManagerID IS NULL UNION ALL SELECT e.EmployeeID, e.ManagerID, c.depth + 1 FROM Employees e "
    "JOIN chain c ON e.ManagerID = c.EmployeeID) SELECT depth, COUNT(*) FROM chain GROUP BY depth",
    "SELECT p.ProductName, SUM(oi.Quantity * oi.UnitPrice * (1 - oi.Discount)) AS revenue FROM OrderItems oi "
    "JOIN Products p ON p.ProductID = oi.ProductID JOIN Orders o ON o.OrderID = oi.OrderID "
    "WHERE o.OrderDate >= '2025-01-01' AND o.OrderDate < '2026-01-01' GROUP BY p.ProductName "
    "ORDER BY revenue DESC LIMIT 5",
    "SELECT pm.PaymentMethod, SUM(pm.Amount) FROM Payments pm GROUP BY pm.PaymentMethod",
    "SELECT o.OrderID, p.Amount FROM Orders o JOIN Payments p ON p.OrderID = o.OrderID WHERE p.Amount <> o.TotalAmount",
    "SELECT o.OrderID, e.FirstName AS rep FROM Orders o LEFT JOIN Employees e ON e.EmployeeID = o.EmployeeID",
    "SELECT * FROM (SELECT CustomerID, COUNT(*) AS n FROM Orders GROUP BY CustomerID) t WHERE t.n > 10",
    "SELECT c.State, COUNT(DISTINCT c.CustomerID) AS customers FROM Customers c JOIN Orders o USING (CustomerID) "
    "GROUP BY c.State",
    "SELECT ProductName, (SELECT COUNT(*) FROM OrderItems oi WHERE oi.ProductID = p.ProductID) AS times_ordered "
    "FROM Products p",
    "SELECT MAX(Salary), MIN(Salary), AVG(Salary), SUM(Salary), COUNT(Salary) FROM Employees",
    "SELECT DISTINCT JobTitle FROM Employees ORDER BY JobTitle",
    "SELECT ROUND(AVG(TotalAmount), 2) AS aov FROM Orders WHERE Status != 'Cancelled'",
    "SELECT IFNULL(ManagerID, 0), NULLIF(IsActive, 0) FROM Employees",
    "SELECT upper(LastName), lower(Email), length(FirstName), substr(Phone, 1, 3), trim(City), "
    "trim(City, ' .'), replace(State, ' ', '_') FROM Customers",
    "SELECT * FROM Orders ORDER BY OrderDate DESC LIMIT 10 OFFSET 20",
    "SELECT Segment, COUNT(*) FROM Customers GROUP BY 1 ORDER BY 2 DESC",
    "SELECT e.FirstName, d.DepartmentName FROM Employees AS e INNER JOIN Departments AS d "
    "ON d.DepartmentID = e.DepartmentID WHERE d.Location = 'New York'",
    "SELECT \"FirstName\", [LastName], `Email` FROM Customers",
    "select firstname, lastname from customers where state = 'California'",
    "SELECT c.* FROM Customers c WHERE c.State = 'Texas'",
    "SELECT o.*, c.FirstName FROM Orders o JOIN Customers c ON c.CustomerID = o.CustomerID",
    "SELECT LAG(TotalAmount) OVER w, LEAD(TotalAmount) OVER w FROM Orders WINDOW w AS (ORDER BY OrderDate)",
    "SELECT NTILE(4) OVER (ORDER BY Salary) AS quartile, FirstName FROM Employees",
    "SELECT DepartmentID, AVG(Salary) FILTER (WHERE IsActive = 1) FROM Employees GROUP BY DepartmentID",
    "SELECT group_concat(ProductName, ', ') FROM Products WHERE Category = 'Software'",
    "SELECT COUNT(*) FROM Orders WHERE OrderDate >= date('now', '-30 days')",
    "SELECT iif(IsDiscontinued = 1, 'yes', 'no') AS discontinued FROM Products",
    "SELECT * FROM Customers WHERE State NOT IN ('California', 'Texas')",
    "SELECT * FROM Products WHERE NOT IsDiscontinued",
    "SELECT 1 + 1 AS two",
    "SELECT x.value FROM json_each('[1,2]') AS x",
    "SELECT o.OrderID, oi.ProductID FROM Orders o, OrderItems oi WHERE oi.OrderID = o.OrderID",
    "SELECT e.EmployeeID, COUNT(o.OrderID) AS orders_handled FROM Employees e LEFT JOIN Orders o "
    "ON o.EmployeeID = e.EmployeeID GROUP BY e.EmployeeID ORDER BY orders_handled DESC",
    "SELECT c.Segment, AVG(o.TotalAmount) FROM Customers c JOIN Orders o ON c.CustomerID = o.CustomerID "
    "WHERE o.OrderDate LIKE '2025%' GROUP BY c.Segment",
    "SELECT * FROM Orders ORDER BY TotalAmount DESC NULLS LAST LIMIT 3",
    "SELECT FirstName FROM Employees e WHERE Salary > (SELECT AVG(Salary) FROM Employees "
    "WHERE DepartmentID = e.DepartmentID)",
    "SELECT d.DepartmentName, e.FirstName FROM Departments d JOIN Employees e "
    "ON e.DepartmentID = d.DepartmentID AND e.IsActive = 1",
    "SELECT oi.OrderID, pay.PaymentID FROM OrderItems oi JOIN Payments pay ON pay.OrderID = oi.OrderID",
    "SELECT Status, SUM(CASE WHEN TotalAmount > 500 THEN 1 ELSE 0 END) AS big FROM Orders GROUP BY Status",
    "SELECT ProductName AS name FROM Products ORDER BY lower(name)",
    "SELECT * FROM Products WHERE UnitPrice > 100 -- expensive ones",
    "/* top customers */ SELECT CustomerID FROM Orders GROUP BY CustomerID ORDER BY SUM(TotalAmount) DESC LIMIT 5;",
    "```sql\nSELECT * FROM Departments;\n```",
    "WITH a AS (SELECT 1 AS x), b AS (SELECT x + 1 AS y FROM a) SELECT y FROM b",
    "SELECT * FROM Orders WHERE Status = 'Deleted' OR ShippingCity = 'drop-off' OR ShippingState = 'update pending'",
    "SELECT 'DELETE FROM Orders' AS note",
    "SELECT * FROM Customers WHERE CreatedAt > '2024-06-01' AND Country = 'USA'",
    "SELECT COUNT(*) FILTER (WHERE Status = 'Cancelled') * 1.0 / COUNT(*) AS cancel_rate FROM Orders",
    "SELECT * FROM Orders WHERE OrderID BETWEEN 10 AND 20 AND CustomerID IN (1, 2, 3)",
    "SELECT * FROM (VALUES (1), (2))",
    "WITH x AS MATERIALIZED (SELECT 1 AS a) SELECT * FROM x",
    "SELECT * FROM main.Orders",
    "SELECT * FROM Orders WHERE Status = 'Shipped' COLLATE NOCASE",
    "SELECT * FROM Orders WHERE Status GLOB 'Ship*'",
    "SELECT * FROM Orders WHERE Status = 'it''s'",
]

INVALID = [
    ("SELECT Region FROM Customers", "columns"),
    ("SELECT * FROM Customers ORDER BY Region", "columns"),
    ("SELECT State, COUNT(*) FROM Customers GROUP BY Region", "columns"),
    ("SELECT State FROM Customers GROUP BY State HAVING SUM(Revenue) > 1", "columns"),
    ("SELECT * FROM Customers WHERE CustomerID IN (SELECT CustID FROM Orders)", "columns"),
    ("WITH t AS (SELECT CustomerID FROM Orders) SELECT t.Total FROM t", "columns"),
    ("SELECT x.Foo FROM (SELECT CustomerID FROM Orders) x", "columns"),
    ("SELECT CustomerID FROM Customers c JOIN Orders o ON o.CustomerID = c.CustomerID", "columns"),
    ("SELECT c.State FROM Customers AS cust", "columns"),
    ("SELECT * FROM Orders WHERE EXISTS (SELECT 1 FROM Payments p WHERE p.Bogus = Orders.OrderID)", "columns"),
    ("SELECT * FROM Customer", "tables"),
    ("SELECT * FROM Orders o JOIN Shipments s ON s.OrderID = o.OrderID", "tables"),
    ("SELECT * FROM Orders o JOIN Products p ON o.OrderID = p.ProductID", "joins"),
    ("SELECT * FROM Customers c JOIN Orders o ON o.EmployeeID = c.CustomerID", "joins"),
    ("SELECT * FROM Orders o, Customers c WHERE o.OrderID = c.CustomerID", "joins"),
    ("SELECT * FROM Employees WHERE HireDate > 2024-01-01", "syntax"),
    ("SELECT TOP 5 * FROM Orders", "syntax"),
    ("SELECT YEAR(OrderDate) FROM Orders", "executable"),
]

# Write, admin and exfiltration attempts: never valid, never executable, refused by the executor.
UNSAFE = [
    ("SELECT 1; DROP TABLE Orders", True),
    ("WITH x AS (SELECT 1) DELETE FROM Orders", True),
    ("WITH x AS (DELETE FROM Orders RETURNING *) SELECT * FROM x", True),
    ("SELECT * INTO Backup FROM Orders", True),
    ("REPLACE INTO Departments VALUES (1, 'a', 'b', 1)", True),
    ("INSERT OR REPLACE INTO Departments VALUES (1, 'a', 'b', 1)", True),
    ("PRAGMA table_info(Orders)", True),
    ("ATTACH DATABASE '/tmp/other.db' AS other", True),
    ("VACUUM INTO '/tmp/copy.db'", True),
    ("SELECT load_extension('x')", True),
    ("SELECT readfile('/etc/hosts')", True),
    ("CREATE TEMP TABLE t AS SELECT * FROM Orders", True),
    ("SELECT * FROM sqlite_master", False),
    ("SELECT * FROM sqlite_schema", False),
    ("SELECT * FROM pragma_table_info('Orders')", False),
    ("SELECT sqlite_version()", False),
    ("SELECT sqlite_compileoption_get(0)", False),
    ("SELECT trim(Email, Phone || City) FROM Customers", False),
]


@pytest.mark.parametrize("sql", VALID)
def test_valid_queries_are_accepted_and_run(sql: str, catalog: SchemaCatalog, db_path: Path) -> None:
    report = validate_sql(sql, catalog, db_path=db_path)
    assert report.valid, report.errors
    result = execute_query(report.sql, db_path, max_rows=5)
    assert result.error is None


def test_the_corpus_is_large_enough() -> None:
    assert len(VALID) >= 40


@pytest.mark.parametrize(("sql", "check"), INVALID)
def test_invalid_queries_are_rejected_by_the_right_check(
    sql: str, check: str, catalog: SchemaCatalog, db_path: Path
) -> None:
    report = validate_sql(sql, catalog, db_path=db_path)
    assert not report.valid
    failed = [c.name for c in report.checks if not c.passed and not c.message.startswith("Skipped")]
    assert check in failed, report.errors


@pytest.mark.parametrize(("sql", "destructive"), UNSAFE)
def test_unsafe_sql_is_blocked_at_every_layer(sql: str, destructive: bool, catalog: SchemaCatalog,
                                              db_path: Path) -> None:
    report = validate_sql(sql, catalog, db_path=db_path)
    assert not report.valid
    assert report.destructive is destructive
    assert not is_read_only(sql)
    with pytest.raises(UnsafeQueryError):
        execute_query(sql, db_path)


@pytest.mark.parametrize("sql", ["SELECT sqlite_version()", "SELECT sqlite_source_id()"])
def test_the_database_layer_denies_system_functions_by_itself(sql: str, db_path: Path) -> None:
    conn = connect_readonly(db_path)
    try:
        with pytest.raises(sqlite3.DatabaseError):
            conn.execute(sql)
    finally:
        conn.close()


def test_unknown_table_functions_are_named_in_the_error(catalog: SchemaCatalog, db_path: Path) -> None:
    report = validate_sql("SELECT * FROM generate_series(1, 3)", catalog, db_path=db_path)
    assert not report.valid
    assert "generate_series" in report.errors[0].lower()
