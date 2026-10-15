"""Deterministic input guard that runs before any LLM call.

* size limits (empty / too long),
* prompt-injection heuristics (low false-positive regexes; ordinary data questions such as "ignore
  cancelled orders" or "act as if the year is 2025" pass),
* destructive SQL *statements* in the text (``DELETE FROM``, ``DROP TABLE``, ...). Natural-language
  requests such as "delete all customers" are left to the LLM intent classifier.

It also provides :func:`extract_sql` (find SQL pasted into a message) and :func:`sanitize_user_text`
(neutralize text before it is embedded in a prompt).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from app.sql.fences import fenced_blocks

GuardCategory = Literal["ok", "empty", "too_long", "prompt_injection", "destructive_sql"]

_FLAGS = re.IGNORECASE | re.MULTILINE
_FILLER = r"(?:(?:all|any|of|the|your|my|these|those)\s+){0,3}"

INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ignore previous instructions", re.compile(
        rf"\b(?:ignore|disregard|forget|skip)\s+{_FILLER}(?:previous|prior|above|earlier|system|preceding|original)"
        r"\s+(?:instructions?|rules|prompts?|directions|guidelines|messages?)\b", _FLAGS)),
    ("reveal system prompt", re.compile(
        r"\b(?:reveal|show|print|repeat|output|leak|display|dump)\s+(?:me\s+)?"
        r"(?:your\s+(?:(?:system|initial|original|hidden|full)\s+)?(?:prompt|instructions|rules)"
        r"|the\s+(?:(?:system|initial|original|hidden|full)\s+(?:prompt|instructions)|prompt))\b", _FLAGS)),
    ("role override", re.compile(
        r"\byou\s+are\s+now\s+(?:an?|the|my|in|no\s+longer|free|unrestricted|unfiltered|uncensored|jailbroken"
        r"|acting|going\s+to)\b", _FLAGS)),
    ("role play", re.compile(r"\bpretend\s+(?:to\s+be|(?:that\s+)?you\s+are)\b", _FLAGS)),
    ("developer mode", re.compile(r"\bdeveloper\s+mode\b", _FLAGS)),
    ("jailbreak", re.compile(r"\bjail\s*break\w*\b", _FLAGS)),
    ("DAN mode", re.compile(r"\bDAN\s+mode\b|\bdo\s+anything\s+now\b", _FLAGS)),
    ("new instructions", re.compile(
        r"\bnew\s+(?:system\s+(?:instructions?|rules|prompt)|instructions?)\s*:", _FLAGS)),
    ("override rules", re.compile(
        r"\boverride\s+(?:your|the|all|any)\s+(?:rules|guardrails|instructions|safety|restrictions|filters)\b",
        _FLAGS)),
    ("chat template token", re.compile(
        r"<\|\s*(?:im_start|im_end|system|user|assistant|endoftext|eot_id|start_header_id)\s*\|>|\[/?INST\]|<</?SYS>>"
        r"|^\s*#{2,}\s*(?:system|instructions?)\b", _FLAGS)),
    ("system role line", re.compile(r"^\s*system\s*:", _FLAGS)),
)

_NAME = r"(?:\"[^\"\n]+\"|`[^`\n]+`|\[[^\]\n]+\]|[\w.]+)"  # plain or quoted table name

DESTRUCTIVE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("DELETE", re.compile(r"\bDELETE\s+FROM\b", _FLAGS)),
    ("UPDATE", re.compile(rf"\bUPDATE\s+(?:OR\s+\w+\s+)?{_NAME}\s+SET\b", _FLAGS)),
    ("INSERT", re.compile(r"\bINSERT\s+(?:OR\s+\w+\s+)?INTO\b", _FLAGS)),
    ("REPLACE", re.compile(r"(?<!\bOR )\bREPLACE\s+INTO\b", _FLAGS)),  # not INSERT OR REPLACE
    ("DROP", re.compile(r"\bDROP\s+(?:TABLE|VIEW|INDEX|DATABASE|SCHEMA|TRIGGER)\b", _FLAGS)),
    ("ALTER", re.compile(r"\bALTER\s+TABLE\b", _FLAGS)),
    ("TRUNCATE", re.compile(r"\bTRUNCATE\s+TABLE\b|\bTRUNCATE\s+[\w\"`\[\].]+\s*(?:;|$)", _FLAGS)),
    ("CREATE", re.compile(
        r"\bCREATE\s+(?:(?:TEMP|TEMPORARY|UNIQUE|VIRTUAL)\s+)?(?:TABLE|VIEW|INDEX|TRIGGER|DATABASE)\b", _FLAGS)),
    ("GRANT", re.compile(r"\bGRANT\s+\w+(?:\s*,\s*\w+)*\s+ON\b", _FLAGS)),
    ("REVOKE", re.compile(r"\bREVOKE\s+\w+(?:\s*,\s*\w+)*\s+ON\b", _FLAGS)),
    ("ATTACH", re.compile(r"\bATTACH\s+(?:DATABASE\b|['\"])", _FLAGS)),
    ("DETACH", re.compile(r"\bDETACH\s+(?:DATABASE\b|[\w\"`]+\s*(?:;|$))", _FLAGS)),
    ("PRAGMA", re.compile(r"\bPRAGMA\s+\w+", _FLAGS)),
    ("VACUUM", re.compile(r"\bVACUUM\s*(?:;|$|INTO\b)", _FLAGS)),
    ("MERGE", re.compile(r"\bMERGE\s+INTO\b", _FLAGS)),
)

_INVISIBLE = re.compile("[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SQL_COMMENT = re.compile(r"/\*.*?(?:\*/|\Z)|--[^\n]*", re.DOTALL)
# "<" / ">" and their full-width and small look-alikes (U+FF1C/U+FF1E, U+FE64/U+FE65).
_TAG = re.compile(
    "[<\uff1c\ufe64](\\s*/?\\s*[A-Za-z_][\\w:.-]*\\s*/?\\s*)[>\uff1e\ufe65]"
    "|[<\uff1c\ufe64](\\|[^<>\\n]{0,40}?\\|)[>\uff1e\ufe65]"
)
_BLANK_RUN = re.compile(r"\n(?:[ \t]*\n){4,}")
_SQL_START = re.compile(r"\b(?:SELECT\b|WITH\s+(?:RECURSIVE\s+)?\w+\s*(?:\([^)]*\))?\s+AS\s*\()", re.IGNORECASE)
_STATEMENT_END = re.compile(r"\n[ \t]*\n")


@dataclass
class GuardResult:
    """Verdict of :func:`check_input`."""

    allowed: bool
    category: GuardCategory
    reason: str | None = None
    matched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {"allowed": self.allowed, "category": self.category, "reason": self.reason,
                "matched": list(self.matched)}


def _normalize(text: str) -> str:
    """Text as matched by the heuristics: NFKC, no invisible characters (defeats zero-width obfuscation)."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", text))


