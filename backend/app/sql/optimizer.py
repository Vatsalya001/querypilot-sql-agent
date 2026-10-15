"""Rule-based static analysis of a query: readability, performance, index and join suggestions.

The rules are deterministic and schema-aware (row counts, primary keys, existing indexes). They feed
the ``optimization`` panel and give the LLM concrete findings when it rewrites a query in optimize mode.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, traverse_scope

from app.db.catalog import SchemaCatalog, TableInfo
from app.sql.dialects import normalize_dialect, parse_statements, render

logger = logging.getLogger(__name__)

Category = Literal["readability", "performance", "index", "join", "correctness"]
Severity = Literal["info", "warning"]

MIN_ROWS_FOR_INDEX = 100
MIN_ROWS_FOR_ORDER_WARNING = 1_000
_NON_SARGABLE_FUNCTIONS = (exp.TimeToStr, exp.Date, exp.Lower, exp.Upper, exp.Substring, exp.Trim, exp.Cast,
                           exp.Year, exp.Month, exp.Day, exp.Abs, exp.Round, exp.Coalesce, exp.Length)
_RANGE_COMPARISONS = (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Between)


@dataclass
class Suggestion:
    """One finding about the query."""

    category: Category
    severity: Severity
    message: str

    def to_dict(self) -> dict[str, str]:
        """JSON-safe representation."""
        return {"category": self.category, "severity": self.severity, "message": self.message}


@dataclass
class OptimizationReport:
    """Formatted SQL plus deduplicated suggestions, index DDL and unnecessary joins."""

    formatted_sql: str
    suggestions: list[Suggestion] = field(default_factory=list)
    index_recommendations: list[str] = field(default_factory=list)
    unnecessary_joins: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """The ``optimization`` object of docs/API.md."""
        return {
            "formatted_sql": self.formatted_sql,
            "suggestions": [suggestion.to_dict() for suggestion in self.suggestions],
            "index_recommendations": list(self.index_recommendations),
            "unnecessary_joins": list(self.unnecessary_joins),
        }


@dataclass
class _Usage:
    """How the query uses one table's columns (canonical names, in order of appearance)."""

    equality: list[str] = field(default_factory=list)
    ranges: list[str] = field(default_factory=list)
    joins: list[str] = field(default_factory=list)
    ordering: list[str] = field(default_factory=list)
    wrapped: list[str] = field(default_factory=list)  # filtered through a function (non-sargable)
    like: list[str] = field(default_factory=list)  # LIKE 'prefix%': only a COLLATE NOCASE index can serve it


