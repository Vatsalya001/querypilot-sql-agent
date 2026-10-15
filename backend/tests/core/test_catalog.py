"""Schema catalog: name resolution, relationships, API payload and prompt rendering."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.catalog import SchemaCatalog, load_catalog

TABLES = ["Departments", "Employees", "Customers", "Products", "Orders", "OrderItems", "Payments"]


def test_tables_in_creation_order(catalog: SchemaCatalog) -> None:
    assert catalog.table_names() == TABLES


@pytest.mark.parametrize("name", ["Customers", "customers", "CUSTOMERS", '"Customers"', "`customers`", "[Customers]", " Customers "])
def test_table_resolution_is_case_and_quote_insensitive(catalog: SchemaCatalog, name: str) -> None:
    table = catalog.get_table(name)
    assert table is not None and table.name == "Customers"
    assert catalog.resolve_table(name) == "Customers"


@pytest.mark.parametrize(
    ("table", "column", "expected"),
    [
        ("orders", "totalamount", "TotalAmount"),
        ('"Orders"', '"OrderDate"', "OrderDate"),
        ("Customers", "STATE", "State"),
        ("Customers", "Region", None),
        ("Clients", "State", None),
    ],
)
def test_column_resolution(catalog: SchemaCatalog, table: str, column: str, expected: str | None) -> None:
    assert catalog.resolve_column(table, column) == expected


def test_unknown_table(catalog: SchemaCatalog) -> None:
    assert catalog.get_table("Clients") is None
    assert catalog.resolve_table("Customer") is None


def test_relationships_include_the_self_reference(catalog: SchemaCatalog) -> None:
    rendered = {str(rel) for rel in catalog.relationships()}
    assert rendered == {
        "Employees.DepartmentID -> Departments.DepartmentID",
        "Employees.ManagerID -> Employees.EmployeeID",
        "Orders.CustomerID -> Customers.CustomerID",
        "Orders.EmployeeID -> Employees.EmployeeID",
        "OrderItems.OrderID -> Orders.OrderID",
        "OrderItems.ProductID -> Products.ProductID",
        "Payments.OrderID -> Orders.OrderID",
    }


@pytest.mark.parametrize(
    ("join", "expected"),
    [
        (("Orders", "CustomerID", "Customers", "CustomerID"), True),
        (("Customers", "CustomerID", "Orders", "CustomerID"), True),
        (("orders", "customerid", "CUSTOMERS", "CustomerId"), True),
        (("Employees", "ManagerID", "Employees", "EmployeeID"), True),
        (("Employees", "EmployeeID", "Employees", "ManagerID"), True),
        (("OrderItems", "ProductID", "Products", "ProductID"), True),
        (("Orders", "OrderID", "Products", "ProductID"), False),
        (("Orders", "CustomerID", "Customers", "State"), False),
        (("Orders", "EmployeeID", "Customers", "CustomerID"), False),
        (("Nope", "ID", "Orders", "OrderID"), False),
    ],
)
def test_is_valid_join(catalog: SchemaCatalog, join: tuple[str, str, str, str], expected: bool) -> None:
    assert catalog.is_valid_join(*join) is expected


def test_key_domain_and_join_path(catalog: SchemaCatalog) -> None:
    assert catalog.key_domain("OrderItems", "OrderID") == ("Orders", "OrderID")
    assert catalog.key_domain("Payments", "OrderID") == ("Orders", "OrderID")
    assert catalog.key_domain("Employees", "ManagerID") == ("Employees", "EmployeeID")
    assert catalog.key_domain("Customers", "State") is None
    path = [str(rel) for rel in catalog.join_path("Customers", "Products")]
    assert path == [
        "Orders.CustomerID -> Customers.CustomerID",
        "OrderItems.OrderID -> Orders.OrderID",
        "OrderItems.ProductID -> Products.ProductID",
    ]
    assert catalog.join_path("Orders", "Orders") == []


def test_to_dict_matches_the_api_contract(catalog: SchemaCatalog) -> None:
    payload = catalog.to_dict()
    assert set(payload) == {"dialect", "tables", "relationships"}
    assert payload["dialect"] == "sqlite"
    assert [table["name"] for table in payload["tables"]] == TABLES
    for table in payload["tables"]:
        assert set(table) == {"name", "description", "row_count", "columns", "indexes"}
        assert table["description"] and isinstance(table["row_count"], int)
        for column in table["columns"]:
            assert set(column) == {"name", "type", "primary_key", "nullable", "description", "foreign_key",
                                   "sample_values"}
            assert column["foreign_key"] is None or set(column["foreign_key"]) == {"table", "column"}
            assert column["description"], (table["name"], column["name"])
        for index in table["indexes"]:
            assert set(index) == {"name", "columns", "unique"}
    for relationship in payload["relationships"]:
        assert set(relationship) == {"from_table", "from_column", "to_table", "to_column"}
    orders = next(table for table in payload["tables"] if table["name"] == "Orders")
    customer_id = next(column for column in orders["columns"] if column["name"] == "CustomerID")
    assert customer_id["foreign_key"] == {"table": "Customers", "column": "CustomerID"}
    customers = next(table for table in payload["tables"] if table["name"] == "Customers")
    state = next(column for column in customers["columns"] if column["name"] == "State")
    assert state["sample_values"][0] == "California"
    assert {"name": "idx_customers_email", "columns": ["Email"], "unique": True} in customers["indexes"]


def test_sample_values(catalog: SchemaCatalog) -> None:
    status = catalog.tables["Orders"].column("Status")
    assert status is not None
    assert set(status.sample_values) == {"Pending", "Shipped", "Delivered", "Cancelled", "Returned"}
    assert catalog.tables["Customers"].column("City").sample_values == ()  # too many distinct values
    assert len(catalog.tables["Departments"].column("DepartmentName").sample_values) == 8
    assert catalog.tables["Orders"].column("OrderDate").sample_values == ()


def test_to_prompt_contents(catalog: SchemaCatalog) -> None:
    text = catalog.to_prompt()
    assert "-- Customers (300 rows): People and businesses" in text
    assert "CREATE TABLE Orders (" in text
    assert "CustomerID INTEGER, -- FK -> Customers.CustomerID." in text
    assert "ManagerID INTEGER, -- FK -> Employees.EmployeeID." in text
    assert "'California'" in text
    assert "Values: 'Customer Support', 'Engineering'" in text
    assert "-- Indexes: idx_orders_customer (CustomerID), idx_orders_date (OrderDate)" in text
    for table in TABLES:
        assert f"CREATE TABLE {table} (" in text
    assert len(text) / 4 < 1600, "the full schema prompt should stay compact"


def test_to_prompt_is_deterministic(catalog: SchemaCatalog, db_path: Path) -> None:
    assert catalog.to_prompt() == catalog.to_prompt()
    assert load_catalog(db_path).to_prompt() == catalog.to_prompt()


def test_to_prompt_subset_and_samples_flag(catalog: SchemaCatalog) -> None:
    text = catalog.to_prompt(["orders", "Nope"])
    assert "CREATE TABLE Orders (" in text and "CREATE TABLE Customers (" not in text
    assert "Common values" in text
    assert "Common values" not in catalog.to_prompt(["Orders"], include_samples=False)


def test_summary_covers_all_tables(catalog: SchemaCatalog) -> None:
    lines = catalog.summary().splitlines()
    assert [line.split("(")[0] for line in lines] == TABLES
    assert "Customers(CustomerID, FirstName, LastName, Email, Phone, City, State, Country, Segment, CreatedAt)" in lines


def test_sqlglot_schema(catalog: SchemaCatalog) -> None:
    schema = catalog.to_sqlglot_schema()
    assert schema["Orders"]["TotalAmount"] == "REAL"
    assert set(schema) == set(TABLES)
