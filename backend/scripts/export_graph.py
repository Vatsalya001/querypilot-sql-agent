"""Export the compiled LangGraph workflow as Mermaid.

The agent is built with the offline mock LLM (LLM_PROVIDER=mock), so no model server or API key is
needed. Writes docs/graph.mmd and prints the diagram.

Usage (from backend/): .venv/bin/python scripts/export_graph.py [--output PATH]
"""

import argparse
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = BACKEND_DIR.parent / "docs" / "graph.mmd"


def export_mermaid(output: Path) -> str:
    """Build the agent with the mock provider, write the graph's Mermaid source to ``output`` and return it."""
    os.environ["LLM_PROVIDER"] = "mock"
    if str(BACKEND_DIR) not in sys.path:
        sys.path.insert(0, str(BACKEND_DIR))
    from app.agent.service import SQLAgent
    from app.config import Settings

    mermaid = SQLAgent(Settings()).mermaid().rstrip() + "\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(mermaid, encoding="utf-8")
    return mermaid


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Export the LangGraph workflow as Mermaid.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help=f"default: {DEFAULT_OUTPUT}")
    args = parser.parse_args(argv)
    print(export_mermaid(args.output), end="")
    print(f"Wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
