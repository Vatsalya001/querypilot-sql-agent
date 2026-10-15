"""Building and opening the sample SQLite database.

* :func:`ensure_database` creates and seeds the database file once (atomically, safe under concurrent
  startup) and rebuilds it when the schema or the seed code changes.
* :func:`connect_readonly` is the only way user-influenced SQL reaches the database: the file is opened
  with ``mode=ro``, ``PRAGMA query_only`` is on, an authorizer allows nothing but reads, and value size,
  memory, temp storage and result width are bounded.
"""

from __future__ import annotations

import hashlib
import logging
import os
import sqlite3
import tempfile
from pathlib import Path

from app.db.seed import seed

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = PACKAGE_DIR / "schema.sql"
DEFAULT_DB_PATH: Path = PACKAGE_DIR.parent.parent / "data" / "company.db"

_ALLOWED_ACTIONS = frozenset(
    {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, getattr(sqlite3, "SQLITE_RECURSIVE", 33)}
)
_BLOCKED_FUNCTIONS = frozenset(
    {"load_extension", "readfile", "writefile", "edit", "fts3_tokenizer", "zipfile", "sqlar_compress",
     "sqlar_uncompress", "lsmode"}
)

# Resource bounds for untrusted queries. The sample data is small (rows under 1 KB), so these never
# affect a legitimate query, but they stop a single valid SELECT from exhausting a small container.
MAX_VALUE_BYTES = 100_000
"""Largest string or BLOB a query may build (``SQLITE_LIMIT_LENGTH``)."""
MAX_SQL_BYTES = 100_000
"""Longest SQL text accepted (``SQLITE_LIMIT_SQL_LENGTH``)."""
MAX_RESULT_COLUMNS = 100
"""Most columns in a result set (``SQLITE_LIMIT_COLUMN``)."""
MAX_LIKE_PATTERN_BYTES = 1_000
"""Longest LIKE/GLOB pattern (``SQLITE_LIMIT_LIKE_PATTERN_LENGTH``); bounds the cost of one comparison."""
SQLITE_HEAP_LIMIT_BYTES = 64 * 1024 * 1024
"""Hard cap on SQLite's heap. Process-wide: the sample database is the only SQLite user in the app."""

_CONNECTION_LIMITS: tuple[tuple[int, int], ...] = (
    (sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES),
    (sqlite3.SQLITE_LIMIT_SQL_LENGTH, MAX_SQL_BYTES),
    (sqlite3.SQLITE_LIMIT_COLUMN, MAX_RESULT_COLUMNS),
    (sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, MAX_LIKE_PATTERN_BYTES),
    (sqlite3.SQLITE_LIMIT_ATTACHED, 0),
)


APPLICATION_ID = 0x51504C54  # "QPLT": marks databases built by QueryPilot (PRAGMA application_id)


class ForeignDatabaseError(RuntimeError):
    """Raised when asked to rebuild a database file that QueryPilot did not create."""


def _application_id(path: Path) -> int | None:
    """The file's PRAGMA application_id, or None when it is missing or not an SQLite database."""
    if not path.is_file():
        return None
    try:
        conn = sqlite3.connect(readonly_uri(path), uri=True)
        try:
            return int(conn.execute("PRAGMA application_id").fetchone()[0])
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def _is_foreign(path: Path) -> bool:
    """True for an existing file that is not the bundled sample database (e.g. your own DATABASE_PATH)."""
    return path.exists() and path != DEFAULT_DB_PATH and _application_id(path) != APPLICATION_ID


def _data_version() -> int:
    """Fingerprint of schema.sql and the seed code, stored in ``PRAGMA user_version``."""
    digest = hashlib.sha256()
    for path in (SCHEMA_PATH, PACKAGE_DIR / "seed.py"):
        digest.update(path.read_bytes())
    return int.from_bytes(digest.digest()[:4], "big") & 0x7FFFFFFF


def readonly_uri(db_path: str | Path) -> str:
    """SQLite URI that opens ``db_path`` read-only (the file must exist)."""
    return f"{Path(db_path).expanduser().resolve().as_uri()}?mode=ro"


