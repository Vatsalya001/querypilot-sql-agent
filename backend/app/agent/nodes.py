"""Graph nodes of the SQL agent.

Every node is a small async method that reads the state and returns a partial update. LLM access
goes only through the injected :class:`~app.agent.llm.LLMGateway`; the deterministic tools (input
guard, schema retriever, validator, optimizer, executor, cost estimator) never depend on the LLM.
Routing decisions live in :mod:`app.agent.routing`.

Each node is wrapped by :func:`~app.agent.events.traced_node`, which streams ``step`` events; a node
can attach a short human-readable ``detail`` to its completed event via ``DETAIL_KEY``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage
from langgraph.config import get_config
from sqlglot.errors import SqlglotError

from app.agent.events import DETAIL_KEY, emit_token, traced_node
from app.agent.llm import LLMError, LLMGateway
from app.agent.prompts import (
    CANNOT_ANSWER_MESSAGE,
    DEFAULT_CLARIFICATION,
    EMPTY_MESSAGE,
    OUT_OF_SCOPE_MESSAGE,
    PROMPT_INJECTION_MESSAGE,
    STAR_EXPANDED_CHANGE,
    SUCCESS_MESSAGES,
    TOO_LONG_MESSAGE,
    SQLMode,
    SQLPromptInput,
    SystemPrompts,
    build_answer_messages,
    build_explain_messages,
    build_intent_messages,
    build_sql_messages,
    destructive_message,
    failure_message,
    missing_sql_message,
)
from app.agent.routing import SQL_INPUT_INTENTS, route_after_scope
from app.agent.state import AgentState
from app.config import Settings
from app.db.catalog import SchemaCatalog
from app.guardrails.input_guard import check_input, extract_sql
from app.retrieval.schema_retriever import retrieve_schema
from app.sql.cost import estimate_cost
from app.sql.dialects import format_sql, parse_statements, transpile
from app.sql.executor import UnsafeQueryError, execute_query
from app.sql.fences import fenced_blocks
from app.sql.optimizer import analyze_query, expand_select_star
from app.sql.validator import ValidationReport, strip_sql_fences, validate_sql

logger = logging.getLogger(__name__)

_MODES: dict[str, SQLMode] = {"modify_sql": "modify", "optimize_sql": "optimize", "debug_sql": "debug"}
_LIST_FIELDS = ("assumptions", "issues", "changes", "suggestions")
_NL_OPERATIONS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("DELETE", re.compile(r"\b(delete|remove|erase|purge|wipe)\b", re.IGNORECASE)),
    ("UPDATE", re.compile(r"\b(update|change|modify|set|raise|increase|decrease|rename)\b", re.IGNORECASE)),
    ("INSERT", re.compile(r"\b(insert|add)\b", re.IGNORECASE)),
    ("DROP", re.compile(r"\bdrop\b", re.IGNORECASE)),
    ("TRUNCATE", re.compile(r"\btruncate\b", re.IGNORECASE)),
    ("ALTER", re.compile(r"\balter\b", re.IGNORECASE)),
)
_FROM_KEYWORD = re.compile(r"\bF(?:ROM|ORM|RM)\b", re.IGNORECASE)  # with the typos people paste
_SQL_PUNCTUATION = re.compile(r"[*,=()<>;'\"`]|\w\.\w")
_UPPER_KEYWORD = re.compile(r"\b(?:SELECT|WITH)\b")  # case-sensitive: typed as code
_MENTIONS_INDEX = re.compile(r"\bindex(es|ed|ing)?\b|\bindices\b", re.IGNORECASE)
_MENTIONS_STAR = re.compile(r"SELECT\s+\*", re.IGNORECASE)
_INDEX_TARGET = re.compile(r"\bON\s+[\"`\[]?(\w+)[\"`\]]?\s*\(([^)]*)\)", re.IGNORECASE)
_SCOPE_VERDICTS = {
    "reject_request": "out of scope",
    "refuse_destructive": "destructive request",
    "ask_clarification": "needs clarification",
    "answer_question": "in scope (question)",
    "retrieve_schema": "in scope",
}


@dataclass(frozen=True)
class AgentDeps:
    """Everything the nodes need, injected once when the graph is built."""

    settings: Settings
    gateway: LLMGateway
    catalog: SchemaCatalog
    db_path: Path
    prompts: SystemPrompts


def conversation_history(state: AgentState) -> list[AnyMessage]:
    """Earlier messages of the thread, excluding the current user message (rendered separately)."""
    messages = list(state.get("messages") or [])
    if messages and isinstance(messages[-1], HumanMessage):
        messages = messages[:-1]
    return messages


def clarified_request(history: Sequence[AnyMessage]) -> str | None:
    """The user's previous message when the assistant answered it with a clarification question.

    The current message then completes that request ("Show the best customers" -> "by number of orders").
    A request for missing SQL ("Please paste the SQL query ...") is not such a question.
    """
    if len(history) < 2 or not isinstance(history[-1], AIMessage) or not isinstance(history[-2], HumanMessage):
        return None
    reply = history[-1].additional_kwargs
    if reply.get("status") != "clarification" or reply.get("intent") in SQL_INPUT_INTENTS:
        return None
    content = history[-2].content
    return content if isinstance(content, str) else str(content)


def pasted_sql(text: str) -> str | None:
    """SQL pasted into a message, or ``None``.

    :func:`extract_sql` takes everything from the word "select" onward, so English such as "explain the
    select statement you wrote" would come back as SQL and hide the conversation's query. Fenced code is
    always SQL. Otherwise the text must look like code: a FROM clause written with an upper-case keyword
    or SQL punctuation (it may be broken: it is pasted for debugging), or a statement that parses and has
    a FROM clause or SQL punctuation.
    """
    sql = extract_sql(text)
    if not sql or any(fenced_blocks(text)):
        return sql
    has_from, has_punctuation = bool(_FROM_KEYWORD.search(sql)), bool(_SQL_PUNCTUATION.search(sql))
    if has_from and (has_punctuation or _UPPER_KEYWORD.search(sql)):
        return sql
    if not (has_from or has_punctuation):
        return None
    try:
        return sql if parse_statements(sql, "sqlite") else None
    except SqlglotError:
        return None


def infer_operations(text: str) -> list[str]:
    """Best-effort names of the write operations a natural-language request asks for."""
    return [name for name, pattern in _NL_OPERATIONS if pattern.search(text)]


def _index_target(statement: str) -> tuple[str, tuple[str, ...]] | None:
    """``(table, columns)`` of a ``CREATE INDEX ... ON Table (col, ...)`` statement, lower-cased."""
    match = _INDEX_TARGET.search(statement)
    if not match:
        return None
    columns = tuple(part.split()[0].strip('"`[]').lower() for part in match.group(2).split(",") if part.strip())
    return match.group(1).lower(), columns


def grounded_suggestions(suggestions: Sequence[str], index_recommendations: Sequence[str]) -> list[str]:
    """The LLM's suggestions without invented indexes.

    Index advice is kept only as a ``CREATE INDEX`` statement on the same table and columns as one the
    deterministic optimizer recommended (it is then replaced by the optimizer's exact statement). Any
    other index advice, such as an index that already exists, is dropped; the optimizer's own
    recommendations are always shown in the optimization report. Other advice passes through unchanged.
    """
    known = {target: statement for statement in index_recommendations if (target := _index_target(statement))}
    kept: list[str] = []
    for item in suggestions:
        if not _MENTIONS_INDEX.search(item):
            kept.append(item)
            continue
        target = _index_target(item)
        if target in known:
            kept.append(known[target])
        else:
            logger.info("Dropped an index suggestion not backed by static analysis: %s", item[:200])
    return list(dict.fromkeys(kept))


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _merge_generation(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    """Keep list fields (issues, changes, ...) from an earlier attempt when a repair attempt drops them."""
    if not previous:
        return current
    merged = dict(current)
    for name in _LIST_FIELDS:
        if not merged.get(name) and previous.get(name):
            merged[name] = previous[name]
    return merged


def _syntax_passed(report: ValidationReport) -> bool:
    return any(check.name == "syntax" and check.passed for check in report.checks)


class AgentNodes:
    """The 16 graph nodes, bound to their dependencies."""

    def __init__(self, deps: AgentDeps) -> None:
        self.deps = deps

    # ---- entry and classification -------------------------------------------------------------

    @traced_node("guard_input")
    async def guard_input(self, state: AgentState) -> dict[str, Any]:
        """Deterministic input checks: empty/too long, prompt-injection patterns, destructive SQL."""
        result = check_input(state.get("user_input", ""), max_chars=self.deps.settings.max_input_chars)
        detail = "passed"
        if not result.allowed:
            matched = f" ({', '.join(result.matched[:3])})" if result.matched else ""
            detail = f"blocked: {result.category}{matched}"
        return {
            "guard": result.to_dict(),
            "prompt_injection": result.category == "prompt_injection",
            DETAIL_KEY: detail,
        }

    @traced_node("detect_intent")
    async def detect_intent(self, state: AgentState) -> dict[str, Any]:
        """LLM intent classification with conversation context."""
        messages = build_intent_messages(
            self.deps.prompts.intent,
            user_input=state.get("user_input", ""),
            history=conversation_history(state),
            previous_sql=state.get("last_sql"),
            window=self.deps.settings.history_window,
        )
        decision = await self.deps.gateway.classify_intent(messages)
        return {
            "intent": decision.intent,
            "intent_confidence": decision.confidence,
            "user_sql": decision.user_sql,
            "prompt_injection": decision.prompt_injection,
            "clarification_question": decision.clarification_question,
            DETAIL_KEY: f"{decision.intent} ({decision.confidence:.2f})",
        }

    @traced_node("validate_scope")
    async def validate_scope(self, state: AgentState) -> dict[str, Any]:
        """Normalize the intent against the conversation and locate the SQL that SQL-input intents need."""
        intent = state.get("intent") or "ambiguous"
        last_sql = state.get("last_sql")
        if intent == "modify_sql" and not last_sql:
            intent = "generate_sql"
        user_sql: str | None = None
        clarification = state.get("clarification_question")
        if intent in SQL_INPUT_INTENTS:
            # Prefer the verbatim SQL from the message; then the classifier's copy; then the
            # conversation's current query ("explain it", "make that faster").
            user_sql = pasted_sql(state.get("user_input", "")) or strip_sql_fences(state.get("user_sql") or "")
            user_sql = user_sql or last_sql
            if not user_sql:
                clarification = missing_sql_message(intent)
        update: dict[str, Any] = {"intent": intent, "user_sql": user_sql or None}
        route = route_after_scope({**state, **update})  # type: ignore[typeddict-item]
        if route == "ask_clarification":
            update["clarification_question"] = clarification or DEFAULT_CLARIFICATION
        verdict = _SCOPE_VERDICTS[route]
        if route == "reject_request" and state.get("prompt_injection"):
            verdict = "prompt injection"
        update[DETAIL_KEY] = verdict
        return update

    # ---- SQL pipeline ---------------------------------------------------------------------------

    @traced_node("retrieve_schema")
    async def retrieve_schema(self, state: AgentState) -> dict[str, Any]:
        """Lexical retrieval of the most relevant tables (plus FK bridge tables)."""
        question = state.get("user_input", "")
        clarified = clarified_request(conversation_history(state))
        if clarified:  # the message answers a clarification question: retrieve for the whole request
            question = f"{clarified}\n{question}"
        context = retrieve_schema(
            question,
            self.deps.catalog,
            previous_sql=state.get("last_sql") if state.get("intent") == "modify_sql" else None,
            user_sql=state.get("user_sql"),
        )
        return {
            "schema_context": {**context.to_dict(), "focus_hint": context.focus_hint()},
            DETAIL_KEY: ", ".join(context.tables) or "all tables",
        }

    @traced_node("diagnose_sql")
    async def diagnose_sql(self, state: AgentState) -> dict[str, Any]:
        """Validate and statically analyze the SQL the user supplied."""
        user_sql = state.get("user_sql") or ""
        # Pasted SQL may be written in the output dialect, but the conversation's query (last_sql) is always
        # SQLite: re-parsing it as MySQL would turn "a || b" into "a OR b" and drop date() modifiers.
        dialect = "sqlite" if user_sql == state.get("last_sql") else (state.get("dialect") or "sqlite")
        report = await asyncio.to_thread(self._validate_user_sql, user_sql, dialect)
        analysis = analyze_query(report.sql or user_sql, self.deps.catalog)
        validation = report.to_dict()
        update: dict[str, Any] = {"diagnosis": {"validation": validation, "optimization": analysis.to_dict()}}
        if state.get("intent") == "explain_sql":
            if report.valid:
                update.update(candidate_sql=report.sql, validation=validation)
            else:
                update["intent"] = "debug_sql"  # cannot explain broken SQL: fix it first
        update[DETAIL_KEY] = "valid" if report.valid else _plural(len(report.errors), "error")
        return update

    @traced_node("generate_sql")
    async def generate_sql(self, state: AgentState) -> dict[str, Any]:
        """Ask the LLM for SQL in the mode matching the intent, with repair feedback on retries."""
        settings = self.deps.settings
        intent = state.get("intent") or "generate_sql"
        mode = _MODES.get(intent, "generate")
        attempts = (state.get("attempts") or 0) + 1
        previous = state.get("validation") if attempts > 1 else None
        repair_errors: tuple[str, ...] = ()
        if previous and not previous.get("valid"):
            repair_errors = tuple(previous.get("errors") or ()) or ("The query failed validation.",)
        context = state.get("schema_context") or {}
        request = SQLPromptInput(
            mode=mode,
            user_input=state.get("user_input", ""),
            focus_hint=context.get("focus_hint", ""),
            schema_text=None if self.deps.prompts.sql_embeds_schema else context.get("text"),
            history=conversation_history(state),
            previous_sql=state.get("last_sql") if mode == "modify" else None,
            user_sql=state.get("user_sql") if mode in ("optimize", "debug") else None,
            findings=self._findings(mode, state.get("diagnosis") or {}),
            repair_sql=state.get("candidate_sql") if repair_errors else None,
            repair_errors=repair_errors,
            clarified_request=clarified_request(conversation_history(state)) if mode == "generate" else None,
        )
        messages = build_sql_messages(self.deps.prompts.sql, request, window=settings.history_window)
        generation = await self.deps.gateway.generate_sql(messages)
        sql = strip_sql_fences(generation.sql or "") or None
        produced = generation.model_dump()
        if mode == "optimize" and sql:
            # Writing out every column is slow for the model and error-prone; the schema does it exactly.
            expanded = expand_select_star(sql, self.deps.catalog)
            if expanded:
                sql = expanded
                notes = [note for note in produced.get("changes", []) if not _MENTIONS_STAR.search(note)]
                produced["changes"] = [*notes, STAR_EXPANDED_CHANGE]
        update: dict[str, Any] = {
            "attempts": attempts,
            "generation": _merge_generation(state.get("generation"), produced),
            "candidate_sql": sql,
            DETAIL_KEY: f"attempt {attempts}: {generation.status}",
        }
        if generation.status == "needs_clarification":
            update["clarification_question"] = generation.clarification_question or DEFAULT_CLARIFICATION
        return update

    @traced_node("validate_sql")
    async def validate_sql(self, state: AgentState) -> dict[str, Any]:
        """Seven deterministic checks, including a dry-run against the read-only database."""
        report = await asyncio.to_thread(
            validate_sql, state.get("candidate_sql") or "", self.deps.catalog, dialect="sqlite",
            db_path=self.deps.db_path,
        )
        update: dict[str, Any] = {"validation": report.to_dict()}
        if report.valid:
            update["candidate_sql"] = report.sql
            update[DETAIL_KEY] = "valid"
        else:
            retrying = (state.get("attempts") or 0) < self.deps.settings.max_sql_attempts
            outcome = "repairing" if retrying else "giving up"
            update[DETAIL_KEY] = f"{_plural(len(report.errors), 'error')} -> {outcome}"
        return update

    @traced_node("optimize_sql")
    async def optimize_sql(self, state: AgentState) -> dict[str, Any]:
        """Rule-based optimization report for the final, validated SQL (marks the turn successful)."""
        report = analyze_query(state.get("candidate_sql") or "", self.deps.catalog)
        data = report.to_dict()
        detail = (
            f"{_plural(len(data.get('suggestions') or []), 'suggestion')}, "
            f"{_plural(len(data.get('index_recommendations') or []), 'index recommendation')}"
        )
        return {"optimization": data, "status": "success", DETAIL_KEY: detail}

    @traced_node("execute_sql")
    async def execute_sql(self, state: AgentState) -> dict[str, Any]:
        """Run the validated query read-only (when requested) and always estimate its cost."""
        settings = self.deps.settings
        sql = state.get("candidate_sql") or ""
        execution: dict[str, Any] | None = None
        if state.get("execute", True):
            try:
                result = await asyncio.to_thread(
                    execute_query, sql, self.deps.db_path, max_rows=settings.max_result_rows,
                    timeout_s=settings.query_timeout_s,
                )
                execution = result.to_dict()
            except UnsafeQueryError as exc:  # defense in depth: validated SQL should never get here
                logger.warning("Executor refused validated SQL: %s", exc)
                execution = {"columns": [], "rows": [], "row_count": 0, "truncated": False, "elapsed_ms": 0.0,
                             "error": "Only read-only queries can be executed."}
        estimate = await asyncio.to_thread(estimate_cost, sql, self.deps.db_path, self.deps.catalog)
        cost = estimate.to_dict() if estimate is not None else None
        if execution is None:
            detail = "not executed"
        elif execution.get("error"):
            detail = f"error: {str(execution['error'])[:80]}"
        else:
            detail = f"{_plural(int(execution.get('row_count') or 0), 'row')} in {execution.get('elapsed_ms', 0):g} ms"
        if cost:
            detail += f"; cost {cost.get('level')}"
        return {"execution": execution, "cost": cost, DETAIL_KEY: detail}

    @traced_node("explain_sql")
    async def explain_sql(self, state: AgentState) -> dict[str, Any]:
        """Plain-English explanation, streamed token by token (template fallback if the LLM fails)."""
        intent = state.get("intent")
        execution = state.get("execution") or {}
        generation = state.get("generation") or {}
        fixes = [issue.get("problem", "") for issue in generation.get("issues") or []] or generation.get("changes") or []
        messages = build_explain_messages(
            self.deps.prompts.explain,
            sql=state.get("candidate_sql") or "",
            user_input=state.get("user_input", ""),
            intent=intent,
            row_count=execution.get("row_count") if execution and not execution.get("error") else None,
            truncated=bool(execution.get("truncated")),
            execution_error=execution.get("error"),
            fixes=[fix for fix in fixes if fix],
        )
        try:
            text = await self.deps.gateway.stream_text(
                messages, on_token=lambda chunk: emit_token("explain_sql", "explanation", chunk)
            )
        except LLMError as exc:
            logger.warning("Explanation failed (%s); using the template explanation", exc.code)
            text = ""
        explanation = text.strip() or self._template_explanation(state)
        return {"explanation": explanation, DETAIL_KEY: _plural(len(explanation.split()), "word")}

    # ---- terminal branches ----------------------------------------------------------------------

    @traced_node("answer_question")
    async def answer_question(self, state: AgentState) -> dict[str, Any]:
        """Prose answer for schema questions and SQL-concept questions, streamed as ``message`` tokens."""
        messages = build_answer_messages(
            self.deps.prompts.answer,
            user_input=state.get("user_input", ""),
            history=conversation_history(state),
            window=self.deps.settings.history_window,
        )
        text = await self.deps.gateway.stream_text(
            messages, on_token=lambda chunk: emit_token("answer_question", "message", chunk)
        )
        answer = text.strip() or "I couldn't find an answer to that. Try asking about a specific table or column."
        return {"status": "answered", "response_message": answer, DETAIL_KEY: _plural(len(answer.split()), "word")}

    @traced_node("ask_clarification")
    async def ask_clarification(self, state: AgentState) -> dict[str, Any]:
        """Ask the user for the missing detail."""
        question = state.get("clarification_question") or DEFAULT_CLARIFICATION
        return {
            "status": "clarification",
            "clarification_question": question,
            "response_message": question,
            DETAIL_KEY: "asked the user",
        }

    @traced_node("reject_request")
    async def reject_request(self, state: AgentState) -> dict[str, Any]:
        """Out-of-scope, prompt-injection, empty or oversized input."""
        category = (state.get("guard") or {}).get("category")
        if category == "empty":
            message, detail = EMPTY_MESSAGE, "empty input"
        elif category == "too_long":
            message = TOO_LONG_MESSAGE.format(max_chars=self.deps.settings.max_input_chars)
            detail = "input too long"
        elif category == "prompt_injection" or state.get("prompt_injection"):
            message, detail = PROMPT_INJECTION_MESSAGE, "prompt injection"
        else:
            message, detail = OUT_OF_SCOPE_MESSAGE, "out of scope"
        return {"status": "rejected", "response_message": message, DETAIL_KEY: detail}

    @traced_node("refuse_destructive")
    async def refuse_destructive(self, state: AgentState) -> dict[str, Any]:
        """Polite refusal of write/DDL requests, offering a read-only alternative."""
        guard = state.get("guard") or {}
        operations: Sequence[str] = []
        if guard.get("category") == "destructive_sql":
            operations = guard.get("matched") or []
        if not operations:
            operations = infer_operations(state.get("user_sql") or state.get("user_input", ""))
        return {
            "status": "refused",
            "response_message": destructive_message(operations),
            DETAIL_KEY: ", ".join(operations) or "write request",
        }

    @traced_node("handle_failure")
    async def handle_failure(self, state: AgentState) -> dict[str, Any]:
        """No valid SQL: either the data does not exist or the repair loop was exhausted."""
        generation = state.get("generation") or {}
        if generation.get("status") == "cannot_answer":
            message = (generation.get("summary") or "").strip() or CANNOT_ANSWER_MESSAGE
            return {"status": "failed", "response_message": message, "error": "cannot_answer",
                    DETAIL_KEY: "data not in schema"}
        errors = (state.get("validation") or {}).get("errors") or []
        return {
            "status": "failed",
            "response_message": failure_message(errors),
            "error": "invalid_sql",
            DETAIL_KEY: f"no valid SQL after {_plural(state.get('attempts') or 0, 'attempt')}",
        }

    @traced_node("respond")
    async def respond(self, state: AgentState) -> dict[str, Any]:
        """Assemble the AgentResponse (docs/API.md) and record the turn in conversation memory."""
        status = state.get("status") or "failed"
        intent = state.get("intent")
        requested = state.get("dialect") or "sqlite"
        validation = state.get("validation")
        if validation is None and status == "refused":  # pasted destructive SQL: show why it was blocked
            validation = (state.get("diagnosis") or {}).get("validation")
        optimization = state.get("optimization")
        generation = state.get("generation") or {}
        success = status == "success"

        normalized = state.get("candidate_sql") if success else None
        sql_sqlite = sql_out = None
        dialect = requested
        if normalized:
            sql_sqlite = (optimization or {}).get("formatted_sql") or format_sql(normalized, "sqlite")
            sql_out, dialect, warning = self._to_dialect(sql_sqlite, requested)
            if warning and validation is not None:
                validation = {**validation, "warnings": [*(validation.get("warnings") or []), warning]}

        message = self._success_message(state) if success else (state.get("response_message") or "")
        thread_id, message_id = self._run_ids()
        final = {
            "thread_id": thread_id,
            "message_id": message_id,
            "intent": intent,
            "status": status,
            "message": message,
            "sql": sql_out,
            "sql_sqlite": sql_sqlite,
            "dialect": dialect,
            "explanation": state.get("explanation") if success else None,
            "validation": validation,
            "optimization": optimization if success else None,
            "execution": state.get("execution") if success else None,
            "cost": state.get("cost") if success else None,
            "issues": self._issues(state, generation),
            "changes": list(generation.get("changes") or []),
            "suggestions": grounded_suggestions(generation.get("suggestions") or [], self._known_indexes(state)),
            "assumptions": list(generation.get("assumptions") or []),
            "clarification_question": state.get("clarification_question") if status == "clarification" else None,
            "schema_tables": list((state.get("schema_context") or {}).get("tables") or []),
            "attempts": state.get("attempts") or 0,
            "steps": [],
            "model": self._model_used(state),
        }
        memory = AIMessage(
            content=f"{message}\nSQL: {normalized}" if normalized else message,
            additional_kwargs={"sql": normalized, "intent": intent, "status": status},
        )
        update: dict[str, Any] = {"final": final, "messages": [memory], DETAIL_KEY: status}
        if normalized:
            update["last_sql"] = normalized
        return update

    # ---- helpers --------------------------------------------------------------------------------

    def _validate_user_sql(self, sql: str, dialect: str) -> ValidationReport:
        """Validate pasted SQL in the selected dialect, falling back to SQLite syntax if it only parses there."""
        report = validate_sql(sql, self.deps.catalog, dialect=dialect, db_path=self.deps.db_path)
        if dialect != "sqlite" and not _syntax_passed(report):
            sqlite_report = validate_sql(sql, self.deps.catalog, dialect="sqlite", db_path=self.deps.db_path)
            if _syntax_passed(sqlite_report):
                return sqlite_report
        return report

    @staticmethod
    def _findings(mode: SQLMode, diagnosis: dict[str, Any]) -> tuple[str, ...]:
        """Deterministic findings about the user's SQL that the LLM should act on."""
        validation = diagnosis.get("validation") or {}
        errors = list(validation.get("errors") or [])
        warnings = list(validation.get("warnings") or [])
        if mode == "debug":
            return tuple(errors + warnings) or ("The validator found no errors; check the logic against the user's goal.",)
        if mode == "optimize":
            optimization = diagnosis.get("optimization") or {}
            suggestions = [item.get("message", "") for item in optimization.get("suggestions") or []]
            joins = [f"Unnecessary join: {item}" for item in optimization.get("unnecessary_joins") or []]
            indexes = list(optimization.get("index_recommendations") or [])
            return tuple(item for item in errors + suggestions + joins + indexes if item)
        return ()

    @staticmethod
    def _to_dialect(sql: str, dialect: str) -> tuple[str, str, str | None]:
        """Transpile SQLite SQL to the requested dialect; keep SQLite (with a warning) if that fails."""
        if dialect == "sqlite":
            return sql, "sqlite", None
        try:
            return transpile(sql, read="sqlite", write=dialect, pretty=True), dialect, None
        except ValueError as exc:
            logger.warning("Transpiling to %s failed: %s", dialect, exc)
            return sql, "sqlite", f"Could not convert the query to {dialect}; showing SQLite syntax instead."

    def _success_message(self, state: AgentState) -> str:
        intent = state.get("intent") or "generate_sql"
        summary = "" if intent == "explain_sql" else ((state.get("generation") or {}).get("summary") or "").strip()
        message = summary or SUCCESS_MESSAGES.get(intent, SUCCESS_MESSAGES["generate_sql"])
        execution = state.get("execution")
        if execution:
            if execution.get("error"):
                message += f" The query is valid but failed when executed: {execution['error']}"
            elif execution.get("truncated"):
                message += f" Showing the first {_plural(int(execution.get('row_count') or 0), 'row')} (more exist)."
            else:
                message += f" It returned {_plural(int(execution.get('row_count') or 0), 'row')}."
        return message

    @staticmethod
    def _known_indexes(state: AgentState) -> list[str]:
        """Index recommendations of the static analysis, for the user's SQL and for the final SQL."""
        diagnosis = (state.get("diagnosis") or {}).get("optimization") or {}
        final = state.get("optimization") or {}
        return [*(diagnosis.get("index_recommendations") or []), *(final.get("index_recommendations") or [])]

    @staticmethod
    def _issues(state: AgentState, generation: dict[str, Any]) -> list[dict[str, str]]:
        """Debug issues from the LLM; falls back to the validator's errors if the model listed none."""
        issues = [dict(issue) for issue in generation.get("issues") or []]
        if not issues and state.get("intent") == "debug_sql" and state.get("status") == "success":
            errors = ((state.get("diagnosis") or {}).get("validation") or {}).get("errors") or []
            issues = [{"problem": error, "reason": "Reported by the SQL validator."} for error in errors]
        return issues

    def _template_explanation(self, state: AgentState) -> str:
        """Deterministic explanation used when the LLM explanation is unavailable."""
        tables = list((state.get("validation") or {}).get("tables") or [])
        source = f"the {', '.join(tables)} table{'s' if len(tables) > 1 else ''}" if tables else "the database"
        text = f"This query reads data from {source}."
        execution = state.get("execution") or {}
        if execution and not execution.get("error"):
            text += f" It returned {_plural(int(execution.get('row_count') or 0), 'row')}."
        return text

    def _model_used(self, state: AgentState) -> str:
        gateway = self.deps.gateway
        if state.get("attempts"):
            return gateway.model
        return gateway.fast_model if state.get("intent") else gateway.model

    @staticmethod
    def _run_ids() -> tuple[str | None, str | None]:
        """Thread and message ids from the run config (``None`` outside a configured run)."""
        try:
            configurable = get_config().get("configurable", {})
        except RuntimeError:
            return None, None
        return configurable.get("thread_id"), configurable.get("message_id")
