"""Schema catalog: tables, columns, keys, indexes, descriptions and sample values of the database.

The catalog is introspected once at startup (:func:`load_catalog`) and then used everywhere the agent
needs schema knowledge: name resolution in the validator, prompt text for the LLM, relevance retrieval
and the ``GET /api/schema`` payload.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.db.database import readonly_uri
from app.db.descriptions import COLUMN_DESCRIPTIONS, TABLE_DESCRIPTIONS

MAX_SAMPLE_VALUES = 8
MAX_DISTINCT_FOR_SAMPLES = 25
_TEXT_AFFINITY = ("CHAR", "CLOB", "TEXT")
_QUOTE_PAIRS = (('"', '"'), ("`", "`"), ("[", "]"))


def normalize_identifier(name: str) -> str:
    """Case-folded identifier with surrounding whitespace and one layer of "", `` or [] quotes removed."""
    text = name.strip()
    for opening, closing in _QUOTE_PAIRS:
        if len(text) >= 2 and text.startswith(opening) and text.endswith(closing):
            text = text[1:-1].strip()
            break
    return text.lower()


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


@dataclass(frozen=True)
class ForeignKey:
    """``column`` of the owning table references ``ref_table.ref_column``."""

    column: str
    ref_table: str
    ref_column: str

    def to_dict(self) -> dict[str, str]:
        """JSON-safe representation."""
        return {"column": self.column, "ref_table": self.ref_table, "ref_column": self.ref_column}


@dataclass(frozen=True)
class ColumnInfo:
    """One column; ``sample_values`` are set for low-cardinality TEXT columns (most frequent first)."""

    name: str
    type: str
    nullable: bool
    primary_key: bool
    description: str = ""
    sample_values: tuple[str, ...] = ()
    distinct_count: int | None = None  # distinct non-NULL values, known when samples were collected

    def to_dict(self, foreign_key: ForeignKey | None = None) -> dict[str, Any]:
        """The column object of ``GET /api/schema`` (docs/API.md)."""
        return {
            "name": self.name,
            "type": self.type,
            "primary_key": self.primary_key,
            "nullable": self.nullable,
            "description": self.description,
            "foreign_key": {"table": foreign_key.ref_table, "column": foreign_key.ref_column} if foreign_key else None,
            "sample_values": list(self.sample_values),
        }


@dataclass(frozen=True)
class IndexInfo:
    """An index and its key columns (in key order)."""

    name: str
    columns: tuple[str, ...]
    unique: bool

    def to_dict(self) -> dict[str, Any]:
        """The index object of ``GET /api/schema``."""
        return {"name": self.name, "columns": list(self.columns), "unique": self.unique}


@dataclass(frozen=True)
class TableInfo:
    """One table with its columns, foreign keys, indexes and row count."""

    name: str
    description: str
    columns: tuple[ColumnInfo, ...]
    foreign_keys: tuple[ForeignKey, ...]
    indexes: tuple[IndexInfo, ...]
    row_count: int

    def column(self, name: str) -> ColumnInfo | None:
        """Column by name (case-insensitive, quotes tolerated)."""
        key = normalize_identifier(name)
        return next((col for col in self.columns if col.name.lower() == key), None)

    def foreign_key(self, column: str) -> ForeignKey | None:
        """The foreign key declared on ``column``, if any."""
        key = normalize_identifier(column)
        return next((fk for fk in self.foreign_keys if fk.column.lower() == key), None)

    @property
    def primary_key(self) -> tuple[str, ...]:
        """Primary-key column names."""
        return tuple(col.name for col in self.columns if col.primary_key)

    def to_dict(self) -> dict[str, Any]:
        """The table object of ``GET /api/schema``."""
        return {
            "name": self.name,
            "description": self.description,
            "row_count": self.row_count,
            "columns": [col.to_dict(self.foreign_key(col.name)) for col in self.columns],
            "indexes": [index.to_dict() for index in self.indexes],
        }


@dataclass(frozen=True)
class Relationship:
    """A foreign-key relationship ``from_table.from_column -> to_table.to_column``."""

    from_table: str
    from_column: str
    to_table: str
    to_column: str

    def to_dict(self) -> dict[str, str]:
        """The relationship object of ``GET /api/schema``."""
        return {
            "from_table": self.from_table,
            "from_column": self.from_column,
            "to_table": self.to_table,
            "to_column": self.to_column,
        }

    def __str__(self) -> str:
        return f"{self.from_table}.{self.from_column} -> {self.to_table}.{self.to_column}"


class SchemaCatalog:
    """Read-only view of the database schema with case-insensitive name resolution."""

    def __init__(self, tables: Iterable[TableInfo], *, dialect: str = "sqlite") -> None:
        self.tables: dict[str, TableInfo] = {table.name: table for table in tables}
        self.dialect = dialect
        self._by_key = {name.lower(): name for name in self.tables}
        self._relationships = [
            Relationship(table.name, fk.column, fk.ref_table, fk.ref_column)
            for table in self.tables.values()
            for fk in table.foreign_keys
        ]

    # ---- name resolution ----------------------------------------------------------------------

    def table_names(self) -> list[str]:
        """Canonical table names in creation order."""
        return list(self.tables)

    def get_table(self, name: str) -> TableInfo | None:
        """Table by name (case-insensitive; "quoted", `backticked` and [bracketed] names are accepted)."""
        canonical = self._by_key.get(normalize_identifier(name))
        return self.tables[canonical] if canonical else None

    def resolve_table(self, name: str) -> str | None:
        """Canonical table name, or ``None`` if the table does not exist."""
        return self._by_key.get(normalize_identifier(name))

    def resolve_column(self, table: str, column: str) -> str | None:
        """Canonical column name of ``table.column``, or ``None`` if either does not exist."""
        info = self.get_table(table)
        col = info.column(column) if info else None
        return col.name if col else None

    # ---- relationships ------------------------------------------------------------------------

    def relationships(self) -> list[Relationship]:
        """All foreign-key relationships (including the Employees.ManagerID self-reference)."""
        return list(self._relationships)

    def is_valid_join(self, t1: str, c1: str, t2: str, c2: str) -> bool:
        """True if ``t1.c1 = t2.c2`` follows a foreign key in either direction (case-insensitive)."""
        left = (self.resolve_table(t1), self.resolve_column(t1, c1))
        right = (self.resolve_table(t2), self.resolve_column(t2, c2))
        if None in left or None in right:
            return False
        for rel in self._relationships:
            pair = ((rel.from_table, rel.from_column), (rel.to_table, rel.to_column))
            if (left, right) == pair or (right, left) == pair:
                return True
        return False

    def key_domain(self, table: str, column: str) -> tuple[str, str] | None:
        """The primary key a key column ultimately identifies, or ``None`` for non-key columns.

        A foreign key maps to the key it references (following chains); a primary key maps to itself.
        Two columns with the same key domain can be equi-joined meaningfully, e.g. OrderItems.OrderID and
        Payments.OrderID (both identify an order).
        """
        table_name, column_name = self.resolve_table(table), self.resolve_column(table, column)
        seen: set[tuple[str, str]] = set()
        while table_name and column_name and (table_name, column_name) not in seen:
            seen.add((table_name, column_name))
            info = self.tables[table_name]
            fk = info.foreign_key(column_name)
            if fk is None:
                return (table_name, column_name) if column_name in info.primary_key else None
            table_name, column_name = self.resolve_table(fk.ref_table), self.resolve_column(fk.ref_table, fk.ref_column)
        return None

    def join_path(self, start: str, end: str) -> list[Relationship]:
        """Shortest chain of foreign keys connecting two tables (empty if unrelated or the same table)."""
        source, target = self.resolve_table(start), self.resolve_table(end)
        if not source or not target or source == target:
            return []
        previous: dict[str, tuple[str, Relationship]] = {}
        frontier = [source]
        visited = {source}
        while frontier and target not in visited:
            next_frontier = []
            for table in frontier:
                for rel in self._relationships:
                    if rel.from_table == rel.to_table or table not in (rel.from_table, rel.to_table):
                        continue
                    other = rel.to_table if rel.from_table == table else rel.from_table
                    if other not in visited:
                        visited.add(other)
                        previous[other] = (table, rel)
                        next_frontier.append(other)
            frontier = next_frontier
        if target not in visited:
            return []
        path: list[Relationship] = []
        node = target
        while node != source:
            node, rel = previous[node]
            path.append(rel)
        return list(reversed(path))

    def relationships_between(self, tables: Iterable[str]) -> list[Relationship]:
        """Relationships whose two ends both belong to ``tables``."""
        names = {self.resolve_table(name) for name in tables} - {None}
        return [rel for rel in self._relationships if rel.from_table in names and rel.to_table in names]

    # ---- renderings ---------------------------------------------------------------------------

    def to_sqlglot_schema(self) -> dict[str, dict[str, str]]:
        """``{table: {column: type}}`` mapping for sqlglot's ``qualify``."""
        return {table.name: {col.name: col.type or "TEXT" for col in table.columns} for table in self.tables.values()}

    def to_prompt(self, tables: Iterable[str] | None = None, *, include_samples: bool = True) -> str:
        """Compact, deterministic CREATE TABLE-style schema text for LLM prompts (catalog order)."""
        if tables is None:
            names = self.table_names()
        else:
            wanted = {self.resolve_table(name) for name in tables}
            names = [name for name in self.tables if name in wanted]
        return "\n\n".join(self._table_prompt(self.tables[name], include_samples) for name in names)

    def summary(self, tables: Iterable[str] | None = None) -> str:
        """One line per table: ``Customers(CustomerID, FirstName, ...)``."""
        names = self.table_names() if tables is None else [n for n in self.tables if n in set(tables)]
        return "\n".join(
            f"{name}({', '.join(col.name for col in self.tables[name].columns)})" for name in names
        )

    def to_dict(self) -> dict[str, Any]:
        """The ``GET /api/schema`` payload (docs/API.md)."""
        return {
            "dialect": self.dialect,
            "tables": [table.to_dict() for table in self.tables.values()],
            "relationships": [rel.to_dict() for rel in self._relationships],
        }

    @staticmethod
    def _column_prompt(table: TableInfo, col: ColumnInfo, include_samples: bool) -> str:
        definition = f"{col.name} {col.type}".rstrip()
        if col.primary_key:
            return f"{definition} PRIMARY KEY,"
        notes: list[str] = []
        fk = table.foreign_key(col.name)
        if fk:
            notes.append(f"FK -> {fk.ref_table}.{fk.ref_column}.")
        if col.description:
            notes.append(col.description)
        if col.nullable and "NULL" not in col.description:
            definition += " NULL"
        if include_samples and col.sample_values and not all(f"'{v}'" in col.description for v in col.sample_values):
            values = ", ".join("'" + value.replace("'", "''") + "'" for value in col.sample_values)
            if col.distinct_count is not None and col.distinct_count > len(col.sample_values):
                notes.append(f"Common values: {values} ({col.distinct_count} distinct).")
            else:
                notes.append(f"Values: {values}.")
        return f"{definition}, -- {' '.join(notes)}" if notes else f"{definition},"

    def _table_prompt(self, table: TableInfo, include_samples: bool) -> str:
        header = f"-- {table.name} ({table.row_count:,} rows)"
        if table.description:
            header += f": {table.description}"
        lines = [header, f"CREATE TABLE {table.name} ("]
        for position, col in enumerate(table.columns):
            line = self._column_prompt(table, col, include_samples)
            if position == len(table.columns) - 1:  # no comma after the last column
                line = line.replace(", --", " --", 1) if ", --" in line else line.rstrip(",")
            lines.append(f"  {line}")
        lines.append(");")
        if table.indexes:
            rendered = ", ".join(
                f"{index.name}{' UNIQUE' if index.unique else ''} ({', '.join(index.columns)})"
                for index in table.indexes
            )
            lines.append(f"-- Indexes: {rendered}")
        return "\n".join(lines)


