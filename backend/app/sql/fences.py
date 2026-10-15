"""Markdown code fences around SQL (as written by LLMs and pasted by users)."""

from __future__ import annotations

import re

_FENCE = re.compile(r"```(.*?)(?:```|\Z)", re.DOTALL)
_TAG_LINE = re.compile(r"[ \t]*[\w+.-]*[ \t]*")
_INLINE_TAG = re.compile(r"[ \t]*([\w+.-]+)[ \t]+")
_LANGUAGES = frozenset(
    {"sql", "sqlite", "sqlite3", "postgres", "postgresql", "pgsql", "psql", "mysql", "mariadb", "tsql", "plsql",
     "ansi", "text"}
)


def _body(block: str) -> str:
    """Fence content without its language tag (a tag line, or a known language name on a one-line fence)."""
    first, newline, rest = block.partition("\n")
    if newline and _TAG_LINE.fullmatch(first):
        return rest
    tag = _INLINE_TAG.match(block)
    if tag and tag.group(1).lower() in _LANGUAGES:
        return block[tag.end():]
    return block


def fenced_blocks(text: str) -> list[str]:
    """Contents of the fenced code blocks in ``text`` (an unterminated last fence runs to the end)."""
    return [_body(match.group(1)).strip() for match in _FENCE.finditer(text)]
