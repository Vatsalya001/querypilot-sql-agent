"""Offline checks of the golden eval set and of the eval runner (mock provider, no network)."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from app.config import Settings
from app.db.catalog import SchemaCatalog
from app.guardrails.input_guard import extract_sql
from app.sql.validator import validate_sql
from evals.compare import compare_results
from evals.golden import CATEGORIES, DEV_PATH, GOLDEN_PATH, GoldenCase, load_golden, parse_case, select_cases
from evals.report import load_report, render_console, write_reports
from evals.run_evals import build_settings, parse_args, run_suite
from evals.scoring import Scorer, execute_table

CASES = load_golden()
DEV_CASES = load_golden(DEV_PATH)
REFERENCE_CASES = [case for case in CASES + DEV_CASES if case.reference_sql]


def _ids(cases: list[GoldenCase]) -> list[str]:
    return [case.id for case in cases]


# ---- the golden file ------------------------------------------------------------------------------


def test_golden_file_is_well_formed() -> None:
    document = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    assert isinstance(document["version"], int)
    assert 30 <= len(CASES) <= 45
    assert len({case.id for case in CASES}) == len(CASES)
    counts = Counter(case.category for case in CASES)
    assert set(counts) == set(CATEGORIES)
    assert counts["generation"] >= 10 and counts["follow_up"] >= 4
    assert counts["optimize"] >= 3 and counts["debug"] >= 3 and counts["guardrail"] >= 9


def test_dev_split_is_well_formed_and_disjoint_from_the_golden_set() -> None:
    assert len(DEV_CASES) >= 10
    assert not {case.id for case in DEV_CASES} & {case.id for case in CASES}
    golden_questions = {turn.lower() for case in CASES for turn in case.turns}
    assert not [turn for case in DEV_CASES for turn in case.turns if turn.lower() in golden_questions and len(turn) > 25]


def test_generation_cases_cover_the_required_query_shapes() -> None:
    tags = {tag for case in CASES if case.category == "generation" for tag in case.tags}
    required = {"simple_filter", "date_filter", "aggregate", "group_by_having", "join_2", "join_3", "join_4",
                "top_n", "self_join", "subquery", "case_when", "ratio"}
    assert required <= tags


def test_guardrail_cases_cover_every_refusal_kind() -> None:
    guardrails = [case for case in CASES if case.category == "guardrail"]
    tags = Counter(tag for case in guardrails for tag in case.tags)
    assert tags["destructive_nl"] >= 2 and tags["destructive_sql"] >= 1
    assert tags["prompt_injection"] >= 2 and tags["ambiguous"] >= 1
    assert {"sports", "politics", "maths"} <= set(tags)
    statuses = {case.id: case.expected_status for case in guardrails}
    assert all(statuses[case.id] == "refused" for case in guardrails if {"destructive_nl", "destructive_sql"} & set(case.tags))
    assert all(statuses[case.id] == "rejected" for case in guardrails if "out_of_scope" in case.tags)


def test_follow_up_cases_have_several_turns_and_others_one() -> None:
    for case in CASES:
        assert (len(case.turns) >= 2) == (case.category == "follow_up"), case.id


def test_ordered_cases_have_an_order_by() -> None:
    for case in CASES:
        if case.ordered:
            assert "ORDER BY" in str(case.reference_sql).upper(), case.id


@pytest.mark.parametrize("case", REFERENCE_CASES, ids=_ids(REFERENCE_CASES))
def test_reference_sql_validates_and_returns_rows(case: GoldenCase, catalog: SchemaCatalog, db_path: Path) -> None:
    report = validate_sql(str(case.reference_sql), catalog, db_path=db_path)
    assert report.valid, report.errors
    table, error = execute_table(str(case.reference_sql), db_path)
    assert error is None and table is not None
    assert table.rows, "a reference query must return rows, or execution accuracy is meaningless"


@pytest.mark.parametrize("case", [c for c in CASES if c.category == "optimize"],
                         ids=_ids([c for c in CASES if c.category == "optimize"]))
def test_optimize_cases_paste_their_original_sql(case: GoldenCase) -> None:
    assert extract_sql(case.question) == case.original_sql


@pytest.mark.parametrize("case", [c for c in CASES if c.category == "debug"],
                         ids=_ids([c for c in CASES if c.category == "debug"]))
def test_debug_cases_contain_sql_that_is_really_broken(case: GoldenCase, db_path: Path) -> None:
    buggy = extract_sql(case.question)
    assert buggy
    table, _ = execute_table(buggy, db_path)
    expected, _ = execute_table(str(case.gold_sql), db_path)
    assert expected is not None
    assert table is None or not compare_results(expected, table).match


def test_parse_case_rejects_malformed_cases() -> None:
    base: dict[str, Any] = {"id": "x", "category": "generation", "question": "Show all customers.",
                            "gold_sql": "SELECT * FROM Customers"}
    assert parse_case(base).turns == ("Show all customers.",)
    for broken in (
        {**base, "category": "nope"},
        {key: value for key, value in base.items() if key != "gold_sql"},
        {**base, "turns": ["a", "b"]},
        {**base, "expected_status": "refused"},
        {**base, "typo_key": 1},
        {**base, "category": "follow_up"},
        {"id": "g", "category": "guardrail", "question": "Drop it", "expected_status": "success"},
    ):
        with pytest.raises(ValueError):
            parse_case(broken)


def test_select_cases_filters_by_category_id_and_limit() -> None:
    assert {case.category for case in select_cases(CASES, categories={"debug"})} == {"debug"}
    assert _ids(select_cases(CASES, ids={"grd_ambiguous"})) == ["grd_ambiguous"]
    assert len(select_cases(CASES, limit=3)) == 3


# ---- scoring --------------------------------------------------------------------------------------


def _response(**overrides: Any) -> dict[str, Any]:
    response: dict[str, Any] = {"status": "success", "intent": "generate_sql", "message": "ok", "sql": None,
                                "sql_sqlite": None, "issues": [], "changes": [], "suggestions": [],
                                "optimization": None, "clarification_question": None}
    response.update(overrides)
    return response


def test_scorer_uses_execution_accuracy(db_path: Path) -> None:
    case = next(case for case in CASES if case.id == "gen_brief_hired_after_jan_2024")
    scorer = Scorer(db_path)
    reordered = "SELECT HireDate, EmployeeID, FirstName, LastName, Email, JobTitle, DepartmentID, ManagerID, Salary, " \
                "IsActive FROM Employees WHERE HireDate > '2023-12-31' ORDER BY HireDate"
    assert scorer.score(case, _response(sql_sqlite=reordered)).passed
    wrong = "SELECT * FROM Employees WHERE HireDate >= '2025-01-01'"
    assert not scorer.score(case, _response(sql_sqlite=wrong)).passed
    assert not scorer.score(case, _response(status="failed", message="no")).passed
    assert not scorer.score(case, None, "llm_error: down").passed


def test_scorer_checks_guardrail_status_and_message(db_path: Path) -> None:
    case = next(case for case in CASES if case.id == "grd_out_of_scope_sports")
    scorer = Scorer(db_path)
    assert scorer.score(case, _response(status="rejected", message=case.expected_message)).passed
    assert not scorer.score(case, _response(status="rejected", message="Nope.")).passed
    assert not scorer.score(case, _response(status="success", sql="SELECT 1", sql_sqlite="SELECT 1")).passed


def test_scorer_requires_equivalence_and_suggestions_for_optimize(db_path: Path) -> None:
    case = next(case for case in CASES if case.id == "opt_correlated_subquery")
    scorer = Scorer(db_path)
    left_join = ("SELECT c.CustomerID, c.FirstName, c.LastName, COUNT(o.OrderID) AS OrderCount FROM Customers AS c "
                 "LEFT JOIN Orders AS o ON o.CustomerID = c.CustomerID GROUP BY c.CustomerID, c.FirstName, c.LastName")
    inner_join = left_join.replace("LEFT JOIN", "JOIN")
    assert scorer.score(case, _response(sql_sqlite=left_join, changes=["JOIN + GROUP BY"])).passed
    assert not scorer.score(case, _response(sql_sqlite=inner_join, changes=["JOIN + GROUP BY"])).passed
    assert not scorer.score(case, _response(sql_sqlite=left_join)).passed  # no changes or suggestions


# ---- runner (mock provider) -----------------------------------------------------------------------


@pytest.fixture(scope="module")
def mock_report(db_path: Path) -> dict[str, Any]:
    import asyncio

    settings = Settings(_env_file=None, llm_provider="mock", database_path=str(db_path), log_level="WARNING")
    return asyncio.run(run_suite(CASES, settings, db_path=db_path, warm_up=False))


def test_runner_runs_every_case_end_to_end_with_the_mock_provider(mock_report: dict[str, Any]) -> None:
    meta, summary, cases = mock_report["meta"], mock_report["summary"], mock_report["cases"]
    assert meta["provider"] == "mock" and meta["database_unchanged"] is True
    assert meta["golden"]["cases_run"] == len(CASES)
    assert [case["id"] for case in cases] == _ids(CASES)
    assert set(summary["categories"]) == set(CATEGORIES)
    assert summary["agent_errors"] == 0
    for case in cases:
        assert isinstance(case["passed"], bool) and case["reason"]
        assert len(case["turns"]) == len(next(c for c in CASES if c.id == case["id"]).turns)
    json.dumps(mock_report)  # JSON-safe


def test_reports_are_written_and_can_be_reloaded(mock_report: dict[str, Any], tmp_path: Path) -> None:
    paths = write_reports(mock_report, tmp_path, names=("latest", "baseline"), baseline=mock_report)
    assert sorted(path.name for path in paths) == ["baseline.json", "baseline.md", "latest.json", "latest.md"]
    markdown = (tmp_path / "latest.md").read_text(encoding="utf-8")
    assert "## Headline metrics" in markdown and "## All cases" in markdown and "Baseline" in markdown
    reloaded = load_report(tmp_path / "latest.json")
    assert reloaded["summary"] == mock_report["summary"]
    assert "Execution accuracy" in render_console(reloaded, reloaded)


def test_cli_settings_apply_provider_and_model_overrides() -> None:
    settings = build_settings(parse_args(["--provider", "mock", "--model", "m1", "--fast-model", "m2"]))
    assert (settings.llm_provider, settings.llm_model, settings.fast_model) == ("mock", "m1", "m2")
    groq = build_settings(parse_args(["--provider", "groq"]))
    if groq.llm_model == "openai/gpt-oss-120b":  # unless LLM_MODEL is set in the environment
        assert groq.fast_model == "openai/gpt-oss-20b"
