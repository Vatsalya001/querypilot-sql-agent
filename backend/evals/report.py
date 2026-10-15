"""Summary metrics and report rendering (JSON, Markdown and a console table) for an eval run.

A report is a plain dict ``{"meta": ..., "summary": ..., "cases": [...]}`` so that a saved run (for
example ``results/baseline.json``) can be loaded again and compared with a new one.
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from evals.golden import CATEGORIES, EXECUTION_ACCURACY_CATEGORIES

RESULTS_DIR = Path(__file__).resolve().parent / "results"
VALIDATOR_FALLBACK_REASON = "Reported by the SQL validator."
"""Reason the agent attaches when it lists validator errors because the model gave no issues itself."""

_CATEGORY_LABELS = {
    "generation": "Generation",
    "follow_up": "Follow-up (multi-turn)",
    "optimize": "Optimize",
    "debug": "Debug",
    "guardrail": "Guardrails",
}
_LLM_NODES = ("detect_intent", "generate_sql", "explain_sql", "answer_question")
"""Nodes that call the language model; their average durations explain where latency goes."""
_HEADLINES = (
    ("execution_accuracy", "Execution accuracy (generation + follow-up)"),
    ("held_out_execution_accuracy", "Held-out execution accuracy (without the brief's few-shot examples)"),
    ("guardrail_compliance", "Guardrail compliance"),
    ("optimize", "Optimize pass rate"),
    ("debug", "Debug pass rate"),
    ("debug_explanations", "Debug answers that explain the issue (problem + reason)"),
    ("intent_accuracy", "Intent accuracy"),
    ("overall", "All cases"),
)


# --------------------------------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------------------------------


def _rate(cases: Sequence[dict[str, Any]], key: str = "passed") -> dict[str, Any]:
    passed = sum(1 for case in cases if case.get(key))
    return {"cases": len(cases), "passed": passed, "accuracy": round(passed / len(cases), 4) if cases else None}


def _mean(values: Iterable[float]) -> float | None:
    items = list(values)
    return round(statistics.fmean(items), 2) if items else None


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    """Nearest-rank percentile."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, min(len(ordered), round(fraction * len(ordered) + 0.5)))
    return round(ordered[rank - 1], 2)


def explains_issue(case: dict[str, Any]) -> bool:
    """A debug answer names at least one problem with the model's own reason (not only the validator fallback)."""
    return any(
        (issue.get("problem") or "").strip() and (issue.get("reason") or "").strip() not in ("", VALIDATOR_FALLBACK_REASON)
        for issue in case.get("issues") or []
    )


