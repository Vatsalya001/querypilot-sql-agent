"""Settings: documented defaults, environment overrides and derived helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, get_settings
from app.db.database import DEFAULT_DB_PATH


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("LLM_PROVIDER", "LLM_MODEL", "LLM_FAST_MODEL", "DATABASE_PATH", "MAX_SQL_ATTEMPTS", "STATIC_DIR"):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(_env_file=None)

    assert (settings.llm_provider, settings.llm_model, settings.fast_model) == ("ollama", "qwen3.5:9b", "qwen3.5:9b")
    assert settings.llm_reasoning is False and settings.llm_temperature == 0.0 and settings.llm_warmup is True
    assert (settings.llm_num_ctx, settings.llm_max_output_tokens, settings.llm_timeout_s) == (8192, 1024, 120.0)
    assert (settings.ollama_base_url, settings.ollama_keep_alive) == ("http://localhost:11434", "30m")
    assert (settings.max_sql_attempts, settings.history_window, settings.max_input_chars) == (3, 8, 2000)
    assert (settings.max_result_rows, settings.query_timeout_s) == (500, 5.0)
    assert (settings.rate_limit_per_minute, settings.rate_limit_per_day) == (20, 500)
    assert settings.resolved_database_path == Path(DEFAULT_DB_PATH)
    assert settings.cors_origin_list == ["http://localhost:5173", "http://127.0.0.1:5173"]


def test_environment_overrides_are_case_insensitive(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("llm_provider", "groq")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-oss-120b")
    monkeypatch.setenv("LLM_FAST_MODEL", "openai/gpt-oss-20b")
    monkeypatch.setenv("LLM_FALLBACK_MODELS", " qwen/qwen3.8-27b , ,other ")
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "x.db"))
    monkeypatch.setenv("UNRELATED_SETTING", "ignored")
    settings = Settings(_env_file=None)

    assert settings.llm_provider == "groq"
    assert settings.fast_model == "openai/gpt-oss-20b"
    assert settings.fallback_models == ["qwen/qwen3.8-27b", "other"]
    assert settings.resolved_database_path == (tmp_path / "x.db").resolve()


def test_static_dir_resolution(tmp_path: Path) -> None:
    assert Settings(_env_file=None, static_dir=str(tmp_path)).resolved_static_dir == tmp_path.resolve()
    assert Settings(_env_file=None, static_dir=str(tmp_path / "missing")).resolved_static_dir is None


def test_get_settings_is_cached() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
