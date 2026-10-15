"""Lexical schema retrieval: which tables a question is about, plus the FK bridge tables to join them.

Signals (weights): table names (3), synonyms such as "staff" -> Employees (2), column names (2, or 1
for a part of a CamelCase name), sample values such as "California" -> Customers.State (2) and
description words (1). Tables referenced by the previous query or by pasted SQL are always included.
The output text always ends with a one-line summary of every other table, so the LLM knows all names.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.db.catalog import SchemaCatalog
from app.db.descriptions import TERM_TABLES
from app.sql.dialects import parse_statements

TABLE_NAME_WEIGHT = 3.0
SYNONYM_WEIGHT = 2.0
COLUMN_WEIGHT = 2.0
COLUMN_PART_WEIGHT = 1.0
VALUE_WEIGHT = 2.0
DESCRIPTION_WEIGHT = 1.0
MIN_SCORE = 2.0

_STOP_WORDS = frozenset({
    "a", "all", "also", "an", "and", "any", "are", "as", "at", "be", "by", "can", "count", "do", "does", "each",
    "find", "for", "from", "get", "give", "has", "have", "how", "i", "in", "is", "it", "its", "list", "many",
    "me", "most", "much", "my", "number", "of", "on", "only", "or", "our", "per", "please", "select", "show",
    "than", "that", "the", "their", "them", "these", "they", "this", "those", "to", "top", "was", "were", "what",
    "when", "where", "which", "who", "whose", "why", "with",
})
_GENERIC_PARTS = frozenset({"id", "name", "date", "is", "at", "first", "last", "unit"})
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def _forms(word: str) -> list[str]:
    """The word plus light stems: plural -> singular, ordered -> order, shipping -> ship."""
    forms = [word, _singular(word)]
    if word.endswith("ed") and len(word) > 4:
        forms.append(word[:-2])
    if word.endswith("ing") and len(word) > 5:
        forms.append(word[:-3])
    return list(dict.fromkeys(forms))


@dataclass
class SchemaContext:
    """The tables selected for a question and the schema text to show the LLM."""

    tables: list[str]
    text: str
    matched_terms: dict[str, list[str]] = field(default_factory=dict)
    strategy: str = "lexical"

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {
            "tables": list(self.tables),
            "text": self.text,
            "matched_terms": {table: list(terms) for table, terms in self.matched_terms.items()},
            "strategy": self.strategy,
        }

    def focus_hint(self) -> str:
        """One compact line for the dynamic part of a prompt."""
        if self.strategy == "full_schema":
            return "Most relevant tables: no specific table matched the question; consider every table."
        parts = []
        for table in self.tables:
            terms = self.matched_terms.get(table) or []
            parts.append(f"{table} (matched {', '.join(terms[:3])})" if terms else table)
        return f"Most relevant tables: {', '.join(parts)}"


class _Scorer:
    def __init__(self, catalog: SchemaCatalog) -> None:
        self.catalog = catalog
        self.scores: dict[str, float] = {}
        self.terms: dict[str, list[str]] = {}

    def hit(self, table: str, weight: float, term: str) -> None:
        self.scores[table] = self.scores.get(table, 0.0) + weight
        terms = self.terms.setdefault(table, [])
        if term not in terms:
            terms.append(term)

    def score(self, question: str) -> None:
        text = question.lower()
        words = [w.lower() for w in _WORD.findall(question)]
        claimed: set[str] = set()  # words explained by a table name or synonym
        for word in dict.fromkeys(words):
            if word in _STOP_WORDS or len(word) < 3:
                continue
            forms = _forms(word)
            for table in self.catalog.table_names():
                if table.lower() in forms or _singular(table.lower()) in forms:
                    self.hit(table, TABLE_NAME_WEIGHT, f"'{word}'")
                    claimed.add(word)
            if word not in claimed:
                for form in forms:
                    for table in TERM_TABLES.get(form, ()):
                        if table in self.catalog.tables:
                            self.hit(table, SYNONYM_WEIGHT, f"'{word}'")
                            claimed.add(word)
                    if word in claimed:
                        break
            self.columns(word, forms, parts=word not in claimed)
            self.descriptions(word)
        self.values(text, claimed)

    def columns(self, word: str, forms: list[str], *, parts: bool) -> None:
        """Exact column-name matches; CamelCase part matches (e.g. 'hire' -> HireDate) for unclaimed words.

        Key columns (``...ID``) are skipped for part matches: their names repeat table names.
        """
        for table in self.catalog.tables.values():
            for col in table.columns:
                if col.name.lower() in forms:
                    self.hit(table.name, COLUMN_WEIGHT, f"'{word}' -> {table.name}.{col.name}")
                elif parts and not col.name.lower().endswith("id"):
                    pieces = [piece.lower() for piece in _CAMEL.findall(col.name)]
                    if any(piece in forms and piece not in _GENERIC_PARTS and len(piece) > 2 for piece in pieces):
                        self.hit(table.name, COLUMN_PART_WEIGHT, f"'{word}' -> {table.name}.{col.name}")

    def descriptions(self, word: str) -> None:
        if len(word) < 5:
            return
        pattern = re.compile(rf"\b{re.escape(word)}\b")
        for table in self.catalog.tables.values():
            if pattern.search(table.description.lower()):
                self.scores[table.name] = self.scores.get(table.name, 0.0) + DESCRIPTION_WEIGHT

    def values(self, text: str, claimed: set[str]) -> None:
        for table in self.catalog.tables.values():
            for col in table.columns:
                for value in col.sample_values:
                    lowered = value.lower()
                    if lowered in claimed or len(lowered) < 3:
                        continue
                    if re.search(rf"(?<![a-z0-9]){re.escape(lowered)}(?![a-z0-9])", text):
                        self.hit(table.name, VALUE_WEIGHT, f"'{lowered}' -> {table.name}.{col.name}")


def _sql_tables(sql: str | None, catalog: SchemaCatalog) -> list[str]:
    """Canonical tables referenced by ``sql`` (parse errors are ignored)."""
    if not sql or not sql.strip():
        return []
    try:
        statements = parse_statements(sql, "sqlite")
    except (SqlglotError, ValueError):
        return []
    names = (catalog.resolve_table(t.name) for s in statements for t in s.find_all(exp.Table))
    return list(dict.fromkeys(name for name in names if name))


def _bridge(selected: list[str], catalog: SchemaCatalog) -> list[str]:
    """Tables needed to connect every selected table to the first one through foreign keys."""
    added: list[str] = []
    connected = list(selected[:1])
    for table in selected[1:]:
        best: list[str] | None = None
        for anchor in connected:
            path = catalog.join_path(anchor, table)
            hops = list(dict.fromkeys(t for rel in path for t in (rel.from_table, rel.to_table)))
            if path and (best is None or len(hops) < len(best)):
                best = hops
        for hop in best or []:
            if hop not in selected and hop not in added:
                added.append(hop)
        connected.extend([table, *(best or [])])
    return added


def _render(catalog: SchemaCatalog, tables: Iterable[str]) -> str:
    chosen = list(tables)
    text = catalog.to_prompt(chosen)
    others = [name for name in catalog.table_names() if name not in chosen]
    if others:
        text += "\n\n-- Other tables (summary):\n" + catalog.summary(others)
    return text


def retrieve_schema(
    question: str,
    catalog: SchemaCatalog,
    *,
    previous_sql: str | None = None,
    user_sql: str | None = None,
    max_tables: int = 5,
) -> SchemaContext:
    """Select the tables relevant to ``question`` (plus FK bridges) and render their schema text."""
    scorer = _Scorer(catalog)
    scorer.score(question or "")
    forced: dict[str, str] = {}
    for table in _sql_tables(previous_sql, catalog):
        forced.setdefault(table, "previous query")
    for table in _sql_tables(user_sql, catalog):
        forced.setdefault(table, "your SQL")
    order = {name: index for index, name in enumerate(catalog.table_names())}
    ranked = sorted(
        (table for table, score in scorer.scores.items() if score >= MIN_SCORE and table not in forced),
        key=lambda table: (-scorer.scores[table], order[table]),
    )
    selected = list(forced) + ranked[: max(0, max_tables - len(forced))]
    if not selected:
        tables = catalog.table_names()
        return SchemaContext(tables=tables, text=catalog.to_prompt(), matched_terms={}, strategy="full_schema")
    matched = {table: list(scorer.terms.get(table, [])) for table in selected}
    for table, reason in forced.items():
        matched[table].insert(0, reason)
    bridges = _bridge(selected, catalog)
    for table in bridges:
        matched[table] = ["join bridge"]
    tables = selected + bridges
    strategy = "lexical+fk_bridge" if bridges else "lexical"
    return SchemaContext(
        tables=tables,
        text=_render(catalog, tables),
        matched_terms={table: terms for table, terms in matched.items() if terms},
        strategy=strategy,
    )