# ---- introspection ------------------------------------------------------------------------------


def _has_text_affinity(declared_type: str) -> bool:
    upper = declared_type.upper()
    return any(token in upper for token in _TEXT_AFFINITY)


def _sample_values(conn: sqlite3.Connection, table: str, column: str) -> tuple[tuple[str, ...], int | None]:
    quoted_table, quoted_col = _quote(table), _quote(column)
    distinct = conn.execute(f"SELECT COUNT(DISTINCT {quoted_col}) FROM {quoted_table}").fetchone()[0]
    if not distinct or distinct > MAX_DISTINCT_FOR_SAMPLES:
        return (), None
    rows = conn.execute(
        f"SELECT {quoted_col}, COUNT(*) AS n FROM {quoted_table} WHERE {quoted_col} IS NOT NULL "
        f"GROUP BY {quoted_col} ORDER BY n DESC, {quoted_col} LIMIT {MAX_SAMPLE_VALUES}"
    ).fetchall()
    return tuple(str(row[0]) for row in rows), int(distinct)


def _load_table(conn: sqlite3.Connection, name: str, primary_keys: dict[str, list[str]]) -> TableInfo:
    quoted = _quote(name)
    descriptions = COLUMN_DESCRIPTIONS.get(name, {})
    fk_rows = conn.execute(f"PRAGMA foreign_key_list({quoted})").fetchall()
    fk_columns = {row[3] for row in fk_rows}
    columns = []
    for _cid, col_name, col_type, notnull, _default, pk in conn.execute(f"PRAGMA table_info({quoted})"):
        samples: tuple[str, ...] = ()
        distinct: int | None = None
        if not pk and col_name not in fk_columns and _has_text_affinity(col_type or ""):
            samples, distinct = _sample_values(conn, name, col_name)
        columns.append(
            ColumnInfo(
                name=col_name,
                type=(col_type or "").upper(),
                nullable=not notnull and not pk,
                primary_key=bool(pk),
                description=descriptions.get(col_name, ""),
                sample_values=samples,
                distinct_count=distinct,
            )
        )
    position = {col.name: index for index, col in enumerate(columns)}
    foreign_keys = tuple(
        ForeignKey(column=row[3], ref_table=row[2], ref_column=row[4] or (primary_keys.get(row[2]) or ["rowid"])[0])
        for row in sorted(fk_rows, key=lambda r: (position.get(r[3], len(position)), r[0], r[1]))
    )
    indexes = []
    for _seq, index_name, unique, *_ in conn.execute(f"PRAGMA index_list({quoted})"):
        info = sorted(conn.execute(f"PRAGMA index_info({_quote(index_name)})").fetchall())
        indexes.append(IndexInfo(index_name, tuple(str(row[2]) for row in info if row[2] is not None), bool(unique)))
    row_count = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
    return TableInfo(
        name=name,
        description=TABLE_DESCRIPTIONS.get(name, ""),
        columns=tuple(columns),
        foreign_keys=foreign_keys,
        indexes=tuple(sorted(indexes, key=lambda index: index.name)),
        row_count=int(row_count),
    )


def load_catalog(db_path: str | Path) -> SchemaCatalog:
    """Introspect the database with PRAGMAs and merge in the business descriptions and sample values."""
    conn = sqlite3.connect(readonly_uri(db_path), uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        names = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite~_%' ESCAPE '~' "
                "ORDER BY rowid"
            )
        ]
        primary_keys = {
            name: [row[1] for row in sorted(conn.execute(f"PRAGMA table_info({_quote(name)})"), key=lambda r: r[5])
                   if row[5]]
            for name in names
        }
        return SchemaCatalog(_load_table(conn, name, primary_keys) for name in names)
    finally:
        conn.close()
