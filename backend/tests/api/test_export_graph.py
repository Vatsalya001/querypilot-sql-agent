"""Test for scripts/export_graph.py (Mermaid export of the compiled LangGraph workflow)."""

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from tests.api.fakes import NODE_NAMES

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "export_graph.py"


def _load_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("export_graph", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_writes_the_compiled_graph_as_mermaid(
    tmp_path: Path, db_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "mock")  # the script sets it too; monkeypatch restores the original
    monkeypatch.setenv("DATABASE_PATH", str(db_path))
    output = tmp_path / "docs" / "graph.mmd"

    mermaid = _load_script().export_mermaid(output)

    assert output.read_text(encoding="utf-8") == mermaid
    assert "graph TD;" in mermaid
    for node in NODE_NAMES:
        assert f"\t{node}({node})" in mermaid
    assert "validate_sql -.-> generate_sql;" in mermaid, "the self-correction loop is missing"
