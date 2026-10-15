"""Run the golden evaluation set through the SQL agent and report accuracy and latency.

Usage (from ``backend/``)::

    python -m evals.run_evals                          # every case, LLM settings from the environment / .env
    python -m evals.run_evals --provider mock          # offline rule-based gateway (no model needed)
    python -m evals.run_evals --category generation --limit 5
    python -m evals.run_evals --case fu_brief_california --case dbg_missing_group_by
    python -m evals.run_evals --save-as baseline       # also write results/baseline.{json,md}
    python -m evals.run_evals --baseline evals/results/baseline.json   # add a baseline column
    python -m evals.run_evals --golden evals/dev.json --out-dir evals/results/dev   # tuning split

Cases run sequentially through :class:`~app.agent.service.SQLAgent` directly (no HTTP). Every case
gets its own conversation thread; follow-up cases send their turns in order on that thread and the
last turn is scored. For Ollama the prompt cache is warmed first, so latencies are warm. Reports go
to ``evals/results/latest.json`` and ``latest.md`` (see :mod:`evals.report` and :mod:`evals.scoring`).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import sys
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.agent.llm import llm_health
from app.agent.service import AgentRunError, ChatInput, SQLAgent
from app.config import Settings
from evals.golden import CATEGORIES, GOLDEN_PATH, GoldenCase, golden_version, load_golden, select_cases
from evals.report import RESULTS_DIR, build_report, load_report, render_console, write_reports
from evals.scoring import Score, Scorer

PROVIDERS = ("ollama", "groq", "google", "openai", "mock")
_GROQ_DEFAULT_MODELS = ("openai/gpt-oss-120b", "openai/gpt-oss-20b")


@dataclass
class TurnRecord:
    """One user message sent to the agent and what came back."""

    message: str
    latency_s: float
    status: str | None = None
    intent: str | None = None
    sql: str | None = None
    attempts: int = 0
    error: str | None = None
    node_ms: dict[str, float] = field(default_factory=dict)
    llm_calls: int = 0

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation."""
        return {"message": self.message, "latency_s": round(self.latency_s, 3), "status": self.status,
                "intent": self.intent, "sql": self.sql, "attempts": self.attempts, "error": self.error,
                "node_ms": self.node_ms, "llm_calls": self.llm_calls}


LLM_NODES = frozenset({"detect_intent", "generate_sql", "explain_sql", "answer_question"})
"""Graph nodes that make one language-model call each time they run."""


class GatewayLogCounter(logging.Handler):
    """Counts the gateway's structured-output recoveries and fallbacks (logged by ``app.agent.llm``)."""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.recovered = 0
        self.fallbacks = 0
        self.samples: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        message = record.getMessage()
        if message.startswith("Recovered "):
            self.recovered += 1
        elif "JSON fallback" in message or message.startswith("Structured call"):
            self.fallbacks += 1
        else:
            return
        if len(self.samples) < 10:
            self.samples.append(message[:200])


def node_durations(steps: Sequence[dict[str, Any]]) -> dict[str, float]:
    """Total milliseconds per graph node of one turn (repair loops run a node several times)."""
    totals: dict[str, float] = {}
    for step in steps:
        node, duration = step.get("node"), step.get("duration_ms")
        if isinstance(node, str) and isinstance(duration, int | float):
            totals[node] = round(totals.get(node, 0.0) + float(duration), 1)
    return totals


@dataclass
class CaseResult:
    """All turns of one case, the final AgentResponse and its score."""

    case: GoldenCase
    turns: list[TurnRecord]
    final: dict[str, Any] | None
    score: Score
    error: str | None = None

    @property
    def latency_s(self) -> float:
        """Total wall-clock time of the case (all turns)."""
        return sum(turn.latency_s for turn in self.turns)

    @property
    def intent_ok(self) -> bool | None:
        """Whether the final turn's intent is the expected one (``None`` when no intent is expected)."""
        if self.case.expected_intent is None:
            return None
        return bool(self.final) and self.final.get("intent") == self.case.expected_intent

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe representation used in the reports."""
        final = self.final or {}
        return {
            "id": self.case.id,
            "category": self.case.category,
            "tags": list(self.case.tags),
            "prompt_example": self.case.prompt_example,
            "question": self.case.question,
            "passed": self.score.passed,
            "reason": self.score.reason,
            "checks": self.score.checks,
            "status": final.get("status"),
            "expected_status": self.case.expected_status,
            "intent": final.get("intent"),
            "expected_intent": self.case.expected_intent,
            "intent_ok": self.intent_ok,
            "attempts": final.get("attempts", 0),
            "latency_s": round(self.latency_s, 3),
            "sql": final.get("sql_sqlite"),
            "reference_sql": self.case.reference_sql,
            "message": final.get("message"),
            "issues": final.get("issues") or [],
            "changes": final.get("changes") or [],
            "suggestions": final.get("suggestions") or [],
            "assumptions": final.get("assumptions") or [],
            "error": self.error,
            "turns": [turn.to_dict() for turn in self.turns],
        }


def file_sha256(path: Path) -> str:
    """Hex digest of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def prompts_fingerprint(agent: SQLAgent) -> str:
    """Hash of the four static system prompts plus the per-mode SQL instructions (the prompt version)."""
    from app.agent.prompts import _MODE_INSTRUCTIONS

    prompts = agent.prompts
    modes = [f"{name}={text}" for name, text in sorted(_MODE_INSTRUCTIONS.items())]
    joined = "\x1e".join((prompts.intent, prompts.sql, prompts.explain, prompts.answer, *modes))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def display_path(path: Path) -> str:
    """Path relative to backend/ for reports (no machine-specific absolute paths); else the file name."""
    try:
        return str(Path(path).resolve().relative_to(Path(__file__).resolve().parent.parent))
    except ValueError:
        return Path(path).name


