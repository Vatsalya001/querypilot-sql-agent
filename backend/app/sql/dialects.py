"""SQL dialect support: names, transpilation and pretty-printing (sqlglot based).

Validation and execution always happen in SQLite; PostgreSQL and MySQL are output (and input) dialects.
The generators render negations idiomatically (``x IS NOT NULL``, ``x NOT IN (...)``) instead of
sqlglot's default ``NOT x IS NULL`` and keep short parenthesized expressions on one line. Common
SQLite-only date idioms are converted when transpiling; anything that cannot be converted raises
``ValueError`` instead of producing invalid SQL.
"""

from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.dialects.mysql import MySQL
from sqlglot.dialects.postgres import Postgres
from sqlglot.dialects.sqlite import SQLite
from sqlglot.errors import ErrorLevel, ParseError, SqlglotError
from sqlglot.generator import Generator

SUPPORTED_DIALECTS: dict[str, str] = {"sqlite": "SQLite", "postgres": "PostgreSQL", "mysql": "MySQL"}

_ALIASES = {
    "sqlite": "sqlite",
    "sqlite3": "sqlite",
    "postgres": "postgres",
    "postgresql": "postgres",
    "pg": "postgres",
    "mysql": "mysql",
    "mariadb": "mysql",
}
_MODIFIER = re.compile(r"^\s*([+-]?\d+)\s+(day|month|year|hour|minute|second)s?\s*$", re.IGNORECASE)
_START_OF = re.compile(r"^\s*start of (month|year)\s*$", re.IGNORECASE)
_SQLITE_ONLY_FUNCTIONS = frozenset(
    {"julianday", "datetime", "printf", "unixepoch", "total", "time", "strftime", "changes", "last_insert_rowid"}
)
_PORTABLE_TIME_TOKENS: dict[str, frozenset[str]] = {
    # strftime() tokens that sqlglot maps to a TO_CHAR / DATE_FORMAT pattern with the same meaning. Others
    # (%w, %W, %u, %s, %f, ...) come out as a different field or as literal text, so they are refused.
    "postgres": frozenset("YmdHMSjI"),
    "mysql": frozenset("YmdHMSjwI%"),
}
_TIME_TOKEN = re.compile(r"%(.)", re.DOTALL)


_INLINE_PAREN_MAX_CHARS = 60
"""Parenthesized expressions up to this length stay on one line when pretty-printing."""


class _ReadableGenerator(Generator):
    """Generator mixin: ``a IS NOT NULL`` / ``a NOT IN (...)`` / ``a NOT BETWEEN x AND y``, and short
    parenthesized expressions such as ``(1 - oi.Discount)`` kept on one line by the pretty printer."""

    def paren_sql(self, expression: exp.Paren) -> str:
        inner = expression.this
        if self.pretty and not isinstance(inner, exp.Query):
            flat = self.sql(inner)
            if "\n" not in flat and len(flat) <= _INLINE_PAREN_MAX_CHARS:
                return f"({flat})"
        return super().paren_sql(expression)

    def not_sql(self, expression: exp.Not) -> str:
        inner = expression.this
        if isinstance(inner, exp.Is):
            return f"{self.sql(inner, 'this')} IS NOT {self.sql(inner, 'expression')}"
        if isinstance(inner, exp.In | exp.Between):
            keyword = " IN " if isinstance(inner, exp.In) else " BETWEEN "
            subject = self.sql(inner, "this")
            rendered = self.sql(inner)
            if rendered.startswith(subject + keyword):
                return f"{subject} NOT{keyword}{rendered[len(subject) + len(keyword):]}"
        return super().not_sql(expression)


class QueryPilotSQLite(SQLite):
    """SQLite with readable negations, and comma joins written back as commas."""

    class Generator(_ReadableGenerator, SQLite.Generator):
        def join_sql(self, expression: exp.Join) -> str:
            # sqlglot reads "FROM a, b" as a CROSS JOIN (that keeps join precedence when transpiling). In SQLite
            # the CROSS JOIN keyword also pins the join order, so the normalized SQL, which is what runs, would
            # stop the query planner from choosing the cheaper order. A comma returns the same rows without it.
            plain_cross = (
                (expression.kind or "").upper() == "CROSS"
                and not expression.side
                and not expression.method
                and expression.args.get("on") is None
                and not expression.args.get("using")
            )
            if plain_cross:
                return f", {self.sql(expression, 'this')}"
            return super().join_sql(expression)


class QueryPilotPostgres(Postgres):
    """PostgreSQL with readable negations."""

    class Generator(_ReadableGenerator, Postgres.Generator):
        pass


class QueryPilotMySQL(MySQL):
    """MySQL with readable negations."""

    class Generator(_ReadableGenerator, MySQL.Generator):
        pass


_WRITERS: dict[str, type[Any]] = {"sqlite": QueryPilotSQLite, "postgres": QueryPilotPostgres, "mysql": QueryPilotMySQL}