def _add(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


class _Analysis:
    """One analysis run over a parsed statement."""

    def __init__(self, statement: exp.Expression, catalog: SchemaCatalog, text: str, dialect: str) -> None:
        self.statement = statement
        self.catalog = catalog
        self.text = text
        self.dialect = dialect
        self.suggestions: list[Suggestion] = []
        self.indexes: list[str] = []
        self.unnecessary: list[str] = []
        self.usage: dict[str, _Usage] = {}
        self.scopes = list(traverse_scope(statement))

    def suggest(self, category: Category, severity: Severity, message: str) -> None:
        if all(existing.message != message for existing in self.suggestions):
            self.suggestions.append(Suggestion(category, severity, message))

    # ---- name resolution -----------------------------------------------------------------------

    def table_for(self, scope: Scope, column: exp.Column) -> TableInfo | None:
        """The real table a column belongs to within ``scope`` (``None`` for CTEs, subqueries, unknowns)."""
        sources = {alias.lower(): source for alias, (_node, source) in scope.selected_sources.items()}
        if column.table:
            source = sources.get(column.table.lower())
            return self.catalog.get_table(source.name) if isinstance(source, exp.Table) else None
        owners = [
            info for source in sources.values()
            if isinstance(source, exp.Table) and (info := self.catalog.get_table(source.name))
            and info.column(column.name)
        ]
        return owners[0] if len(owners) == 1 else None

    def canonical(self, scope: Scope, column: exp.Column) -> tuple[TableInfo, str] | None:
        info = self.table_for(scope, column)
        col = info.column(column.name) if info else None
        return (info, col.name) if info and col else None

    def usage_of(self, table: str) -> _Usage:
        return self.usage.setdefault(table, _Usage())

    # ---- rules ---------------------------------------------------------------------------------

    def run(self) -> None:
        for scope in self.scopes:
            select = scope.expression
            if not isinstance(select, exp.Select):
                continue
            self.select_star(scope, select)
            self.collect_usage(scope, select)
            self.unnecessary_joins(scope, select)
            self.distinct_with_group_by(select)
            self.correlated_select_subquery(scope)
        self.not_in_subquery()
        self.comma_joins()
        self.order_without_limit()
        self.recommend_indexes()

    def select_star(self, scope: Scope, select: exp.Select) -> None:
        if select.find_ancestor(exp.Exists) is not None:
            return  # SELECT * inside EXISTS is idiomatic
        for projection in select.expressions:
            if isinstance(projection, exp.Star) or (
                isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star)
            ):
                tables = _dedupe(
                    info.name for _alias, (_node, source) in scope.selected_sources.items()
                    if isinstance(source, exp.Table) and (info := self.catalog.get_table(source.name))
                )
                width = sum(len(self.catalog.tables[name].columns) for name in tables)
                detail = f" ({_oxford(tables)}: {width} columns)" if tables else ""
                self.suggest("readability", "info",
                             f"Select only the columns you need instead of SELECT *{detail}.")
                return

    def collect_usage(self, scope: Scope, select: exp.Select) -> None:
        where = select.args.get("where")
        if where is not None:
            self.filters(scope, where.this)
        for join in select.args.get("joins") or []:
            on = join.args.get("on")
            if on is not None:
                for column in on.find_all(exp.Column):
                    resolved = self.canonical(scope, column)
                    if resolved and column.find_ancestor(exp.Select) is select:
                        _add(self.usage_of(resolved[0].name).joins, resolved[1])
                self.filters(scope, on, join_condition=True)
        order, group = select.args.get("order"), select.args.get("group")
        keys = [*(item.this for item in (order.expressions if order else [])),
                *(group.expressions if group else [])]
        for key in keys:
            # Only a bare column can be read in index order; LOWER(col) or strftime(..., col) cannot.
            resolved = self.canonical(scope, key) if isinstance(key, exp.Column) else None
            if resolved:
                _add(self.usage_of(resolved[0].name).ordering, resolved[1])

    def filters(self, scope: Scope, condition: exp.Expression, *, join_condition: bool = False) -> None:
        """Classify predicates: equality / range filters, non-sargable ones, leading wildcards, OR."""
        for node in condition.find_all(exp.EQ, exp.In, exp.Like, exp.ILike, *_RANGE_COMPARISONS, exp.Or):
            if node.parent_select is not scope.expression:
                continue  # belongs to a nested subquery (analyzed in its own scope)
            if isinstance(node, exp.Or):
                self.or_across_columns(scope, node)
                continue
            subject = node.this
            column = subject if isinstance(subject, exp.Column) else None
            if column is None:
                wrapped = subject.find(exp.Column) if isinstance(subject, _NON_SARGABLE_FUNCTIONS) else None
                if wrapped is not None:
                    self.non_sargable(scope, node, subject, wrapped)
                continue
            resolved = self.canonical(scope, column)
            if resolved is None:
                continue
            info, name = resolved
            other = node.expression if not isinstance(node, exp.In) else None
            if isinstance(other, exp.Column) or join_condition:
                continue  # column = column (a join predicate) is collected separately
            if _row_dependent(node, scope.expression):
                continue  # e.g. UnitPrice > Quantity * 10: no constant range for an index to search
            usage = self.usage_of(info.name)
            under_or = any(isinstance(ancestor, exp.Or) for ancestor in _ancestors(node, condition))
            if isinstance(node, exp.EQ | exp.In) and not under_or:
                _add(usage.equality, name)
            elif isinstance(node, exp.EQ | exp.In):
                _add(usage.ranges, name)  # OR branches need separate single-column indexes
            elif isinstance(node, exp.Like | exp.ILike):
                self.like(node, info, name)
            else:
                _add(usage.ranges, name)

    def like(self, node: exp.Like | exp.ILike, info: TableInfo, column: str) -> None:
        pattern = node.expression
        if not isinstance(pattern, exp.Literal) or not pattern.is_string:
            return
        if pattern.this.startswith(("%", "_")):
            self.suggest("performance", "warning",
                         f"LIKE '{pattern.this}' starts with a wildcard, so no index on {info.name}.{column} can be "
                         "used and every row is scanned; match a prefix (LIKE 'abc%') if possible, which an index "
                         "declared COLLATE NOCASE can serve (SQLite's LIKE ignores case).")
        else:
            _add(self.usage_of(info.name).like, column)

    def non_sargable(self, scope: Scope, node: exp.Expression, function: exp.Expression, column: exp.Column) -> None:
        resolved = self.canonical(scope, column)
        if resolved is None:
            return
        info, name = resolved
        _add(self.usage_of(info.name).wrapped, name)
        rewrite = self.range_rewrite(node, function, render(column))
        message = (f"{render(function)} applies a function to {info.name}.{name}, which prevents index use on "
                   f"that column; compare the bare column with a range instead")
        message += f", e.g. {rewrite}." if rewrite else "."
        self.suggest("performance", "warning", message)

    @staticmethod
    def range_rewrite(node: exp.Expression, function: exp.Expression, column: str) -> str | None:
        """``strftime('%Y', col) = '2024'`` -> ``col >= '2024-01-01' AND col < '2025-01-01'``."""
        if not isinstance(node, exp.EQ) or not isinstance(function, exp.TimeToStr):
            return None
        fmt, value = function.args.get("format"), node.expression
        if not isinstance(fmt, exp.Literal) or not isinstance(value, exp.Literal) or not value.is_string:
            return None
        if fmt.this == "%Y" and re.fullmatch(r"\d{4}", value.this):
            year = int(value.this)
            return f"{column} >= '{year}-01-01' AND {column} < '{year + 1}-01-01'"
        if fmt.this == "%Y-%m" and re.fullmatch(r"\d{4}-\d{2}", value.this):
            year, month = (int(part) for part in value.this.split("-"))
            next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
            return f"{column} >= '{year}-{month:02d}-01' AND {column} < '{next_year}-{next_month:02d}-01'"
        return None

    def or_across_columns(self, scope: Scope, node: exp.Or) -> None:
        columns = {
            f"{resolved[0].name}.{resolved[1]}"
            for column in node.find_all(exp.Column)
            if (resolved := self.canonical(scope, column))
        }
        if len(columns) > 1:
            self.suggest("performance", "info",
                         f"OR across different columns ({', '.join(sorted(columns))}) usually prevents index use; "
                         "if it is slow, split it into a UNION of two indexed queries.")

    def unnecessary_joins(self, scope: Scope, select: exp.Select) -> None:
        if any(isinstance(p, exp.Star) for p in select.expressions):
            return
        for join in select.args.get("joins") or []:
            target = join.this
            if not isinstance(target, exp.Table):
                continue
            info = self.catalog.get_table(target.name)
            on = join.args.get("on")
            if info is None or on is None:
                continue
            alias = target.alias_or_name.lower()
            used_elsewhere = any(
                (column.table.lower() == alias or (not column.table and info.column(column.name)
                                                   and self.table_for(scope, column) is info))
                and not _inside(column, on)
                for column in select.find_all(exp.Column)
            )
            if used_elsewhere:
                continue
            label = f"{info.name} AS {target.alias}" if target.alias else info.name
            left = (join.side or "").upper() == "LEFT"
            if not self.joins_on_unique_key(scope, on, alias, info):
                # One-to-many (e.g. Customers -> Orders): each row repeats once per match, so removing the join
                # changes the result even though none of its columns are selected. Never list it as removable.
                extra = "" if left else " and drops rows without a match"
                alternative = "" if left else " (use EXISTS to keep only rows that have a match)"
                self.suggest("join", "info",
                             f"None of the columns of {label} are used, but the join is not on its primary key, so it "
                             f"repeats each row once per matching {info.name} row{extra}. Keep it if those repeated "
                             f"rows are intended; otherwise remove it{alternative}. Removing it changes the result.")
                continue
            self.unnecessary.append(label)
            if left:
                self.suggest("join", "warning",
                             f"The LEFT JOIN to {label} is unnecessary: none of its columns are used and joining on "
                             "its primary key cannot change the result; remove it.")
            else:
                self.suggest("join", "warning",
                             f"The join to {label} may be removable: none of its columns are used, unless it is "
                             "needed to filter out rows without a match.")

    def joins_on_unique_key(self, scope: Scope, on: exp.Expression, alias: str, info: TableInfo) -> bool:
        """Whether ``on`` equates the joined table's primary key (or a unique index), so at most one row matches."""
        def owned(column: exp.Column) -> bool:
            return column.table.lower() == alias if column.table else self.table_for(scope, column) is info

        equated: set[str] = set()
        for eq in on.find_all(exp.EQ):
            if any(isinstance(ancestor, exp.Or) for ancestor in _ancestors(eq, on)):
                continue  # one branch of an OR does not pin the key
            for mine, other in ((eq.this, eq.expression), (eq.expression, eq.this)):
                if not isinstance(mine, exp.Column) or not owned(mine):
                    continue
                if any(owned(column) for column in other.find_all(exp.Column)):
                    continue  # compares two columns of the joined table itself
                column = info.column(mine.name)
                if column is not None:
                    equated.add(column.name.lower())
        keys = [tuple(info.primary_key)] + [index.columns for index in info.indexes if index.unique]
        return any(key and {name.lower() for name in key} <= equated for key in keys)

    def distinct_with_group_by(self, select: exp.Select) -> None:
        if select.args.get("distinct") and select.args.get("group"):
            self.suggest("readability", "info",
                         "DISTINCT is redundant with GROUP BY: grouped rows are already unique; remove DISTINCT.")

    def correlated_select_subquery(self, scope: Scope) -> None:
        if not scope.is_correlated_subquery:
            return
        node = scope.expression
        parent = node.parent_select
        if parent is None:
            return
        in_projection = any(_inside(node, projection) for projection in parent.expressions)
        if in_projection and node.find(exp.AggFunc) is not None:
            self.suggest("performance", "warning",
                         "A correlated subquery in the SELECT list runs once per row; rewrite it as a JOIN to a "
                         "grouped (aggregated) subquery.")
        elif in_projection:
            self.suggest("performance", "info",
                         "A correlated subquery in the SELECT list runs once per row; a JOIN is usually faster.")

    def not_in_subquery(self) -> None:
        for node in self.statement.find_all(exp.Not):
            inner = node.this
            if isinstance(inner, exp.In) and inner.args.get("query") is not None:
                self.suggest("correctness", "warning",
                             "NOT IN (subquery) returns no rows at all if the subquery yields a NULL; use NOT EXISTS "
                             "with a correlated condition instead (it is also easier to optimize).")

    def comma_joins(self) -> None:
        cross_joins = sum(
            1 for join in self.statement.find_all(exp.Join) if (join.kind or "").upper() == "CROSS"
        )
        explicit = len(re.findall(r"\bCROSS\s+JOIN\b", self.text, re.IGNORECASE))
        if cross_joins > explicit:
            self.suggest("readability", "info",
                         "Use explicit JOIN ... ON instead of comma-separated tables in FROM; it keeps join "
                         "conditions next to the tables and avoids accidental Cartesian products.")

    def order_without_limit(self) -> None:
        root = self.statement
        if not isinstance(root, exp.Select) or not root.args.get("order") or root.args.get("limit"):
            return
        if root.args.get("group") or any(projection.find(exp.AggFunc) for projection in root.expressions):
            return
        for table in root.find_all(exp.Table):
            info = self.catalog.get_table(table.name)
            if info and info.row_count >= MIN_ROWS_FOR_ORDER_WARNING:
                self.suggest("performance", "info",
                             f"ORDER BY without LIMIT sorts every matching row of {info.name} "
                             f"({info.row_count:,} rows); add LIMIT if you only need the top rows.")
                return

    def recommend_indexes(self) -> None:
        for table, usage in self.usage.items():
            info = self.catalog.tables[table]
            if info.row_count < MIN_ROWS_FOR_INDEX:
                self.note_small_table(info, [*usage.equality, *usage.ranges, *usage.like])
                continue
            for column in usage.like:
                col = info.column(column)
                if column not in usage.wrapped and not (col and col.primary_key and col.type == "INTEGER"):
                    self.index(info, [column], "matched with LIKE 'prefix%'", nocase=True)
            leading = {index.columns[0].lower() for index in info.indexes if index.columns}
            covered = set(leading)

            def needs(column: str, *, _covered: set[str] = covered, _info: TableInfo = info) -> bool:
                col = _info.column(column)
                is_rowid = col is not None and col.primary_key and col.type == "INTEGER"
                return not is_rowid and column.lower() not in _covered

            equality = [c for c in usage.equality if needs(c) and c not in usage.wrapped]
            ranges = [c for c in usage.ranges if needs(c) and c not in usage.wrapped and c not in equality]
            if len(equality) >= 2:
                self.index(info, equality + ranges[:1], "filtered together")
                covered.add(equality[0].lower())
            else:
                for column in equality + ranges:
                    self.index(info, [column], "filtered")
                    covered.add(column.lower())
            for column in usage.joins:
                if needs(column):
                    self.index(info, [column], "used in a join")
                    covered.add(column.lower())
            sortable = [c for c in usage.ordering if needs(c)]
            if sortable:
                self.index(info, sortable[:2], "used for sorting or grouping")

    def note_small_table(self, info: TableInfo, filtered: list[str]) -> None:
        """Mention unindexed filter columns on a small table without recommending an index (scans are cheap)."""
        leading = {index.columns[0].lower() for index in info.indexes if index.columns}
        columns = [
            column
            for column in dict.fromkeys(filtered)
            if column.lower() not in leading
            and not ((col := info.column(column)) and col.primary_key and col.type == "INTEGER")
        ]
        if not columns:
            return
        described = ", ".join(f"{info.name}.{column}" for column in columns)
        verb = "are" if len(columns) > 1 else "is"
        self.suggest(
            "index",
            "info",
            f"{described} {verb} filtered but not indexed. {info.name} has only {info.row_count} rows, so a "
            f"full scan is cheap; add an index only if the table grows.",
        )

    def index(self, info: TableInfo, columns: list[str], reason: str, *, nocase: bool = False) -> None:
        name = f"idx_{info.name}_{'_'.join(columns)}{'_nocase' if nocase else ''}".lower()
        keys = ", ".join(f"{column} COLLATE NOCASE" if nocase else column for column in columns)
        statement = f"CREATE INDEX {name} ON {info.name} ({keys});"
        if statement in self.indexes:
            return
        self.indexes.append(statement)
        described = ", ".join(f"{info.name}.{column}" for column in columns)
        verb = "are" if len(columns) > 1 else "is"
        if nocase:
            self.suggest("index", "info", f"{described} {verb} {reason} but has no case-insensitive index; SQLite's "
                                          "LIKE ignores case, so only an index declared COLLATE NOCASE can serve it.")
        else:
            self.suggest("index", "info", f"{described} {verb} {reason} but not indexed.")


