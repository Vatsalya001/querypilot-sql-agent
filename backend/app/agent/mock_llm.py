"""Deterministic, offline, rule-based stand-in for the language model (``LLM_PROVIDER=mock``).

The mock reads exactly the prompts a real model receives (it never sees the graph state), so it
exercises the real prompt builders and can drive every branch of the workflow:

* :meth:`MockGateway.classify_intent` applies keyword rules to the ``<user_message>``, the
  "Previous query" line and the rendered history;
* :meth:`MockGateway.generate_sql` returns canned SQL for the demo questions (GENERATE), minimal edits
  of ``<previous_sql>`` (MODIFY), SELECT * expansion, sargable date ranges and removal of flagged
  joins (OPTIMIZE), fuzzy fixes of misspelled keywords and identifiers against the schema text in
  the prompt (DEBUG), and fixes driven by the validator's "Did you mean" hints (REPAIR);
* :meth:`MockGateway.stream_text` produces templated explanations and answers, streamed word by word.

It powers the unit tests and keyless demos. It is deliberately simple, not a real SQL generator.
"""

from __future__ import annotations

import asyncio
import difflib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import cached_property

import sqlglot
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.agent.llm import TokenCallback
from app.agent.prompts import STAR_EXPANDED_CHANGE
from app.agent.schemas import IntentDecision, SQLGeneration, SQLIssue

US_STATES: tuple[str, ...] = (
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "District of Columbia", "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa",
    "Kansas", "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey",
    "New Mexico", "New York", "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah",
    "Vermont", "Virginia", "Washington", "West Virginia", "Wisconsin", "Wyoming",
)
_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december")
_MONTH_PATTERN = "|".join(_MONTHS)

_ENTITY_TERMS: dict[str, tuple[str, ...]] = {
    "Employees": ("employee", "staff", "worker", "hire", "hired", "salar", "manager", "job title"),
    "Customers": ("customer", "client", "buyer"),
    "Orders": ("order", "sale", "purchase", "revenue", "sold", "shipped", "delivered"),
    "Products": ("product", "item", "goods", "categor", "stock", "inventory"),
    "Departments": ("department", "dept", "team", "budget"),
    "Payments": ("payment", "paid", "transaction"),
}
"""Words that point at a table (matched at the start of a word, so plurals match too)."""

_DATE_COLUMNS = {"Employees": "HireDate", "Orders": "OrderDate", "Customers": "CreatedAt", "Payments": "PaymentDate"}
_VALUE_FILTERS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("Customers", "Segment", ("Consumer", "Corporate", "Small Business")),
    ("Orders", "Status", ("Pending", "Shipped", "Delivered", "Cancelled", "Returned")),
    ("Products", "Category", ("Electronics", "Furniture", "Office Supplies", "Software", "Accessories")),
)
_KEYWORD_TYPOS = {
    "selec": "SELECT", "selet": "SELECT", "slect": "SELECT", "seelct": "SELECT", "form": "FROM",
    "frm": "FROM", "fom": "FROM", "whre": "WHERE", "wher": "WHERE", "wehre": "WHERE", "gropu": "GROUP",
    "grup": "GROUP", "oder": "ORDER", "ordr": "ORDER", "limt": "LIMIT", "jion": "JOIN", "joni": "JOIN",
    "havng": "HAVING", "adn": "AND",
}
_UNKNOWN_DATA = re.compile(
    r"\b(suppliers?|vendors?|reviews?|ratings?|loyalty|warehouses?|coupons?|promotions?|social media|"
    r"weather|competitors?)\b",
    re.IGNORECASE,
)

# --- intent rules (checked in order; see classify_message) ---
_INJECTION = re.compile(
    r"\b(ignore|disregard|forget)\b.{0,40}\b(instructions|rules|prompt)\b|\bsystem prompt\b|"
    r"\byour (instructions|rules|prompt)\b|\bjailbreak\b|\bdeveloper mode\b|\byou are now\b|\bpretend\b",
    re.IGNORECASE,
)
_CREATIVE = re.compile(r"\b(poem|story|song|joke|essay|haiku|limerick|lyrics)\b", re.IGNORECASE)
_OFF_TOPIC = re.compile(  # sports, politics, maths and non-SQL programming (only without SQL words)
    r"\b(nba|nfl|mlb|nhl|fifa|world cup|super bowl|olympics?|championships?|football|soccer|basketball|"
    r"baseball|cricket|elections?|vote|voting|president|prime minister|politic\w*|derivative|integral|"
    r"equation|calculus|python|javascript|typescript|java|c\+\+|rust|golang|html|css|scrap(?:e|er|ing)|"
    r"weather|recipe)\b",
    re.IGNORECASE,
)
_SQL_TOPIC = re.compile(r"\b(sql|query|queries|database|tables?|schema|columns?)\b", re.IGNORECASE)
_FENCED_SQL = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
_PLAIN_SQL = re.compile(r"\b(SELECT|SELEC|SELET|SLECT|WITH)\b\s.*?(?:\n\s*\n|$)", re.IGNORECASE | re.DOTALL)
_DEBUG_WORDS = re.compile(r"\b(fix|debug|error|errors|wrong|fail|fails|failing|broken|bug|issue|doesn'?t work)\b", re.I)
_OPTIMIZE_WORDS = re.compile(r"\b(optimi[sz]e|faster|speed up|efficient|performance|improve|clean ?up|rewrite)\b", re.I)
_EXPLAIN_WORDS = re.compile(r"\b(explain|what does|what is this|meaning|describe|walk me through)\b", re.I)
_DESTRUCTIVE_WORDS = re.compile(
    r"\b(delete|remove|erase|purge|wipe|drop|truncate|insert|update|alter|rename)\b|"
    r"\b(add|create)\s+(?:a\s+|an\s+)?(?:new\s+)?(row|record|product|customer|employee|order|table|column|"
    r"department|payment|index)\b|\b(give|grant)\b.{0,40}\b(raise|bonus|increase)\b|"
    r"\b(raise|increase|decrease|reduce|cut|set)\b.{0,30}\b(salar(?:y|ies)|prices?|budgets?|stock)\b",
    re.IGNORECASE,
)
_QUERY_EDIT_WORDS = re.compile(r"\b(query|sql|filters?|columns?|conditions?|limit|sort|order by|where clause)\b", re.I)
_STRUCTURE_WORDS = re.compile(r"\b(tables?|database|schema|index|rows?|records?)\b", re.IGNORECASE)
_FOLLOW_UP = re.compile(
    r"^(only|just|and|also|now|but|then|sort|order|filter|limit|show only|include|exclude|instead|"
    r"what about|how about|same|those|them|these|add|without|group|remove|change|make it)\b",
    re.IGNORECASE,
)
_GREETING = re.compile(
    r"^(hi|hello|hey|good (morning|afternoon|evening)|thanks|thank you)\b|\bwhat can you do\b|"
    r"\bwho are you\b|\bhow do i use\b",
    re.IGNORECASE,
)
_QUESTION_START = re.compile(r"(what|which|how|describe|tell|list|explain|are|is|does|do)\b", re.IGNORECASE)
_SQL_CONCEPTS = re.compile(
    r"\b(difference between|what is|what are|what's|how does|how do|when should i|why use|explain)\b.{0,60}"
    r"\b(joins?|index(es)?|indices|group by|having|subquer(y|ies)|cte|union|primary keys?|foreign keys?|"
    r"normali[sz]ation|window functions?|null|distinct|aggregates?|views?|transactions?|where clause)\b",
    re.IGNORECASE,
)
_SCHEMA_WORDS = re.compile(
    r"\b(tables?|columns?|schema|fields?|relationships?|foreign keys?|primary keys?|linked|related|relate|"
    r"connected|structure|data types?)\b",
    re.IGNORECASE,
)
_VAGUE_WORDS = re.compile(
    r"\b(data|records|rows|results|info|information|stuff|everything|anything|numbers|report|best|top|ones|"
    r"those|them|these)\b",
    re.IGNORECASE,
)
_METRIC_WORDS = re.compile(
    r"\b(revenue|sales|spend|spent|spending|orders|amount|salar(y|ies)|price|quantity|count|number|units|"
    r"budget|stock)\b",
    re.IGNORECASE,
)
_DID_YOU_MEAN = re.compile(r"Unknown (?:column|table) '([^']+)'.*?Did you mean '([^']+)'", re.IGNORECASE)
_DATE_LITERAL = re.compile(r"\d{4}-\d{2}(-\d{2})?")

