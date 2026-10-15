"""Dialect helpers: names, transpilation and formatting."""

from __future__ import annotations

import pytest

from app.sql.dialects import SUPPORTED_DIALECTS, format_sql, normalize_dialect, transpile


@pytest.mark.parametrize(
    ("name", "expected"),
    [(None, "sqlite"), ("", "sqlite"), ("SQLite", "sqlite"), ("sqlite3", "sqlite"), ("postgresql", "postgres"),
     ("pg", "postgres"), ("Postgres", "postgres"), ("mysql", "mysql"), ("MariaDB", "mysql")],
)
def test_normalize_dialect(name: str | None, expected: str) -> None:
    assert normalize_dialect(name) == expected


def test_unknown_dialect_raises() -> None:
    with pytest.raises(ValueError, match="Unsupported SQL dialect"):
        normalize_dialect("oracle")
    assert SUPPORTED_DIALECTS == {"sqlite": "SQLite", "postgres": "PostgreSQL", "mysql": "MySQL"}


@pytest.mark.parametrize(
    ("sql", "dialect", "expected"),
    [
        ("SELECT strftime('%Y', OrderDate) AS y FROM Orders", "mysql", "SELECT DATE_FORMAT(OrderDate, '%Y') AS y FROM Orders"),
        ("SELECT * FROM Orders WHERE OrderDate >= date('now', '-30 days')", "postgres",
         "SELECT * FROM Orders WHERE OrderDate >= CURRENT_DATE - INTERVAL '30 DAY'"),
        ("SELECT * FROM Orders WHERE OrderDate >= date('now', '-30 days')", "mysql",
         "SELECT * FROM Orders WHERE OrderDate >= DATE_SUB(CURRENT_DATE, INTERVAL 30 DAY)"),
        ("SELECT julianday(ShippedDate) - julianday(OrderDate) AS d FROM Orders", "mysql",
         "SELECT DATEDIFF(ShippedDate, OrderDate) AS d FROM Orders"),
        ("SELECT FirstName || ' ' || LastName FROM Customers", "mysql",
         "SELECT CONCAT(FirstName, ' ', LastName) FROM Customers"),
        ("SELECT * FROM Customers WHERE Phone IS NOT NULL", "postgres",
         "SELECT * FROM Customers WHERE Phone IS NOT NULL"),
        # PostgreSQL has round(numeric, int) but no round(double precision, int)
        ("SELECT ROUND(AVG(Salary), 2) AS a, ROUND(AVG(Salary)) AS b FROM Employees", "postgres",
         "SELECT ROUND(CAST(AVG(Salary) AS DECIMAL), 2) AS a, ROUND(AVG(Salary)) AS b FROM Employees"),
        ("SELECT ROUND(CAST(Salary AS NUMERIC), 1) FROM Employees", "postgres",
         "SELECT ROUND(CAST(Salary AS DECIMAL), 1) FROM Employees"),
        ("SELECT ROUND(AVG(Salary), 2) AS a FROM Employees", "mysql", "SELECT ROUND(AVG(Salary), 2) AS a FROM Employees"),
    ],
)
def test_transpile(sql: str, dialect: str, expected: str) -> None:
    assert transpile(sql, write=dialect, pretty=False) == expected


@pytest.mark.parametrize("sql", ["SELECT printf('%.2f', Salary) FROM Employees", "SELECT * FROM t WHERE a GLOB 'x*'"])
def test_transpile_refuses_sqlite_only_functions(sql: str) -> None:
    with pytest.raises(ValueError, match="no automatic PostgreSQL equivalent"):
        transpile(sql, write="postgres")


@pytest.mark.parametrize("sql", ["SELECT * FROM (", "", "SELECT 'unterminated"])
def test_transpile_raises_value_error_on_bad_input(sql: str) -> None:
    with pytest.raises(ValueError):
        transpile(sql, write="postgres")


def test_format_sql() -> None:
    assert format_sql("select * from customers where state='California' and phone is not null") == (
        "SELECT\n  *\nFROM customers\nWHERE\n  state = 'California' AND phone IS NOT NULL"
    )
    assert format_sql("this is ( not sql") == "this is ( not sql"
    assert format_sql("SELECT 1", "postgres") == "SELECT\n  1"


def test_format_sql_keeps_short_parentheses_inline() -> None:
    formatted = format_sql(
        "SELECT ROUND(SUM(oi.Quantity * oi.UnitPrice * (1 - oi.Discount)), 2) AS Revenue FROM OrderItems oi "
        "WHERE (oi.Quantity > 1 OR oi.Discount = 0) AND oi.OrderID IN (SELECT OrderID FROM Orders)"
    )
    assert "ROUND(SUM(oi.Quantity * oi.UnitPrice * (1 - oi.Discount)), 2) AS Revenue" in formatted
    assert "\n  (oi.Quantity > 1 OR oi.Discount = 0)\n" in formatted
    assert "IN (\n    SELECT" in formatted  # subqueries keep the multi-line layout
    long_group = format_sql(f"SELECT 1 FROM t WHERE ({' OR '.join(f'column_number_{i} = {i}' for i in range(4))})")
    assert "(\n    column_number_0 = 0\n    OR column_number_1 = 1" in long_group