async def run_case(agent: SQLAgent, case: GoldenCase, scorer: Scorer) -> CaseResult:
    """Send every turn of ``case`` on a fresh thread, then score the final response."""
    thread_id = f"eval-{case.id}-{uuid.uuid4().hex[:8]}"
    turns: list[TurnRecord] = []
    final: dict[str, Any] | None = None
    error: str | None = None
    for message in case.turns:
        started = time.perf_counter()
        try:
            final = await agent.run(ChatInput(message=message, thread_id=thread_id, dialect="sqlite", execute=True))
        except AgentRunError as exc:
            final, error = None, f"{exc.code}: {exc.message}"
        latency = time.perf_counter() - started
        response = final or {}
        turns.append(TurnRecord(
            message=message, latency_s=latency, status=response.get("status"), intent=response.get("intent"),
            sql=response.get("sql_sqlite"), attempts=int(response.get("attempts") or 0), error=error,
            node_ms=node_durations(response.get("steps") or []),
            llm_calls=sum(1 for step in response.get("steps") or [] if step.get("node") in LLM_NODES),
        ))
        if error:
            break
    try:
        score = scorer.score(case, final, error)
    except ValueError as exc:  # broken golden SQL: report it instead of aborting the whole run
        score = Score(False, f"golden case error: {exc}")
    return CaseResult(case=case, turns=turns, final=final, score=score, error=error)


