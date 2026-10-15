"""Heuristic query cost estimate built from SQLite's ``EXPLAIN QUERY PLAN``.

The plan is a tree of loops. Every ``SCAN`` reads a whole table (or index) once per row of the loops
around it, every ``SEARCH`` reads a few rows per outer row, temporary B-trees (sorting, grouping,
DISTINCT) add a penalty and correlated subqueries run once per outer row. The score is the estimated
number of rows touched; it maps to a level: < 2,000 low, < 100,000 medium, otherwise high.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.db.catalog import SchemaCatalog
from app.db.database import connect_readonly
from app.sql.dialects import parse_statements
from app.sql.validator import is_read_only, strip_sql_fences

logger = logging.getLogger(__name__)

LOW_LIMIT = 2_000
MEDIUM_LIMIT = 100_000
INDEX_EQUALITY_FRACTION = 0.05  # rows matched by an equality lookup on a non-unique, non-FK index
INDEX_RANGE_FRACTION = 0.3  # one-sided range (col > ?)
INDEX_BETWEEN_FRACTION = 0.1  # two-sided range (col > ? AND col < ?)
DERIVED_ROWS = 100  # assumed size of a CTE / subquery result
SORT_PENALTY = 0.5  # extra cost per row sorted in a temporary B-tree
EQUALITY_SELECTIVITY = 0.1
RANGE_SELECTIVITY = 0.3

_ACCESS = re.compile(r"^(SCAN|SEARCH) (\S+)(.*)$")
_INDEX_NAME = re.compile(r"USING (?:COVERING )?INDEX (\w+)")
_CONDITION = re.compile(r"\(([^()]*)\)")


@dataclass
class CostEstimate:
    """Estimated cost of a query and the plan it is based on."""

    level: Literal["low", "medium", "high"]
    score: int
    estimated_rows_scanned: int
    plan: list[str]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        """The ``cost`` object of docs/API.md."""
        return {
            "level": self.level,
            "score": self.score,
            "estimated_rows_scanned": self.estimated_rows_scanned,
            "plan": list(self.plan),
            "notes": list(self.notes),
        }


@dataclass
class _PlanNode:
    detail: str
    children: list[_PlanNode] = field(default_factory=list)


def _plan_tree(rows: list[tuple[int, int, int, str]]) -> tuple[list[_PlanNode], list[str]]:
    """Plan rows ``(id, parent, _, detail)`` -> root nodes plus indented text lines."""
    nodes: dict[int, _PlanNode] = {}
    depth: dict[int, int] = {0: -1}
    roots: list[_PlanNode] = []
    lines = []
    for node_id, parent, _unused, detail in rows:
        node = _PlanNode(str(detail))
        nodes[node_id] = node
        depth[node_id] = depth.get(parent, -1) + 1
        (nodes[parent].children if parent in nodes else roots).append(node)
        lines.append("  " * depth[node_id] + node.detail)
    return roots, lines


def _filter_selectivity(statement: exp.Expression) -> dict[str, float]:
    """Alias (lower case) -> fraction of rows kept by WHERE predicates comparing its columns to constants."""
    tables = [t.alias_or_name.lower() for t in statement.find_all(exp.Table)]
    factors: dict[str, float] = {}
    comparisons = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.In, exp.Between, exp.Is)
    for where in statement.find_all(exp.Where):
        for node in where.find_all(*comparisons):
            columns = list(node.find_all(exp.Column))
            if len(columns) != 1 or node.find(exp.Select):
                continue
            alias = columns[0].table.lower() or (tables[0] if len(set(tables)) == 1 else "")
            if not alias:
                continue
            factor = EQUALITY_SELECTIVITY if isinstance(node, exp.EQ | exp.In | exp.Is) else RANGE_SELECTIVITY
            factors[alias] = max(0.01, factors.get(alias, 1.0) * factor)
    return factors


class _Estimator:
    """Walks the plan tree accumulating rows scanned, penalties and notes."""

    def __init__(self, catalog: SchemaCatalog, statement: exp.Expression | None) -> None:
        self.catalog = catalog
        self.aliases: dict[str, str] = {}
        self.selectivity: dict[str, float] = {}
        if statement is not None:
            for table in statement.find_all(exp.Table):
                canonical = catalog.resolve_table(table.name)
                if canonical:
                    self.aliases[table.alias_or_name.lower()] = canonical
            self.selectivity = _filter_selectivity(statement)
        self.scanned = 0.0
        self.penalty = 0.0
        self.notes: list[str] = []

    def note(self, text: str) -> None:
        if text not in self.notes:
            self.notes.append(text)

    def walk(self, nodes: list[_PlanNode], outer: float) -> None:
        loop = outer
        for node in nodes:
            loop = self.visit(node, loop, outer)

    def visit(self, node: _PlanNode, loop: float, outer: float) -> float:
        """Account for ``node``; returns the loop multiplier for the following sibling (nested loops)."""
        detail = node.detail
        match = _ACCESS.match(detail)
        if match:
            return self.access(match.group(1), match.group(2), match.group(3), loop)
        if detail.startswith("USE TEMP B-TREE"):
            purpose = detail.split(" FOR ", 1)[1] if " FOR " in detail else "sorting"
            self.penalty += max(10.0, SORT_PENALTY * loop)
            self.note(f"Temporary B-tree for {purpose}.")
        elif "SUBQUERY" in detail:
            correlated = detail.startswith("CORRELATED")
            if correlated:
                self.note("Correlated subquery runs once per outer row.")
            self.walk(node.children, loop if correlated else outer)
            return loop
        elif detail.startswith(("CO-ROUTINE", "MATERIALIZE")):
            self.walk(node.children, outer)
            return loop
        self.walk(node.children, loop)
        return loop

    def access(self, operation: str, name: str, rest: str, loop: float) -> float:
        if name == "CONSTANT":  # SCAN CONSTANT ROW
            return loop
        table = self.aliases.get(name.lower()) or self.catalog.resolve_table(name)
        if table is None:  # a CTE or subquery result
            self.scanned += loop * DERIVED_ROWS
            return loop * DERIVED_ROWS
        rows = max(1, self.catalog.tables[table].row_count)
        keep = self.selectivity.get(name.lower(), 1.0)
        index = _INDEX_NAME.search(rest)
        if operation == "SCAN":
            self.scanned += loop * rows
            if index:
                self.note(f"Full scan of index {index.group(1)} on {table} ({rows:,} rows).")
            else:
                self.note(f"Full table scan on {table} ({rows:,} rows).")
            return loop * max(1.0, rows * keep)
        per_lookup = self.lookup_rows(table, rows, rest)
        if "AUTOMATIC" in rest:
            self.scanned += rows  # SQLite builds a temporary index first
            self.note(f"SQLite builds a temporary index on {table} for this join; a permanent index would help.")
        elif "PRIMARY KEY" in rest:
            self.note(f"Primary-key lookup on {table}.")
        elif index:
            self.note(f"Uses index {index.group(1)} on {table}.")
        self.scanned += loop * per_lookup
        return loop * max(1.0, per_lookup * keep)

    def lookup_rows(self, table: str, rows: int, rest: str) -> float:
        """Rows returned by one SEARCH: 1 for a key lookup, the average fan-out for a foreign key."""
        if "PRIMARY KEY" in rest:
            return 1.0
        condition = _CONDITION.search(rest)
        terms = condition.group(1).split(" AND ") if condition else []
        if any(">" in term or "<" in term for term in terms):
            two_sided = sum(1 for term in terms if ">" in term or "<" in term) >= 2
            return max(1.0, rows * (INDEX_BETWEEN_FRACTION if two_sided else INDEX_RANGE_FRACTION))
        info = self.catalog.tables[table]
        index = _INDEX_NAME.search(rest)
        if index and any(ix.name == index.group(1) and ix.unique for ix in info.indexes):
            return 1.0
        if len(terms) == 1 and "=" in terms[0]:
            fk = info.foreign_key(terms[0].split("=")[0].strip())
            if fk is not None:
                parent_rows = max(1, self.catalog.tables[fk.ref_table].row_count)
                return max(1.0, rows / parent_rows)
        return max(1.0, rows * INDEX_EQUALITY_FRACTION)


def _level(score: int) -> Literal["low", "medium", "high"]:
    if score < LOW_LIMIT:
        return "low"
    return "medium" if score < MEDIUM_LIMIT else "high"


def estimate_cost(sql: str, db_path: str | Path, catalog: SchemaCatalog) -> CostEstimate | None:
    """Estimate the cost of a read-only query from its SQLite query plan (``None`` if it cannot be planned)."""
    text = strip_sql_fences(sql)
    if not text or not is_read_only(text):
        return None
    conn = connect_readonly(db_path)
    try:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {text}").fetchall()
    except (sqlite3.Error, MemoryError) as exc:  # MemoryError: SQLITE_NOMEM under the shared heap limit
        logger.debug("EXPLAIN QUERY PLAN failed: %r", exc)
        return None
    finally:
        conn.close()
    try:
        statement: exp.Expression | None = parse_statements(text, "sqlite")[0]
    except (SqlglotError, IndexError):
        statement = None
    roots, lines = _plan_tree(rows)
    estimator = _Estimator(catalog, statement)
    estimator.walk(roots, 1.0)
    scanned = round(estimator.scanned)
    score = round(estimator.scanned + estimator.penalty)
    notes = estimator.notes or ["Constant expression; no table is read."]
    return CostEstimate(level=_level(score), score=score, estimated_rows_scanned=scanned, plan=lines, notes=notes)
