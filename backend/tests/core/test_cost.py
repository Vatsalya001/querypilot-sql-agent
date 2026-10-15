"""Cost estimator: plan parsing, levels, notes and failure handling."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.catalog import SchemaCatalog
from app.sql.cost import CostEstimate, estimate_cost


def estimate(sql: str, db_path: Path, catalog: SchemaCatalog) -> CostEstimate:
    result = estimate_cost(sql, db_path, catalog)
    assert result is not None
    return result


def test_full_scan_matches_the_api_example(db_path: Path, catalog: SchemaCatalog) -> None:
    cost = estimate("SELECT * FROM Customers WHERE State = 'California'", db_path, catalog)
    assert cost.to_dict() == {
        "level": "low",
        "score": 300,
        "estimated_rows_scanned": 300,
        "plan": ["SCAN Customers"],
        "notes": ["Full table scan on Customers (300 rows)."],
    }


def test_primary_key_lookup_is_cheap(db_path: Path, catalog: SchemaCatalog) -> None:
    cost = estimate("SELECT * FROM Customers WHERE CustomerID = 7", db_path, catalog)
    assert cost.level == "low" and cost.score == 1
    assert cost.notes == ["Primary-key lookup on Customers."]


def test_index_search_is_cheaper_than_a_scan(db_path: Path, catalog: SchemaCatalog) -> None:
    search = estimate("SELECT OrderID FROM Orders WHERE OrderDate >= '2026-09-01'", db_path, catalog)
    scan = estimate("SELECT OrderID FROM Orders WHERE Status = 'Pending'", db_path, catalog)
    assert "Uses index idx_orders_date on Orders." in search.notes
    assert search.score < scan.score
    assert scan.notes == ["Full table scan on Orders (2,000 rows)."]


def test_nested_scans_multiply(db_path: Path, catalog: SchemaCatalog) -> None:
    cost = estimate("SELECT * FROM Orders o, OrderItems i", db_path, catalog)
    assert cost.level == "high"
    assert cost.estimated_rows_scanned > 1_000_000


def test_sorting_adds_a_penalty(db_path: Path, catalog: SchemaCatalog) -> None:
    cost = estimate("SELECT Status, COUNT(*) FROM Orders GROUP BY Status", db_path, catalog)
    assert "Temporary B-tree for GROUP BY." in cost.notes
    assert cost.score > cost.estimated_rows_scanned
    assert cost.level == "medium"


def test_correlated_subquery_and_plan_indentation(db_path: Path, catalog: SchemaCatalog) -> None:
    cost = estimate("SELECT o.OrderID, (SELECT SUM(p.Amount) FROM Payments p WHERE p.OrderID = o.OrderID) "
                    "FROM Orders o", db_path, catalog)
    assert "Correlated subquery runs once per outer row." in cost.notes
    assert any(line.startswith("  SEARCH p") for line in cost.plan)


def test_automatic_index_note(db_path: Path, catalog: SchemaCatalog) -> None:
    cost = estimate("SELECT * FROM Orders o JOIN Customers c ON c.State = o.ShippingState", db_path, catalog)
    assert any("temporary index on Customers" in note for note in cost.notes)


@pytest.mark.parametrize("sql", ["SELECT * FROM Nope", "SELEC 1", "DELETE FROM Orders", ""])
def test_returns_none_when_no_plan_is_possible(db_path: Path, catalog: SchemaCatalog, sql: str) -> None:
    assert estimate_cost(sql, db_path, catalog) is None


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM OrderItems LIMIT 5",  # used to be "medium, 5,063 rows scanned"
        "SELECT * FROM Orders WHERE Status = 'Shipped' LIMIT 10",
        "SELECT * FROM Orders ORDER BY OrderDate DESC LIMIT 10",  # read in index order, stops after 10
        "SELECT o.OrderID, c.FirstName FROM Orders o JOIN Customers c ON c.CustomerID = o.CustomerID LIMIT 20",
    ],
)
def test_a_limit_without_sorting_stops_the_scan_early(db_path: Path, catalog: SchemaCatalog, sql: str) -> None:
    cost = estimate(sql, db_path, catalog)
    assert cost.level == "low", cost
    assert cost.estimated_rows_scanned < 1_000
    assert any("LIMIT" in note for note in cost.notes), cost.notes


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM OrderItems ORDER BY Quantity DESC LIMIT 5",  # every row is sorted first
        "SELECT COUNT(*) FROM OrderItems LIMIT 5",  # every row is counted
        "SELECT DISTINCT ProductID, Quantity FROM OrderItems LIMIT 5",
        "SELECT * FROM OrderItems LIMIT 5 OFFSET 4000",  # the skipped rows are read too
        "SELECT * FROM OrderItems WHERE UnitPrice > (SELECT AVG(UnitPrice) FROM OrderItems) LIMIT 5",
    ],
)
def test_a_limit_does_not_hide_work_that_reads_every_row(db_path: Path, catalog: SchemaCatalog, sql: str) -> None:
    cost = estimate(sql, db_path, catalog)
    assert cost.level != "low", cost