ASK_FOR_DETAILS = (
    "Which data would you like to see? For example: customers, orders, products, employees, payments or "
    "departments, plus any filters such as a date range, a state or a status."
)


# ==================================================================================================
# Reading the prompt
# ==================================================================================================


def _text(message: BaseMessage) -> str:
    return message.content if isinstance(message.content, str) else str(message.content)


def _block(text: str, tag: str) -> str | None:
    """Content of the last ``<tag>...</tag>`` block in ``text``."""
    matches = re.findall(rf"<{tag}>\n?(.*?)\n?</{tag}>", text, re.DOTALL)
    return matches[-1].strip() if matches else None


def _bullets(text: str | None) -> list[str]:
    return [line[2:].strip() for line in (text or "").splitlines() if line.startswith("- ")]


_CREATE_TABLE = re.compile(r"CREATE TABLE\s+[\"`\[]?(\w+)[\"`\]]?\s*\((.*?)\n\s*\)", re.DOTALL | re.IGNORECASE)
_COLUMN_LINE = re.compile(r"^\s*[\"`\[]?([A-Za-z_]\w*)[\"`\]]?\s+[A-Za-z]+")
_SUMMARY_LINE = re.compile(r"^\s*(?:-\s*)?([A-Za-z_]\w*)\(([^()]*)\)\s*$", re.MULTILINE)
_NOT_COLUMNS = {"primary", "foreign", "constraint", "unique", "check"}


def parse_schema_text(text: str) -> dict[str, list[str]]:
    """Tables and their columns from schema text in a prompt (CREATE TABLE blocks or ``T(a, b)`` lines)."""
    tables: dict[str, list[str]] = {}
    for match in _CREATE_TABLE.finditer(text):
        columns = []
        for line in match.group(2).splitlines():
            column = _COLUMN_LINE.match(line)
            if column and column.group(1).lower() not in _NOT_COLUMNS and not line.strip().startswith("--"):
                columns.append(column.group(1))
        if columns:
            tables.setdefault(match.group(1), columns)
    for match in _SUMMARY_LINE.finditer(text):
        columns = [part.strip() for part in match.group(2).split(",")]
        if columns and all(re.fullmatch(r"\w+", column) for column in columns):
            tables.setdefault(match.group(1), columns)
    return tables


@dataclass(frozen=True)
class PromptView:
    """The parts of a rendered prompt (system + last human message) that the mock reacts to."""

    system: str
    human: str

    @classmethod
    def of(cls, messages: Sequence[BaseMessage]) -> PromptView:
        """Split a message list into its system text and its last human message."""
        system = "\n".join(_text(m) for m in messages if isinstance(m, SystemMessage))
        humans = [_text(m) for m in messages if isinstance(m, HumanMessage)]
        return cls(system=system, human=humans[-1] if humans else "")

    @property
    def user_message(self) -> str:
        return _block(self.human, "user_message") or self.human.strip()

    @property
    def previous_sql(self) -> str | None:
        block = _block(self.human, "previous_sql")
        if block:
            return block
        line = re.search(r"^Previous query: (.+)$", self.human, re.MULTILINE)
        return line.group(1).strip() if line and line.group(1).strip().lower() != "none" else None

    @property
    def user_sql(self) -> str | None:
        return _block(self.human, "user_sql")

    @property
    def mode(self) -> str:
        task = _block(self.human, "task") or "GENERATE"
        return task.split(":", 1)[0].strip().lower()

    @property
    def findings(self) -> list[str]:
        return _bullets(_block(self.human, "findings"))

    @property
    def repair(self) -> tuple[str, list[str]] | None:
        """``(previous attempt, errors)`` when the prompt asks for a repair."""
        block = _block(self.human, "repair")
        if block is None:
            return None
        attempt = re.search(r"Previous attempt:\n(.*?)\nErrors:\n", block, re.DOTALL)
        errors = _bullets(block.split("\nErrors:\n", 1)[-1])
        return (attempt.group(1).strip() if attempt else ""), errors

    @property
    def history(self) -> list[tuple[str, str]]:
        lines = (_block(self.human, "history") or "").splitlines()
        return [(line.split(": ", 1)[0], line.split(": ", 1)[1]) for line in lines if ": " in line]

    @property
    def sql_to_explain(self) -> str | None:
        return _block(self.human, "sql")

    @cached_property
    def schema(self) -> dict[str, list[str]]:
        return parse_schema_text(f"{self.system}\n{self.human}")


# ==================================================================================================
# Shared helpers
# ==================================================================================================


def _find_state(text: str) -> str | None:
    """The first US state named in ``text`` (longest names first, so "West Virginia" beats "Virginia")."""
    for state in sorted(US_STATES, key=len, reverse=True):
        if re.search(rf"\b{state}\b", text, re.IGNORECASE):
            return state
    return None


def _find_value(text: str, values: Iterable[str]) -> str | None:
    for value in values:
        if re.search(rf"\b{value}\b", text, re.IGNORECASE):
            return value
    return None


def _entity_tables(text: str) -> list[str]:
    """Tables mentioned in ``text``, in order of first mention."""
    positions: dict[str, int] = {}
    for table, terms in _ENTITY_TERMS.items():
        for term in terms:
            match = re.search(rf"\b{term}", text, re.IGNORECASE)
            if match and (table not in positions or match.start() < positions[table]):
                positions[table] = match.start()
    return sorted(positions, key=positions.__getitem__)


