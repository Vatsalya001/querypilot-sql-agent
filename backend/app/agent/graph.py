"""Assembly of the LangGraph ``StateGraph`` (16 nodes, conditional routing, self-correction loop).

```
START -> guard_input -> detect_intent -> validate_scope -> retrieve_schema -> [diagnose_sql] ->
generate_sql <-> validate_sql (repair loop) -> optimize_sql -> execute_sql -> explain_sql -> respond -> END
```
Side exits: ``reject_request`` (out of scope / prompt injection), ``refuse_destructive``,
``ask_clarification``, ``answer_question`` (schema and SQL-concept questions) and ``handle_failure``;
all of them end in ``respond``.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from app.agent.events import NODE_LABELS
from app.agent.nodes import AgentDeps, AgentNodes
from app.agent.routing import (
    make_route_after_validate,
    route_after_diagnose,
    route_after_generate,
    route_after_guard,
    route_after_retrieve,
    route_after_scope,
)
from app.agent.state import AgentState

NODE_NAMES: tuple[str, ...] = tuple(NODE_LABELS)
"""All node names in pipeline order (matches docs/API.md)."""

_TERMINAL_NODES = ("answer_question", "ask_clarification", "reject_request", "refuse_destructive", "handle_failure")


def build_graph(deps: AgentDeps, *, checkpointer: Any = None) -> CompiledStateGraph:
    """Build and compile the agent graph with the given dependencies and checkpointer."""
    nodes = AgentNodes(deps)
    graph = StateGraph(AgentState)
    for name in NODE_NAMES:
        graph.add_node(name, getattr(nodes, name))

    graph.add_edge(START, "guard_input")
    graph.add_conditional_edges(
        "guard_input", route_after_guard, ["detect_intent", "reject_request", "refuse_destructive"]
    )
    graph.add_edge("detect_intent", "validate_scope")
    graph.add_conditional_edges(
        "validate_scope",
        route_after_scope,
        ["reject_request", "refuse_destructive", "ask_clarification", "answer_question", "retrieve_schema"],
    )
    graph.add_conditional_edges("retrieve_schema", route_after_retrieve, ["diagnose_sql", "generate_sql"])
    graph.add_conditional_edges(
        "diagnose_sql", route_after_diagnose, ["refuse_destructive", "optimize_sql", "generate_sql"]
    )
    graph.add_conditional_edges(
        "generate_sql", route_after_generate, ["ask_clarification", "handle_failure", "validate_sql"]
    )
    graph.add_conditional_edges(
        "validate_sql",
        make_route_after_validate(deps.settings.max_sql_attempts),
        ["optimize_sql", "generate_sql", "handle_failure"],
    )
    graph.add_edge("optimize_sql", "execute_sql")
    graph.add_edge("execute_sql", "explain_sql")
    graph.add_edge("explain_sql", "respond")
    for name in _TERMINAL_NODES:
        graph.add_edge(name, "respond")
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=checkpointer, name="querypilot")