def find_prompt_injection(text: str) -> list[str]:
    """Labels of the prompt-injection patterns found in ``text``."""
    normalized = _normalize(text)
    return [label for label, pattern in INJECTION_PATTERNS if pattern.search(normalized)]


def find_destructive_statements(text: str) -> list[str]:
    """Write/DDL/admin SQL operations present as statements in ``text`` (uppercase, deduplicated, in order).

    SQL comments count as whitespace, so ``DELETE/**/FROM`` is found like ``DELETE FROM``.
    """
    normalized = _SQL_COMMENT.sub(" ", _normalize(text))
    hits: list[tuple[int, str]] = []
    for operation, pattern in DESTRUCTIVE_PATTERNS:
        hits.extend((match.start(), operation) for match in pattern.finditer(normalized))
    return list(dict.fromkeys(operation for _, operation in sorted(hits)))


def check_input(text: str, *, max_chars: int = 2000) -> GuardResult:
    """Classify user input as ok, empty, too long, a prompt-injection attempt or destructive SQL."""
    if not text or not text.strip():
        return GuardResult(False, "empty", "The message is empty.")
    if len(text) > max_chars:
        return GuardResult(False, "too_long", f"The message is longer than {max_chars} characters.")
    injection = find_prompt_injection(text)
    if injection:
        return GuardResult(False, "prompt_injection", f"Possible prompt injection: {', '.join(injection)}.", injection)
    operations = find_destructive_statements(text)
    if operations:
        return GuardResult(False, "destructive_sql", f"Destructive SQL is not allowed: {', '.join(operations)}.",
                           operations)
    return GuardResult(True, "ok")


def extract_sql(text: str) -> str | None:
    """SQL pasted into a message: a fenced code block, else the first ``SELECT``/``WITH ... AS (`` statement
    up to a blank line or the end of the text; ``None`` if there is none."""
    for block in fenced_blocks(text):
        if block:
            return re.sub(r"[\s;]+$", "", block) or None
    start = _SQL_START.search(text)
    if start is None:
        return None
    rest = text[start.start():]
    end = _STATEMENT_END.search(rest)
    statement = rest[: end.start()] if end else rest
    return re.sub(r"[\s;]+$", "", statement.strip()) or None


def sanitize_user_text(text: str) -> str:
    """Make untrusted text safe to embed in a prompt.

    Removes control and invisible characters (newline and tab are kept), neutralizes tag-like sequences
    (``</user_message>``, ``<system>``, ``<|im_start|>``) by swapping their angle brackets for the
    look-alikes U+2039/U+203A, and collapses runs of more than three blank lines.
    """
    cleaned = _INVISIBLE.sub("", _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n")))
    cleaned = _TAG.sub(lambda match: f"\u2039{match.group(1) or match.group(2)}\u203a", cleaned)
    return _BLANK_RUN.sub("\n\n", cleaned)
