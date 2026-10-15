"""Deterministic SQL validation: seven checks that every query passes before it is returned or executed.

Checks, in order: ``syntax``, ``single_statement``, ``read_only``, ``tables``, ``columns``, ``joins`` and
``executable`` (an ``EXPLAIN QUERY PLAN`` dry-run on the read-only database). Error messages are written
for an LLM repair loop: they name the offending identifier, list what exists and suggest the closest
match (for example "Unknown column 'Region' in table Customers. ... Did you mean 'State'?").
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlglot import exp
from sqlglot.dialects.dialect import Dialect
from sqlglot.errors import OptimizeError, ParseError, SqlglotError, TokenError
from sqlglot.optimizer.qualify import qualify
from sqlglot.optimizer.scope import Scope, ScopeType, traverse_scope
from sqlglot.tokens import Token, TokenType

from app.db.catalog import SchemaCatalog
from app.db.database import connect_readonly
from app.sql.dialects import SUPPORTED_DIALECTS, normalize_dialect, parse_statements, render
from app.sql.fences import fenced_blocks
from app.sql.suggest import suggest_columns, suggest_tables

logger = logging.getLogger(__name__)

CHECK_LABELS: dict[str, str] = {
    "syntax": "Syntax",
    "single_statement": "Single statement",
    "read_only": "Read-only",
    "tables": "Tables exist",
    "columns": "Columns exist",
    "joins": "Join relationships",
    "executable": "Database dry-run",
}

# Statement / clause nodes that write, change the schema or administer the database.
_FORBIDDEN_NODES: dict[str, str] = {
    "Insert": "INSERT",
    "Update": "UPDATE",
    "Delete": "DELETE",
    "Drop": "DROP",
    "Alter": "ALTER",
    "TruncateTable": "TRUNCATE",
    "Create": "CREATE",
    "Merge": "MERGE",
    "Pragma": "PRAGMA",
    "Attach": "ATTACH",
    "Detach": "DETACH",
    "Transaction": "BEGIN",
    "Commit": "COMMIT",
    "Rollback": "ROLLBACK",
    "Set": "SET",
    "Use": "USE",
    "Grant": "GRANT",
    "Revoke": "REVOKE",
    "Copy": "COPY",
    "LoadData": "LOAD DATA",
    "Analyze": "ANALYZE",
    "Into": "SELECT INTO",
    "Command": "COMMAND",
}
_FORBIDDEN_TYPES: tuple[tuple[type[exp.Expression], str], ...] = tuple(
    (getattr(exp, name), label) for name, label in _FORBIDDEN_NODES.items() if hasattr(exp, name)
)
# Not allowed, but not a write either (they do not make a request "destructive").
_NON_DESTRUCTIVE = frozenset({"EXPLAIN", "BEGIN", "COMMIT", "ROLLBACK", "SET", "USE", "SHOW", "DESCRIBE", "DESC"})
_TOKEN_KEYWORDS = frozenset(
    {"DELETE", "UPDATE", "INSERT", "DROP", "ALTER", "TRUNCATE", "CREATE", "MERGE", "GRANT", "REVOKE", "ATTACH",
     "DETACH", "PRAGMA", "VACUUM", "REINDEX"}
)
# Statements sqlglot may only parse as a generic Command; anything else parsed that way is a typo.
_KNOWN_COMMANDS = _TOKEN_KEYWORDS | _NON_DESTRUCTIVE | frozenset(
    {"REPLACE", "ANALYZE", "CALL", "EXEC", "EXECUTE", "LOCK", "UNLOCK", "KILL", "LOAD", "OPTIMIZE", "FLUSH",
     "RESET", "CHECKPOINT", "CLUSTER", "COMMENT", "DECLARE", "DO", "PREPARE", "REFRESH", "RENAME", "SAVEPOINT",
     "RELEASE", "REPAIR", "BACKUP", "RESTORE", "COPY", "GRANT", "REVOKE", "UPSERT"}
)
_DANGEROUS_FUNCTIONS = frozenset(
    {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "sqlar_compress", "sqlar_uncompress"}
)
_ALLOWED_TABLE_FUNCTIONS = frozenset({"json_each", "json_tree"})
_MAX_TRIM_SET_CHARS = 64  # trim(X, Y) costs len(X) * len(Y) in one uninterruptible step
_DEFAULT_SCHEMAS = frozenset({"main", "public"})
_QUERY_TYPES = (exp.Select, exp.SetOperation)
_LITERAL_TOKENS = frozenset(t for t in TokenType if t.name.endswith("STRING")) | {TokenType.IDENTIFIER}
_CLAUSES = {
    "Where": "WHERE clause", "Order": "ORDER BY clause", "Group": "GROUP BY clause", "Having": "HAVING clause",
    "Limit": "LIMIT clause", "Offset": "OFFSET clause", "Join": "JOIN clause", "From": "FROM clause",
    "Select": "SELECT list", "Case": "CASE expression", "Window": "window (OVER) clause", "In": "IN list",
    "Between": "BETWEEN expression", "Cast": "CAST expression", "With": "WITH clause", "CTE": "WITH clause",
}
_FUNCTION_HINTS: dict[str, str] = {
    "date_trunc": "use strftime('%Y-%m', col) or date(col, 'start of month')",
    "year": "use strftime('%Y', col)",
    "month": "use strftime('%m', col)",
    "day": "use strftime('%d', col)",
    "extract": "use strftime() with a format such as '%Y' or '%m'",
    "date_format": "use strftime(format, col)",
    "to_char": "use strftime(format, col)",
    "datediff": "use julianday(end) - julianday(start)",
    "now": "use date('now') or datetime('now')",
    "getdate": "use date('now') or datetime('now')",
    "curdate": "use date('now')",
    "isnull": "use IFNULL(x, y) or COALESCE(x, y)",
    "nvl": "use IFNULL(x, y) or COALESCE(x, y)",
    "len": "use length(x)",
}


@dataclass
class ValidationCheck:
    """Outcome of one validation check."""

    name: str
    label: str
    passed: bool
    message: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation (docs/API.md ``validation.checks[]``)."""
        return {"name": self.name, "label": self.label, "passed": self.passed, "message": self.message}


@dataclass
class ValidationReport:
    """All seven checks plus the normalized SQL and the tables and columns it references."""

    valid: bool
    checks: list[ValidationCheck]
    errors: list[str]
    warnings: list[str]
    sql: str
    tables: list[str]
    columns: list[str]
    destructive: bool

    def to_dict(self) -> dict[str, Any]:
        """The ``validation`` object of docs/API.md."""
        return {
            "valid": self.valid,
            "checks": [check.to_dict() for check in self.checks],
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "sql": self.sql,
            "tables": list(self.tables),
            "columns": list(self.columns),
            "destructive": self.destructive,
        }


# ---- small helpers ------------------------------------------------------------------------------


_LABEL = re.compile(r"^\s*sql\s*:\s*", re.IGNORECASE)


