"""Suggestions ("did you mean ...?") for unknown table and column names in validator error messages."""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable

from app.db.catalog import SchemaCatalog
from app.db.descriptions import COLUMN_SYNONYMS, TERM_TABLES

MAX_SUGGESTIONS = 3


def _key(name: str) -> str:
    """Comparison key: lower case without quotes, underscores, spaces or dashes."""
    return re.sub(r"[\s_\-\"'`\[\]]", "", name).lower()


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _rank(name: str, pool: list[str], synonyms: Iterable[str]) -> list[str]:
    key = _key(name)
    by_key = {_key(item): item for item in pool}
    found: list[str] = []
    if key in by_key:
        found.append(by_key[key])
    found.extend(item for item in synonyms if item in pool)
    found.extend(by_key[match] for match in difflib.get_close_matches(key, list(by_key), n=MAX_SUGGESTIONS, cutoff=0.75))
    if len(key) >= 4:
        found.extend(item for item_key, item in by_key.items() if key in item_key or item_key in key)
    return list(dict.fromkeys(found))[:MAX_SUGGESTIONS]


def suggest_tables(name: str, catalog: SchemaCatalog) -> list[str]:
    """Closest existing table names for an unknown table ``name``."""
    key = _key(name)
    synonyms = TERM_TABLES.get(key, ()) or TERM_TABLES.get(_singular(key), ())
    return _rank(name, catalog.table_names(), synonyms)


def suggest_columns(name: str, catalog: SchemaCatalog, tables: Iterable[str] = ()) -> list[str]:
    """Closest existing column names for an unknown column ``name`` (searching ``tables``, else all tables)."""
    names = [table for table in tables if table in catalog.tables] or catalog.table_names()
    pool = list(dict.fromkeys(col.name for table in names for col in catalog.tables[table].columns))
    key = _key(name)
    synonyms = list(COLUMN_SYNONYMS.get(key, ()) or COLUMN_SYNONYMS.get(_singular(key), ()))
    if key == "id":
        synonyms.extend(pk for table in names for pk in catalog.tables[table].primary_key)
    return _rank(name, pool, synonyms)