async def run_suite(
    cases: Sequence[GoldenCase],
    settings: Settings,
    *,
    db_path: Path | None = None,
    warm_up: bool = True,
    on_result: Callable[[int, int, CaseResult], None] | None = None,
    golden_path: Path = GOLDEN_PATH,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run ``cases`` sequentially on one agent and return the report dict (see :mod:`evals.report`)."""
    agent = SQLAgent(settings, db_path=db_path)
    database = agent.db_path
    database_hash = file_sha256(database)
    started_at = datetime.now().astimezone()
    started = time.perf_counter()

    warmup_s: float | None = None
    if warm_up:
        warm_started = time.perf_counter()
        if await agent.warm_up():
            warmup_s = round(time.perf_counter() - warm_started, 2)

    scorer = Scorer(database)
    results: list[CaseResult] = []
    gateway_logger = logging.getLogger("app.agent.llm")
    counter, previous_level = GatewayLogCounter(), gateway_logger.level
    gateway_logger.addHandler(counter)
    gateway_logger.setLevel(min(previous_level or logging.INFO, logging.INFO))
    try:
        for index, case in enumerate(cases, 1):
            result = await run_case(agent, case, scorer)
            results.append(result)
            if on_result is not None:
                on_result(index, len(cases), result)
    finally:
        gateway_logger.removeHandler(counter)
        gateway_logger.setLevel(previous_level)

    gateway = agent.gateway
    meta = {
        "provider": gateway.provider,
        "model": gateway.model,
        "fast_model": gateway.fast_model,
        "started_at": started_at.isoformat(timespec="seconds"),
        "duration_s": round(time.perf_counter() - started, 1),
        "warmup_s": warmup_s,
        "agent_turns": sum(len(result.turns) for result in results),
        "llm_calls": sum(turn.llm_calls for result in results for turn in result.turns),
        "structured_output": {"recovered": counter.recovered, "fallbacks": counter.fallbacks,
                              "samples": counter.samples},
        "database": display_path(database),
        "database_unchanged": file_sha256(database) == database_hash,
        "prompts_sha256": prompts_fingerprint(agent),
        "max_sql_attempts": settings.max_sql_attempts,
        "golden": {
            "path": display_path(golden_path),
            "version": golden_version(golden_path),
            "sha256": file_sha256(golden_path),
            "cases_total": len(load_golden(golden_path)),
            "cases_run": len(cases),
            "filters": filters or {},
        },
    }
    return build_report([result.to_dict() for result in results], meta)


# --------------------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------------------


def _split(values: Sequence[str] | None) -> set[str]:
    """Flatten repeated and comma-separated CLI values."""
    return {item.strip() for value in values or () for item in value.split(",") if item.strip()}


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """Command-line options."""
    parser = argparse.ArgumentParser(prog="python -m evals.run_evals", description=__doc__.split("\n\n")[0])
    parser.add_argument("--category", action="append", help=f"only these categories ({', '.join(CATEGORIES)}); repeatable")
    parser.add_argument("--case", action="append", help="only these case ids; repeatable or comma-separated")
    parser.add_argument("--limit", type=int, help="run at most N cases (after filtering)")
    parser.add_argument("--provider", choices=PROVIDERS, help="LLM provider (default: LLM_PROVIDER from the environment)")
    parser.add_argument("--model", help="main model (default: LLM_MODEL)")
    parser.add_argument("--fast-model", help="model for intent and explanations (default: LLM_FAST_MODEL)")
    parser.add_argument("--golden", type=Path, default=GOLDEN_PATH, help="golden file (default: evals/golden.json)")
    parser.add_argument("--db", help="SQLite database path (default: DATABASE_PATH or backend/data/company.db)")
    parser.add_argument("--out-dir", type=Path, default=RESULTS_DIR, help="report directory (default: evals/results)")
    parser.add_argument("--save-as", help="also write <out-dir>/<NAME>.json and .md (e.g. baseline)")
    parser.add_argument("--baseline", type=Path, help="earlier report JSON to compare against")
    parser.add_argument("--no-warmup", action="store_true", help="skip the prompt-cache warm-up")
    parser.add_argument("--verbose", action="store_true", help="show the agent's INFO logs")
    return parser.parse_args(argv)


def build_settings(args: argparse.Namespace) -> Settings:
    """Settings from the environment, with the CLI overrides applied."""
    overrides: dict[str, Any] = {"log_level": "INFO" if args.verbose else "WARNING"}
    if args.provider:
        overrides["llm_provider"] = args.provider
    if args.model:
        overrides["llm_model"] = args.model
    if args.fast_model is not None:
        overrides["llm_fast_model"] = args.fast_model
    if args.db:
        overrides["database_path"] = args.db
    settings = Settings(**overrides)
    default_model = Settings.model_fields["llm_model"].default
    if settings.llm_provider == "groq" and not args.model and settings.llm_model == default_model:
        main_model, fast_model = _GROQ_DEFAULT_MODELS
        settings = settings.model_copy(update={"llm_model": main_model, "llm_fast_model": args.fast_model or fast_model})
    return settings


def _print_progress(index: int, total: int, result: CaseResult) -> None:
    final = result.final or {}
    verdict = "PASS" if result.score.passed else "FAIL"
    print(
        f"[{index:>2}/{total}] {verdict}  {result.case.id:<36} {result.latency_s:>6.1f} s  "
        f"{final.get('status') or '-'}/{final.get('intent') or '-'}  {result.score.reason[:100]}",
        flush=True,
    )


async def _run(args: argparse.Namespace) -> int:
    cases = select_cases(
        load_golden(args.golden), categories=_split(args.category), ids=_split(args.case), limit=args.limit
    )
    if not cases:
        print("No cases match the filters.", file=sys.stderr)
        return 2
    settings = build_settings(args)
    if settings.llm_provider != "mock":
        health = await llm_health(settings)
        if not health.get("configured"):
            print(f"The {settings.llm_provider} model is not usable ({health}). Start Ollama / set the API key, "
                  "or run with --provider mock.", file=sys.stderr)
            return 2
    model = "rule-based mock" if settings.llm_provider == "mock" else settings.llm_model
    print(f"Running {len(cases)} cases with {settings.llm_provider} / {model} ...", flush=True)
    filters = {"category": sorted(_split(args.category)), "case": sorted(_split(args.case)), "limit": args.limit}
    report = await run_suite(
        cases, settings, warm_up=not args.no_warmup, on_result=_print_progress, golden_path=args.golden,
        filters=filters,
    )
    baseline = load_report(args.baseline) if args.baseline else None
    names = ["latest", *([args.save_as] if args.save_as else [])]
    paths = write_reports(report, args.out_dir, names=names, baseline=baseline)
    print()
    print(render_console(report, baseline))
    print("\nReports: " + ", ".join(str(path) for path in paths))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
