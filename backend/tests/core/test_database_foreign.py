"""ensure_database never overwrites a database that QueryPilot did not create."""

import sqlite3
from pathlib import Path

import pytest

from app.db.database import APPLICATION_ID, ForeignDatabaseError, ensure_database


def _make_foreign_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE Books (BookID INTEGER PRIMARY KEY, Title TEXT)")
    conn.execute("INSERT INTO Books (Title) VALUES ('Dune')")
    conn.commit()
    conn.close()


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    finally:
        conn.close()


def test_a_foreign_database_is_used_as_is(tmp_path: Path) -> None:
    own = tmp_path / "my_books.db"
    _make_foreign_db(own)
    assert ensure_database(own) == own.resolve()
    assert _tables(own) == {"Books"}


def test_reset_refuses_to_replace_a_foreign_database(tmp_path: Path) -> None:
    own = tmp_path / "my_books.db"
    _make_foreign_db(own)
    with pytest.raises(ForeignDatabaseError):
        ensure_database(own, reset=True)
    assert _tables(own) == {"Books"}


def test_built_databases_carry_the_marker_and_can_be_reset(tmp_path: Path) -> None:
    sample = ensure_database(tmp_path / "sample.db")
    conn = sqlite3.connect(sample)
    try:
        assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    finally:
        conn.close()
    assert ensure_database(sample, reset=True) == sample  # our own file may be rebuilt
    assert "Customers" in _tables(sample)