def strip_sql_fences(text: str) -> str:
    """Remove Markdown code fences (with or without a language tag), a leading ``SQL:`` label,
    surrounding whitespace and trailing semicolons."""
    blocks = [block for block in fenced_blocks(text) if block]
    cleaned = blocks[0] if blocks else text.strip()
    cleaned = _LABEL.sub("", cleaned, count=1)
    return re.sub(r"[\s;]+$", "", cleaned).strip()


def _dedupe(items: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _oxford(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" and {items[-1]}"


# ---- read-only analysis ---------------------------------------------------------------------------


@dataclass
class _ReadOnlyFindings:
    operations: list[str] = field(default_factory=list)  # e.g. ["DROP"], ["SELECT INTO"], ["sqlite_master"]
    errors: list[str] = field(default_factory=list)
    destructive: bool = False

    def add(self, operation: str, error: str, *, destructive: bool) -> None:
        if operation not in self.operations:
            self.operations.append(operation)
            self.errors.append(error)
        self.destructive = self.destructive or destructive


def _command_name(node: exp.Command) -> str:
    name = str(node.this or "").strip().split(" ")[0].upper()
    tail = node.expression
    tail_text = tail.name if isinstance(tail, exp.Expression) else str(tail or "")
    if name == "REPLACE" and tail_text.strip().upper().startswith("INTO"):
        return "REPLACE INTO"
    return name


def _scan_tree(statement: exp.Expression, findings: _ReadOnlyFindings) -> None:
    root = statement.unnest() if isinstance(statement, exp.Subquery) else statement
    if not isinstance(root, _QUERY_TYPES) and not any(isinstance(root, kind) for kind, _ in _FORBIDDEN_TYPES):
        words = root.sql(dialect="sqlite").split(None, 1)
        label = words[0].upper() if words else root.key.upper()
        findings.add(label, f"Only SELECT queries are allowed; this statement starts with {label}.",
                     destructive=False)
    for node in statement.walk():
        for node_type, label in _FORBIDDEN_TYPES:
            if isinstance(node, node_type):
                if isinstance(node, exp.Command):
                    label = _command_name(node)
                destructive = label not in _NON_DESTRUCTIVE
                kind = "Write and schema operations" if destructive else "Statements other than SELECT"
                findings.add(label, f"{kind} are not allowed: {label} found. Only read-only SELECT queries "
                                    "can be run.", destructive=destructive)
        if isinstance(node, exp.Func) and not isinstance(node.parent, exp.Table):
            name = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
            if name in _DANGEROUS_FUNCTIONS:
                findings.add(f"{name}()", f"The function {name}() is not allowed.", destructive=True)
            elif name.startswith(("pragma_", "sqlite_")) or isinstance(node, exp.CurrentVersion):
                label = "sqlite_version" if isinstance(node, exp.CurrentVersion) else name
                findings.add(f"{label}()", f"The function {label}() is not allowed; query only the provided "
                                           "schema.", destructive=False)
            elif _unbounded_trim(node, name):
                findings.add("trim()", "The characters to remove in trim(), ltrim() or rtrim() must be a short "
                                       f"string literal (at most {_MAX_TRIM_SET_CHARS} characters), "
                                       "e.g. trim(Name, ' .').", destructive=False)
        if isinstance(node, exp.Table):
            name = node.name.lower() if not isinstance(node.this, exp.Func) else node.this.name.lower()
            if name.startswith(("sqlite_", "pragma_")) or name == "dbstat":
                findings.add(name, f"System table {name} is not accessible; query only the provided schema.",
                             destructive=False)


def _unbounded_trim(node: exp.Func, name: str) -> bool:
    """True for trim()/ltrim()/rtrim() whose character set is not a short string literal."""
    if isinstance(node, exp.Trim):
        chars = node.args.get("expression")
    elif isinstance(node, exp.Anonymous) and name in ("trim", "ltrim", "rtrim") and len(node.expressions) > 1:
        chars = node.expressions[1]
    else:
        return False
    if chars is None:
        return False
    return not (isinstance(chars, exp.Literal) and chars.is_string and len(chars.this) <= _MAX_TRIM_SET_CHARS)


def _tokens(text: str, dialect: str) -> list[Token] | None:
    try:
        return Dialect.get_or_raise(dialect).tokenize(text)
    except (TokenError, SqlglotError, ValueError):
        return None


def _scan_tokens(text: str, dialect: str, findings: _ReadOnlyFindings, depth: int = 0) -> None:
    """Keyword scan that ignores string literals and quoted identifiers (so 'Deleted' is fine).

    sqlglot tokenizes the remainder of an unsupported statement (``REPLACE INTO ...``, ``VACUUM ...``) as a
    single string token; that remainder is scanned again.
    """
    tokens = _tokens(text, dialect)
    if tokens is None:
        return
    commands = Dialect.get_or_raise(dialect).tokenizer_class.COMMANDS
    for index, token in enumerate(tokens):
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if token.token_type in _LITERAL_TOKENS:
            continue
        word = token.text.upper()
        command_tail = (
            token.token_type in commands
            and following is not None
            and following.token_type in _LITERAL_TOKENS
            and (index == 0 or tokens[index - 1].token_type in (TokenType.SEMICOLON, TokenType.BEGIN))
        )
        if word == "REPLACE" and following is not None and (
            following.token_type is TokenType.INTO or (command_tail and following.text.upper().startswith("INTO"))
        ):
            findings.add("REPLACE INTO", "Write operations are not allowed: REPLACE INTO found.", destructive=True)
        elif word in _TOKEN_KEYWORDS:
            findings.add(word, f"Write and schema operations are not allowed: {word} found. Only read-only "
                               "SELECT queries can be run.", destructive=True)
        if command_tail and depth < 2 and following is not None:
            _scan_tokens(following.text, dialect, findings, depth + 1)


_YEAR_TEXT = re.compile(r"(19|20|21)\d\d")
_DATE_PART_TEXT = re.compile(r"\d{1,2}")


def _adjacent(tokens: Iterable[Token]) -> bool:
    """True when the tokens are written back to back, without whitespace between them."""
    items = list(tokens)
    return all(right.start == left.end + 1 for left, right in zip(items, items[1:], strict=False))


def _date_part(token: Token | None, low: int, high: int) -> bool:
    return (
        token is not None
        and token.token_type is TokenType.NUMBER
        and bool(_DATE_PART_TEXT.fullmatch(token.text))
        and low <= int(token.text) <= high
    )


def _unquoted_date_errors(text: str, dialect: str) -> list[str]:
    """Errors for dates written without quotes, such as ``HireDate > 2024-01-01``.

    SQL reads ``2024-01-01`` as the arithmetic ``2024 - 1 - 1`` (the number 2022), so the comparison runs
    without any database error but silently returns wrong rows. Only digits written back to back as
    ``YYYY-MM-DD`` (or ``YYYY-MM`` with a two-digit month) outside string literals and comments are flagged;
    spaced arithmetic such as ``2024 - 1`` is left alone.
    """
    tokens = _tokens(text, dialect) or []
    errors: list[str] = []
    for index, year in enumerate(tokens):
        if year.token_type is not TokenType.NUMBER or not _YEAR_TEXT.fullmatch(year.text):
            continue
        window = tokens[index : index + 5]
        dashes_ok = [token.token_type is TokenType.DASH for token in window[1::2]]
        if len(window) >= 5 and all(dashes_ok[:2]) and _adjacent(window) and _date_part(window[2], 1, 12) \
                and _date_part(window[4], 1, 31):
            month, day = int(window[2].text), int(window[4].text)
            written, value = text[year.start : window[4].end + 1], int(year.text) - month - day
            literal = f"'{year.text}-{month:02d}-{day:02d}'"
        elif len(window) >= 3 and dashes_ok[0] and _adjacent(window[:3]) and len(window[2].text) == 2 \
                and _date_part(window[2], 1, 12):
            month = int(window[2].text)
            written, value = text[year.start : window[2].end + 1], int(year.text) - month
            literal = f"'{year.text}-{month:02d}'"
        else:
            continue
        errors.append(
            f"Unquoted date {written}: SQL reads it as the arithmetic {' - '.join(written.split('-'))} "
            f"(= {value}), not as a date. Write dates as quoted text, e.g. {literal}."
        )
    return _dedupe(errors)


_TEXT_RESULTS = (exp.TimeToStr, exp.Substring, exp.Upper, exp.Lower, exp.Trim, exp.Replace, exp.DPipe)
_TEXT_RESULT_FUNCTIONS = frozenset({"strftime", "substr", "printf", "format", "ltrim", "rtrim", "char", "hex", "quote"})
_DATE_RESULT_FUNCTIONS = frozenset({"datetime", "time"})


def _text_result(node: exp.Expression) -> str | None:
    """``"date"`` for date()/datetime()/time(), ``"text"`` for other expressions that always return text."""
    if isinstance(node, exp.Date):
        return "date"
    if isinstance(node, exp.Anonymous):
        name = node.name.lower()
        return "date" if name in _DATE_RESULT_FUNCTIONS else "text" if name in _TEXT_RESULT_FUNCTIONS else None
    return "text" if isinstance(node, _TEXT_RESULTS) else None


def _is_number(node: exp.Expression | None) -> bool:
    node = node.this if isinstance(node, exp.Neg) else node
    return isinstance(node, exp.Literal) and not node.is_string


def _text_number_errors(tree: exp.Expression) -> list[str]:
    """Errors for text compared with a number literal, such as ``strftime('%Y', OrderDate) = 2024``.

    A function result has no type affinity, so SQLite compares it as text: text never equals a number and
    always sorts after one. The query runs without any error but the condition is always true or always false
    (0 rows instead of 494 for the example). Columns are fine (their TEXT affinity converts the number).
    """
    errors: list[str] = []
    comparisons = (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.In, exp.Between)
    for node in tree.find_all(*comparisons):
        if isinstance(node, exp.In):
            subject = node.this if node.args.get("query") is None else None
            numbers = [value for value in node.expressions if _is_number(value)]
        elif isinstance(node, exp.Between):
            subject, numbers = node.this, [v for v in (node.args.get("low"), node.args.get("high")) if _is_number(v)]
        elif _text_result(node.this) and _is_number(node.expression):
            subject, numbers = node.this, [node.expression]
        else:
            subject, numbers = node.expression, [node.this] if _is_number(node.this) else []
        kind = _text_result(subject) if subject is not None else None
        if kind is None or not numbers:
            continue
        written, function = render(node, "sqlite"), render(subject, "sqlite")
        label = "the || operator" if isinstance(subject, exp.DPipe) else f"{function.split('(')[0].lower()}()"
        problem = (f"{written} compares {'a date' if kind == 'date' else 'text'} with a number: {label} returns "
                   "text, and in SQLite text never equals a number (it always sorts after numbers), so the condition "
                   "is always true or always false.")
        if kind == "date":
            errors.append(f"{problem} Write dates as quoted text, e.g. '2024-01-01'.")
            continue
        fixed = node.copy()
        target = fixed.this if fixed.this == subject else fixed.expression
        target.replace(exp.cast(target.copy(), "INTEGER"))
        errors.append(f"{problem} Compare numbers with numbers, e.g. {render(fixed, 'sqlite')}, or text with "
                      "quoted text.")
    return _dedupe(errors)


def _read_only_findings(statements: list[exp.Expression], text: str, dialect: str) -> _ReadOnlyFindings:
    findings = _ReadOnlyFindings()
    for statement in statements:
        _scan_tree(statement, findings)
    _scan_tokens(text, dialect, findings)
    return findings


def is_read_only(sql: str, dialect: str = "sqlite") -> bool:
    """True only for exactly one parseable, read-only query (AST and keyword checks; used before execution).

    Fails closed: anything that cannot be analyzed is treated as not read-only.
    """
    try:
        name = normalize_dialect(dialect)
        text = strip_sql_fences(sql)
        statements = parse_statements(text, name)
        if len(statements) != 1 or _unrecognized_command(statements[0]):
            return False
        return not _read_only_findings(statements, text, name).errors
    except (SqlglotError, ValueError, RecursionError):
        return False


def _unrecognized_command(statement: exp.Expression) -> str | None:
    """The first word of a statement sqlglot could only parse as a generic command, if it is not a known one."""
    if not isinstance(statement, exp.Command):
        return None
    name = _command_name(statement).split(" ")[0]
    return None if name in _KNOWN_COMMANDS else (name or "?")


# ---- name resolution (tables and columns) -------------------------------------------------------


@dataclass
class _ColumnRef:
    table: str  # canonical table name
    column: str  # canonical column name

    def __str__(self) -> str:
        return f"{self.table}.{self.column}"


class _Resolver:
    """Resolves column references scope by scope and explains every failure."""

    def __init__(self, catalog: SchemaCatalog, tree: exp.Expression) -> None:
        self.catalog = catalog
        self.scopes = list(traverse_scope(tree))
        self.by_expression = {id(scope.expression): scope for scope in self.scopes}
        self.errors: list[str] = []
        self.referenced: list[str] = []

    # sources -------------------------------------------------------------------------------------

    @staticmethod
    def sources(scope: Scope) -> dict[str, exp.Table | Scope]:
        """Alias (lower case) -> source (a real table node or a nested scope) of the FROM/JOIN clauses."""
        return {alias.lower(): source for alias, (_node, source) in scope.selected_sources.items()}

    def table_of(self, source: exp.Table | Scope) -> str | None:
        """Canonical table for a real-table source."""
        return self.catalog.resolve_table(source.name) if isinstance(source, exp.Table) else None

    def output_columns(self, scope: Scope, seen: set[int] | None = None) -> set[str] | None:
        """Lower-case output column names of a derived table / CTE scope (``None`` = unknown)."""
        seen = seen or set()
        if id(scope) in seen:
            return None
        seen.add(id(scope))
        top = scope.expression
        while isinstance(top.parent, exp.SetOperation):  # e.g. the anchor branch of a recursive CTE
            top = top.parent
        parent = top.parent
        alias = parent.args.get("alias") if isinstance(parent, exp.CTE | exp.Subquery) else None
        if isinstance(alias, exp.TableAlias) and alias.columns:
            return {column.name.lower() for column in alias.columns}
        query = top
        while isinstance(query, exp.SetOperation):
            query = query.this
        if not isinstance(query, exp.Select):
            return None
        names: set[str] = set()
        inner = self.by_expression.get(id(query), scope)
        for projection in query.expressions:
            star_source = None
            if isinstance(projection, exp.Star):
                star_source = "*"
            elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                star_source = projection.table.lower()
            if star_source is None:
                names.add(projection.alias_or_name.lower())
                continue
            for alias_name, source in self.sources(inner).items():
                if star_source not in ("*", alias_name):
                    continue
                table = self.table_of(source)
                if table:
                    names.update(col.name.lower() for col in self.catalog.tables[table].columns)
                elif isinstance(source, Scope):
                    nested = self.output_columns(source, seen)
                    if nested is None:
                        return None
                    names.update(nested)
                else:
                    return None
        return names

    # resolution ----------------------------------------------------------------------------------

    def owner(self, column: exp.Column) -> Scope | None:
        node = column.find_ancestor(exp.Select, exp.SetOperation)
        while node is not None:
            scope = self.by_expression.get(id(node))
            if scope is not None:
                return scope
            node = node.find_ancestor(exp.Select, exp.SetOperation)
        return None

    def run(self, tree: exp.Expression) -> None:
        for column in tree.find_all(exp.Column, bfs=False):
            scope = self.owner(column)
            if scope is None:
                continue
            if column.table:
                self.resolve_qualified(scope, column)
            elif not isinstance(column.this, exp.Star):
                self.resolve_unqualified(scope, column)
        for scope in self.scopes:
            self.check_using(scope)

    def has_column(self, source: exp.Table | Scope | None, name: str) -> bool:
        """Whether ``source`` has column ``name`` (unknown sources are given the benefit of the doubt)."""
        table = self.table_of(source) if source is not None else None
        if table:
            return self.catalog.resolve_column(table, name) is not None
        if isinstance(source, Scope):
            outputs = self.output_columns(source)
            return outputs is None or name.lower() in outputs
        return True

    def check_using(self, scope: Scope) -> None:
        """``JOIN ... USING (col)`` needs ``col`` on both sides of the join."""
        select = scope.expression
        if not isinstance(select, exp.Select):
            return
        sources = self.sources(scope)
        from_clause = select.args.get("from_") or select.args.get("from")
        left = [from_clause.this.alias_or_name.lower()] if from_clause else []
        for join in select.args.get("joins") or []:
            right = join.this.alias_or_name.lower()
            for ident in join.args.get("using") or []:
                missing = [] if self.has_column(sources.get(right), ident.name) else [right]
                if not any(self.has_column(sources.get(alias), ident.name) for alias in left):
                    missing.extend(left)
                if missing:
                    names = _dedupe(self.table_of(sources[alias]) or alias for alias in missing if alias in sources)
                    self.errors.append(
                        f"USING ({ident.name}) needs column '{ident.name}' in both joined tables, but it is missing "
                        f"from {_oxford(names or missing)}. Use JOIN ... ON with the foreign-key columns."
                    )
            left.append(right)

    @staticmethod
    def outer(scope: Scope) -> Scope | None:
        """Enclosing scope whose names are visible (correlated subqueries only; CTEs and derived tables
        cannot see the outer query)."""
        return scope.parent if scope.scope_type in (ScopeType.SUBQUERY, ScopeType.SET_OPERATION) else None

    def find_alias(self, scope: Scope | None, alias: str) -> exp.Table | Scope | None:
        while scope is not None:
            source = self.sources(scope).get(alias)
            if source is not None:
                return source
            scope = self.outer(scope)
        return None

    def record(self, table: str, column: str) -> None:
        self.referenced.append(str(_ColumnRef(table, column)))

    def resolve_qualified(self, scope: Scope, column: exp.Column) -> None:
        qualifier, name = column.table, column.name
        source = self.find_alias(scope, qualifier.lower())
        if source is None:
            self.errors.append(self.unknown_qualifier(scope, qualifier, name))
            return
        if isinstance(column.this, exp.Star):
            return
        table = self.table_of(source)
        if table:
            canonical = self.catalog.resolve_column(table, name)
            if canonical:
                self.record(table, canonical)
            else:
                self.errors.append(self.unknown_column_message(name, [table], quoted=column.this.quoted))
        elif isinstance(source, Scope):
            outputs = self.output_columns(source)
            if outputs is not None and name.lower() not in outputs:
                available = ", ".join(sorted(outputs)) or "none"
                self.errors.append(f"Unknown column '{name}' in '{qualifier}'. Its columns are: {available}.")

    def unknown_qualifier(self, scope: Scope, qualifier: str, name: str) -> str:
        table = self.catalog.resolve_table(qualifier)
        if table:
            for alias, source in self.sources(scope).items():
                if self.table_of(source) == table:
                    return (f"Table {table} is aliased as '{alias}' in this query; write {alias}.{name} "
                            f"instead of {qualifier}.{name}.")
            return f"Table {table} is used in {qualifier}.{name} but is not in the FROM clause; join it first."
        in_scope = ", ".join(sorted(self.sources(scope))) or "none"
        return f"Unknown table or alias '{qualifier}' in {qualifier}.{name}. Tables/aliases in scope: {in_scope}."

    def resolve_unqualified(self, scope: Scope, column: exp.Column) -> None:
        name = column.name
        key = name.lower()
        current: Scope | None = scope
        while current is not None:
            matches, unknown = self.candidates(current, key)
            if len(matches) == 1 or (matches and self.is_using_column(current, key)):
                alias, table = matches[0]
                if table:
                    self.record(table, self.catalog.resolve_column(table, name) or name)
                if current is not scope:
                    self.check_outer_projection(scope, column, alias, table)
                return
            if len(matches) > 1:
                tables = _dedupe(f"{table or alias} ({alias})" if table and table.lower() != alias else
                                 (table or alias) for alias, table in matches)
                example = matches[0][0]
                self.errors.append(f"Ambiguous column '{name}': it exists in {_oxford(tables)}. Qualify it with a "
                                   f"table alias, e.g. {example}.{name}.")
                return
            if unknown or key in self.select_aliases(current):
                return
            current = self.outer(current)
        tables = _dedupe(self.table_of(source) or "" for source in self.sources(scope).values())
        self.errors.append(self.unknown_column_message(name, tables, quoted=column.this.quoted))

    def check_outer_projection(self, scope: Scope, column: exp.Column, alias: str, table: str | None) -> None:
        """Reject a bare column selected by a value subquery that its own tables do not have.

        In ``CustomerID IN (SELECT CustomerID FROM Payments)`` Payments has no CustomerID, so SQLite silently
        reads the outer query's column instead: the condition compares the column with itself and the query
        returns wrong rows without any error. Columns used elsewhere in a subquery (ordinary correlation) and
        the select list of EXISTS (never compared) are not affected.
        """
        select = scope.expression
        if scope.scope_type is not ScopeType.SUBQUERY or not isinstance(select, exp.Select):
            return
        projection = column.parent if isinstance(column.parent, exp.Alias) else column
        if not any(projection is item for item in select.expressions) or isinstance(select.parent, exp.Exists):
            return
        inner = self.sources(scope)
        if not inner:
            return  # (SELECT outer_column) without FROM: a deliberate scalar reference
        tables = _dedupe(self.table_of(source) or "" for source in inner.values())
        canonical = self.catalog.resolve_column(table, column.name) if table else None
        outer = f"{table}.{canonical}" if table and canonical else f"{alias}.{column.name}"
        self.errors.append(
            self.unknown_column_message(column.name, tables, quoted=column.this.quoted)
            + f" SQLite would silently read {outer} from the outer query instead, so the subquery would compare "
            "that column with itself and return wrong rows; select a column of the subquery's own tables "
            "(join through the foreign keys if needed)."
        )

    def candidates(self, scope: Scope, key: str) -> tuple[list[tuple[str, str | None]], bool]:
        """Sources of ``scope`` that have column ``key``: [(alias, canonical table or None)], any-unknown flag."""
        matches: list[tuple[str, str | None]] = []
        unknown = False
        for alias, source in self.sources(scope).items():
            table = self.table_of(source)
            if table:
                if self.catalog.resolve_column(table, key):
                    matches.append((alias, table))
            elif isinstance(source, Scope):
                outputs = self.output_columns(source)
                if outputs is None:
                    unknown = True
                elif key in outputs:
                    matches.append((alias, None))
            else:
                unknown = True  # table-valued function or unknown table (reported by the tables check)
        if isinstance(scope.expression, exp.SetOperation):
            left = scope.expression
            while isinstance(left, exp.SetOperation):
                left = left.this
            if isinstance(left, exp.Select) and key in {name.lower() for name in left.named_selects}:
                unknown = True
        return matches, unknown

    @staticmethod
    def select_aliases(scope: Scope) -> set[str]:
        if not isinstance(scope.expression, exp.Select):
            return set()
        return {e.alias.lower() for e in scope.expression.expressions if isinstance(e, exp.Alias) and e.alias}

    @staticmethod
    def is_using_column(scope: Scope, key: str) -> bool:
        for join in scope.expression.args.get("joins") or []:
            if any(ident.name.lower() == key for ident in join.args.get("using") or []):
                return True
            if join.method and join.method.upper() == "NATURAL":
                return True
        return False

    def unknown_column_message(self, name: str, tables: list[str], *, quoted: bool) -> str:
        tables = [table for table in tables if table]
        if len(tables) == 1:
            columns = [col.name for col in self.catalog.tables[tables[0]].columns]
            message = f"Unknown column '{name}' in table {tables[0]}. Available columns: {', '.join(columns)}."
        else:
            where = f" in {_oxford(tables)}" if tables else ""
            message = f"Unknown column '{name}'{where}."
        suggestions = suggest_columns(name, self.catalog, tables)
        if suggestions:
            message += f" Did you mean {' or '.join(repr(s) for s in suggestions)}?"
        elsewhere = [t for t in self.catalog.table_names() if t not in tables and self.catalog.resolve_column(t, name)]
        if elsewhere:
            verb = "is" if len(elsewhere) == 1 else "are"
            message += f" Column '{name}' exists in {_oxford(elsewhere)}, which {verb} not joined here."
        if quoted:
            message += f" If \"{name}\" is a text value, write it in single quotes: '{name}'."
        return message


# ---- the validator --------------------------------------------------------------------------------


class _Validation:
    """One validation run; each ``check_*`` method appends exactly one ValidationCheck."""

    def __init__(self, sql: str, catalog: SchemaCatalog, dialect: str, db_path: str | Path | None) -> None:
        self.catalog = catalog
        self.dialect = dialect
        self.db_path = db_path
        self.text = strip_sql_fences(sql)
        self.normalized = self.text
        self.statements: list[exp.Expression] = []
        self.tree: exp.Expression | None = None
        self.checks: list[ValidationCheck] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.tables: list[str] = []
        self.columns: list[str] = []
        self.destructive = False
        self.skip_reason: str | None = None
        self._semantic: exp.Expression | None = None

    # bookkeeping ---------------------------------------------------------------------------------

    def add(self, name: str, passed: bool, message: str, errors: Iterable[str] = ()) -> None:
        self.checks.append(ValidationCheck(name, CHECK_LABELS[name], passed, message))
        self.errors.extend(errors)

    def skip(self, name: str) -> bool:
        if self.skip_reason is None:
            return False
        self.add(name, False, f"Skipped: {self.skip_reason}.")
        return True

    def report(self) -> ValidationReport:
        errors = _dedupe(self.errors)
        return ValidationReport(
            valid=not errors,
            checks=self.checks,
            errors=errors,
            warnings=_dedupe(self.warnings),
            sql=self.normalized,
            tables=self.tables,
            columns=_dedupe(self.columns),
            destructive=self.destructive,
        )

    # 1. syntax -----------------------------------------------------------------------------------

    def check_syntax(self) -> None:
        label = SUPPORTED_DIALECTS[self.dialect]
        if not self.text:
            self.add("syntax", False, "The query is empty.", ["The query is empty; write one SELECT statement."])
            self.skip_reason = "no SQL"
            return
        try:
            self.statements = parse_statements(self.text, self.dialect)
        except TokenError:
            message = "Syntax error: unterminated string literal or quoted identifier."
            self.add("syntax", False, message, [message])
            self.skip_reason = "syntax error"
            return
        except ParseError as exc:
            message = self.parse_error_message(exc)
            self.add("syntax", False, message, [message])
            self.skip_reason = "syntax error"
            return
        except SqlglotError as exc:
            message = f"Syntax error: {exc}"
            self.add("syntax", False, message, [message])
            self.skip_reason = "syntax error"
            return
        problem = self.structural_problem()
        if problem:
            self.add("syntax", False, problem, [problem])
            self.skip_reason = "syntax error"
            return
        if len(self.statements) == 1:
            self.tree = self.statements[0]
            self.normalized = render(self.tree, "sqlite")
        dates = _unquoted_date_errors(self.text, self.dialect)
        if self.tree is not None:
            try:
                dates += _text_number_errors(self.semantic_tree())
            except Exception:  # noqa: BLE001 - a correctness hint, not a safety check: never reject a query over it
                logger.exception("Text/number comparison check failed")
        if dates:  # parses, but silently compares against a number: an error, yet not fatal for later checks
            self.add("syntax", False, dates[0], dates)
            return
        self.add("syntax", True, f"Valid {label} syntax.")

    def structural_problem(self) -> str | None:
        if not self.statements:
            return "The query is empty; write one SELECT statement."
        for statement in self.statements:
            command = _unrecognized_command(statement)
            if command:
                return f"Syntax error: unrecognized statement starting with '{command}'. Queries must start with SELECT or WITH."
            for select in statement.find_all(exp.Select):
                if not select.expressions:
                    return "Syntax error: the SELECT list is empty; list the columns to return (or *)."
        return None

    def parse_error_message(self, exc: ParseError) -> str:
        first = exc.errors[0] if exc.errors else {}
        description = str(first.get("description") or exc).split("\n")[0]
        description = re.sub(
            r"Required keyword: '\w+' missing for <class '[\w.]*?(\w+)'>",
            lambda match: f"incomplete {_CLAUSES.get(match.group(1), 'expression')}",
            description,
        )
        near = first.get("highlight")
        position = f" near '{near}'" if near else ""
        if first.get("line"):
            position += f" (line {first.get('line')}, column {first.get('col')})"
        message = f"Syntax error{position}: {description.rstrip('.')}."
        first_word = self.text.split(None, 1)[0].upper() if self.text.split() else ""
        if first_word not in ("SELECT", "WITH", "(", "VALUES") and first_word not in _KNOWN_COMMANDS:
            message += " Queries must start with SELECT or WITH."
        if re.search(r"\bSELECT\s+TOP\s+\d+", self.text, re.IGNORECASE):
            message += " SQLite uses LIMIT n at the end of the query instead of TOP n."
        return message

    # 2. single statement ---------------------------------------------------------------------------

    def check_single_statement(self) -> None:
        if self.skip("single_statement"):
            return
        count = len(self.statements)
        if count == 1:
            self.add("single_statement", True, "Exactly one statement.")
            return
        message = f"Found {count} statements; submit exactly one SELECT statement (no ';'-separated batches)."
        self.add("single_statement", False, message, [message])

    # 3. read-only --------------------------------------------------------------------------------

    def check_read_only(self) -> None:
        if self.skip_reason in ("no SQL",):
            self.skip("read_only")
            return
        if self.skip_reason == "syntax error":
            findings = _ReadOnlyFindings()
            _scan_tokens(self.text, self.dialect, findings)
            if findings.errors:  # flag writes even when the statement does not parse
                self.destructive = findings.destructive
                self.add("read_only", False, findings.errors[0], findings.errors)
            else:
                self.skip("read_only")
            return
        findings = _read_only_findings(self.statements, self.text, self.dialect)
        self.destructive = findings.destructive
        if findings.errors:
            self.add("read_only", False, findings.errors[0], findings.errors)
            self.skip_reason = "not a read-only query"
            return
        if len(self.statements) != 1:
            self.add("read_only", True, "No write operations found.")
            self.skip_reason = "multiple statements"
            return
        self.add("read_only", True, "SELECT only.")

    # 4. tables -----------------------------------------------------------------------------------

    def check_tables(self) -> None:
        if self.skip("tables"):
            return
        assert self.tree is not None
        cte_names = {cte.alias_or_name.lower() for cte in self.tree.find_all(exp.CTE)}
        errors: list[str] = []
        for table in self.tree.find_all(exp.Table, bfs=False):
            if isinstance(table.this, exp.Func):
                function = table.this
                name = function.name if isinstance(function, exp.Anonymous) else function.sql_name()
                if name.lower() not in _ALLOWED_TABLE_FUNCTIONS:
                    errors.append(f"Unknown table function '{name.lower()}'. Query the provided tables instead.")
                continue
            name = table.name
            if table.db and table.db.lower() not in _DEFAULT_SCHEMAS:
                errors.append(f"Unknown schema '{table.db}' in {table.db}.{name}; the tables live in the main schema.")
                continue
            if not table.db and name.lower() in cte_names:
                continue
            canonical = self.catalog.resolve_table(name)
            if canonical:
                self.tables.append(canonical)
                continue
            message = f"Unknown table '{name}'. Available tables: {', '.join(self.catalog.table_names())}."
            suggestions = suggest_tables(name, self.catalog)
            if suggestions:
                message += f" Did you mean {' or '.join(repr(s) for s in suggestions)}?"
            errors.append(message)
        self.tables = _dedupe(self.tables)
        if errors:
            self.add("tables", False, errors[0], _dedupe(errors))
            self.skip_reason = "unknown table"
            return
        self.add("tables", True, ", ".join(self.tables) if self.tables else "No tables referenced.")

    # 5. columns ----------------------------------------------------------------------------------

    def semantic_tree(self) -> exp.Expression:
        """The statement as SQLite (identifier rules of the database the query will run on)."""
        assert self.tree is not None
        if self._semantic is None:
            self._semantic = self.tree
            if self.dialect != "sqlite":
                try:
                    self._semantic = parse_statements(self.normalized, "sqlite")[0]
                except (SqlglotError, IndexError):
                    logger.debug("Could not re-parse the transpiled SQL; checking the original tree")
        return self._semantic

    def check_columns(self) -> None:
        if self.skip("columns"):
            return
        tree = self.semantic_tree()
        resolver = _Resolver(self.catalog, tree)
        resolver.run(tree)
        self.columns = resolver.referenced
        errors = _dedupe(resolver.errors)
        if not errors:
            failure = self.qualify_failure(tree)
            if failure:
                errors = [failure]
        if errors:
            self.add("columns", False, errors[0], errors)
            self.skip_reason = "unknown or ambiguous columns"
            return
        count = len(_dedupe(self.columns))
        message = f"All {count} referenced columns exist." if count else "No column references."
        self.add("columns", True, message)

    def qualify_failure(self, tree: exp.Expression) -> str | None:
        """sqlglot's own resolution as a second opinion; the database decides when the two disagree."""
        try:
            qualify(
                tree.copy(),
                dialect="sqlite",
                schema=self.catalog.to_sqlglot_schema(),
                validate_qualify_columns=True,
                identify=False,
                quote_identifiers=False,
            )
            return None
        except (OptimizeError, SqlglotError) as exc:
            reason = str(exc).split(". Line:")[0]
        except Exception as exc:  # noqa: BLE001 - sqlglot internals must never break validation
            logger.debug("qualify crashed: %s", exc)
            return None
        sqlite_error = self.dry_run()
        if sqlite_error is None and self.db_path is not None:
            logger.debug("qualify rejected SQL that SQLite accepts (%s); trusting SQLite", reason)
            return None
        detail = f" (SQLite: {sqlite_error})" if sqlite_error else ""
        return f"Column check failed: {reason}{detail}."

    # 6. joins ------------------------------------------------------------------------------------

    def check_joins(self) -> None:
        if self.skip_reason in ("no SQL", "syntax error", "not a read-only query", "multiple statements",
                                "unknown table"):
            self.skip("joins")
            return
        tree = self.semantic_tree()
        checker = _JoinChecker(self.catalog, tree)
        checker.run()
        self.warnings.extend(checker.warnings)
        if checker.errors:
            self.add("joins", False, checker.errors[0], checker.errors)
            return
        if not checker.conditions and not checker.warnings:
            self.add("joins", True, "No joins.")
        elif checker.warnings:
            self.add("joins", True, f"Joins are valid, with {_plural(len(checker.warnings), 'warning')}.")
        else:
            self.add("joins", True, f"All {_plural(checker.conditions, 'join condition')} follow foreign keys.")

    # 7. executable -------------------------------------------------------------------------------

    def dry_run(self) -> str | None:
        """``None`` if SQLite compiles a query plan for the normalized SQL, else SQLite's error text."""
        if self.db_path is None:
            return None
        try:
            conn = connect_readonly(self.db_path)
        except sqlite3.Error as exc:
            return f"could not open the database ({exc})"
        try:
            conn.execute(f"EXPLAIN QUERY PLAN {self.normalized}").fetchall()
            return None
        except sqlite3.Error as exc:
            return str(exc)
        except MemoryError:  # SQLITE_NOMEM: the shared SQLite heap limit is momentarily exhausted
            return "out of memory while planning the query"
        finally:
            conn.close()

    @staticmethod
    def sqlite_error_message(error: str) -> str:
        message = f"Database dry-run failed: {error.rstrip('.')}."
        match = re.match(r"no such function: (\w+)", error)
        if match:
            hint = _FUNCTION_HINTS.get(match.group(1).lower())
            message += f" SQLite has no {match.group(1)}() function" + (f"; {hint}." if hint else ".")
        return message

    def check_executable(self) -> None:
        if self.skip("executable"):
            return
        if self.db_path is None:
            self.add("executable", True, "Skipped (no database)")
            return
        error = self.dry_run()
        if error:
            message = self.sqlite_error_message(error)
            self.add("executable", False, message, [message])
        else:
            self.add("executable", True, "Query plan compiled.")

    def run(self) -> ValidationReport:
        checks = (
            ("syntax", self.check_syntax), ("single_statement", self.check_single_statement),
            ("read_only", self.check_read_only), ("tables", self.check_tables), ("columns", self.check_columns),
            ("joins", self.check_joins), ("executable", self.check_executable),
        )
        for name, check in checks:
            try:
                check()
            except Exception as exc:  # noqa: BLE001 - fail closed instead of crashing the agent
                logger.exception("Validation check %s crashed", name)
                message = f"The {CHECK_LABELS[name].lower()} check could not analyze this query ({type(exc).__name__})."
                self.add(name, False, message, [message])
                self.skip_reason = self.skip_reason or "the query could not be analyzed"
        return self.report()


# ---- joins ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Side:
    alias: str
    table: str
    column: str

    def __str__(self) -> str:
        return f"{self.table}.{self.column}"


class _JoinChecker:
    """Checks every JOIN condition (and implicit comma-join predicates) against the foreign keys.

    Columns of a CTE or derived table are traced to the real column their select list passes through
    unchanged (``WITH t AS (SELECT CustomerID ...)``: ``t.CustomerID`` is ``Orders.CustomerID``), and key
    equalities between a subquery and its outer query (``EXISTS (... WHERE o.CustomerID = c.CustomerID)``)
    are checked like joins.
    """

    _MAX_LINEAGE_DEPTH = 8

    def __init__(self, catalog: SchemaCatalog, tree: exp.Expression) -> None:
        self.catalog = catalog
        self.tree = tree
        self.resolver = _Resolver(catalog, tree)
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.conditions = 0

    def run(self) -> None:
        for scope in self.resolver.scopes:
            select = scope.expression
            if isinstance(select, exp.Select) and select.args.get("joins"):
                self.check_scope(scope, select)
            if isinstance(select, exp.Select) and scope.scope_type is ScopeType.SUBQUERY:
                self.check_correlation(scope, select)
        self.errors = _dedupe(self.errors)
        self.warnings = _dedupe(self.warnings)

    def check_scope(self, scope: Scope, select: exp.Select) -> None:
        sources = _Resolver.sources(scope)
        from_clause = select.args.get("from_") or select.args.get("from")
        left: list[str] = [from_clause.this.alias_or_name.lower()] if from_clause else []
        for join in select.args["joins"]:
            right = join.this.alias_or_name.lower()
            on, using = join.args.get("on"), join.args.get("using")
            if isinstance(on, exp.Boolean) and on.this is True:
                on = None  # sqlglot writes a bare "JOIN t" as "JOIN t ON TRUE"; its condition, if any, is in WHERE
            method = (join.method or "").upper()
            allowed = set(left) | {right}
            if using:
                for ident in using:
                    self.check_using(sources, left, right, ident.name)
            elif on is not None:
                if not self.linked(on, sources, {right}, allowed) and self.is_table(sources, right):
                    self.warnings.append(
                        f"The join with {self.label(sources, right)} has no column equality condition (ON "
                        f"{on.sql(dialect='sqlite')}); this can multiply rows like a Cartesian product."
                    )
                for first, second in self.equalities(on, sources, {right}, allowed):
                    self.check_pair(first, second)
            elif method == "NATURAL":
                self.warnings.append(
                    f"NATURAL JOIN with {self.label(sources, right)} joins on every column with the same name; "
                    "prefer an explicit JOIN ... ON condition."
                )
            else:
                where = select.args.get("where")
                for first, second in self.equalities(where, sources, {right}, allowed):
                    self.check_pair(first, second)
                if not self.linked(where, sources, {right}, allowed) and self.is_table(sources, right):
                    self.warnings.append(self.cartesian_warning(sources, left, right))
            left.append(right)

    def check_correlation(self, scope: Scope, select: exp.Select) -> None:
        """Key equalities that tie a subquery to its outer query (a semi-join) must follow the foreign keys too.

        Only key columns are checked (``o.OrderID = c.CustomerID`` is an error); comparisons of ordinary
        values, such as ``o.ShippingState = c.State``, are left alone.
        """
        where = select.args.get("where")
        inner = _Resolver.sources(scope)
        if where is None or not inner:
            return
        outer: dict[str, exp.Table | Scope] = {}
        current = _Resolver.outer(scope)
        while current is not None:
            for alias, source in _Resolver.sources(current).items():
                outer.setdefault(alias, source)  # the nearest enclosing query wins
            current = _Resolver.outer(current)
        outer = {alias: source for alias, source in outer.items() if alias not in inner}
        if not outer:
            return
        for eq in where.find_all(exp.EQ):
            if eq.parent_select is not select:
                continue  # belongs to a deeper subquery, checked in its own scope
            first = self.scoped_side(eq.this, inner, outer)
            second = self.scoped_side(eq.expression, inner, outer)
            if first is None or second is None or (first.alias in inner) == (second.alias in inner):
                continue
            if first.column.lower().endswith("id") and second.column.lower().endswith("id"):
                self.check_key_pair(first, second)

    def scoped_side(
        self, column: exp.Expression, inner: dict[str, exp.Table | Scope], outer: dict[str, exp.Table | Scope]
    ) -> _Side | None:
        """Side of a correlated predicate: unqualified names resolve in the subquery first, then outside."""
        if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star):
            return None
        if column.table:
            alias = column.table.lower()
            sources = inner if alias in inner else outer
            return self.side(column, sources, {alias}) if alias in sources else None
        for sources in (inner, outer):
            owners = [alias for alias in sources if self.resolver.has_column(sources[alias], column.name)]
            if owners:
                return self.side(column, sources, set(owners)) if len(owners) == 1 else None
        return None

    @staticmethod
    def is_table(sources: dict[str, exp.Table | Scope], alias: str) -> bool:
        return isinstance(sources.get(alias), exp.Table)

    def label(self, sources: dict[str, exp.Table | Scope], alias: str) -> str:
        source = sources.get(alias)
        table = self.catalog.resolve_table(source.name) if isinstance(source, exp.Table) else None
        return table or alias

    def side(self, column: exp.Expression, sources: dict[str, exp.Table | Scope], allowed: set[str]) -> _Side | None:
        """The real ``table.column`` behind a column of one of the ``allowed`` sources (``None`` if unknown)."""
        alias = self.owner(column, sources, allowed)
        origin = self.origin(sources.get(alias), column.name) if alias else None
        return _Side(alias, *origin) if alias and origin else None

    def owner(self, column: exp.Expression, sources: dict[str, exp.Table | Scope], allowed: set[str]) -> str | None:
        """The alias in ``allowed`` a column belongs to: its qualifier, or the one source that has it."""
        if not isinstance(column, exp.Column) or isinstance(column.this, exp.Star):
            return None
        if column.table:
            alias = column.table.lower()
            return alias if alias in allowed else None
        owners = [alias for alias in allowed if alias in sources and self.resolver.has_column(sources[alias], column.name)]
        return owners[0] if len(owners) == 1 else None

    def origin(self, source: exp.Table | Scope | None, name: str, depth: int = 0) -> tuple[str, str] | None:
        """``(table, column)`` that column ``name`` of ``source`` reads: the table's own column, or for a CTE or
        derived table the real column its select list passes through unchanged (``None`` for expressions,
        aggregates and set operations, whose values are not a key of any table)."""
        if isinstance(source, exp.Table):
            table = self.catalog.resolve_table(source.name)
            column = self.catalog.resolve_column(table, name) if table else None
            return (table, column) if table and column else None
        if not isinstance(source, Scope) or depth > self._MAX_LINEAGE_DEPTH:
            return None
        query = source.expression
        if not isinstance(query, exp.Select):
            return None
        key = name.lower()
        alias = query.parent.args.get("alias") if isinstance(query.parent, exp.CTE | exp.Subquery) else None
        if isinstance(alias, exp.TableAlias) and alias.columns:  # WITH t(a, b) AS (...): names by position
            names = [column.name.lower() for column in alias.columns]
            position = names.index(key) if key in names else len(query.expressions)
            projections = [query.expressions[position]] if position < len(query.expressions) else []
        else:
            projections = [item for item in query.expressions if not item.is_star and item.alias_or_name.lower() == key]
        inner = _Resolver.sources(source)
        if len(projections) > 1:
            return None
        if not projections:  # SELECT * / SELECT t.*: the one source that has the column
            stars = [item for item in query.expressions if item.is_star]
            holders = [
                inner_source for alias_name, inner_source in inner.items()
                for star in stars
                if (not isinstance(star, exp.Column) or star.table.lower() == alias_name)
                and self.resolver.has_column(inner_source, name)
            ]
            return self.origin(holders[0], name, depth + 1) if len(holders) == 1 else None
        projection = projections[0].this if isinstance(projections[0], exp.Alias) else projections[0]
        if not isinstance(projection, exp.Column) or isinstance(projection.this, exp.Star):
            return None
        owner = self.owner(projection, inner, set(inner))
        return self.origin(inner[owner], projection.name, depth + 1) if owner else None

    def equalities(
        self, condition: exp.Expression | None, sources: dict[str, exp.Table | Scope], must: set[str], allowed: set[str]
    ) -> list[tuple[_Side, _Side]]:
        """Column = column predicates linking one of ``must`` with another source in ``allowed``."""
        if condition is None:
            return []
        pairs = []
        for eq in condition.find_all(exp.EQ):
            first, second = self.side(eq.this, sources, allowed), self.side(eq.expression, sources, allowed)
            if first is None or second is None or first.alias == second.alias:
                continue
            if first.alias in must or second.alias in must:
                pairs.append((first, second))
        return pairs

    def linked(
        self, condition: exp.Expression | None, sources: dict[str, exp.Table | Scope], must: set[str], allowed: set[str]
    ) -> bool:
        """Whether some column = column predicate links one of ``must`` with another source, traceable or not
        (``t.total = c.CustomerID`` with a computed ``t.total`` is still a join condition)."""
        if condition is None:
            return False
        for eq in condition.find_all(exp.EQ):
            first, second = self.owner(eq.this, sources, allowed), self.owner(eq.expression, sources, allowed)
            if first and second and first != second and (first in must or second in must):
                return True
        return False

    def check_using(self, sources: dict[str, exp.Table | Scope], left: list[str], right: str, name: str) -> None:
        right_side = self.side(exp.column(name, table=right), sources, {right})
        left_side = next(
            (side for alias in left if (side := self.side(exp.column(name, table=alias), sources, {alias}))), None
        )
        if right_side and left_side:
            self.check_pair(left_side, right_side)

    def check_pair(self, first: _Side, second: _Side) -> None:
        self.conditions += 1
        if self.matching_keys(first, second):
            return
        condition = f"{first} = {second}"
        if first.column.lower().endswith("id") and second.column.lower().endswith("id"):
            self.check_key_pair(first, second)
        else:
            self.warnings.append(
                f"Join condition {condition} does not follow a foreign key; make sure the match is intended."
            )

    def matching_keys(self, first: _Side, second: _Side) -> bool:
        """A foreign key in either direction, or two columns that identify the same thing."""
        if self.catalog.is_valid_join(first.table, first.column, second.table, second.column):
            return True
        domain = self.catalog.key_domain(first.table, first.column)
        return domain is not None and domain == self.catalog.key_domain(second.table, second.column)

    def check_key_pair(self, first: _Side, second: _Side) -> None:
        """Error for two id columns that do not identify the same thing (OrderID = CustomerID)."""
        if self.matching_keys(first, second):
            return  # e.g. OrderItems.OrderID = Payments.OrderID: both identify an order
        self.errors.append(
            f"Invalid join: {first} = {second} is not a valid relationship. "
            f"Valid relationships for these tables: {self.valid_relationships(first.table, second.table)}."
        )

    def valid_relationships(self, first: str, second: str) -> str:
        direct = self.catalog.relationships_between({first, second})
        if first != second:
            direct = [rel for rel in direct if {rel.from_table, rel.to_table} == {first, second}]
        path = direct or self.catalog.join_path(first, second)
        if not path:
            return f"none ({first} and {second} are not related through foreign keys)"
        return ", ".join(str(rel) for rel in path)

    def cartesian_warning(self, sources: dict[str, exp.Table | Scope], left: list[str], right: str) -> str:
        right_table = self.label(sources, right)
        left_tables = [self.label(sources, alias) for alias in left]
        hints = []
        for table in left_tables:
            path = self.catalog.join_path(table, right_table) if table != right_table else []
            if len(path) == 1:
                hints.append(str(path[0]))
        message = (f"{right_table} is joined to {_oxford(left_tables)} without a join condition (CROSS JOIN), "
                   "which returns every combination of rows.")
        if hints:
            message += f" Use JOIN ... ON with the foreign key: {hints[0]}."
        return message


def validate_sql(
    sql: str, catalog: SchemaCatalog, *, dialect: str = "sqlite", db_path: str | Path | None = None
) -> ValidationReport:
    """Run the seven checks on ``sql`` (written in ``dialect``) and return the full report.

    ``report.sql`` is the normalized single statement in SQLite syntax (what will be executed); checks
    after a fatal failure are reported as ``passed=False`` with a "Skipped: ..." message.
    """
    return _Validation(sql, catalog, normalize_dialect(dialect), db_path).run()