def _ancestors(node: exp.Expression, stop: exp.Expression) -> list[exp.Expression]:
    """Ancestors of ``node`` up to and including ``stop``."""
    found: list[exp.Expression] = []
    current = node.parent
    while current is not None:
        found.append(current)
        if current is stop:
            break
        current = current.parent
    return found


def _row_dependent(node: exp.Expression, select: exp.Expression) -> bool:
    """Whether a filter compares its column with a value that changes per row (a column of the same query).

    ``UnitPrice > Quantity * 10`` has no constant bound, so an index on UnitPrice cannot narrow the scan;
    a column inside a subquery (``TotalAmount > (SELECT AVG(TotalAmount) ...)``) does not count.
    """
    if isinstance(node, exp.In):
        values = list(node.expressions)
    elif isinstance(node, exp.Between):
        values = [node.args.get("low"), node.args.get("high")]
    else:
        values = [node.expression]
    return any(
        column.find_ancestor(exp.Select) is select
        for value in values
        if value is not None
        for column in value.find_all(exp.Column)
    )


def _inside(node: exp.Expression, container: exp.Expression) -> bool:
    current: exp.Expression | None = node
    while current is not None:
        if current is container:
            return True
        current = current.parent
    return False


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _oxford(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" and {items[-1]}"


def analyze_query(sql: str, catalog: SchemaCatalog, *, dialect: str = "sqlite") -> OptimizationReport:
    """Static optimization report for one query; never raises (on failure: the input text, no findings)."""
    try:
        name = normalize_dialect(dialect)
        statements = parse_statements(sql, name)
        if len(statements) != 1:
            return OptimizationReport(formatted_sql=sql)
        statement = statements[0]
        analysis = _Analysis(statement, catalog, sql, name)
        analysis.run()
        return OptimizationReport(
            formatted_sql=render(statement, "sqlite", pretty=True),
            suggestions=analysis.suggestions,
            index_recommendations=analysis.indexes,
            unnecessary_joins=_dedupe(analysis.unnecessary),
        )
    except (SqlglotError, ValueError) as exc:
        logger.debug("Query analysis skipped: %s", exc)
    except Exception:  # noqa: BLE001 - analysis is best-effort and must never break the agent
        logger.exception("Query analysis failed")
    return OptimizationReport(formatted_sql=sql)


def _star_sources(select: exp.Select, catalog: SchemaCatalog) -> list[tuple[str, TableInfo]] | None:
    """``(reference, table)`` for every FROM/JOIN source, or ``None`` if a source is not a plain table.

    Joins with USING or NATURAL change which columns ``*`` returns, so they are not supported either.
    """
    from_clause = select.args.get("from_")
    joins = select.args.get("joins") or []
    sources = [from_clause.this if from_clause else None, *(join.this for join in joins)]
    if any(join.args.get("using") or join.args.get("method") for join in joins):
        return None
    resolved: list[tuple[str, TableInfo]] = []
    for source in sources:
        if not isinstance(source, exp.Table) or source.db:
            return None
        table = catalog.get_table(source.name)
        if table is None:
            return None
        resolved.append((source.alias_or_name, table))
    return resolved


def expand_select_star(sql: str, catalog: SchemaCatalog) -> str | None:
    """Rewrite ``SELECT *`` / ``SELECT t.*`` into the explicit column list from the schema.

    The result returns exactly the same columns in the same order, so the rewrite is always safe. Only a
    single plain SELECT over real tables is rewritten (no CTE, derived table, USING or NATURAL join); returns
    ``None`` when there is nothing to expand or the query is out of scope for the rewrite.
    """
    try:
        statements = parse_statements(sql, "sqlite")
    except SqlglotError:
        return None
    if len(statements) != 1 or not isinstance(statements[0], exp.Select) or statements[0].args.get("with_"):
        return None
    select = statements[0]
    if not any(projection.is_star for projection in select.expressions):
        return None
    sources = _star_sources(select, catalog)
    if not sources:
        return None
    qualify = len(sources) > 1
    projections: list[exp.Expression] = []
    for projection in select.expressions:
        if isinstance(projection, exp.Star):
            chosen = sources
        elif isinstance(projection, exp.Column) and projection.is_star:
            chosen = [source for source in sources if source[0].lower() == projection.table.lower()]
            if not chosen:
                return None
        else:
            projections.append(projection)
            continue
        for reference, table in chosen:
            for column in table.columns:
                projections.append(exp.column(column.name, table=reference if qualify else None))
    select.set("expressions", projections)
    return render(select, "sqlite")
