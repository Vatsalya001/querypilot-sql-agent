"""Export every LLM prompt of the agent to docs/PROMPTS.md (deterministic and offline).

* Static text is rendered from ``app.agent.prompts``: the four system prompts (built from the sample
  database's schema catalog exactly as at startup), the SQL mode blocks, the fixed user-facing messages
  and the tuning constants. Module constants are discovered by name, so new ones appear automatically.
* The tools the model fills in come from ``app.agent.schemas``.
* Dynamic messages are captured from real runs of the LangGraph agent: a recording gateway wraps the
  offline mock LLM and stores the exact messages each node sends.

No model, network or API key is needed. Settings are the built-in defaults (``.env`` files and environment
variables are ignored), so re-running the script on unchanged code reproduces the file byte for byte.

Usage (from backend/): .venv/bin/python scripts/export_prompts.py [--output PATH] [--check]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import BaseMessage
from langchain_core.utils.function_calling import convert_to_openai_tool

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent import llm, prompts
from app.agent.mock_llm import MockGateway
from app.agent.parsing import json_instructions
from app.agent.schemas import INTENTS, IntentDecision, SQLGeneration
from app.agent.service import ChatInput, SQLAgent
from app.config import Settings
from app.db.catalog import SchemaCatalog, load_catalog
from app.db.database import DEFAULT_DB_PATH, ensure_database

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = BACKEND_DIR.parent / "docs" / "PROMPTS.md"

ROLES: tuple[tuple[str, str, str], ...] = (
    ("intent", "Intent classifier", "detect_intent"),
    ("sql", "SQL generator", "generate_sql"),
    ("explain", "Explainer", "explain_sql"),
    ("answer", "Answerer", "answer_question"),
)
"""(SystemPrompts field, role title, graph node) for every LLM role."""

MESSAGE_USES: dict[str, str] = {
    "OUT_OF_SCOPE_MESSAGE": "`reject_request`: out-of-scope questions (the brief's exact wording)",
    "PROMPT_INJECTION_MESSAGE": "`reject_request`: prompt-injection attempts (guard or classifier)",
    "EMPTY_MESSAGE": "`reject_request`: empty input",
    "TOO_LONG_MESSAGE": "`reject_request`: input longer than `MAX_INPUT_CHARS`",
    "DEFAULT_CLARIFICATION": "`ask_clarification`: when the model gave no question of its own",
    "MISSING_SQL_MESSAGE": "`validate_scope`: optimize/debug/explain requested without any SQL",
    "CANNOT_ANSWER_MESSAGE": "`handle_failure`: data not in the schema and no summary from the model",
    "REJECTED_HISTORY_PLACEHOLDER": "history rendering: replaces the user text of a rejected turn",
    "STAR_EXPANDED_CHANGE": "`generate_sql` (optimize): change note after `SELECT *` is expanded",
}
"""Where each fixed text is used (constants missing here are still listed, without a description)."""

TUNING_NOTES: dict[str, str] = {
    "FULL_SCHEMA_MAX_TABLES": "Up to this many tables, the whole schema is embedded in the cached SQL system prompt",
    "HISTORY_USER_CHARS": "Each earlier user message is clipped to this many characters in prompts",
    "HISTORY_ASSISTANT_CHARS": "Each earlier assistant reply is clipped to this many characters",
    "HISTORY_SQL_CHARS": "Each earlier query (and the previous query) is clipped to this many characters",
    "INTENT_MAX_TOKENS": "Output cap of the intent call on a local model",
    "TEXT_MAX_TOKENS": "Output cap of explanations and prose answers on a local model",
}

MESSAGE_EXAMPLES: dict[str, tuple[tuple[Any, ...], ...]] = {
    "destructive_message": ((["DELETE"],), (["UPDATE", "DROP"],), ([],)),
    "failure_message": ((["Unknown column 'Region' in table Customers. Did you mean 'State'?"],),),
    "missing_sql_message": (("optimize_sql",), ("explain_sql",)),
}
"""Example arguments for the public ``*_message`` builders of ``app.agent.prompts``."""


@dataclass(frozen=True)
class Scenario:
    """A conversation run through the real graph; the prompts of its last turn are documented."""

    title: str
    note: str
    turns: tuple[str, ...]
    scripted_sql: tuple[str, ...] = ()
    """SQL answers returned before the mock answers (stands in for a model mistake)."""


SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        "Generate SQL (the brief's example)",
        "A first message on a new thread: no history, no previous query.",
        ("Show all employees hired after January 2024.",),
    ),
    Scenario(
        "Follow-up that modifies the previous query (MODIFY)",
        "The second turn of a conversation. The history and the previous query come from the thread's memory.",
        ("Show all customers.", "Only those from California."),
    ),
    Scenario(
        "Optimize pasted SQL (OPTIMIZE)",
        "The findings come from the deterministic optimizer run on the user's SQL by `diagnose_sql`.",
        (
            "Optimize this query: SELECT * FROM Orders o JOIN Customers c ON o.CustomerID = c.CustomerID "
            "WHERE strftime('%Y', o.OrderDate) = '2025' ORDER BY o.OrderDate",
        ),
    ),
    Scenario(
        "Debug pasted SQL (DEBUG)",
        "The findings are the validator's errors for the user's SQL (`diagnose_sql`).",
        (
            "This query fails, please fix it: SELECT FirstName, LastName FROM Employee WHERE HireDate > 2024-01-01",
        ),
    ),
    Scenario(
        "Self-correction after failed validation (REPAIR)",
        "The first answer is scripted to use a column that does not exist (`Region`), as a model might. "
        "`validate_sql` rejects it and the second `generate_sql` call carries the REPAIR block.",
        ("List the names and region of customers in California.",),
        scripted_sql=("SELECT FirstName, LastName, Region FROM Customers WHERE Region = 'California'",),
    ),
    Scenario(
        "Explain pasted SQL",
        "The SQL is valid, so `diagnose_sql` routes straight to optimization and execution: no `generate_sql` call.",
        ("What does this query do? SELECT Status, COUNT(*) AS OrderCount FROM Orders GROUP BY Status",),
    ),
    Scenario(
        "Schema question (prose answer)",
        "Schema and SQL-concept questions are answered in prose by `answer_question`, without generating SQL.",
        ("How are payments linked to orders?",),
    ),
)


@dataclass
class LLMCall:
    """One captured model call: the gateway method and the exact messages sent."""

    method: str
    messages: list[BaseMessage]


class RecordingGateway:
    """LLM gateway that records every prompt and lets the offline mock answer it."""

    provider = "mock"
    model = "mock"
    fast_model = "mock"
    mock = True

    def __init__(self, scripted_sql: Sequence[str] = ()) -> None:
        self.calls: list[LLMCall] = []
        self._mock = MockGateway()
        self._scripted = [SQLGeneration(status="ok", sql=sql, summary="") for sql in scripted_sql]

    async def classify_intent(self, messages: list[BaseMessage]) -> IntentDecision:
        """Record the intent prompt and classify it with the mock."""
        self.calls.append(LLMCall("classify_intent", list(messages)))
        return await self._mock.classify_intent(messages)

    async def generate_sql(self, messages: list[BaseMessage]) -> SQLGeneration:
        """Record the SQL prompt; answer with the next scripted SQL, else with the mock."""
        self.calls.append(LLMCall("generate_sql", list(messages)))
        if self._scripted:
            return self._scripted.pop(0)
        return await self._mock.generate_sql(messages)

    async def stream_text(
        self, messages: list[BaseMessage], *, on_token: llm.TokenCallback | None = None
    ) -> str:
        """Record the explain/answer prompt and stream the mock's text."""
        self.calls.append(LLMCall("stream_text", list(messages)))
        return await self._mock.stream_text(messages, on_token=on_token)