def _closest(name: str, candidates: Iterable[str], cutoff: float = 0.6) -> str | None:
    """Best fuzzy match for ``name``; CamelCase parts count too (``Nmae`` -> ``FirstName``)."""
    best, best_score = None, cutoff
    for candidate in candidates:
        if candidate.lower() == name.lower():
            return candidate
        score = difflib.SequenceMatcher(None, name.lower(), candidate.lower()).ratio()
        for part in re.findall(r"[A-Z][a-z]+|[a-z]+", candidate):
            score = max(score, 0.9 * difflib.SequenceMatcher(None, name.lower(), part.lower()).ratio())
        if score > best_score:
            best, best_score = candidate, score
    return best


def _join(items: Sequence[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def _parse(sql: str) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(sql, read="sqlite")
    except SqlglotError:
        return None


def _year_range(column: str, year: int) -> str:
    return f"{column} >= '{year}-01-01' AND {column} < '{year + 1}-01-01'"


def _ok(sql: str, summary: str, **extra: list[str] | list[SQLIssue]) -> SQLGeneration:
    return SQLGeneration(status="ok", sql=sql, summary=summary, **extra)


def _clarify(question: str) -> SQLGeneration:
    return SQLGeneration(status="needs_clarification", clarification_question=question, summary=question)


# ==================================================================================================
# Intent classification
# ==================================================================================================


def _find_sql(text: str) -> str | None:
    """SQL pasted into a message: fenced, or code-like text from SELECT/WITH to the end (typos included).

    English such as "select all customers from Texas" or "customers with the most orders from Texas" is
    not code: plain text counts only with upper-case keywords or SQL punctuation.
    """
    fenced = _FENCED_SQL.search(text)
    if fenced:
        return fenced.group(1).strip().rstrip(";").strip() or None
    match = _PLAIN_SQL.search(text)
    if not match:
        return None
    sql = match.group(0).strip().rstrip(";").strip()
    keyword = match.group(1)
    if keyword.upper() == "WITH" and not re.match(r"WITH\s+\w+\s+AS\s*\(", sql, re.IGNORECASE):
        return None
    if not re.search(r"\b(FROM|FORM|FRM)\b", sql, re.IGNORECASE):
        return None
    return sql if keyword.isupper() or re.search(r"[*,=()<>]", sql) else None


def classify_message(
    text: str, *, previous_sql: str | None = None, history: Sequence[tuple[str, str]] = ()
) -> IntentDecision:
    """Keyword-rule intent classification mirroring the rules of the real intent prompt."""

    def decide(intent: str, confidence: float, reason: str, **extra: object) -> IntentDecision:
        return IntentDecision.model_validate({"intent": intent, "confidence": confidence, "reasoning": reason, **extra})

    if _INJECTION.search(text):
        return decide("out_of_scope", 0.95, "Tries to change the assistant's rules.", prompt_injection=True)
    if _CREATIVE.search(text):
        return decide("out_of_scope", 0.9, "Creative writing is not a database task.")
    pasted = _find_sql(text)
    prose = text.replace(pasted, " ") if pasted else text
    if not pasted and _OFF_TOPIC.search(text) and not _SQL_TOPIC.search(text):
        return decide("out_of_scope", 0.9, "Not about SQL or this database.")
    if _DESTRUCTIVE_WORDS.search(prose):
        if _STRUCTURE_WORDS.search(prose) or not _QUERY_EDIT_WORDS.search(prose):
            return decide("destructive", 0.95, "Asks to change data or structure.")
        if not pasted:
            return decide("modify_sql" if previous_sql else "generate_sql", 0.8, "Asks to change the query.")
    if pasted:
        if _DEBUG_WORDS.search(prose):
            return decide("debug_sql", 0.9, "Pasted SQL that needs fixing.", user_sql=pasted)
        if _OPTIMIZE_WORDS.search(prose):
            return decide("optimize_sql", 0.9, "Pasted SQL to optimize.", user_sql=pasted)
        return decide("explain_sql", 0.85, "Pasted SQL to explain.", user_sql=pasted)
    if previous_sql and _FOLLOW_UP.search(text.strip()):
        return decide("modify_sql", 0.9, "Refines the previous query.")
    for words, intent in ((_OPTIMIZE_WORDS, "optimize_sql"), (_DEBUG_WORDS, "debug_sql"), (_EXPLAIN_WORDS, "explain_sql")):
        if words.search(text) and re.search(r"\b(query|sql|it|this)\b", text, re.IGNORECASE):
            return decide(intent, 0.8, "Refers to a query without pasting it.")
    if _GREETING.search(text) or (_SQL_CONCEPTS.search(text) and not _entity_tables(text)):
        return decide("sql_help", 0.9, "General SQL help or greeting.")
    if _SCHEMA_WORDS.search(text) and _QUESTION_START.match(text.strip()):
        return decide("schema_info", 0.85, "Asks about the database structure.")
    asked_before = bool(history) and history[-1][0] == "Assistant" and history[-1][1].rstrip().endswith("?")
    if _entity_tables(text) or _UNKNOWN_DATA.search(text) or (asked_before and len(text.split()) <= 12):
        return decide("generate_sql", 0.9, "A data question about the schema.")
    if _VAGUE_WORDS.search(text) or _find_state(text):
        return decide("ambiguous", 0.8, "Too vague to write a query.", clarification_question=ASK_FOR_DETAILS)
    return decide("out_of_scope", 0.9, "Not about SQL or this database.")


# ==================================================================================================
# GENERATE: canned SQL for the demo questions, then a generic single-table query
# ==================================================================================================


def _date_filter(text: str, column: str) -> tuple[str, str] | None:
    """``(condition, description)`` for phrases like "after January 2024", "since 2023" or "in 2025"."""
    match = re.search(rf"\b(after|since|from|before|in|during)\s+(?:({_MONTH_PATTERN})\s+)?(\d{{4}})\b", text, re.I)
    if not match:
        return None
    word, month_name, year = match.group(1).lower(), match.group(2), int(match.group(3))
    month = _MONTHS.index(month_name.lower()) + 1 if month_name else None
    start = f"{year}-{month or 1:02d}-01"
    label = f"{month_name.title()} 1, {year}" if month_name else f"January 1, {year}"
    if word in ("after", "since", "from"):
        if word == "after" and month is None:  # "after 2023" starts with 2024
            start, label = f"{year + 1}-01-01", f"January 1, {year + 1}"
        return f"{column} >= '{start}'", f"on or after {label}"
    if word == "before":
        return f"{column} < '{start}'", f"before {label}"
    if month is None:
        return _year_range(column, year), f"in {year}"
    end = f"{year + (month == 12)}-{month % 12 + 1:02d}-01"
    return f"{column} >= '{start}' AND {column} < '{end}'", f"in {month_name.title()} {year}"


def _gen_unknown_data(text: str) -> SQLGeneration | None:
    match = _UNKNOWN_DATA.search(text)
    if not match:
        return None
    return SQLGeneration(
        status="cannot_answer",
        summary=f"The database has no {match.group(1).lower()} data. The closest available information is in "
        "Products (category, price, stock), Orders and Customers.",
    )


def _gen_hired(text: str) -> SQLGeneration | None:
    if not re.search(r"\bhired\b", text, re.I):
        return None
    date = _date_filter(text, "HireDate")
    if date is None:
        return None
    condition, label = date
    return _ok(
        f"SELECT * FROM Employees WHERE {condition}",
        f"Here are all employees hired {label}.",
        assumptions=[f"Hire dates are compared as 'YYYY-MM-DD' text ({label})."],
    )


def _gen_top_products(text: str) -> SQLGeneration | None:
    match = re.search(r"\btop\s+(\d+)\s+(?:selling\s+|best[- ]selling\s+)?products?\b|\bbest[- ]selling products?\b", text, re.I)
    if not match:
        return None
    limit = int(match.group(1)) if match.group(1) else 5
    if re.search(r"\b(units|quantity|sold)\b", text, re.I):
        metric, label = "SUM(oi.Quantity)", "UnitsSold"
    else:
        metric, label = "ROUND(SUM(oi.Quantity * oi.UnitPrice * (1 - oi.Discount)), 2)", "Revenue"
    source, where, period = "FROM OrderItems AS oi", "", ""
    date = _date_filter(text, "o.OrderDate")
    if date is not None:  # dates live on Orders: join it through OrderItems.OrderID
        source = "FROM OrderItems AS oi JOIN Orders AS o ON o.OrderID = oi.OrderID"
        where, period = f" WHERE {date[0]}", f" {date[1]}"
    return _ok(
        f"SELECT p.ProductName, {metric} AS {label} {source} JOIN Products AS p "
        f"ON p.ProductID = oi.ProductID{where} GROUP BY p.ProductID, p.ProductName ORDER BY {label} DESC "
        f"LIMIT {limit}",
        f"Here are the top {limit} products by {'units sold' if label == 'UnitsSold' else 'revenue'}{period}.",
        assumptions=["Revenue is Quantity * UnitPrice * (1 - Discount) over all order lines."],
    )


def _gen_top_customers(text: str) -> SQLGeneration | None:
    match = re.search(r"\btop\s+(\d+)\s+customers\b|\b(?:best|top|biggest)\s+customers\b|\bcustomers?\b.{0,30}"
                      r"\bmost orders\b", text, re.I)
    if not match:
        return None
    limit = int(match.group(1)) if match.group(1) else 10
    by_count = bool(re.search(r"\b(most orders|number of orders|order count)\b", text, re.I))
    if not by_count and not _METRIC_WORDS.search(text):
        return None  # "top customers" without a metric: let the clarification rule ask
    metric, label = ("COUNT(o.OrderID)", "OrderCount") if by_count else ("ROUND(SUM(o.TotalAmount), 2)", "TotalSpent")
    return _ok(
        f"SELECT c.CustomerID, c.FirstName, c.LastName, {metric} AS {label} FROM Customers AS c JOIN Orders AS o "
        f"ON o.CustomerID = c.CustomerID GROUP BY c.CustomerID, c.FirstName, c.LastName ORDER BY {label} DESC "
        f"LIMIT {limit}",
        f"Here are the top {limit} customers by {'number of orders' if by_count else 'total spend'}.",
    )


def _gen_orders_by_status(text: str) -> SQLGeneration | None:
    if not re.search(r"\borders?\b.{0,40}\b(per|by|for each|each)\s+status\b|\bstatus(es)? of (the )?orders\b", text, re.I):
        return None
    return _ok(
        "SELECT Status, COUNT(*) AS OrderCount FROM Orders GROUP BY Status ORDER BY OrderCount DESC",
        "Here is the number of orders for each status.",
    )


def _gen_average_salary(text: str) -> SQLGeneration | None:
    if not re.search(r"\b(average|avg|mean)\s+salar(y|ies)\b", text, re.I):
        return None
    if not re.search(r"\b(department|dept|team)", text, re.I):
        return _ok("SELECT ROUND(AVG(Salary), 2) AS AverageSalary FROM Employees", "Here is the average salary.")
    return _ok(
        "SELECT d.DepartmentName, ROUND(AVG(e.Salary), 2) AS AverageSalary FROM Employees AS e JOIN Departments AS d "
        "ON d.DepartmentID = e.DepartmentID GROUP BY d.DepartmentID, d.DepartmentName ORDER BY AverageSalary DESC",
        "Here is the average salary in each department.",
    )


def _gen_per_month(text: str) -> SQLGeneration | None:
    if not re.search(r"\b(monthly|per month|by month|each month|every month)\b", text, re.I):
        return None
    year = re.search(r"\b(20\d\d)\b", text)
    where = f" WHERE {_year_range('OrderDate', int(year.group(1)))}" if year else ""
    suffix = f" in {year.group(1)}" if year else ""
    if re.search(r"\b(revenue|sales|amount|income)\b", text, re.I):
        return _ok(
            f"SELECT strftime('%Y-%m', OrderDate) AS Month, ROUND(SUM(TotalAmount), 2) AS Revenue FROM Orders{where} "
            "GROUP BY Month ORDER BY Month",
            f"Here is the revenue per month{suffix}.",
        )
    return _ok(
        f"SELECT strftime('%Y-%m', OrderDate) AS Month, COUNT(*) AS OrderCount FROM Orders{where} GROUP BY Month "
        "ORDER BY Month",
        f"Here is the number of orders per month{suffix}.",
    )


def _gen_payment_methods(text: str) -> SQLGeneration | None:
    if not re.search(r"\bpayments?\b.{0,40}\bmethods?\b|\bpayment methods?\b", text, re.I):
        return None
    return _ok(
        "SELECT PaymentMethod, COUNT(*) AS PaymentCount, ROUND(SUM(Amount), 2) AS TotalAmount FROM Payments "
        "GROUP BY PaymentMethod ORDER BY TotalAmount DESC",
        "Here are the payments grouped by payment method.",
    )


def _gen_managers(text: str) -> SQLGeneration | None:
    if not re.search(r"\bemployees?\b.{0,40}\bmanagers?\b", text, re.I):
        return None
    return _ok(
        "SELECT e.FirstName, e.LastName, m.FirstName AS ManagerFirstName, m.LastName AS ManagerLastName "
        "FROM Employees AS e LEFT JOIN Employees AS m ON m.EmployeeID = e.ManagerID",
        "Here is each employee with their manager.",
    )


def _gen_low_stock(text: str) -> SQLGeneration | None:
    match = re.search(r"\b(low|out of)\s+(?:in\s+)?stock\b|\bstock\s+(?:below|under|less than)\s+(\d+)", text, re.I)
    if not match:
        return None
    threshold = int(match.group(2)) if match.group(2) else (1 if match.group(1).lower() == "out of" else 10)
    return _ok(
        f"SELECT ProductID, ProductName, StockQuantity FROM Products WHERE StockQuantity < {threshold} "
        "ORDER BY StockQuantity",
        f"Here are the products with fewer than {threshold} units in stock.",
        assumptions=[f"'Low stock' means fewer than {threshold} units."] if not match.group(2) else [],
    )


def _gen_total_revenue(text: str) -> SQLGeneration | None:
    if not re.search(r"\btotal\s+(revenue|sales)\b", text, re.I):
        return None
    date = _date_filter(text, "OrderDate")
    where = f" WHERE {date[0]}" if date else ""
    return _ok(
        f"SELECT ROUND(SUM(TotalAmount), 2) AS TotalRevenue FROM Orders{where}",
        f"Here is the total revenue{' ' + date[1] if date else ''}.",
    )


def _filters_for(table: str, text: str) -> tuple[list[str], list[str]]:
    """Conditions (and their descriptions) implied by ``text`` for a single-table query on ``table``."""
    conditions: list[str] = []
    labels: list[str] = []
    state = _find_state(text)
    if state and table in ("Customers", "Orders"):
        conditions.append(f"{'State' if table == 'Customers' else 'ShippingState'} = '{state}'")
        labels.append(f"from {state}")
    for owner, column, values in _VALUE_FILTERS:
        value = _find_value(text, values)
        if owner == table and value:
            conditions.append(f"{column} = '{value}'")
            labels.append(f"with {column.lower()} {value}")
    if table == "Employees" and re.search(r"\b(inactive|active)\b", text, re.I):
        active = re.search(r"\binactive\b", text, re.I) is None
        conditions.append(f"IsActive = {int(active)}")
        labels.append("who are active" if active else "who are inactive")
    if table in _DATE_COLUMNS:
        date = _date_filter(text, _DATE_COLUMNS[table])
        if date:
            conditions.append(date[0])
            labels.append(date[1])
    return conditions, labels


def _gen_count(text: str) -> SQLGeneration | None:
    if not re.search(r"\b(how many|number of|count)\b", text, re.I):
        return None
    tables = _entity_tables(text)
    if not tables:
        return None
    table = tables[0]
    conditions, labels = _filters_for(table, text)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return _ok(
        f"SELECT COUNT(*) AS {table}Count FROM {table}{where}",
        f"Here is the number of {table.lower()}{' ' + ' '.join(labels) if labels else ''}.",
    )


def _gen_rank_without_metric(text: str) -> SQLGeneration | None:
    if re.search(r"\b(best|top|most valuable|biggest|most important)\b", text, re.I) and not _METRIC_WORDS.search(text):
        return _clarify("How should I rank them: by total revenue, by number of orders, or by something else?")
    return None


def _gen_table_listing(text: str) -> SQLGeneration | None:
    tables = _entity_tables(text)
    if not tables:
        return None
    table = tables[0]
    conditions, labels = _filters_for(table, text)
    where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
    return _ok(
        f"SELECT * FROM {table}{where}",
        f"Here are all {table.lower()}{' ' + ' '.join(labels) if labels else ''}.",
    )


_GENERATE_RULES: tuple[Callable[[str], SQLGeneration | None], ...] = (
    _gen_unknown_data,
    _gen_hired,
    _gen_top_products,
    _gen_top_customers,
    _gen_orders_by_status,
    _gen_average_salary,
    _gen_per_month,
    _gen_payment_methods,
    _gen_managers,
    _gen_low_stock,
    _gen_total_revenue,
    _gen_count,
    _gen_rank_without_metric,
    _gen_table_listing,
)


def generate_query(text: str, history: Sequence[tuple[str, str]] = ()) -> SQLGeneration:
    """Canned SQL for a new question; a reply to a clarification question is combined with that question."""
    candidates = [text]
    earlier = [content for role, content in history if role == "User"]
    if earlier and history[-1][0] == "Assistant" and history[-1][1].rstrip().endswith("?"):
        candidates.insert(0, f"{earlier[-1]} {text}")
    for candidate in candidates:
        for rule in _GENERATE_RULES:
            result = rule(candidate)
            if result is not None:
                return result
    return _clarify(ASK_FOR_DETAILS)


# ==================================================================================================
# MODIFY: minimal edits of the previous query
# ==================================================================================================


def _alias_of(tree: exp.Expression, table: str) -> str | None:
    """The alias (or name) used for ``table`` in the query, or ``None`` if the table is not used."""
    for node in tree.find_all(exp.Table):
        if node.name.lower() == table.lower():
            return node.alias_or_name
    return None


def _main_table(tree: exp.Select) -> str | None:
    source = tree.args.get("from_")
    return source.this.name if source is not None and isinstance(source.this, exp.Table) else None


def _qualified(tree: exp.Select, table: str, column: str) -> str | None:
    """``alias.column`` in multi-table or aliased queries, else just ``column``; ``None`` if ``table`` is unused."""
    alias = _alias_of(tree, table)
    if alias is None:
        return None
    return f"{alias}.{column}" if tree.args.get("joins") or alias != table else column


def _replace_equality(tree: exp.Select, column: str, value: str) -> bool:
    """Change ``column = '<old>'`` to ``column = '<value>'``; True if such a condition existed."""
    where = tree.args.get("where")
    for node in where.find_all(exp.EQ) if where else ():
        if isinstance(node.this, exp.Column) and node.this.name.lower() == column.lower():
            node.set("expression", exp.Literal.string(value))
            return True
    return False


def _add_filter(tree: exp.Select, table: str, column: str, value: str, changes: list[str]) -> None:
    """Replace an existing ``column = ...`` condition, or AND a new one onto the WHERE clause."""
    target = _qualified(tree, table, column)
    if target is None:
        return
    if _replace_equality(tree, column, value):
        changes.append(f"Changed the {column} filter to '{value}'.")
    else:
        tree.where(f"{target} = '{value}'", copy=False)
        changes.append(f"Added the filter {target} = '{value}'.")


def _schema_columns(schema: dict[str, list[str]], table: str) -> list[str]:
    """Columns of ``table`` (case-insensitive lookup)."""
    return next((columns for name, columns in schema.items() if name.lower() == table.lower()), [])


def _column_phrase(phrase: str, columns: Iterable[str]) -> str | None:
    """Resolve words such as "hire date" or "salary" to a column name."""
    squashed = re.sub(r"[^a-z0-9]", "", phrase.lower())
    columns = list(columns)
    for column in columns:
        if column.lower() == squashed or column.lower().endswith(squashed):
            return column
    return _closest(squashed, columns, cutoff=0.7)


_SORT = re.compile(
    r"\b(?:sort|order)(?:ed)?\s+(?:them\s+|it\s+|the results\s+)?by\s+([a-z ]+?)(?=\s*(?:,|\.|$|\b(?:highest|lowest|"
    r"desc|asc|descending|ascending|newest|oldest|largest|smallest|first)\b))",
    re.IGNORECASE,
)
_DESCENDING = re.compile(r"\b(desc|descending|highest|largest|most|newest|latest|biggest)\b", re.IGNORECASE)


def _apply_filters(tree: exp.Select, text: str, changes: list[str]) -> None:
    """State, enumerated values (segment, status, category), active flag and date ranges."""
    state = _find_state(text)
    if state and _alias_of(tree, "Customers"):
        _add_filter(tree, "Customers", "State", state, changes)
    elif state and _alias_of(tree, "Orders"):
        _add_filter(tree, "Orders", "ShippingState", state, changes)
    for table, column, values in _VALUE_FILTERS:
        value = _find_value(text, values)
        if value and _alias_of(tree, table):
            _add_filter(tree, table, column, value, changes)
    active = re.search(r"\b(inactive|active)\b", text, re.IGNORECASE)
    target = _qualified(tree, "Employees", "IsActive")
    if active and target:
        tree.where(f"{target} = {int(active.group(1).lower() == 'active')}", copy=False)
        changes.append(f"Added the filter {target} = {int(active.group(1).lower() == 'active')}.")
    main = _main_table(tree)
    if main in _DATE_COLUMNS:
        column = _qualified(tree, main, _DATE_COLUMNS[main]) or _DATE_COLUMNS[main]
        date = _date_filter(text, column)
        if date:
            tree.where(date[0], copy=False)
            changes.append(f"Added the date filter {date[0]}.")


def modify_query(previous_sql: str, text: str, schema: dict[str, list[str]]) -> SQLGeneration:
    """Apply the follow-up in ``text`` to ``previous_sql`` with the smallest possible edit."""
    tree = _parse(previous_sql)
    if not isinstance(tree, exp.Select):
        return _clarify("What would you like to change in the previous query?")
    changes: list[str] = []
    if re.search(r"\b(remove|drop|clear|without)\b.{0,20}\bfilters?\b", text, re.IGNORECASE) and tree.args.get("where"):
        tree.set("where", None)
        changes.append("Removed the WHERE filter.")
    _apply_filters(tree, text, changes)
    sort = _SORT.search(text)
    if sort:
        owners: dict[str, str] = {}
        for node in tree.find_all(exp.Table):
            owners.update({column: node.name for column in _schema_columns(schema, node.name) if column not in owners})
        column = _column_phrase(sort.group(1), owners)
        if column:
            direction = " DESC" if _DESCENDING.search(text) else ""
            target = _qualified(tree, owners[column], column) or column
            tree.order_by(f"{target}{direction}", append=False, copy=False)
            changes.append(f"Sorted by {target}{' descending' if direction else ''}.")
    limit = re.search(r"\b(?:top|first|limit(?: it)?(?: to)?|only)\s+(\d+)\b", text, re.IGNORECASE)
    if limit:
        tree.limit(int(limit.group(1)), copy=False)
        changes.append(f"Limited the result to {limit.group(1)} rows.")
    if not changes:
        return _clarify("What would you like to change in the previous query?")
    summary = f"I updated the previous query: {changes[0][0].lower()}{changes[0][1:]}"
    return _ok(tree.sql(dialect="sqlite"), summary, changes=changes)


# ==================================================================================================
# DEBUG / REPAIR / OPTIMIZE
# ==================================================================================================


def _outside_strings(sql: str, transform: Callable[[str], str]) -> str:
    """Apply ``transform`` to the parts of ``sql`` that are not single-quoted string literals."""
    parts = re.split(r"('(?:[^']|'')*')", sql)
    return "".join(part if index % 2 else transform(part) for index, part in enumerate(parts))


_TEXT_FIXES: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (re.compile(r",\s*(FROM)\b", re.IGNORECASE), r" \1", "Extra comma before FROM.",
     "The last selected column must not be followed by a comma."),
    (re.compile(r"(?:<>|!=)\s*NULL\b", re.IGNORECASE), "IS NOT NULL", "Comparison with NULL using <> or !=.",
     "Nothing equals NULL in SQL, so the condition never matches; IS NOT NULL is correct."),
    (re.compile(r"(?<![<>!])=\s*NULL\b", re.IGNORECASE), "IS NULL", "Comparison with NULL using =.",
     "Nothing equals NULL in SQL, so the condition never matches; IS NULL is correct."),
    (re.compile(r"(=|<>|!=|\bLIKE)\s*\"([^\"]*)\"", re.IGNORECASE), r"\1 '\2'", "A text value is written in double quotes.",
     "Double quotes mean an identifier (a column name) in SQL; text values need single quotes."),
    (re.compile(r"(?<![\w.'-])((?:19|20)\d{2}-\d{2}-\d{2})(?![\w.-])"), r"'\1'", "A date is written without quotes.",
     "SQL reads an unquoted date such as 2024-01-01 as arithmetic (2024 - 1 - 1 = 2022), so the comparison "
     "silently matches the wrong rows; dates are stored as 'YYYY-MM-DD' text and need single quotes."),
)


def _fix_text(sql: str) -> tuple[str, list[SQLIssue]]:
    """Fix misspelled keywords, ``= NULL``, double-quoted values and a comma before FROM."""
    issues: list[SQLIssue] = []

    def replace_keyword(match: re.Match[str]) -> str:
        fixed = _KEYWORD_TYPOS[match.group(0).lower()]
        issues.append(SQLIssue(problem=f"'{match.group(0)}' is not a SQL keyword.", reason=f"It should be {fixed}."))
        return fixed

    def fix(part: str) -> str:
        part = re.sub(rf"\b({'|'.join(_KEYWORD_TYPOS)})\b", replace_keyword, part, flags=re.IGNORECASE)
        for pattern, replacement, problem, reason in _TEXT_FIXES:
            if pattern.search(part):
                issues.append(SQLIssue(problem=problem, reason=reason))
                part = pattern.sub(replacement, part)
        return part

    return _outside_strings(sql, fix), issues


def _fix_identifiers(sql: str, schema: dict[str, list[str]]) -> tuple[str, list[SQLIssue]]:
    """Replace unknown tables and columns with their closest schema names."""
    tree = _parse(sql)
    if tree is None or not schema:
        return sql, []
    issues: list[SQLIssue] = []
    ctes = {cte.alias_or_name.lower() for cte in tree.find_all(exp.CTE)}
    canonical = {name.lower(): name for name in schema}
    renamed: dict[str, str] = {}
    scope: dict[str, str] = {}
    for node in tree.find_all(exp.Table):
        name = node.name
        if name.lower() in ctes:
            continue
        table = canonical.get(name.lower()) or _closest(name, schema)
        if table is None:
            continue
        if table.lower() != name.lower():
            issues.append(SQLIssue(problem=f"Table '{name}' does not exist.",
                                   reason=f"The schema has no table named {name}; the closest match is {table}."))
            node.set("this", exp.to_identifier(table))
            renamed[name.lower()] = table
        scope[node.alias_or_name.lower()] = table
    aliases = {alias.alias.lower() for alias in tree.find_all(exp.Alias)}
    for column in tree.find_all(exp.Column):
        if column.is_star:
            continue
        qualifier = column.table.lower()
        if qualifier in renamed:
            column.set("table", exp.to_identifier(renamed[qualifier]))
            qualifier = renamed[qualifier].lower()
        tables = [scope[qualifier]] if qualifier in scope else list(dict.fromkeys(scope.values()))
        known = [name for table in tables for name in schema[table]]
        if not known or column.name.lower() in {name.lower() for name in known} or column.name.lower() in aliases:
            continue
        replacement = _closest(column.name, known)
        if replacement:
            issues.append(SQLIssue(
                problem=f"Column '{column.name}' does not exist in {_join(tables)}.",
                reason=f"{_join(tables)} has no column named {column.name}; the closest match is {replacement}.",
            ))
            column.set("this", exp.to_identifier(replacement))
    return (tree.sql(dialect="sqlite") if issues else sql), issues


def _apply_hints(sql: str, errors: Iterable[str]) -> tuple[str, list[SQLIssue]]:
    """Apply the validator's "Unknown ... 'X' ... Did you mean 'Y'?" suggestions."""
    issues: list[SQLIssue] = []
    for error in errors:
        for wrong, right in _DID_YOU_MEAN.findall(error):
            fixed = _outside_strings(sql, lambda part, w=wrong, r=right: re.sub(rf"\b{re.escape(w)}\b", r, part))
            if fixed != sql:
                sql = fixed
                issues.append(SQLIssue(problem=f"'{wrong}' does not exist in the schema.",
                                       reason=f"The validator reported it as unknown; the intended name is {right}."))
    return sql, issues


def debug_query(user_sql: str, findings: Sequence[str], schema: dict[str, list[str]]) -> SQLGeneration:
    """Fix the user's SQL: keyword typos first, then the validator's hints, then fuzzy identifier matches."""
    sql, issues = _fix_text(user_sql)
    for fixer in (lambda text: _apply_hints(text, findings), lambda text: _fix_identifiers(text, schema)):
        sql, found = fixer(sql)
        issues += found
    issues = list({issue.problem: issue for issue in issues}.values())  # one issue per distinct problem
    if not issues:
        return _ok(user_sql, "I couldn't find an error in this query; it is valid for this schema.")
    plural = "s" if len(issues) > 1 else ""
    return _ok(sql, f"I found and fixed {len(issues)} problem{plural} in your query.", issues=issues,
               changes=[issue.problem for issue in issues])


def repair_query(attempt: str, errors: Sequence[str], schema: dict[str, list[str]]) -> SQLGeneration | None:
    """Fix a failed attempt using the validator's errors; ``None`` when nothing could be changed."""
    sql, hinted = _apply_hints(attempt, errors)
    sql, fuzzy = _fix_identifiers(sql, schema)
    if not hinted and not fuzzy:
        return None
    return _ok(sql, "Here is the SQL query for your request.", changes=[issue.problem for issue in hinted + fuzzy])


_YEAR_EQUALS = re.compile(r"strftime\(\s*'%Y'\s*,\s*([\w.]+)\s*\)\s*=\s*'(\d{4})'", re.IGNORECASE)


def _remove_flagged_joins(tree: exp.Select, findings: Sequence[str], changes: list[str]) -> None:
    """Drop LEFT JOINs that static analysis flagged as unnecessary and whose columns are unused elsewhere."""
    flagged = " ".join(f for f in findings if f.lower().startswith("unnecessary join")).lower()
    for join in list(tree.args.get("joins") or []):
        table = join.this
        if join.side != "LEFT" or not isinstance(table, exp.Table) or table.name.lower() not in flagged:
            continue
        alias = table.alias_or_name.lower()
        used = any(column.table.lower() == alias for column in tree.find_all(exp.Column)
                   if column.find_ancestor(exp.Join) is not join)
        if not used:
            tree.set("joins", [other for other in tree.args["joins"] if other is not join])
            changes.append(f"Removed the unnecessary LEFT JOIN to {table.name}; none of its columns are used.")


def optimize_query(user_sql: str, findings: Sequence[str], schema: dict[str, list[str]]) -> SQLGeneration:
    """Sargable date ranges, explicit columns instead of SELECT *, and removal of flagged LEFT JOINs."""
    fixed = debug_query(user_sql, findings, schema)
    sql = fixed.sql or user_sql
    changes = list(fixed.changes)
    rewritten = _YEAR_EQUALS.sub(lambda m: _year_range(m.group(1), int(m.group(2))), sql)
    if rewritten != sql:
        sql = rewritten
        changes.append("Replaced strftime('%Y', ...) = 'YYYY' with a date range, so an index on the date column "
                       "can be used.")
    tree = _parse(sql)
    if isinstance(tree, exp.Select):
        _remove_flagged_joins(tree, findings, changes)
        main = _main_table(tree)
        columns = _schema_columns(schema, main) if main else []
        if len(tree.expressions) == 1 and tree.expressions[0].is_star and not tree.args.get("joins") and columns:
            tree.set("expressions", [exp.column(column) for column in columns])
            changes.append(STAR_EXPANDED_CHANGE)
        sql = tree.sql(dialect="sqlite")
    suggestions = [f for f in findings if f.upper().startswith("CREATE INDEX")]
    if not changes:
        changes.append("The query is already efficient; only the formatting changed.")
    return _ok(sql, "Here is an optimized version of your query.", changes=changes, suggestions=suggestions)


# ==================================================================================================
# Prose: explanations and answers
# ==================================================================================================

_COMPARISONS: tuple[tuple[type[exp.Expression], str, str | None], ...] = (
    (exp.EQ, "is", None),
    (exp.NEQ, "is not", None),
    (exp.GTE, "is at least", "is on or after"),
    (exp.GT, "is greater than", "is after"),
    (exp.LTE, "is at most", "is on or before"),
    (exp.LT, "is less than", "is before"),
)


def _describe_condition(node: exp.Expression) -> str:
    """Plain-English rendering of a WHERE condition."""
    if isinstance(node, exp.Paren):
        return _describe_condition(node.this)
    if isinstance(node, exp.And | exp.Or):
        joiner = " and " if isinstance(node, exp.And) else " or "
        return _describe_condition(node.left) + joiner + _describe_condition(node.right)
    for kind, words, date_words in _COMPARISONS:
        if isinstance(node, kind):
            value = node.expression
            if date_words and isinstance(value, exp.Literal) and value.is_string and _DATE_LITERAL.fullmatch(value.this):
                words = date_words
            return f"{node.this.sql()} {words} {value.sql()}"
    return node.sql()


def _projection_name(projection: exp.Expression) -> str:
    """How a SELECT-list item is named in an explanation (aliases and columns by name, other expressions as SQL)."""
    if isinstance(projection, exp.Alias | exp.Column):
        return projection.alias_or_name
    if isinstance(projection, exp.Count) and isinstance(projection.this, exp.Star):
        return "the number of rows"
    return projection.sql(dialect="sqlite")


def describe_sql(sql: str) -> str:
    """One templated sentence describing what a SELECT statement returns."""
    tree = _parse(sql)
    select = tree if isinstance(tree, exp.Select) else (tree.find(exp.Select) if tree else None)
    if select is None:
        return "This query reads data from the database."
    tables = list(dict.fromkeys(node.name for node in tree.find_all(exp.Table)))
    names = [_projection_name(projection) for projection in select.expressions]
    if any(projection.is_star for projection in select.expressions):
        what = "all columns"
    else:
        what = _join(names[:4]) + (f" and {len(names) - 4} more columns" if len(names) > 4 else "")
    source = f"the {tables[0]} table" if len(tables) == 1 else f"the {_join(tables)} tables (joined)"
    sentence = f"This query retrieves {what} from {source}"
    if select.args.get("where"):
        sentence += f", keeping only rows where {_describe_condition(select.args['where'].this)}"
    if select.args.get("group"):
        sentence += f", grouped by {_join([e.sql() for e in select.args['group'].expressions])}"
    if select.args.get("order"):
        keys = [f"{o.this.sql()} ({'highest first' if o.args.get('desc') else 'lowest first'})"
                for o in select.args["order"].expressions]
        sentence += f", sorted by {_join(keys)}"
    limit = select.args.get("limit")
    if isinstance(limit, exp.Limit) and limit.expression is not None:
        sentence += f", limited to {limit.expression.sql()} rows"
    return sentence + "."


def explain_text(view: PromptView) -> str:
    """Templated explanation for the explain role."""
    parts: list[str] = []
    fixed = re.search(r"^(Fixed|Improved):\n((?:- .*\n?)+)", view.human, re.MULTILINE)
    if fixed:
        items = _bullets(fixed.group(2))
        first = items[0].rstrip(".")
        word = first.split(" ", 1)[0]
        first = first if len(word) > 1 and word.isupper() else first[:1].lower() + first[1:]  # keep acronyms
        verb = "I fixed" if fixed.group(1) == "Fixed" else "I improved the query"
        parts.append(f"{verb}: {first}" + (f" (plus {len(items) - 1} more)." if len(items) > 1 else "."))
    parts.append(describe_sql(view.sql_to_explain or ""))
    rows = re.search(r"Execution: returned (\d+) rows?( \(more rows exist)?", view.human)
    if rows:
        more = " (more rows exist)" if rows.group(2) else ""
        parts.append(f"It returned {rows.group(1)} row{'s' if rows.group(1) != '1' else ''}{more}.")
    elif "Execution: failed" in view.human:
        parts.append("It failed when it was run against the database.")
    return " ".join(parts)


_CONCEPT_ANSWERS: tuple[tuple[str, str], ...] = (
    (r"\bhaving\b", "WHERE filters individual rows before they are grouped, while HAVING filters the groups "
     "produced by GROUP BY, so it can use aggregates such as COUNT(*) > 5."),
    (r"\bjoins?\b", "A JOIN combines rows from two tables through related columns. INNER JOIN keeps only "
     "matching rows; LEFT JOIN keeps every row of the left table and fills missing matches with NULL."),
    (r"\bindex(es)?\b|\bindices\b", "An index is a sorted lookup structure on one or more columns. It lets the "
     "database find matching rows without scanning the whole table, at the cost of slower writes."),
    (r"\bgroup by\b", "GROUP BY collapses rows that share the same values into one row per group, so aggregates "
     "such as SUM, AVG or COUNT are computed per group."),
)
_EXAMPLE_QUESTIONS = "You could ask:\n- Show all customers from California.\n- What are the top 5 products by revenue?"


def answer_text(view: PromptView) -> str:
    """Templated answer for schema questions, SQL-concept questions and greetings."""
    question = view.user_message
    tables = view.schema
    if _GREETING.search(question):
        body = ("Hi! I'm QueryPilot. I turn questions about the company database into validated, read-only SQL, "
                "explain what queries do, optimize and debug your SQL, and remember the conversation so you can "
                "refine results with follow-up questions.")
    elif _SCHEMA_WORDS.search(question) or any(re.search(rf"\b{t}\b", question, re.I) for t in tables):
        named = [table for table in tables if re.search(rf"\b{table}\b", question, re.IGNORECASE)]
        if named:
            table = named[0]
            links = [line[2:] for line in view.system.splitlines() if line.startswith("- ") and f"{table}." in line]
            body = f"{table} has these columns: {', '.join(tables[table])}."
            if links:
                body += f" Relationships: {'; '.join(links)}."
        else:
            body = f"The database has {len(tables)} tables: {_join(list(tables))}."
    else:
        body = next((answer for pattern, answer in _CONCEPT_ANSWERS if re.search(pattern, question, re.I)),
                    "I can help with SQL and the company database: writing queries, explaining, optimizing and "
                    "debugging them, and describing the tables.")
    return f"{body}\n\n{_EXAMPLE_QUESTIONS}"


# ==================================================================================================
# Gateway
# ==================================================================================================


class MockGateway:
    """Rule-based gateway that needs no model, no network and no API key (deterministic)."""

    provider = "mock"
    model = "mock"
    fast_model = "mock"
    mock = True

    async def classify_intent(self, messages: list[BaseMessage]) -> IntentDecision:
        """Classify the ``<user_message>`` of an intent prompt with keyword rules."""
        view = PromptView.of(messages)
        return classify_message(view.user_message, previous_sql=view.previous_sql, history=view.history)

    async def generate_sql(self, messages: list[BaseMessage]) -> SQLGeneration:
        """Answer a SQL prompt according to its mode block (and REPAIR feedback, if any)."""
        view = PromptView.of(messages)
        repair = view.repair
        if repair is not None:
            repaired = repair_query(repair[0], repair[1], view.schema)
            if repaired is not None:
                return repaired
        if view.mode == "modify" and view.previous_sql:
            return modify_query(view.previous_sql, view.user_message, view.schema)
        if view.mode == "optimize" and view.user_sql:
            return optimize_query(view.user_sql, view.findings, view.schema)
        if view.mode == "debug" and view.user_sql:
            return debug_query(view.user_sql, view.findings, view.schema)
        return generate_query(view.user_message, view.history)

    async def stream_text(self, messages: list[BaseMessage], *, on_token: TokenCallback | None = None) -> str:
        """Stream a templated explanation (prompts with ``<sql>``) or answer, word by word."""
        view = PromptView.of(messages)
        text = explain_text(view) if view.sql_to_explain is not None else answer_text(view)
        for chunk in re.findall(r"\S+\s*", text):
            if on_token is not None:
                on_token(chunk)
            await asyncio.sleep(0)
        return text
