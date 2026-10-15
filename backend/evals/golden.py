"""Golden evaluation cases: data model, loading and structural validation.

``golden.json`` holds one object per case. Single-turn cases use ``question``; multi-turn follow-ups
use ``turns`` (the last turn is scored). What a case must provide depends on its category:

* ``generation``, ``follow_up``, ``debug``: ``gold_sql``, the reference query whose result set the
  agent's SQL must reproduce;
* ``optimize``: ``original_sql``, the user's query pasted in the question; the rewrite must return
  the same result set;
* ``guardrail``: ``expected_status`` (``refused``, ``rejected`` or ``clarification``) and optionally
  ``expected_message`` (the exact reply).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GOLDEN_PATH = Path(__file__).resolve().parent / "golden.json"
DEV_PATH = Path(__file__).resolve().parent / "dev.json"
"""Development split used while tuning prompts; the reported numbers come from ``GOLDEN_PATH``."""
CATEGORIES: tuple[str, ...] = ("generation", "follow_up", "optimize", "debug", "guardrail")
GOLD_SQL_CATEGORIES = frozenset({"generation", "follow_up", "debug"})
EXECUTION_ACCURACY_CATEGORIES = ("generation", "follow_up")
GUARDRAIL_STATUSES = frozenset({"refused", "rejected", "clarification"})

_ALLOWED_KEYS = frozenset({
    "id", "category", "question", "turns", "gold_sql", "original_sql", "ordered", "expected_status",
    "expected_intent", "expected_message", "prompt_example", "tags", "notes",
})


@dataclass(frozen=True)
class GoldenCase:
    """One evaluation case. ``turns`` always holds at least one user message; the last one is scored."""

    id: str
    category: str
    turns: tuple[str, ...]
    gold_sql: str | None = None
    original_sql: str | None = None
    ordered: bool = False
    expected_status: str = "success"
    expected_intent: str | None = None
    expected_message: str | None = None
    prompt_example: bool = False
    tags: tuple[str, ...] = ()
    notes: str = ""

    @property
    def question(self) -> str:
        """The scored (last) user message."""
        return self.turns[-1]

    @property
    def reference_sql(self) -> str | None:
        """The query whose result set the agent must reproduce (gold or original SQL)."""
        return self.original_sql if self.category == "optimize" else self.gold_sql


def _text(data: dict[str, Any], key: str, case_id: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{case_id}: '{key}' must be a non-empty string")
    return value


def parse_case(data: dict[str, Any]) -> GoldenCase:
    """Build a :class:`GoldenCase` from its JSON object; raises ``ValueError`` when it is malformed."""
    case_id = data.get("id")
    if not isinstance(case_id, str) or not case_id.strip():
        raise ValueError(f"case without a valid id: {data!r:.120}")
    unknown = sorted(set(data) - _ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"{case_id}: unknown keys {unknown}")
    category = data.get("category")
    if category not in CATEGORIES:
        raise ValueError(f"{case_id}: category must be one of {CATEGORIES}, got {category!r}")

    question, turns = data.get("question"), data.get("turns")
    if (question is None) == (turns is None):
        raise ValueError(f"{case_id}: give exactly one of 'question' or 'turns'")
    messages = [question] if question is not None else turns
    if not isinstance(messages, list) or not all(isinstance(m, str) and m.strip() for m in messages):
        raise ValueError(f"{case_id}: turns must be non-empty strings")
    if category == "follow_up" and len(messages) < 2:
        raise ValueError(f"{case_id}: follow_up cases need at least two turns")
    if category != "follow_up" and len(messages) != 1:
        raise ValueError(f"{case_id}: only follow_up cases may have several turns")

    gold_sql = _text(data, "gold_sql", case_id)
    original_sql = _text(data, "original_sql", case_id)
    if category in GOLD_SQL_CATEGORIES and gold_sql is None:
        raise ValueError(f"{case_id}: {category} cases need gold_sql")
    if category == "optimize" and original_sql is None:
        raise ValueError(f"{case_id}: optimize cases need original_sql")

    expected_status = data.get("expected_status", "success")
    if category == "guardrail" and expected_status not in GUARDRAIL_STATUSES:
        raise ValueError(f"{case_id}: guardrail cases need expected_status in {sorted(GUARDRAIL_STATUSES)}")
    if category != "guardrail" and expected_status != "success":
        raise ValueError(f"{case_id}: only guardrail cases may expect a status other than success")

    tags = data.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError(f"{case_id}: tags must be a list of strings")
    return GoldenCase(
        id=case_id,
        category=category,
        turns=tuple(messages),
        gold_sql=gold_sql,
        original_sql=original_sql,
        ordered=bool(data.get("ordered", False)),
        expected_status=expected_status,
        expected_intent=data.get("expected_intent"),
        expected_message=_text(data, "expected_message", case_id),
        prompt_example=bool(data.get("prompt_example", False)),
        tags=tuple(tags),
        notes=str(data.get("notes", "")),
    )


def load_golden(path: str | Path = GOLDEN_PATH) -> list[GoldenCase]:
    """Load and validate every case of a golden file (ids must be unique)."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_cases = document.get("cases") if isinstance(document, dict) else None
    if not isinstance(raw_cases, list) or not raw_cases:
        raise ValueError(f"{path}: expected an object with a non-empty 'cases' list")
    cases = [parse_case(item) for item in raw_cases]
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise ValueError(f"duplicate case id: {case.id}")
        seen.add(case.id)
    return cases


def golden_version(path: str | Path = GOLDEN_PATH) -> int | None:
    """The ``version`` field of a golden file, if present."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    version = document.get("version") if isinstance(document, dict) else None
    return version if isinstance(version, int) else None


def select_cases(
    cases: list[GoldenCase],
    *,
    categories: set[str] | None = None,
    ids: set[str] | None = None,
    limit: int | None = None,
) -> list[GoldenCase]:
    """Filter cases by category and id (keeping file order), then keep at most ``limit`` of them."""
    selected = [
        case for case in cases
        if (not categories or case.category in categories) and (not ids or case.id in ids)
    ]
    return selected[:limit] if limit is not None and limit >= 0 else selected
