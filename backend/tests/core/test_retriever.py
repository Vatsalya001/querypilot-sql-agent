"""Schema retriever: lexical matching, synonyms, sample values, FK bridges and fallbacks."""

from __future__ import annotations

import pytest

from app.db.catalog import SchemaCatalog
from app.retrieval.schema_retriever import retrieve_schema


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        ("Show all employees hired after January 2024.", ["Employees"]),
        ("How many staff do we have?", ["Employees"]),
        ("List our clients", ["Customers"]),
        ("Which payment method is most popular?", ["Payments"]),
        ("average salary per department", ["Departments", "Employees"]),
        ("show stock levels", ["Products"]),
    ],
)
def test_names_and_synonyms(catalog: SchemaCatalog, question: str, expected: list[str]) -> None:
    context = retrieve_schema(question, catalog)
    assert sorted(context.tables) == sorted(expected)
    assert context.strategy == "lexical"


def test_sample_values_point_to_columns(catalog: SchemaCatalog) -> None:
    context = retrieve_schema("customers from California", catalog)
    assert context.tables[0] == "Customers"
    assert "'california' -> Customers.State" in context.matched_terms["Customers"]
    engineering = retrieve_schema("How many people work in Engineering?", catalog)
    assert "Departments" in engineering.tables


def test_fk_bridge_tables_are_added(catalog: SchemaCatalog) -> None:
    context = retrieve_schema("list customers and products", catalog)
    assert context.tables[:2] == ["Customers", "Products"]
    assert set(context.tables[2:]) == {"Orders", "OrderItems"}
    assert context.strategy == "lexical+fk_bridge"
    assert context.matched_terms["OrderItems"] == ["join bridge"]


def test_previous_sql_tables_are_always_included(catalog: SchemaCatalog) -> None:
    context = retrieve_schema("Only those from California.", catalog, previous_sql="SELECT * FROM Customers")
    assert context.tables[0] == "Customers"
    assert context.matched_terms["Customers"][0] == "previous query"


def test_user_sql_tables_are_included_and_bad_sql_is_ignored(catalog: SchemaCatalog) -> None:
    context = retrieve_schema("Why does this fail?", catalog, user_sql="SELECT Nmae FROM Payments")
    assert context.tables == ["Payments"]
    assert retrieve_schema("customers", catalog, previous_sql="SELEC ((( broken").tables == ["Customers"]


def test_full_schema_fallback(catalog: SchemaCatalog) -> None:
    context = retrieve_schema("show me the data", catalog)
    assert context.strategy == "full_schema"
    assert context.tables == catalog.table_names()
    assert context.text == catalog.to_prompt()
    assert "no specific table" in context.focus_hint()


def test_text_includes_other_tables_summary(catalog: SchemaCatalog) -> None:
    context = retrieve_schema("Show all employees hired after January 2024.", catalog)
    assert context.text.startswith("-- Employees (80 rows)")
    assert "\n\n-- Other tables (summary):\n" in context.text
    assert "Payments(PaymentID, OrderID, PaymentDate, Amount, PaymentMethod)" in context.text
    assert "CREATE TABLE Payments" not in context.text


def test_focus_hint_and_to_dict(catalog: SchemaCatalog) -> None:
    context = retrieve_schema("customers from California", catalog)
    assert context.focus_hint().startswith("Most relevant tables: Customers (matched ")
    assert "'california' -> Customers.State" in context.focus_hint()
    assert set(context.to_dict()) == {"tables", "text", "matched_terms", "strategy"}


def test_max_tables_is_respected(catalog: SchemaCatalog) -> None:
    question = "employees departments customers products orders payments"
    assert len(retrieve_schema(question, catalog, max_tables=2).tables) <= 4  # 2 matches + bridges
