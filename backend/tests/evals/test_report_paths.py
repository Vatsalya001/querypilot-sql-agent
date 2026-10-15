"""Eval reports never contain machine-specific absolute paths."""

from pathlib import Path

from evals.run_evals import display_path

BACKEND = Path(__file__).resolve().parents[2]


def test_paths_inside_backend_are_relative() -> None:
    assert display_path(BACKEND / "evals" / "golden.json") == "evals/golden.json"
    assert display_path(BACKEND / "data" / "company.db") == "data/company.db"


def test_paths_outside_backend_keep_only_the_file_name(tmp_path: Path) -> None:
    assert display_path(tmp_path / "custom.db") == "custom.db"
