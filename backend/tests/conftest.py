"""Shared fixtures: a freshly built sample database and its schema catalog (built once per test session)."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.db.catalog import SchemaCatalog, load_catalog
from app.db.database import ensure_database


@pytest.fixture(scope="session")
def db_path(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Path of a sample database built from scratch in a temporary directory (read-only use only)."""
    return ensure_database(tmp_path_factory.mktemp("db") / "company.db")


@pytest.fixture(scope="session")
def catalog(db_path: Path) -> SchemaCatalog:
    """Schema catalog of the session database."""
    return load_catalog(db_path)
