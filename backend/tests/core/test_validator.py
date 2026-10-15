"""SQL validator: the seven checks, actionable error messages and the read-only helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.catalog import SchemaCatalog
from app.sql.validator import CHECK_LABELS, ValidationReport, is_read_only, strip_sql_fences, validate_sql

VALID_QUERIES = [
    ("brief_example", "SELECT * FROM Employees WHERE HireDate >= '2024-01-01';", "sqlite"),
    ("filter_text", "SELECT FirstName, LastName FROM Customers WHERE State = 'California'", "sqlite"),
    ("inner_join", "SELECT c.FirstName, o.OrderID FROM Customers c JOIN Orders o ON o.CustomerID = c.CustomerID",
     "sqlite"),
    ("three_way_join", "SELECT p.Category, SUM(oi.Quantity * oi.UnitPrice * (1 - oi.Discount)) AS revenue "
                       "FROM OrderItems oi JOIN Products p ON p.ProductID = oi.ProductID "
                       "JOIN Orders o ON o.OrderID = oi.OrderID GROUP BY p.Category ORDER BY revenue DESC", "sqlite"),
    ("aggregate_having", "SELECT CustomerID, COUNT(*) AS n FROM Orders GROUP BY CustomerID HAVING COUNT(*) > 10",
     "sqlite"),
    ("count_star", "SELECT COUNT(*) FROM Orders", "sqlite"),
    ("cte", "WITH t AS (SELECT CustomerID, SUM(TotalAmount) AS total FROM Orders GROUP BY CustomerID) "
            "SELECT c.FirstName, t.total FROM Customers c JOIN t ON t.CustomerID = c.CustomerID "
            "ORDER BY t.total DESC LIMIT 5", "sqlite"),
    ("recursive_cte", "WITH RECURSIVE chain(EmployeeID, depth) AS (SELECT EmployeeID, 0 FROM Employees "
                      "WHERE ManagerID IS NULL UNION ALL SELECT e.EmployeeID, c.depth + 1 FROM Employees e "
                      "JOIN chain c ON e.ManagerID = c.EmployeeID) SELECT depth, COUNT(*) FROM chain GROUP BY depth",
     "sqlite"),
    ("window_function", "SELECT EmployeeID, RANK() OVER (PARTITION BY DepartmentID ORDER BY Salary DESC) AS r "
                        "FROM Employees", "sqlite"),
    ("scalar_subquery", "SELECT FirstName FROM Employees WHERE Salary > (SELECT AVG(Salary) FROM Employees)",
     "sqlite"),
    ("correlated_subquery", "SELECT e.FirstName FROM Employees e WHERE e.Salary > (SELECT AVG(Salary) "
                            "FROM Employees WHERE DepartmentID = e.DepartmentID)", "sqlite"),
    ("exists", "SELECT o.OrderID FROM Orders o WHERE EXISTS (SELECT 1 FROM Payments p "
               "WHERE p.OrderID = o.OrderID AND PaymentMethod = 'PayPal')", "sqlite"),
    ("in_subquery", "SELECT * FROM Customers WHERE CustomerID IN (SELECT CustomerID FROM Orders)", "sqlite"),
    ("union", "SELECT FirstName FROM Customers UNION SELECT FirstName FROM Employees ORDER BY FirstName", "sqlite"),
    ("case_expression", "SELECT CASE WHEN Salary > 100000 THEN 'high' ELSE 'low' END AS band, COUNT(*) "
                        "FROM Employees GROUP BY band", "sqlite"),
    ("date_functions", "SELECT strftime('%Y-%m', OrderDate) AS month, SUM(TotalAmount) FROM Orders "
                       "WHERE OrderDate >= date('now', '-365 days') GROUP BY month", "sqlite"),
    ("self_join", "SELECT e.FirstName, m.FirstName AS ManagerName FROM Employees e "
                  "LEFT JOIN Employees m ON e.ManagerID = m.EmployeeID", "sqlite"),
    ("alias_in_order_by", "SELECT State, COUNT(*) AS n FROM Customers GROUP BY State ORDER BY n DESC", "sqlite"),
    ("quoted_identifiers", 'SELECT "FirstName" FROM "Customers" WHERE "State" = \'California\'', "sqlite"),
    ("lower_case_names", "select firstname, lastname from customers where state = 'Texas'", "sqlite"),
    ("using_join", "SELECT OrderID, COUNT(*) FROM Orders JOIN OrderItems USING (OrderID) GROUP BY OrderID", "sqlite"),
    ("sibling_key_join", "SELECT oi.OrderID, p.PaymentID FROM OrderItems oi JOIN Payments p ON p.OrderID = oi.OrderID",
     "sqlite"),
    ("derived_table", "SELECT x.n FROM (SELECT COUNT(*) AS n FROM Orders) x", "sqlite"),
    ("deleted_literal", "SELECT * FROM Orders WHERE Status = 'Deleted' OR ShippingCity = 'DROP TABLE'", "sqlite"),
    ("postgres_input", 'SELECT "FirstName", "LastName" FROM "Customers" WHERE "State" = \'Texas\' LIMIT 5',
     "postgres"),
    ("mysql_input", "SELECT `FirstName`, DATE_FORMAT(`HireDate`, '%Y') AS y FROM `Employees`", "mysql"),
    ("fenced", "```sql\nSELECT COUNT(*) FROM Payments\n```", "sqlite"),
]

INVALID_QUERIES = [
    # (id, sql, failing check, message fragment, destructive)
    ("empty", "   ", "syntax", "empty", False),
    ("syntax_error", "SELECT * FROM Orders WHERE", "syntax", "incomplete WHERE clause", False),
    ("misspelled_keyword", "SELEC * FROM Orders", "syntax", "must start with SELECT", False),
    ("top_n", "SELECT TOP 5 * FROM Orders", "syntax", "LIMIT", False),
    ("unterminated_string", "SELECT * FROM Orders WHERE Status = 'Shipped", "syntax", "unterminated", False),
    ("empty_select_list", "SELECT FROM Orders", "syntax", "SELECT list is empty", False),
    ("two_selects", "SELECT 1; SELECT 2", "single_statement", "2 statements", False),
    ("select_then_drop", "SELECT 1; DROP TABLE Orders", "read_only", "DROP", True),
    ("delete", "DELETE FROM Orders WHERE OrderID = 1", "read_only", "DELETE", True),
    ("update", "UPDATE Employees SET Salary = Salary * 1.1", "read_only", "UPDATE", True),
    ("insert", "INSERT INTO Departments VALUES (9, 'Legal', 'Boston', 1)", "read_only", "INSERT", True),
    ("drop", "DROP TABLE Customers", "read_only", "DROP", True),
    ("alter", "ALTER TABLE Orders ADD COLUMN Notes TEXT", "read_only", "ALTER", True),
    ("truncate", "TRUNCATE TABLE Orders", "read_only", "TRUNCATE", True),
    ("create", "CREATE TABLE Backup AS SELECT * FROM Orders", "read_only", "CREATE", True),
    ("replace_into", "REPLACE INTO Departments VALUES (1, 'X', 'Y', 1)", "read_only", "REPLACE INTO", True),
    ("cte_wrapped_delete", "WITH x AS (SELECT 1) DELETE FROM Orders", "read_only", "DELETE", True),
    ("select_into", "SELECT * INTO Backup FROM Orders", "read_only", "SELECT INTO", True),
    ("pragma", "PRAGMA table_info(Orders)", "read_only", "PRAGMA", True),
    ("attach", "ATTACH DATABASE '/tmp/other.db' AS other", "read_only", "ATTACH", True),
    ("vacuum", "VACUUM", "read_only", "VACUUM", True),
    ("grant", "GRANT SELECT ON Orders TO bob", "read_only", "GRANT", True),
    ("load_extension", "SELECT load_extension('/tmp/evil.so')", "read_only", "load_extension", True),
    ("sqlite_master", "SELECT name, sql FROM sqlite_master", "read_only", "sqlite_master", False),
    ("pragma_function", "SELECT * FROM pragma_table_info('Orders')", "read_only", "pragma_table_info", False),
    ("explain", "EXPLAIN SELECT 1", "read_only", "EXPLAIN", False),
    ("unknown_table", "SELECT * FROM Customer", "tables", "Did you mean 'Customers'?", False),
    ("unknown_table_synonym", "SELECT * FROM Clients", "tables", "Did you mean 'Customers'?", False),
    ("unknown_schema", "SELECT * FROM temp.Orders", "tables", "Unknown schema 'temp'", False),
    ("unknown_column", "SELECT Region FROM Customers", "columns", "Did you mean 'State'?", False),
    ("snake_case_column", "SELECT hire_date FROM Employees", "columns", "Did you mean 'HireDate'?", False),
    ("ambiguous_column", "SELECT CustomerID FROM Customers c JOIN Orders o ON o.CustomerID = c.CustomerID",
     "columns", "Ambiguous column 'CustomerID'", False),
    ("table_name_instead_of_alias", "SELECT Orders.OrderID FROM Orders o", "columns", "aliased as 'o'", False),
    ("unknown_alias", "SELECT x.OrderID FROM Orders o", "columns", "Unknown table or alias 'x'", False),
    ("double_quoted_value", 'SELECT COUNT(*) FROM Orders WHERE Status = "Shipped"', "columns", "single quotes",
     False),
    ("derived_column", "SELECT t.missing FROM (SELECT SUM(TotalAmount) AS total FROM Orders) t", "columns",
     "Its columns are: total", False),
    ("bad_using", "SELECT * FROM Orders o JOIN Customers c USING (Foo)", "columns", "USING (Foo)", False),
    ("wrong_key_join", "SELECT o.OrderID, p.ProductName FROM Orders o JOIN Products p ON o.OrderID = p.ProductID",
     "joins", "Valid relationships for these tables", False),
    ("wrong_self_join", "SELECT e.FirstName FROM Employees e JOIN Employees m ON e.EmployeeID = m.DepartmentID",
     "joins", "Invalid join", False),
    ("dry_run_function", "SELECT * FROM Orders WHERE OrderDate > NOW()", "executable", "date('now')", False),
    ("dry_run_date_trunc", "SELECT DATE_TRUNC('month', OrderDate) FROM Orders", "executable", "DATE_TRUNC", False),
]


def checks_by_name(report: ValidationReport) -> dict[str, bool]:
    return {check.name: check.passed for check in report.checks}


@pytest.mark.parametrize(("sql", "dialect"), [(sql, dialect) for _, sql, dialect in VALID_QUERIES],
                         ids=[case[0] for case in VALID_QUERIES])
def test_valid_queries_pass(catalog: SchemaCatalog, db_path: Path, sql: str, dialect: str) -> None:
    report = validate_sql(sql, catalog, dialect=dialect, db_path=db_path)
    assert report.valid, report.errors
    assert all(checks_by_name(report).values()), report.checks
    assert not report.destructive
    assert report.tables


@pytest.mark.parametrize(
    ("sql", "check", "fragment", "destructive"),
    [case[1:] for case in INVALID_QUERIES],
    ids=[case[0] for case in INVALID_QUERIES],
)
def test_invalid_queries_fail_the_right_check(
    catalog: SchemaCatalog, db_path: Path, sql: str, check: str, fragment: str, destructive: bool
) -> None:
    report = validate_sql(sql, catalog, db_path=db_path)
    assert not report.valid
    assert checks_by_name(report)[check] is False
    assert any(fragment in error for error in report.errors), report.errors
    assert report.destructive is destructive


def test_all_seven_checks_in_order(catalog: SchemaCatalog, db_path: Path) -> None:
    report = validate_sql("SELECT 1", catalog, db_path=db_path)
    assert [(check.name, check.label) for check in report.checks] == list(CHECK_LABELS.items())
    assert list(CHECK_LABELS) == ["syntax", "single_statement", "read_only", "tables", "columns", "joins",
                                  "executable"]


def test_checks_after_a_fatal_failure_are_skipped(catalog: SchemaCatalog, db_path: Path) -> None:
    report = validate_sql("SELECT * FROM Customer", catalog, db_path=db_path)
    by_name = {check.name: check for check in report.checks}
    assert by_name["syntax"].passed and by_name["read_only"].passed
    for name in ("columns", "joins", "executable"):
        assert not by_name[name].passed
        assert by_name[name].message.startswith("Skipped: ")


def test_api_example_report(catalog: SchemaCatalog, db_path: Path) -> None:
    report = validate_sql("SELECT * FROM Customers WHERE State = 'California';", catalog, db_path=db_path)
    payload = report.to_dict()
    assert set(payload) == {"valid", "checks", "errors", "warnings", "sql", "tables", "columns", "destructive"}
    assert payload["valid"] is True
    assert payload["sql"] == "SELECT * FROM Customers WHERE State = 'California'"
    assert payload["tables"] == ["Customers"]
    assert payload["columns"] == ["Customers.State"]
    messages = {check["name"]: check["message"] for check in payload["checks"]}
    assert messages["read_only"] == "SELECT only."
    assert messages["tables"] == "Customers"
    assert messages["columns"] == "All 1 referenced columns exist."
    assert messages["joins"] == "No joins."
    assert messages["executable"] == "Query plan compiled."
    for check in payload["checks"]:
        assert set(check) == {"name", "label", "passed", "message"}


def test_columns_are_reported_with_canonical_names(catalog: SchemaCatalog, db_path: Path) -> None:
    report = validate_sql("select c.firstname from customers c join orders o on o.customerid = c.customerid "
                          "where o.status = 'Shipped'", catalog, db_path=db_path)
    assert report.valid
    assert report.tables == ["Customers", "Orders"]
    assert set(report.columns) == {"Customers.FirstName", "Orders.CustomerID", "Customers.CustomerID",
                                   "Orders.Status"}


def test_unknown_column_message_lists_available_columns(catalog: SchemaCatalog) -> None:
    report = validate_sql("SELECT Region FROM Customers", catalog)
    assert report.errors[0].startswith("Unknown column 'Region' in table Customers. Available columns: CustomerID,")


def test_non_fk_join_is_a_warning(catalog: SchemaCatalog, db_path: Path) -> None:
    report = validate_sql("SELECT o.OrderID, c.CustomerID FROM Orders o JOIN Customers c "
                          "ON o.ShippingState = c.State", catalog, db_path=db_path)
    assert report.valid
    assert any("does not follow a foreign key" in warning for warning in report.warnings)


@pytest.mark.parametrize("sql", ["SELECT * FROM Orders o CROSS JOIN Products p", "SELECT * FROM Orders, Products",
                                 "SELECT * FROM Orders o JOIN Products p ON 1 = 1"])
def test_cartesian_products_are_warnings(catalog: SchemaCatalog, db_path: Path, sql: str) -> None:
    report = validate_sql(sql, catalog, db_path=db_path)
    assert report.valid
    assert report.warnings


def test_comma_join_with_where_predicate_is_checked(catalog: SchemaCatalog, db_path: Path) -> None:
    ok = validate_sql("SELECT c.State FROM Orders o, Customers c WHERE o.CustomerID = c.CustomerID", catalog,
                      db_path=db_path)
    assert ok.valid and not ok.warnings
    wrong = validate_sql("SELECT * FROM Orders o, Products p WHERE o.OrderID = p.ProductID", catalog, db_path=db_path)
    assert not wrong.valid and "Invalid join" in wrong.errors[0]


def test_without_database_the_dry_run_is_skipped(catalog: SchemaCatalog) -> None:
    report = validate_sql("SELECT * FROM Orders", catalog)
    executable = report.checks[-1]
    assert report.valid and executable.passed and executable.message == "Skipped (no database)"


@pytest.mark.parametrize(
    ("sql", "dialect", "expected"),
    [
        ("SELECT * FROM Orders;", "sqlite", "SELECT * FROM Orders"),
        ("```sql\nselect * from orders\n```", "sqlite", "SELECT * FROM orders"),
        ("SELECT `FirstName` FROM `Customers` LIMIT 3", "mysql", 'SELECT "FirstName" FROM "Customers" LIMIT 3'),
        ("SELECT * FROM Orders WHERE ShippedDate IS NOT NULL -- note", "sqlite",
         "SELECT * FROM Orders WHERE ShippedDate IS NOT NULL"),
    ],
)
def test_report_sql_is_normalized_sqlite(catalog: SchemaCatalog, sql: str, dialect: str, expected: str) -> None:
    assert validate_sql(sql, catalog, dialect=dialect).sql == expected


def test_unparseable_sql_is_returned_stripped(catalog: SchemaCatalog) -> None:
    assert validate_sql("  SELECT * FROM Orders WHERE ;  ", catalog).sql == "SELECT * FROM Orders WHERE"


def test_destructive_keywords_are_flagged_even_when_unparseable(catalog: SchemaCatalog) -> None:
    report = validate_sql("DELETE Orders WHERE (", catalog)
    assert report.destructive and not report.valid


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM Orders", True),
        ("```sql\nSELECT 1;\n```", True),
        ("WITH t AS (SELECT 1 AS x) SELECT x FROM t", True),
        ("SELECT 'DELETE FROM Orders' AS text", True),
        ("DELETE FROM Orders", False),
        ("SELECT 1; DROP TABLE Orders", False),
        ("SELECT 1; SELECT 2", False),
        ("PRAGMA table_info(Orders)", False),
        ("SELECT * INTO t FROM Orders", False),
        ("SELECT load_extension('x')", False),
        ("SELEC * FROM Orders", False),
        ("", False),
    ],
)
def test_is_read_only(sql: str, expected: bool) -> None:
    assert is_read_only(sql) is expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("```sql\nSELECT 1;\n```", "SELECT 1"),
        ("```\nSELECT 1\n```", "SELECT 1"),
        ("```SQL SELECT 2```", "SELECT 2"),
        ("SQL: SELECT 3;;", "SELECT 3"),
        ("  SELECT 4 ;  \n", "SELECT 4"),
        ("Here you go:\n```sqlite\nSELECT 5\n```\nDone.", "SELECT 5"),
        ("```SELECT 6```", "SELECT 6"),
        ("```\nSELECT 7", "SELECT 7"),
        ("SELECT 8", "SELECT 8"),
    ],
)
def test_strip_sql_fences(text: str, expected: str) -> None:
    assert strip_sql_fences(text) == expected


def test_pathological_nesting_fails_closed(catalog: SchemaCatalog, db_path: Path) -> None:
    deep = "SELECT " + "(" * 3000 + "1" + ")" * 3000
    report = validate_sql(deep, catalog, db_path=db_path)
    assert not report.valid and "nested too deeply" in report.errors[0]
    assert is_read_only(deep) is False
    reasonable = "SELECT " + "(" * 25 + "1" + ")" * 25
    assert validate_sql(reasonable, catalog, db_path=db_path).valid


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT FirstName FROM Employees WHERE HireDate > 2024-01-01", ["'2024-01-01'"]),
        ("SELECT * FROM Orders WHERE OrderDate BETWEEN 2024-1-1 AND 2024-12-31", ["'2024-01-01'", "'2024-12-31'"]),
        ("SELECT * FROM Orders WHERE strftime('%Y-%m', OrderDate) = 2025-03", ["'2025-03'"]),
    ],
)
def test_unquoted_dates_are_errors_but_not_fatal(
    catalog: SchemaCatalog, db_path: Path, sql: str, expected: list[str]
) -> None:
    report = validate_sql(sql, catalog, db_path=db_path)
    assert not report.valid
    assert len(report.errors) == len(expected)
    for error, literal in zip(report.errors, expected, strict=True):
        assert error.startswith("Unquoted date ") and "arithmetic" in error and error.endswith(f"{literal}.")
    failed = [check.name for check in report.checks if not check.passed]
    assert failed == ["syntax"]  # the remaining checks still ran (no "Skipped")
    assert report.checks[-1].message == "Query plan compiled."


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM Orders WHERE OrderDate >= '2024-01-01'",
        "SELECT 2024 - 01 - 01, OrderID-1 FROM Orders",
        "SELECT * FROM Orders WHERE ShippingCity = 'x 2024-01-01' -- 2024-01-01",
        "SELECT * FROM Orders WHERE OrderID > 2024-1",
        "SELECT * FROM Orders LIMIT 2000-13",
    ],
)
def test_quoted_dates_and_real_arithmetic_are_not_flagged(catalog: SchemaCatalog, db_path: Path, sql: str) -> None:
    report = validate_sql(sql, catalog, db_path=db_path)
    assert report.valid, report.errors