def normalize_dialect(name: str | None) -> str:
    """Canonical dialect key (``sqlite``, ``postgres`` or ``mysql``); ``None``/empty means ``sqlite``.

    Raises ``ValueError`` for unknown dialect names.
    """
    if name is None or not name.strip():
        return "sqlite"
    key = name.strip().lower()
    if key not in _ALIASES:
        raise ValueError(f"Unsupported SQL dialect '{name}'. Supported dialects: {', '.join(SUPPORTED_DIALECTS)}.")
    return _ALIASES[key]


def render(expression: exp.Expression, dialect: str = "sqlite", *, pretty: bool = False) -> str:
    """Generate SQL text for ``expression`` in ``dialect`` with the readable generators (comments dropped)."""
    return expression.sql(dialect=_WRITERS[normalize_dialect(dialect)], pretty=pretty, comments=False)


def parse_statements(sql: str, dialect: str = "sqlite") -> list[exp.Expression]:
    """Parse ``sql`` strictly (raises ``sqlglot.errors.SqlglotError``).

    Empty statements, including a comment after the last semicolon, are dropped.
    """
    try:
        parsed = sqlglot.parse(sql, read=normalize_dialect(dialect), error_level=ErrorLevel.RAISE)
    except RecursionError as exc:
        raise ParseError("The SQL is nested too deeply to analyze.") from exc
    return [statement for statement in parsed if statement is not None and not isinstance(statement, exp.Semicolon)]


def _interval(unit: str, amount: int, base: exp.Expression) -> exp.Expression:
    node_type = exp.DateSub if amount < 0 else exp.DateAdd
    return node_type(this=base, expression=exp.Literal.number(abs(amount)), unit=exp.var(unit.upper()))


def _is_now(node: exp.Expression | None) -> bool:
    return isinstance(node, exp.Literal) and node.is_string and node.this.strip().lower() == "now"


def _convert_date(node: exp.Date) -> exp.Expression | None:
    """SQLite ``date(x [, modifier, ...])`` -> portable expression, or ``None`` if there is no safe equivalent.

    The modifiers apply left to right (``date('now', 'start of month', '-1 month')`` is the first day of the
    previous month); sqlglot keeps the first in ``zone`` and the rest in ``expressions``. If any modifier
    cannot be converted the whole call is left alone, so :func:`_assert_portable` refuses it.
    """
    subject = node.this
    modifiers = [modifier for modifier in (node.args.get("zone"), *node.expressions) if modifier is not None]
    is_now = _is_now(subject)
    result: exp.Expression = exp.CurrentDate() if is_now else exp.cast(subject, "DATE")
    for position, modifier in enumerate(modifiers):
        if not isinstance(modifier, exp.Literal) or not modifier.is_string:
            return None
        if match := _MODIFIER.match(modifier.this):
            result = _interval(match.group(2), int(match.group(1)), result)
        elif match := _START_OF.match(modifier.this):
            target = subject if position == 0 and not is_now else result
            result = exp.DateTrunc(unit=exp.var(match.group(1).upper()), this=target)
        else:
            return None
    return result


def _convert_anonymous(node: exp.Anonymous) -> exp.Expression | None:
    name = node.name.lower()
    args = node.expressions
    if name == "datetime" and len(args) == 1 and _is_now(args[0]):
        return exp.CurrentTimestamp()
    if name == "total" and len(args) == 1:
        return exp.Coalesce(this=exp.Sum(this=args[0].copy()), expressions=[exp.Literal.number(0)])
    return None


def _julianday_arg(node: exp.Expression) -> exp.Expression | None:
    if isinstance(node, exp.Anonymous) and node.name.lower() == "julianday" and len(node.expressions) == 1:
        argument = node.expressions[0]
        return exp.CurrentDate() if _is_now(argument) else argument.copy()
    return None


def _portable(node: exp.Expression) -> exp.Expression:
    """Transform callback: rewrite SQLite-only idioms into dialect-neutral sqlglot nodes."""
    if isinstance(node, exp.Sub):
        left, right = _julianday_arg(node.this), _julianday_arg(node.expression)
        if left is not None and right is not None:
            return exp.DateDiff(this=left, expression=right, unit=exp.var("DAY"))
    if isinstance(node, exp.TimeToStr):
        subject = node.this.this if isinstance(node.this, exp.TsOrDsToTimestamp | exp.TsOrDsToDate) else node.this
        if _is_now(subject):
            node = node.copy()  # strftime(fmt, 'now'): MySQL cannot parse the text 'now' as a date
            node.set("this", exp.CurrentTimestamp())
            return node
    if isinstance(node, exp.Date):
        return _convert_date(node) or node
    if isinstance(node, exp.Anonymous):
        return _convert_anonymous(node) or node
    return node


