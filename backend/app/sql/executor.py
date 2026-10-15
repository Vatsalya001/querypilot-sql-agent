"""Read-only query execution with a row cap, a result-size cap and a wall-clock time limit."""

from __future__ import annotations

import contextlib
import logging
import math
import sqlite3
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.db.database import MAX_VALUE_BYTES, SQLITE_HEAP_LIMIT_BYTES, connect_readonly
from app.sql.validator import is_read_only, strip_sql_fences

logger = logging.getLogger(__name__)

MAX_RESULT_BYTES = 5_000_000
"""Approximate size of the returned values after which no more rows are read (the result is truncated)."""


class UnsafeQueryError(Exception):
    """Raised when SQL that is not a single read-only query reaches the executor."""


@dataclass
class ExecutionResult:
    """Rows returned by a query (at most ``max_rows``) or the database error it raised."""

    columns: list[str]
    rows: list[list[Any]]
    row_count: int
    truncated: bool
    elapsed_ms: float
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The ``execution`` object of docs/API.md (JSON scalars only)."""
        return {
            "columns": list(self.columns),
            "rows": [[json_safe(value) for value in row] for row in self.rows],
            "row_count": self.row_count,
            "truncated": self.truncated,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


def json_safe(value: Any) -> Any:
    """Convert a database value to a JSON scalar (number, string or null)."""
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value).hex()
    return str(value)


def _size(value: Any) -> int:
    """Approximate size of a JSON-safe value in the response."""
    return len(value) if isinstance(value, str) else 8


class _Deadline:
    """Interrupts the connection's running statement once ``timeout_s`` has passed (from another thread).

    Unlike a progress handler, which only runs every N virtual-machine steps, this fires on time even
    when each step is slow.
    """

    def __init__(self, conn: sqlite3.Connection, timeout_s: float) -> None:
        self.expired = threading.Event()
        self._conn = conn
        self._timer = threading.Timer(timeout_s, self._fire)
        self._timer.daemon = True

    def _fire(self) -> None:
        self.expired.set()
        with contextlib.suppress(sqlite3.ProgrammingError):  # the connection may already be closed
            self._conn.interrupt()

    def __enter__(self) -> _Deadline:
        self._timer.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._timer.cancel()


def _error_message(exc: BaseException, timed_out: bool, timeout_s: float) -> str:
    """User-facing text for a failed query."""
    if timed_out:
        return f"Query exceeded the {timeout_s:.1f} s time limit."
    if isinstance(exc, MemoryError):  # Python's sqlite3 raises MemoryError for SQLITE_NOMEM
        return (f"Query needed more than the {SQLITE_HEAP_LIMIT_BYTES // (1024 * 1024)} MB memory limit; "
                "add filters or a LIMIT.")
    text = str(exc)
    if "string or blob too big" in text:
        return f"A value in the query exceeds the {MAX_VALUE_BYTES:,}-byte limit for strings and BLOBs."
    return text


def execute_query(sql: str, db_path: str | Path, *, max_rows: int = 500, timeout_s: float = 5.0) -> ExecutionResult:
    """Run one read-only query and return at most ``max_rows`` rows (and about ``MAX_RESULT_BYTES`` of data).

    Raises :class:`UnsafeQueryError` unless ``sql`` is a single read-only query. Database errors, resource
    limits and timeouts are reported in ``ExecutionResult.error`` instead of being raised.
    """
    text = strip_sql_fences(sql)
    if not is_read_only(text):
        raise UnsafeQueryError("Only a single read-only SELECT query can be executed.")
    conn = connect_readonly(db_path)
    started = time.perf_counter()
    deadline = _Deadline(conn, timeout_s)
    try:
        with deadline:
            cursor = conn.execute(text)
            columns = [description[0] for description in cursor.description or ()]
            rows: list[list[Any]] = []
            size = 0
            truncated = False
            while (row := cursor.fetchone()) is not None:
                if len(rows) == max_rows or size > MAX_RESULT_BYTES:
                    truncated = True
                    break
                values = [json_safe(value) for value in row]
                size += sum(_size(value) for value in values)
                rows.append(values)
        return ExecutionResult(
            columns=columns,
            rows=rows,
            row_count=len(rows),
            truncated=truncated,
            elapsed_ms=_elapsed_ms(started),
        )
    except (sqlite3.Error, MemoryError) as exc:
        error = _error_message(exc, deadline.expired.is_set(), timeout_s)
        logger.info("Query failed: %s", error)
        return ExecutionResult(columns=[], rows=[], row_count=0, truncated=False, elapsed_ms=_elapsed_ms(started),
                               error=error)
    finally:
        conn.close()


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 2)
