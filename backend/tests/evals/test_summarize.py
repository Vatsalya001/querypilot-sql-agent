"""The consolidated eval report is generated from run artifacts (no hand-typed numbers)."""

from pathlib import Path

from evals.summarize import RESULTS_DIR, Run, build_report


def _report(passed: int, total: int, *, failed_id: str | None = None) -> dict:
    block = {"cases": total, "passed": passed, "accuracy": passed / total}
    cases = [{"id": "ok_case", "category": "generation", "question": "Q?", "passed": True, "reason": ""}]
    if failed_id:
        cases.append({"id": failed_id, "category": "generation", "question": "Hard?", "passed": False,
                      "reason": "result mismatch"})
    return {
        "meta": {"golden": {"cases_run": total}, "prompts_sha256": "abc123def4567890",
                 "structured_output": {"fallbacks": 0}, "started_at": "2026-10-16T02:00:00"},
        "summary": {"overall": block, "execution_accuracy": block, "held_out_execution_accuracy": block,
                    "guardrail_compliance": block, "intent_accuracy": block, "agent_errors": 0,
                    "categories": {"generation": {**block, "avg_latency_s": 3.2}},
                    "latency": {"avg_llm_turn_s": 4.0, "p90_llm_turn_s": 7.5}},
        "cases": cases,
    }


def test_report_uses_the_run_numbers_and_lists_failures() -> None:
    run = Run("groq", "Hosted · Groq", "golden", RESULTS_DIR / "groq" / "final.json")
    text = build_report([(run, _report(19, 20, failed_id="gen_hard"))], baseline=None)
    assert "19/20 (95.0%)" in text
    assert "`gen_hard`" in text and "result mismatch" in text
    assert "abc123def456" in text
    assert "[`results/groq/final.md`](results/groq/final.md)" in text


def test_report_without_failures_says_so() -> None:
    run = Run("ollama", "Local", "dev", Path(RESULTS_DIR / "ollama" / "dev" / "final.json"))
    assert "no failures" in build_report([(run, _report(5, 5))], baseline=None)