def _filter_to_case(node: exp.Expression) -> exp.Expression:
    """MySQL has no ``AGG(...) FILTER (WHERE c)``; ``AGG(CASE WHEN c THEN x END)`` means the same."""
    if not isinstance(node, exp.Filter):
        return node
    aggregate, where = node.this, node.expression
    condition = where.this if isinstance(where, exp.Where) else where
    if not isinstance(aggregate, exp.AggFunc) or condition is None:
        raise ValueError("This FILTER clause has no automatic MySQL equivalent.")
    argument = aggregate.this
    if isinstance(argument, exp.Star) and isinstance(aggregate, exp.Count):
        replacement: exp.Expression = exp.Literal.number(1)
        holder, key = aggregate, "this"
    elif isinstance(argument, exp.Distinct) and len(argument.expressions) == 1:
        replacement, holder, key = argument.expressions[0], argument, "expressions"
    elif isinstance(argument, exp.Expression) and not isinstance(argument, exp.Star | exp.Distinct):
        replacement, holder, key = argument, aggregate, "this"
    else:
        raise ValueError("This FILTER clause has no automatic MySQL equivalent.")
    case = exp.Case(ifs=[exp.If(this=condition.copy(), true=replacement.copy())])
    rewritten = aggregate.copy()
    target = rewritten if holder is aggregate else rewritten.this
    target.set(key, [case] if key == "expressions" else case)
    return rewritten


def _numeric_round(node: exp.Expression) -> exp.Expression:
    """PostgreSQL only rounds NUMERIC to n decimals (``round(double precision, int)`` does not exist)."""
    if isinstance(node, exp.Round) and node.args.get("decimals") is not None and not node.this.is_type("decimal"):
        node = node.copy()
        node.set("this", exp.cast(node.this, "NUMERIC"))
    return node


def _portable_time_format(node: exp.TimeToStr, target: str) -> bool:
    """Whether every strftime() token in the format means the same in the target's formatting function."""
    fmt = node.args.get("format")
    if not isinstance(fmt, exp.Literal) or not fmt.is_string:
        return False
    allowed = _PORTABLE_TIME_TOKENS[target]
    if any(token not in allowed for token in _TIME_TOKEN.findall(fmt.this)) or fmt.this.endswith("%"):
        return False
    # TO_CHAR reads letters outside the tokens as patterns too ('quarter' contains Q, the quarter number)
    return target != "postgres" or not re.search(r"[A-Za-z]", _TIME_TOKEN.sub("", fmt.this))


def _assert_portable(expression: exp.Expression, target: str) -> None:
    for node in expression.walk():
        if isinstance(node, exp.Anonymous) and node.name.lower() in _SQLITE_ONLY_FUNCTIONS:
            name = node.name.lower()
        elif isinstance(node, exp.Glob):
            name = "glob"
        elif isinstance(node, exp.Date) and (node.args.get("zone") is not None or node.expressions):
            name = "date (with modifiers)"
        elif isinstance(node, exp.TimeToStr) and not _portable_time_format(node, target):
            name = "strftime (with this format)"
        else:
            continue
        raise ValueError(f"The SQLite function {name}() has no automatic {SUPPORTED_DIALECTS[target]} equivalent.")


def transpile(sql: str, *, read: str = "sqlite", write: str = "sqlite", pretty: bool = True) -> str:
    """Translate ``sql`` from dialect ``read`` to dialect ``write``.

    Raises ``ValueError`` if the SQL cannot be parsed or contains constructs with no equivalent in
    ``write`` (for example SQLite's ``printf()`` when writing PostgreSQL).
    """
    source, target = normalize_dialect(read), normalize_dialect(write)
    try:
        statements = parse_statements(sql, source)
    except SqlglotError as exc:
        raise ValueError(f"Could not parse the SQL as {SUPPORTED_DIALECTS[source]}: {exc}") from exc
    if not statements:
        raise ValueError("There is no SQL to transpile.")
    rendered = []
    for statement in statements:
        if source == "sqlite" and target != "sqlite":
            statement = statement.transform(_portable)
            _assert_portable(statement, target)
        if target == "postgres":
            statement = statement.transform(_numeric_round)
        if target == "mysql":
            statement = statement.transform(_filter_to_case)
        try:
            rendered.append(
                statement.sql(
                    dialect=_WRITERS[target], pretty=pretty, comments=False, unsupported_level=ErrorLevel.RAISE
                )
            )
        except (SqlglotError, RecursionError) as exc:
            raise ValueError(f"Could not convert the SQL to {SUPPORTED_DIALECTS[target]}: {exc}") from exc
    return ";\n\n".join(rendered)


def format_sql(sql: str, dialect: str = "sqlite") -> str:
    """Pretty-print ``sql`` in ``dialect``; returns the input unchanged if it cannot be parsed."""
    try:
        name = normalize_dialect(dialect)
        statements = parse_statements(sql, name)
    except (SqlglotError, ValueError):
        return sql
    if not statements:
        return sql
    try:
        return ";\n\n".join(render(statement, name, pretty=True) for statement in statements)
    except (SqlglotError, RecursionError):
        return sql