@dataclass
class Capture:
    """The model calls of a scenario's last turn and the response it produced."""

    scenario: Scenario
    calls: list[LLMCall]
    final: dict[str, Any]


def docs_settings() -> Settings:
    """Built-in defaults with the offline mock LLM; ``.env`` files and environment variables are ignored."""
    values: dict[str, Any] = {name: info.default for name, info in Settings.model_fields.items()}
    values.update(llm_provider="mock", llm_warmup=False, database_path=str(DEFAULT_DB_PATH))
    return Settings(_env_file=None, **values)


async def capture(scenario: Scenario, settings: Settings, catalog: SchemaCatalog) -> Capture:
    """Run ``scenario`` through a fresh agent and keep the model calls of its last turn."""
    gateway = RecordingGateway(scenario.scripted_sql)
    agent = SQLAgent(settings, gateway=gateway, catalog=catalog, db_path=DEFAULT_DB_PATH)
    final: dict[str, Any] = {}
    for turn in scenario.turns:
        gateway.calls.clear()
        final = await agent.run(ChatInput(message=turn, thread_id="prompts-doc"))
    return Capture(scenario, list(gateway.calls), final)


# ---- Markdown helpers -------------------------------------------------------------------------------


def fenced(text: str, language: str = "text") -> str:
    """A fenced code block whose fence is longer than any backtick run inside ``text``."""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{text.rstrip()}\n{fence}"