def _is_current(path: Path, version: int) -> bool:
    if not path.is_file():
        return False
    try:
        conn = sqlite3.connect(readonly_uri(path), uri=True)
        try:
            return conn.execute("PRAGMA user_version").fetchone()[0] == version
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _build(path: Path, version: int) -> dict[str, int]:
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        counts = seed(conn)
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"Seed data violates foreign keys: {violations[:3]}")
        conn.execute(f"PRAGMA user_version = {version}")
        conn.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        conn.commit()
        conn.execute("VACUUM")
        return counts
    finally:
        conn.close()


def ensure_database(db_path: str | Path | None = None, *, reset: bool = False) -> Path:
    """Create and seed the sample database if it is missing, outdated or ``reset`` is set; return its path.

    An existing file that QueryPilot did not build (no ``APPLICATION_ID`` marker and not the bundled default
    path) is returned untouched, so pointing ``DATABASE_PATH`` at your own SQLite file never overwrites it.

    The database is built in a temporary file next to the target and moved into place with
    ``os.replace``, so readers never see a half-built file and concurrent builders cannot corrupt it.
    """
    path = Path(db_path).expanduser().resolve() if db_path else DEFAULT_DB_PATH
    version = _data_version()
    if _is_foreign(path):
        # Someone else's database (DATABASE_PATH pointed at an existing file): use it as-is, never replace it.
        if reset:
            raise ForeignDatabaseError(f"Refusing to rebuild {path}: it was not created by QueryPilot.")
        logger.info("Using existing database %s as-is (not the bundled sample)", path)
        return path
    if not reset and _is_current(path, version):
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".building", dir=path.parent)
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        counts = _build(tmp_path, version)
        tmp_path.chmod(0o644)  # mkstemp creates 0600 files
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    logger.info("Built sample database %s (%s)", path, ", ".join(f"{t}={n}" for t, n in counts.items()))
    return path


def _authorize(action: int, arg1: str | None, arg2: str | None, _db: str | None, _source: str | None) -> int:
    """SQLite authorizer: only plain reads of user tables and safe functions are permitted."""
    if action not in _ALLOWED_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_FUNCTION:
        name = (arg2 or "").lower()
        if name in _BLOCKED_FUNCTIONS or name.startswith("sqlite_"):  # sqlite_*(): build and version details
            return sqlite3.SQLITE_DENY
    if action == sqlite3.SQLITE_READ and (arg1 or "").lower().startswith("sqlite_"):
        return sqlite3.SQLITE_DENY  # internal catalog tables are not part of the provided schema
    return sqlite3.SQLITE_OK


def connect_readonly(db_path: str | Path) -> sqlite3.Connection:
    """Open the database for untrusted read-only SQL.

    Three independent layers: the URI ``mode=ro`` open flag, ``PRAGMA query_only=ON`` and an authorizer
    that denies every action except SELECT, column reads, (safe) function calls and recursive CTEs, so
    writes, DDL, ATTACH and PRAGMA statements fail even if every check before execution was bypassed.

    Resources are bounded as well: strings and BLOBs are capped at :data:`MAX_VALUE_BYTES`, sorts and
    temporary B-trees stay in memory (never in temp files on disk) under a :data:`SQLITE_HEAP_LIMIT_BYTES`
    cap, and result width, SQL length and LIKE patterns are limited. The settings are applied before the
    authorizer is installed, so queries cannot change them back.
    """
    conn = sqlite3.connect(readonly_uri(db_path), uri=True, check_same_thread=False)
    try:
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute(f"PRAGMA hard_heap_limit = {SQLITE_HEAP_LIMIT_BYTES}")
        conn.execute("PRAGMA query_only = ON")
        for category, limit in _CONNECTION_LIMITS:
            conn.setlimit(category, limit)
        conn.set_authorizer(_authorize)
    except sqlite3.Error:
        conn.close()
        raise
    return conn
