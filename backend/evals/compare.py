"""Execution-accuracy comparison of two query results.

Two results match when they hold the same rows, compared by value:

* **Columns.** Order and names are ignored. Every expected column must be matched by a distinct
  column of the generated result with the same values row for row. Extra generated columns are
  allowed, because an answer may show more context (an id next to a name). A generated text column
  such as ``'Ava Smith'`` may also stand for two expected columns (``'Ava'`` and ``'Smith'``).
* **Values.** Numbers are rounded to 2 decimals and compared with a small tolerance (0.011 absolute
  or 0.01 % relative) so sums computed in a different order still match; numeric strings such as
  ``'2025'`` equal the number 2025; text is compared exactly after trimming.
* **Rows.** Compared as multisets, or in order when the question asks for an ordering or a top-N.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

ABS_TOLERANCE = 0.011
REL_TOLERANCE = 1e-4
MAX_ASSIGNMENTS = 5000
"""Upper bound on column assignments tried before giving up (only reached by pathological results)."""

_NUMERIC = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")

Value = float | str | None


@dataclass(frozen=True)
class ResultTable:
    """A query result: column names and rows."""

    columns: Sequence[str]
    rows: Sequence[Sequence[Any]]


@dataclass(frozen=True)
class Comparison:
    """Outcome of :func:`compare_results` with a short human-readable reason."""

    match: bool
    reason: str
    extra_columns: int = 0


def normalize_value(value: Any) -> Value:
    """Canonical form of one cell: numbers as floats rounded to 2 decimals, text trimmed, NULL as None."""
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, int | float):
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return str(number)
        rounded = round(number, 2)
        return 0.0 if rounded == 0 else rounded
    if isinstance(value, bytes):
        return value.hex()
    text = str(value).strip()
    return normalize_value(float(text)) if _NUMERIC.fullmatch(text) else text


def values_equal(left: Value, right: Value) -> bool:
    """Equality of normalized values, with the numeric tolerance."""
    if left == right:
        return True
    if isinstance(left, float) and isinstance(right, float):
        return abs(left - right) <= max(ABS_TOLERANCE, REL_TOLERANCE * max(abs(left), abs(right)))
    return False


def _sort_key(value: Value) -> tuple[int, Any]:
    """Total order over mixed values: NULL < numbers < text."""
    if value is None:
        return (0, 0.0)
    if isinstance(value, float):
        return (1, value)
    return (2, value)


def _row_key(row: Sequence[Value]) -> tuple[tuple[int, Any], ...]:
    return tuple(_sort_key(value) for value in row)


def _rows_equal(left: Sequence[Value], right: Sequence[Value]) -> bool:
    return len(left) == len(right) and all(values_equal(a, b) for a, b in zip(left, right, strict=True))


def _same_values(left: list[Value], right: list[Value]) -> bool:
    """Two columns hold the same multiset of values (with tolerance)."""
    if Counter(left) == Counter(right):
        return True
    return all(
        values_equal(a, b)
        for a, b in zip(sorted(left, key=_sort_key), sorted(right, key=_sort_key), strict=True)
    )


def _rows_match(expected: list[tuple[Value, ...]], actual: list[tuple[Value, ...]], *, ordered: bool) -> bool:
    if ordered:
        return all(_rows_equal(e, a) for e, a in zip(expected, actual, strict=True))
    if Counter(expected) == Counter(actual):
        return True
    return all(
        _rows_equal(e, a)
        for e, a in zip(sorted(expected, key=_row_key), sorted(actual, key=_row_key), strict=True)
    )


@dataclass(frozen=True)
class _Candidate:
    """A generated column (or one half of a split text column) that may match an expected column."""

    source: int
    values: list[Value]


def _candidates(rows: list[list[Value]], width: int) -> list[_Candidate]:
    """Generated columns plus, for text columns with a space in every value, their two halves."""
    columns = [_Candidate(index, [row[index] for row in rows]) for index in range(width)]
    derived: list[_Candidate] = []
    for column in columns:
        values = column.values
        if not values or not all(isinstance(value, str) and " " in value for value in values):
            continue
        for splitter in (lambda text: text.split(" ", 1), lambda text: text.rsplit(" ", 1)):
            parts = [splitter(str(value)) for value in values]
            for half in (0, 1):
                candidate = _Candidate(column.source, [normalize_value(part[half]) for part in parts])
                if all(candidate.values != existing.values for existing in [*columns, *derived]):
                    derived.append(candidate)
    return columns + derived


def compare_results(expected: ResultTable, actual: ResultTable, *, ordered: bool = False) -> Comparison:
    """Compare a generated result with the expected one (see the module docstring for the rules)."""
    expected_rows = [[normalize_value(value) for value in row] for row in expected.rows]
    actual_rows = [[normalize_value(value) for value in row] for row in actual.rows]
    if len(expected_rows) != len(actual_rows):
        return Comparison(False, f"row count differs: expected {len(expected_rows)}, got {len(actual_rows)}")

    width = len(expected.columns)
    expected_columns = [[row[index] for row in expected_rows] for index in range(width)]
    candidates = _candidates(actual_rows, len(actual.columns))
    options: list[list[int]] = []
    for index, values in enumerate(expected_columns):
        matching = [k for k, candidate in enumerate(candidates) if _same_values(values, candidate.values)]
        if not matching:
            return Comparison(False, f"no generated column has the values of expected column '{expected.columns[index]}'")
        options.append(matching)

    expected_tuples = [tuple(row) for row in expected_rows]

    def project(assignment: dict[int, int]) -> list[tuple[Value, ...]]:
        return [tuple(candidates[assignment[j]].values[r] for j in range(width)) for r in range(len(expected_rows))]

    def find(strict_order: bool) -> dict[int, int] | None:
        """Depth-first search for an injective column assignment that reproduces the expected rows."""
        search_order = sorted(range(width), key=lambda j: len(options[j]))
        budget = [MAX_ASSIGNMENTS]

        def search(position: int, chosen: dict[int, int]) -> dict[int, int] | None:
            if position == width:
                budget[0] -= 1
                return dict(chosen) if _rows_match(expected_tuples, project(chosen), ordered=strict_order) else None
            column = search_order[position]
            for k in options[column]:
                if budget[0] <= 0:
                    return None
                if k in chosen.values():
                    continue
                chosen[column] = k
                found = search(position + 1, chosen)
                del chosen[column]
                if found is not None:
                    return found
            return None

        return search(0, {})

    assignment = find(ordered)
    if assignment is None:
        if ordered and find(False) is not None:
            return Comparison(False, "same rows but in a different order")
        return Comparison(False, f"values differ{_missing_row_hint(expected_tuples, project, options)}")
    used_sources = {candidates[k].source for k in assignment.values()}
    extra = len(actual.columns) - len(used_sources)
    return Comparison(True, f"match (+{extra} extra column{'s' if extra != 1 else ''})" if extra else "match", extra)


def _missing_row_hint(
    expected: list[tuple[Value, ...]],
    project: Callable[[dict[int, int]], list[tuple[Value, ...]]],
    options: list[list[int]],
) -> str:
    """A short example of an expected row the result lacks, under the first plausible column assignment."""
    first = {column: choices[0] for column, choices in enumerate(options)}
    if len(set(first.values())) != len(first):
        return ""
    remaining = Counter(project(first))
    for row in expected:
        if remaining[row] > 0:
            remaining[row] -= 1
            continue
        shown = ", ".join("NULL" if value is None else repr(value) for value in row)
        return f": e.g. expected row ({shown[:120]}) is missing"
    return ""