def anchor(title: str) -> str:
    """GitHub's anchor for a heading titled ``title``."""
    return re.sub(r"[^\w\- ]", "", title.strip().lower()).replace(" ", "-")


def cell(text: str) -> str:
    """``text`` made safe for one Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", "<br>")


def size(text: str) -> str:
    """Characters and lines of a prompt, e.g. ``4,788 characters, 52 lines``."""
    return f"{len(text):,} characters, {len(text.splitlines())} lines"


def module_constants(module: object, kind: type) -> dict[str, Any]:
    """Public UPPER_CASE module attributes of type ``kind``, in definition order."""
    return {
        name: value
        for name, value in vars(module).items()
        if name.isupper() and not name.startswith("_") and isinstance(value, kind)
    }


def numbered_rules(text: str) -> int:
    """How many numbered rules (``1. ...``) a prompt contains."""
    return len(re.findall(r"^\d+\. ", text, re.MULTILINE))


# ---- Sections ---------------------------------------------------------------------------------------

SECTION_TITLES = (
    "1. Design rationale",
    "2. LLM roles at a glance",
    "3. Static system prompts",
    "4. Structured output tools",
    "5. Dynamic messages",
    "6. SQL mode blocks",
    "7. JSON fallback",
    "8. Fixed user-facing messages",
    "9. Tuning constants",
)


def header() -> list[str]:
    """Title, provenance note and table of contents."""
    lines = [
        "# Prompts",
        "",
        "<!-- Generated by backend/scripts/export_prompts.py. Do not edit by hand. -->",
        "",
        "> Generated from [`backend/app/agent/prompts.py`](../backend/app/agent/prompts.py) and "
        "[`backend/app/agent/schemas.py`](../backend/app/agent/schemas.py) by "
        "[`backend/scripts/export_prompts.py`](../backend/scripts/export_prompts.py). After changing a "
        "prompt, regenerate this file with `cd backend && .venv/bin/python scripts/export_prompts.py`.",
        "",
        "Every LLM call the agent makes is shown verbatim below:",
        "",
        "- the four static system prompts, built at startup from the sample database's schema;",
        "- the tools the model fills in;",
        "- the dynamic message of each kind, captured from real runs of the LangGraph graph. The offline "
        "mock model answers during the capture, so the file is reproducible and needs no model.",
        "",
        "## Contents",
        "",
    ]
    lines += [
        f"{index}. [{title.split('. ', 1)[1]}](#{anchor(title)})"
        for index, title in enumerate(SECTION_TITLES, 1)
    ]
    return lines


def rationale(settings: Settings) -> list[str]:
    """Why the prompts look the way they do (design notes and measured facts)."""
    tools, dynamic, fallback, fixed = (anchor(SECTION_TITLES[index]) for index in (3, 4, 6, 7))
    return [
        f"## {SECTION_TITLES[0]}",
        "",
        "| # | Decision | Why |",
        "|---|---|---|",
        "| 1 | **Four roles, one local model.** Intent, SQL, explanation and prose answers each have their "
        f"own system prompt. | Locally one model (`{settings.llm_model}`, thinking off, temperature "
        f"{settings.llm_temperature:g}) serves all four roles, so a 16 GB Mac never swaps models. With Groq the "
        "cheap roles use `LLM_FAST_MODEL` (`gpt-oss-20b`) and SQL uses `LLM_MODEL` (`gpt-oss-120b`). |",
        "| 2 | **Static prefix, dynamic suffix.** Every call is exactly two messages: a system message that is "
        "byte-identical for every request of its role, then one human message with everything "
        f"request-specific (order in [section 5](#{dynamic})). | Ollama and most hosted "
        "APIs reuse the processed prompt prefix. Measured on the Apple M5 with cache checkpoints in place: "
        "1,447 of 1,477 intent-prompt tokens and 2,801 of 2,841 SQL-prompt tokens came from the cache "
        "(about 0.5 s and 1 s of prefill instead of 11 s and 23-25 s). The system prompts contain no "
        "timestamps, ids or history, and a test checks they stay byte-identical across requests. |",
        "| 3 | **Warm-up at startup.** `SQLAgent.warm_up()` sends two one-token requests per role "
        f"(user messages {', '.join(repr(probe) for probe in llm.WARM_UP_PROBES)}). | `qwen3.5` is a hybrid "
        "model: Ollama resumes only from saved cache checkpoints, which it creates where two requests "
        "diverge. Two probes that differ in their first token leave a checkpoint exactly at the end of each "
        "static prefix. Measured: 1.5-2.6 s when checkpoints exist, 20-35 s after a prompt change, about "
        "2 minutes on a cold model load. No-op for hosted providers; `LLM_WARMUP=false` disables it. |",
        "| 4 | **The whole schema lives in the cached SQL prompt.** Up to "
        f"{prompts.FULL_SCHEMA_MAX_TABLES} tables, `catalog.to_prompt()` is embedded in the system prompt. | "
        "The sample schema is about 1,200 tokens and is cached after the first request, and the model always "
        "sees every table and foreign key. Schema retrieval therefore adds a one-line `Focus:` hint "
        "(the most relevant tables and why they matched) instead of trimming the schema, which would "
        "break the cache. For larger databases the retrieved subset moves into the dynamic message. |",
        "| 5 | **Schema text written for a model.** CREATE TABLE-style text with a description per column, "
        "`FK ->` markers, allowed values, common sample values, row counts and existing indexes. | The "
        "model can write correct filters (`State = 'California'`, never `'CA'`) and joins without guessing. |",
        "| 6 | **Structured output by tool calling.** The intent and SQL roles answer by calling a tool "
        f"([section 4](#{tools})) whose arguments Pydantic validates. | Ollama's MLX "
        "runner rejects JSON-schema and JSON-mode requests, but tool calling works. Hosted providers try JSON "
        "schema first, then tool calling. A reply that is not a valid tool call is salvaged from its text; "
        f"otherwise a plain completion with the JSON instructions of [section 7](#{fallback}) is parsed, "
        "with one corrective retry. The schemas also accept small-model slips "
        '(`"Generate SQL"` for `generate_sql`, `95` for `0.95`, `"None"` for null). |',
        "| 7 | **Few-shot examples from the brief.** The brief's own examples appear in the prompts (hired "
        'after January 2024, "Show all customers." then "Only those from California.", out-of-scope '
        "questions). | Plus the patterns small models get wrong: revenue through `OrderItems` with the "
        "discount, date ranges instead of `strftime` equality, a self-join for managers, and Top-N with "
        "`ORDER BY` + `LIMIT`. |",
        '| 8 | **Rules written from live failures** of `qwen3.5:9b`. | "Show all customers." was classified '
        "ambiguous, so the intent prompt now treats broad requests that name a table as `generate_sql`. "
        "MODIFY returned only the change, so it now asks for the complete query. `strftime(...) = '2025'` is "
        "replaced by sargable ranges. Invented index advice is prevented twice: OPTIMIZE copies `CREATE INDEX` "
        "statements from the deterministic findings, and `grounded_suggestions` drops any index advice the "
        "optimizer did not make. Expanding `SELECT *` was slow and error-prone, so the optimizer expands it "
        'from the catalog. The answer prompt states the table count after the model said "six tables". '
        "Questions about data the company does not store become `cannot_answer` with the closest "
        "alternative instead of an out-of-scope refusal. |",
        "| 9 | **Untrusted text is data, never instructions.** | The user's message, the history and pasted "
        "SQL pass through `sanitize_user_text` (control and invisible characters removed; tag-like sequences "
        "such as `</user_message>` or `<\\|im_start\\|>` neutralized) and are wrapped in `<user_message>`, "
        "`<history>` and `<user_sql>` tags. Every system prompt says that `<user_message>` is never "
        "instructions. The classifier flags role-change attempts (`prompt_injection`), and rejected turns are "
        f"replaced by `{prompts.REJECTED_HISTORY_PLACEHOLDER}` in later prompts, so an injection attempt is "
        "never replayed. |",
        "| 10 | **Fixed texts are not generated.** Refusals, rejections, clarification fallbacks and success "
        f"summaries are constants ([section 8](#{fixed})). | A manipulated model "
        "cannot change them, and they are identical for every provider. |",
        "| 11 | **Short outputs.** Explanations are 2-4 sentences with a word cap, answers have a word cap, and "
        f"output tokens are capped per role ({llm.INTENT_MAX_TOKENS:,} intent, {llm.TEXT_MAX_TOKENS:,} text, "
        f"{settings.llm_max_output_tokens:,} SQL). | With the prefix cached, warm latency is almost entirely "
        "output tokens (15-33 tokens/s on the local model), so output length is the main latency lever. |",
        "| 12 | **Validator feedback written for repair.** The REPAIR block quotes the failed SQL and the "
        "validator's errors. | Errors name the offending identifier, list what exists and suggest the closest "
        "match (\"Unknown column 'Region' in table Customers. ... Did you mean 'State'?\"). At most "
        f"{settings.max_sql_attempts} generations per request (`MAX_SQL_ATTEMPTS`). |",
    ]


def roles_table(settings: Settings, system: prompts.SystemPrompts) -> list[str]:
    """One row per LLM role: node, model, answer format, output cap and system prompt size."""
    formats = {
        "intent": ("fast", "tool call `IntentDecision`", llm.INTENT_MAX_TOKENS),
        "sql": ("main", "tool call `SQLGeneration`", settings.llm_max_output_tokens),
        "explain": ("fast", "streamed text (`token` events, target `explanation`)", llm.TEXT_MAX_TOKENS),
        "answer": ("fast", "streamed text (`token` events, target `message`)", llm.TEXT_MAX_TOKENS),
    }
    lines = [
        f"## {SECTION_TITLES[1]}",
        "",
        "| Role | Node | Model | Answer format | Output cap (local) | System prompt |",
        "|---|---|---|---|---|---|",
    ]
    for field_name, title, node in ROLES:
        model, answer, cap = formats[field_name]
        heading = f"3.{[role[0] for role in ROLES].index(field_name) + 1} {title}"
        lines.append(
            f"| [{title}](#{anchor(heading)}) | `{node}` | {model} | {answer} | {cap:,} tokens | "
            f"{len(getattr(system, field_name)):,} characters |"
        )
    lines += [
        "",
        "- **Models:** *main* is `LLM_MODEL` and *fast* is `LLM_FAST_MODEL` (empty means `LLM_MODEL`). Both "
        f"are `{settings.llm_model}` locally; the hosted deployment uses `openai/gpt-oss-120b` (main) and "
        "`openai/gpt-oss-20b` (fast) on Groq.",
        f"- **Sampling:** temperature {settings.llm_temperature:g}, thinking off (`LLM_REASONING=false`), "
        f"context window {settings.llm_num_ctx:,} tokens on Ollama.",
        f"- **Output caps:** the local caps apply to Ollama with thinking off; hosted providers use "
        f"`LLM_MAX_OUTPUT_TOKENS` ({settings.llm_max_output_tokens}) for every role.",
    ]
    return lines


def system_prompts_section(system: prompts.SystemPrompts) -> list[str]:
    """The four static system prompts, verbatim."""
    schema_note = (
        "the full schema in `<schema>` tags" if system.sql_embeds_schema else "no schema (too large)"
    )
    notes = {
        "intent": f"Role, the {len(INTENTS)} intents with definitions, classification rules and examples, plus a "
        "one-line summary of every table. Sent with the `IntentDecision` tool.",
        "sql": f"Role, {numbered_rules(system.sql)} numbered rules, few-shot examples and {schema_note}. Sent "
        "with the `SQLGeneration` tool.",
        "explain": "Style rules for a short plain-English explanation, plus the table summary.",
        "answer": "What QueryPilot does, answer rules, the table summary and every foreign-key relationship.",
    }
    lines = [
        f"## {SECTION_TITLES[2]}",
        "",
        "Built once per process by `build_system_prompts(catalog)` and reused byte for byte on every call.",
    ]
    for index, (field_name, title, node) in enumerate(ROLES, 1):
        text = getattr(system, field_name)
        lines += [
            "",
            f"### 3.{index} {title}",
            "",
            f"Node `{node}`. {notes[field_name]} Size: {size(text)}.",
            "",
        ]
        lines.append(fenced(text))
    return lines


def tools_section() -> list[str]:
    """The tool definitions the model fills in (as LangChain sends them with tool calling)."""
    lines = [
        f"## {SECTION_TITLES[3]}",
        "",
        "LangChain converts the Pydantic models into these tool definitions (`convert_to_openai_tool`). With "
        "Ollama they are bound as tools; hosted providers first receive the same schema as a JSON-schema "
        "response format. Field descriptions are deliberately short, because the model reads them.",
    ]
    for schema in (IntentDecision, SQLGeneration):
        tool = json.dumps(convert_to_openai_tool(schema), indent=2, ensure_ascii=False)
        lines += [
            "",
            "<details>",
            f"<summary><code>{schema.__name__}</code> tool ({len(tool):,} characters of JSON)</summary>",
            "",
            fenced(tool, "json"),
            "",
            "</details>",
        ]
    return lines


def call_label(call: LLMCall, system: prompts.SystemPrompts) -> tuple[str, str]:
    """(graph node, link to its system prompt) for a captured call."""
    content = call.messages[0].content if call.messages else ""
    for index, (field_name, title, node) in enumerate(ROLES, 1):
        if content == getattr(system, field_name):
            return node, f"[{title} system prompt](#{anchor(f'3.{index} {title}')})"
    return call.method, "an unrecognized system prompt"


def dynamic_section(system: prompts.SystemPrompts, captures: Sequence[Capture]) -> list[str]:
    """The dynamic human message of every captured call, scenario by scenario."""
    lines = [
        f"## {SECTION_TITLES[4]}",
        "",
        "The human message holds everything request-specific. For the SQL role the blocks come in this order "
        "(blocks that do not apply are left out):",
        "",
        "1. `<schema>` with the retrieved tables, only when the schema is too large to embed in the system "
        "prompt;",
        "2. `Focus:` the most relevant tables from `retrieve_schema` and the terms that matched;",
        "3. `<history>`: the last `HISTORY_WINDOW` messages of the thread, clipped, each with its query;",
        "4. `<previous_sql>` (MODIFY), or `<user_sql>` and `<findings>` (OPTIMIZE and DEBUG);",
        f"5. `<task>`: the mode block of [section 6](#{anchor(SECTION_TITLES[5])});",
        "6. `<repair>`: the failed attempt and the validator's errors (retries only);",
        "7. `<user_message>`: the sanitized user message;",
        "8. a one-line reminder of what to do.",
        "",
        "The intent role sends the history, a `Previous query:` line, the user message and a reminder. The "
        "explainer sends the final SQL in `<sql>`, the execution result, what was fixed or improved, the user "
        "message and a reminder. The answerer sends the history, the user message and a reminder.",
        "",
        "Each example below is the last turn of a conversation run through the real graph. The system message "
        "of each call is the static prompt linked in its heading and is not repeated.",
    ]
    for index, item in enumerate(captures, 1):
        scenario = item.scenario
        lines += ["", f"### 5.{index} {scenario.title}", "", scenario.note, ""]
        conversation = " then ".join(f'"{turn}"' for turn in scenario.turns)
        lines.append(f"Conversation: {conversation}")
        attempts: dict[str, int] = {}
        for call in item.calls:
            node, link = call_label(call, system)
            attempts[node] = attempts.get(node, 0) + 1
            suffix = f" (attempt {attempts[node]})" if node == "generate_sql" else ""
            lines += ["", f"**`{node}`{suffix}**: {link}, then this human message:", ""]
            human = "\n\n".join(str(message.content) for message in call.messages[1:])
            lines.append(fenced(human))
        lines += ["", outcome(item.final)]
        if item.final.get("sql_sqlite"):
            lines += ["", fenced(item.final["sql_sqlite"], "sql")]
    return lines


def outcome(final: dict[str, Any]) -> str:
    """One line summarizing the response of a captured turn."""
    execution = final.get("execution") or {}
    rows = f", {execution.get('row_count')} rows" if execution else ""
    attempts = final.get("attempts") or 0
    return (
        f"Outcome with the mock model: intent `{final.get('intent')}`, status `{final.get('status')}`, "
        f"{attempts} SQL generation{'s' if attempts != 1 else ''}{rows}."
    )


def mode_blocks_section() -> list[str]:
    """The SQL mode blocks (``*_INSTRUCTIONS`` constants)."""
    lines = [
        f"## {SECTION_TITLES[5]}",
        "",
        "`generate_sql` picks the block from the intent (`modify_sql` uses MODIFY, `optimize_sql` OPTIMIZE, "
        "`debug_sql` DEBUG, anything else GENERATE) and adds REPAIR on retries.",
    ]
    for name, text in module_constants(prompts, str).items():
        if name.endswith("_INSTRUCTIONS"):
            lines += ["", f"`{name}`", "", fenced(text)]
    return lines


def fallback_section() -> list[str]:
    """Instructions used when the model does not return a usable tool call."""
    lines = [
        f"## {SECTION_TITLES[6]}",
        "",
        "If the model answers without a valid tool call and no JSON object can be salvaged from its text, the "
        "gateway appends one of these instructions as a new human message and parses the plain completion. "
        "If that reply is still invalid, it sends one corrective retry quoting the validation error: "
        "`That reply was not valid (<error>). Reply again with ONLY the corrected JSON object.`",
    ]
    for schema in (IntentDecision, SQLGeneration):
        lines += ["", f"`{schema.__name__}`", "", fenced(json_instructions(schema))]
    return lines


def fixed_messages_section(settings: Settings) -> list[str]:
    """User-facing texts that never come from the LLM."""
    skip = ("_SYSTEM_PROMPT", "_INSTRUCTIONS", "SCHEMA_BLOCK")
    lines = [
        f"## {SECTION_TITLES[7]}",
        "",
        "| Constant | Used by | Text |",
        "|---|---|---|",
    ]
    for name, text in module_constants(prompts, str).items():
        if name.endswith(skip):
            continue
        rendered = text.format(max_chars=settings.max_input_chars) if "{max_chars}" in text else text
        lines.append(f"| `{name}` | {MESSAGE_USES.get(name, '')} | {cell(rendered)} |")
    for name, mapping in module_constants(prompts, dict).items():
        lines += [
            "",
            f"`{name}` (`respond`: the message of a successful turn, before the row count is added)",
            "",
        ]
        lines += ["| Key | Text |", "|---|---|"]
        lines += [f"| `{key}` | {cell(str(value))} |" for key, value in mapping.items()]
    builders: dict[str, Callable[..., str]] = {
        name: function
        for name, function in vars(prompts).items()
        if name.endswith("_message") and not name.startswith("_") and callable(function)
    }
    if builders:
        lines += ["", "Message builders, with example arguments:", "", "| Call | Text |", "|---|---|"]
        for name, function in builders.items():
            for args in MESSAGE_EXAMPLES.get(name, ()):
                call = f"{name}({', '.join(repr(arg) for arg in args)})"
                lines.append(f"| `{cell(call)}` | {cell(function(*args))} |")
    return lines


def tuning_section(settings: Settings) -> list[str]:
    """Numeric constants and settings that shape the prompts."""
    values: dict[str, Any] = {**module_constants(prompts, int), **module_constants(llm, int)}
    values = {
        name: value for name, value in values.items() if name in TUNING_NOTES or name.endswith("_CHARS")
    }
    lines = [
        f"## {SECTION_TITLES[8]}",
        "",
        "| Name | Value | Effect |",
        "|---|---|---|",
    ]
    lines += [f"| `{name}` | {value:,} | {TUNING_NOTES.get(name, '')} |" for name, value in values.items()]
    lines += [
        f"| `HISTORY_WINDOW` (setting) | {settings.history_window} | Earlier messages rendered into "
        "`<history>` |",
        f"| `MAX_SQL_ATTEMPTS` (setting) | {settings.max_sql_attempts} | SQL generations per request, "
        "including repairs |",
        f"| `MAX_INPUT_CHARS` (setting) | {settings.max_input_chars:,} | Longest accepted user message |",
        f"| `WARM_UP_PROBES` | {', '.join(repr(probe) for probe in llm.WARM_UP_PROBES)} | User messages of "
        "the startup warm-up requests (one output token each) |",
    ]
    return lines


def build_document(settings: Settings, system: prompts.SystemPrompts, captures: Sequence[Capture]) -> str:
    """The complete Markdown document."""
    sections = [
        header(),
        rationale(settings),
        roles_table(settings, system),
        system_prompts_section(system),
        tools_section(),
        dynamic_section(system, captures),
        mode_blocks_section(),
        fallback_section(),
        fixed_messages_section(settings),
        tuning_section(settings),
    ]
    return "\n\n".join("\n".join(section) for section in sections).rstrip() + "\n"


async def render() -> tuple[str, int]:
    """Render the document; also return how many model calls were captured."""
    settings = docs_settings()
    catalog = load_catalog(ensure_database(DEFAULT_DB_PATH))
    system = prompts.build_system_prompts(catalog)
    captures = [await capture(scenario, settings, catalog) for scenario in SCENARIOS]
    return build_document(settings, system, captures), sum(len(item.calls) for item in captures)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Export every LLM prompt to docs/PROMPTS.md.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"default: {DEFAULT_OUTPUT}")
    parser.add_argument(
        "--check", action="store_true", help="exit 1 if the file is out of date; write nothing"
    )
    args = parser.parse_args(argv)
    document, calls = asyncio.run(render())
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        if current != document:
            print(f"{args.output} is out of date: run scripts/export_prompts.py", file=sys.stderr)
            return 1
        print(f"{args.output} is up to date", file=sys.stderr)
        return 0
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(document, encoding="utf-8")
    print(
        f"Wrote {args.output} ({len(document.splitlines())} lines, {calls} model calls captured)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
