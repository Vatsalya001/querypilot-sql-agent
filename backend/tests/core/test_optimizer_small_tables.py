"""Small tables get an informational index note instead of an index recommendation."""

from app.db.catalog import SchemaCatalog
from app.sql.optimizer import analyze_query


def test_small_table_filter_gets_a_note_but_no_index(catalog: SchemaCatalog) -> None:
    report = analyze_query("SELECT FirstName FROM Employees WHERE HireDate >= '2024-01-01'", catalog)
    assert report.index_recommendations == []
    notes = [s.message for s in report.suggestions if s.category == "index"]
    assert len(notes) == 1
    assert "Employees.HireDate" in notes[0] and "only 80 rows" in notes[0]


def test_indexed_or_primary_key_filters_on_small_tables_get_no_note(catalog: SchemaCatalog) -> None:
    for sql in (
        "SELECT FirstName FROM Employees WHERE EmployeeID = 5",
        "SELECT FirstName FROM Employees WHERE DepartmentID = 2",
    ):
        report = analyze_query(sql, catalog)
        assert [s for s in report.suggestions if s.category == "index"] == [], sql


def test_large_tables_still_get_index_recommendations(catalog: SchemaCatalog) -> None:
    report = analyze_query("SELECT CustomerID FROM Customers WHERE State = 'California'", catalog)
    assert report.index_recommendations == ["CREATE INDEX idx_customers_state ON Customers (State);"]
