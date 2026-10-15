"""Unit tests of the eval report: summary metrics, baseline comparison and re-rendering."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from evals.report import VALIDATOR_FALLBACK_REASON, explains_issue, main, render_markdown, summarize


def case(case_id: str, category: str, passed: bool, **extra: Any) -> dict[str, Any]:
    turn = {"message": "q", "latency_s": 2.0, "status": "success", "intent": "generate_sql", "node_ms": {"detect_intent": 900.0}}
    return {"id": case_id, "category": category, "passed": passed, "reason": "r", "latency_s": 2.0, "turns": [turn],
            "status": "success", "intent": "generate_sql", "intent_ok": True, "attempts": 1, "issues": [], **extra}


def report_of(cases: list[dict[str, Any]]) -> dict[str, Any]:
    meta = {"provider": "mock", "model": "mock", "fast_model": "mock", "started_at": "t", "duration_s": 1.0,
            "agent_turns": len(cases), "golden": {"path": "evals/golden.json", "cases_run": len(cases)}}
    return {"meta": meta, "summary": summarize(cases), "cases": cases}


def test_explains_issue_ignores_the_validator_fallback() -> None:
    assert explains_issue({"issues": [{"problem": "Missing GROUP BY", "reason": "AVG collapses all rows."}]})
    assert not explains_issue({"issues": [{"problem": "Unknown column", "reason": VALIDATOR_FALLBACK_REASON}]})
    assert not explains_issue({"issues": []})


def test_summary_metrics() -> None:
    cases = [
        case("g1", "generation", True), case("g2", "generation", False, prompt_example=True),
        case("f1", "follow_up", True),
        case("d1", "debug", True, issues=[{"problem": "p", "reason": "why"}]), case("d2", "debug", True),
        case("x1", "guardrail", True, intent_ok=None),
    ]
    summary = summarize(cases)
    assert summary["execution_accuracy"] == {"cases": 3, "passed": 2, "accuracy": 0.6667}
    assert summary["held_out_execution_accuracy"]["accuracy"] == 1.0
    assert summary["debug_explanations"] == {"cases": 2, "passed": 1, "accuracy": 0.5}
    assert summary["intent_accuracy"]["cases"] == 5
    assert summary["latency"]["avg_node_s"] == {"detect_intent": 0.9}


def test_baseline_metrics_are_recomputed_from_cases() -> None:
    current = report_of([case("d1", "debug", True, issues=[{"problem": "p", "reason": "why"}])])
    old = report_of([case("d1", "debug", True)])
    old["summary"].pop("debug_explanations")  # an older report without the metric
    markdown = render_markdown(current, old)
    assert "| Debug answers that explain the issue (problem + reason) | 1/1 (100.0%) | 0/1 (0.0%) |" in markdown


def test_main_rerenders_a_saved_report(tmp_path: Path) -> None:
    path = tmp_path / "run.json"
    stale = report_of([case("g1", "generation", True)])
    stale["summary"] = {}  # e.g. written by an older version of the runner
    path.write_text(json.dumps(stale), encoding="utf-8")
    assert main([str(path)]) == 0
    assert json.loads(path.read_text(encoding="utf-8"))["summary"]["overall"]["passed"] == 1
    assert "## Headline metrics" in (tmp_path / "run.md").read_text(encoding="utf-8")