# ---- review regressions: translations that were silently wrong ----------------------------------------


@pytest.mark.parametrize(
    ("sql", "dialect", "expected"),
    [
        # date() applies every modifier in order; the second one used to be dropped without a trace
        ("SELECT * FROM Orders WHERE OrderDate >= date('now', '-1 month', 'start of month')", "postgres",
         "SELECT * FROM Orders WHERE OrderDate >= DATE_TRUNC('MONTH', CURRENT_DATE - INTERVAL '1 MONTH')"),
        ("SELECT * FROM Orders WHERE OrderDate >= date('now', 'start of month', '-1 month') "
         "AND OrderDate < date('now', 'start of month')", "postgres",
         "SELECT * FROM Orders WHERE OrderDate >= DATE_TRUNC('MONTH', CURRENT_DATE) - INTERVAL '1 MONTH' "
         "AND OrderDate < DATE_TRUNC('MONTH', CURRENT_DATE)"),
        ("SELECT * FROM Orders WHERE OrderDate >= date('now', '-1 year', '+1 day')", "mysql",
         "SELECT * FROM Orders WHERE OrderDate >= DATE_ADD(DATE_SUB(CURRENT_DATE, INTERVAL 1 YEAR), INTERVAL 1 DAY)"),
        ("SELECT date(OrderDate, 'start of month') AS m FROM Orders", "postgres",
         "SELECT DATE_TRUNC('MONTH', OrderDate) AS m FROM Orders"),
        # 'now' is not a date in MySQL: DATE_FORMAT('now', ...) and DATEDIFF('now', ...) return NULL
        ("SELECT * FROM Employees WHERE strftime('%Y', HireDate) = strftime('%Y', 'now')", "mysql",
         "SELECT * FROM Employees WHERE DATE_FORMAT(HireDate, '%Y') = DATE_FORMAT(CURRENT_TIMESTAMP(), '%Y')"),
        ("SELECT julianday('now') - julianday(HireDate) AS days FROM Employees", "mysql",
         "SELECT DATEDIFF(CURRENT_DATE, HireDate) AS days FROM Employees"),
        # MySQL has no FILTER clause on aggregates
        ("SELECT COUNT(*) FILTER (WHERE Status = 'Delivered') AS n, SUM(TotalAmount) FILTER (WHERE Status = "
         "'Returned') AS r FROM Orders", "mysql",
         "SELECT COUNT(CASE WHEN Status = 'Delivered' THEN 1 END) AS n, SUM(CASE WHEN Status = 'Returned' "
         "THEN TotalAmount END) AS r FROM Orders"),
        ("SELECT COUNT(*) FILTER (WHERE Status = 'Delivered') AS n FROM Orders", "postgres",
         "SELECT COUNT(*) FILTER(WHERE Status = 'Delivered') AS n FROM Orders"),
    ],
)
def test_transpile_keeps_the_sqlite_meaning(sql: str, dialect: str, expected: str) -> None:
    assert transpile(sql, write=dialect, pretty=False) == expected


@pytest.mark.parametrize(
    ("sql", "dialect"),
    [
        # modifiers without a portable equivalent must not be dropped silently
        ("SELECT * FROM Orders WHERE OrderDate >= date('now', '-7 days', 'weekday 1')", "postgres"),
        ("SELECT * FROM Orders WHERE OrderDate >= date('now', 'start of month', 'localtime')", "mysql"),
        # strftime tokens that TO_CHAR / DATE_FORMAT would print as something else
        ("SELECT strftime('%w', OrderDate) AS weekday, COUNT(*) FROM Orders GROUP BY weekday", "postgres"),
        ("SELECT strftime('%W', OrderDate) AS week FROM Orders", "postgres"),
        ("SELECT strftime('%u', OrderDate) AS weekday FROM Orders", "postgres"),
        ("SELECT strftime('%s', OrderDate) AS epoch FROM Orders", "mysql"),
        ("SELECT strftime('%u', OrderDate) AS weekday FROM Orders", "mysql"),
        ("SELECT strftime('%Y quarter', OrderDate) AS q FROM Orders", "postgres"),
    ],
)
def test_transpile_refuses_what_it_cannot_translate_exactly(sql: str, dialect: str) -> None:
    with pytest.raises(ValueError, match="no automatic"):
        transpile(sql, write=dialect)


@pytest.mark.parametrize(
    ("fmt", "postgres", "mysql"),
    [("%Y-%m", "YYYY-MM", "%Y-%m"), ("%Y-%m-%d %H:%M:%S", "YYYY-MM-DD HH24:MI:SS", "%Y-%m-%d %T"),
     ("%j", "DDD", "%j")],
)
def test_common_strftime_formats_still_translate(fmt: str, postgres: str, mysql: str) -> None:
    sql = f"SELECT strftime('{fmt}', OrderDate) FROM Orders"
    assert f"'{postgres}')" in transpile(sql, write="postgres", pretty=False)
    assert f"'{mysql}')" in transpile(sql, write="mysql", pretty=False)
    assert "DATE_FORMAT(OrderDate, '%w')" in transpile("SELECT strftime('%w', OrderDate) FROM Orders", write="mysql")