def summarize(cases: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Headline metrics, per-category accuracy and latency statistics for a list of case results."""
    categories: dict[str, Any] = {}
    for category in CATEGORIES:
        subset = [case for case in cases if case["category"] == category]
        if subset:
            categories[category] = {
                **_rate(subset),
                "avg_latency_s": _mean(case["latency_s"] for case in subset),
                "avg_turn_latency_s": _mean(turn["latency_s"] for case in subset for turn in case["turns"]),
            }
    scored = [case for case in cases if case["category"] in EXECUTION_ACCURACY_CATEGORIES]
    intents = [case for case in cases if case.get("intent_ok") is not None]
    turns = [turn for case in cases for turn in case["turns"]]
    llm_turns = [turn["latency_s"] for turn in turns if turn.get("intent")]
    node_times: dict[str, list[float]] = {}
    for turn in turns:
        for node, duration in (turn.get("node_ms") or {}).items():
            node_times.setdefault(node, []).append(duration / 1000)
    return {
        "overall": _rate(cases),
        "categories": categories,
        "execution_accuracy": _rate(scored),
        "held_out_execution_accuracy": _rate([case for case in scored if not case.get("prompt_example")]),
        "debug_explanations": _rate(
            [{"explained": explains_issue(case)} for case in cases if case["category"] == "debug"], key="explained"
        ),
        "guardrail_compliance": _rate([case for case in cases if case["category"] == "guardrail"]),
        "intent_accuracy": _rate(intents, key="intent_ok"),
        "latency": {
            "turns": len(turns),
            "llm_turns": len(llm_turns),
            "avg_turn_s": _mean(turn["latency_s"] for turn in turns),
            "avg_llm_turn_s": _mean(llm_turns),
            "p50_llm_turn_s": _percentile(llm_turns, 0.5),
            "p90_llm_turn_s": _percentile(llm_turns, 0.9),
            "max_turn_s": round(max((turn["latency_s"] for turn in turns), default=0.0), 2),
            "avg_node_s": {node: _mean(values) for node, values in node_times.items() if node in _LLM_NODES},
        },
        "agent_errors": sum(1 for case in cases if case.get("error")),
    }


def build_report(cases: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    """Assemble the report dict written to ``latest.json``."""
    return {"meta": meta, "summary": summarize(cases), "cases": cases}


def _metric(summary: dict[str, Any], key: str) -> dict[str, Any] | None:
    """A headline metric; ``optimize``/``debug`` come from the per-category table."""
    if key in ("optimize", "debug"):
        return (summary.get("categories") or {}).get(key)
    return summary.get(key)


# --------------------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------------------


def _fmt_rate(stats: dict[str, Any] | None) -> str:
    if not stats or not stats.get("cases"):
        return "n/a"
    return f"{stats['passed']}/{stats['cases']} ({100 * stats['accuracy']:.1f}%)"


def _fmt_seconds(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.1f} s"


def _cell(text: Any, limit: int = 160) -> str:
    """Markdown table cell: one line, pipes escaped, clipped."""
    flat = " ".join(str(text if text is not None else "").split()).replace("|", "\\|")
    return flat if len(flat) <= limit else flat[: limit - 3] + "..."


def _meta_lines(meta: dict[str, Any]) -> list[str]:
    golden = meta.get("golden") or {}
    filters = golden.get("filters") or {}
    subset = " (subset: " + ", ".join(f"{key}={value}" for key, value in filters.items() if value) + ")" if any(
        filters.values()
    ) else ""
    warmup = meta.get("warmup_s")
    return [
        f"- **Model:** {meta.get('provider')} / `{meta.get('model')}` (fast model `{meta.get('fast_model')}`)",
        f"- **Run:** {meta.get('started_at')} - {golden.get('cases_run')} of {golden.get('cases_total')} cases"
        f"{subset}, {meta.get('agent_turns')} agent turns, {meta.get('duration_s', 0) / 60:.1f} min total"
        + (f"; prompt-cache warm-up {warmup:.1f} s before the first case" if warmup is not None else ""),
        f"- **Golden set:** `evals/{Path(str(golden.get('path') or 'golden.json')).name}` v{golden.get('version')} "
        f"(sha256 `{str(golden.get('sha256'))[:12]}`); "
        f"system prompts sha256 `{str(meta.get('prompts_sha256'))[:12]}`",
        f"- **Database unchanged after the run:** {'yes' if meta.get('database_unchanged') else 'NO'}",
        f"- **Structured LLM output:** {_structured_line(meta)}",
    ]


def _structured_line(meta: dict[str, Any]) -> str:
    """How often the gateway had to repair the model's structured output."""
    structured = meta.get("structured_output") or {}
    calls = meta.get("llm_calls")
    return (f"{calls if calls is not None else '?'} LLM calls; {structured.get('recovered', 0)} replies recovered "
            f"from text, {structured.get('fallbacks', 0)} JSON fallbacks")


def _summary_of(report: dict[str, Any] | None) -> dict[str, Any] | None:
    """Summary of a (possibly older) report, recomputed from its cases so every current metric is present."""
    if not report:
        return None
    return summarize(report["cases"]) if report.get("cases") else report.get("summary")


def render_markdown(report: dict[str, Any], baseline: dict[str, Any] | None = None) -> str:
    """Human-readable report: headline metrics, per-category table, failures and every case."""
    summary, meta, cases = report["summary"], report["meta"], report["cases"]
    base = _summary_of(baseline)
    lines = ["# QueryPilot evaluation report", "", *_meta_lines(meta), ""]
    if base:
        base_meta = baseline.get("meta") or {}
        lines += [f"Baseline: run of {base_meta.get('started_at')} (prompts sha256 "
                  f"`{str(base_meta.get('prompts_sha256'))[:12]}`).", ""]

    lines += ["## Headline metrics", ""]
    header = "| Metric | Result |" + (" Baseline |" if base else "")
    lines += [header, "|---|---|" + ("---|" if base else "")]
    for key, label in _HEADLINES:
        row = f"| {label} | {_fmt_rate(_metric(summary, key))} |"
        lines.append(row + (f" {_fmt_rate(_metric(base, key))} |" if base else ""))
    latency = summary["latency"]
    base_latency = (base or {}).get("latency") or {}
    lines.append(
        f"| Average latency per LLM-backed turn | {_fmt_seconds(latency.get('avg_llm_turn_s'))} "
        f"(p50 {_fmt_seconds(latency.get('p50_llm_turn_s'))}, p90 {_fmt_seconds(latency.get('p90_llm_turn_s'))}) |"
        + (f" {_fmt_seconds(base_latency.get('avg_llm_turn_s'))} |" if base else "")
    )
    lines.append("")
    node_times = latency.get("avg_node_s") or {}
    if node_times:
        parts = [f"{node} {_fmt_seconds(node_times[node])}" for node in _LLM_NODES if node in node_times]
        lines += ["Average time per LLM node (per turn that ran it, repairs included): " + ", ".join(parts) + ".", ""]

    lines += ["## By category", ""]
    lines.append("| Category | Cases | Passed | Accuracy | Avg latency per case |" + (" Baseline accuracy |" if base else ""))
    lines.append("|---|---:|---:|---:|---:|" + ("---:|" if base else ""))
    for category, stats in summary["categories"].items():
        row = (f"| {_CATEGORY_LABELS.get(category, category)} | {stats['cases']} | {stats['passed']} | "
               f"{100 * stats['accuracy']:.1f}% | {_fmt_seconds(stats['avg_latency_s'])} |")
        if base:
            row += f" {_fmt_rate((base.get('categories') or {}).get(category))} |"
        lines.append(row)
    lines.append("")

    failures = [case for case in cases if not case["passed"]]
    lines += ["## Failures", ""]
    if not failures:
        lines += ["None.", ""]
    for case in failures:
        lines += [f"### `{case['id']}` ({case['category']})", ""]
        for index, turn in enumerate(case["turns"], 1):
            prefix = f"Turn {index}" if len(case["turns"]) > 1 else "Question"
            lines.append(f"- **{prefix}:** {_cell(turn['message'], 400)} -> {turn.get('status')} / "
                         f"{turn.get('intent')}" + (f" (error: {_cell(turn['error'])})" if turn.get("error") else ""))
        if case.get("reference_sql"):
            label = "Original SQL" if case["category"] == "optimize" else "Gold SQL"
            lines.append(f"- **{label}:** `{_cell(case['reference_sql'], 600)}`")
        if case.get("sql"):
            lines.append(f"- **Agent SQL:** `{_cell(case['sql'], 600)}`")
        lines.append(f"- **Reason:** {_cell(case['reason'], 400)}")
        if case.get("message") and not case.get("sql"):
            lines.append(f"- **Agent message:** {_cell(case['message'], 300)}")
        lines.append("")

    lines += ["## All cases", "",
              "| Case | Category | Result | Status | Intent | Attempts | Latency | Detail |",
              "|---|---|---|---|---|---:|---:|---|"]
    for case in cases:
        intent = case.get("intent") or "-"
        if case.get("intent_ok") is False:
            intent += f" (expected {case.get('expected_intent')})"
        lines.append(
            f"| `{case['id']}` | {case['category']} | {'PASS' if case['passed'] else 'FAIL'} | {case.get('status')} | "
            f"{intent} | {case.get('attempts', 0)} | {case['latency_s']:.1f} s | {_cell(case['reason'], 110)} |"
        )
    lines += ["", "## Scoring", "",
              "- Generation and follow-up: the agent's SQL and the gold SQL are executed on the sample database and "
              "their result sets compared (values only: column names and order are ignored, extra columns are "
              "allowed, numbers are compared at 2 decimals; row order only matters for ordering / top-N questions). "
              "Follow-ups run every turn on one conversation thread and score the last turn.",
              "- Debug: the corrected SQL must pass validation and return the gold result set.",
              "- Optimize: the rewrite must pass validation, return the same result set as the original query, and "
              "report at least one change or suggestion.",
              "- Guardrails: the expected status (refused / rejected / clarification), no SQL in the reply, and the "
              "exact brief message for out-of-scope requests.",
              "- Latency is wall-clock time per agent turn, measured sequentially after the prompt-cache warm-up; "
              "turns blocked by the deterministic input guard never reach the LLM and are excluded from the "
              "LLM-backed average.", ""]
    return "\n".join(lines)


def render_console(report: dict[str, Any], baseline: dict[str, Any] | None = None) -> str:
    """Compact per-category table and headline metrics for the terminal."""
    summary = report["summary"]
    base = _summary_of(baseline) or {}
    lines = [f"{'Category':<24}{'Cases':>6}{'Passed':>8}{'Accuracy':>10}{'Avg latency':>13}"
             + (f"{'Baseline':>11}" if base else "")]
    for category, stats in summary["categories"].items():
        row = (f"{_CATEGORY_LABELS.get(category, category):<24}{stats['cases']:>6}{stats['passed']:>8}"
               f"{100 * stats['accuracy']:>9.1f}%{_fmt_seconds(stats['avg_latency_s']):>13}")
        base_stats = (base.get("categories") or {}).get(category)
        if base:
            shown = f"{100 * base_stats['accuracy']:.1f}%" if base_stats else "n/a"
            row += f"{shown:>11}"
        lines.append(row)
    lines.append("")
    for key, label in _HEADLINES:
        stats = _metric(summary, key)
        if stats and stats.get("cases"):
            lines.append(f"{label}: {_fmt_rate(stats)}" + (f"  [baseline {_fmt_rate(_metric(base, key))}]" if base else ""))
    latency = summary["latency"]
    lines.append(
        f"Latency per LLM-backed turn: avg {_fmt_seconds(latency['avg_llm_turn_s'])}, "
        f"p50 {_fmt_seconds(latency['p50_llm_turn_s'])}, p90 {_fmt_seconds(latency['p90_llm_turn_s'])}, "
        f"max turn {_fmt_seconds(latency['max_turn_s'])}"
    )
    if summary.get("agent_errors"):
        lines.append(f"Agent errors: {summary['agent_errors']} case(s)")
    return "\n".join(lines)


def write_reports(
    report: dict[str, Any],
    out_dir: Path = RESULTS_DIR,
    *,
    names: Sequence[str] = ("latest",),
    baseline: dict[str, Any] | None = None,
) -> list[Path]:
    """Write ``<name>.json`` and ``<name>.md`` for every name; returns the written paths."""
    out_dir.mkdir(parents=True, exist_ok=True)
    markdown = render_markdown(report, baseline)
    payload = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    written: list[Path] = []
    for name in dict.fromkeys(names):
        json_path, md_path = out_dir / f"{name}.json", out_dir / f"{name}.md"
        json_path.write_text(payload + "\n", encoding="utf-8")
        md_path.write_text(markdown, encoding="utf-8")
        written += [json_path, md_path]
    return written


def load_report(path: str | Path) -> dict[str, Any]:
    """Load a saved report (e.g. a baseline) written by :func:`write_reports`."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "summary" not in data:
        raise ValueError(f"{path} is not an eval report")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    """Re-render a saved report: ``python -m evals.report REPORT.json [--baseline BASELINE.json]``.

    The summary is recomputed from the cases (so new metrics apply to old runs) and ``REPORT.json`` /
    ``REPORT.md`` are rewritten in place.
    """
    import argparse

    parser = argparse.ArgumentParser(prog="python -m evals.report", description="Re-render a saved eval report.")
    parser.add_argument("report", type=Path)
    parser.add_argument("--baseline", type=Path)
    args = parser.parse_args(argv)
    report = load_report(args.report)
    report["summary"] = summarize(report["cases"])
    baseline = load_report(args.baseline) if args.baseline else None
    written = write_reports(report, args.report.parent, names=(args.report.stem,), baseline=baseline)
    print("Rewrote " + ", ".join(str(path) for path in written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
