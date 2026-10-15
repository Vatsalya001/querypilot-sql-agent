"""Runtime configuration loaded from environment variables (and optional ``.env`` files).

Every setting can be overridden with an environment variable of the same name (case-insensitive),
e.g. ``LLM_PROVIDER=groq`` or ``LLM_MODEL=openai/gpt-oss-120b``. A ``.env`` file in the repository
root and/or in ``backend/`` is read when present; real environment variables take precedence.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_DIR.parent
_CONTAINER_STATIC_DIR = Path("/app/static")

LLMProvider = Literal["ollama", "groq", "google", "openai", "mock"]


class Settings(BaseSettings):
    """Application settings. Field names map 1:1 to environment variables."""

    model_config = SettingsConfigDict(
        env_file=(REPO_ROOT / ".env", BACKEND_DIR / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- LLM ---
    llm_provider: LLMProvider = "ollama"
    llm_model: str = "qwen3.5:9b"
    llm_fast_model: str = ""
    llm_fallback_models: str = ""
    llm_temperature: float = 0.0
    llm_timeout_s: float = 120.0
    llm_reasoning: bool = False
    llm_num_ctx: int = 8192
    llm_max_output_tokens: int = 1024
    ollama_base_url: str = "http://localhost:11434"
    ollama_keep_alive: str = "30m"
    llm_warmup: bool = True  # prime the local model's prompt cache at startup (Ollama only)
    groq_api_key: str = ""
    google_api_key: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""

    # --- Database and execution ---
    database_path: str = ""
    max_result_rows: int = 500
    query_timeout_s: float = 5.0

    # --- Agent behaviour ---
    max_sql_attempts: int = 3
    max_input_chars: int = 2000
    history_window: int = 8

    # --- API ---
    rate_limit_enabled: bool = True
    rate_limit_per_minute: int = 20
    rate_limit_per_day: int = 500
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    static_dir: str = ""
    log_level: str = "INFO"

    @field_validator(
        "llm_model", "ollama_base_url", "groq_api_key", "google_api_key", "openai_api_key", "openai_base_url",
        mode="before",
    )
    @classmethod
    def _strip_whitespace(cls, value: object) -> object:
        """Drop whitespace pasted around keys, model ids and URLs (e.g. a key copied with its trailing
        newline into a hosting dashboard), which would otherwise make every request's auth header invalid."""
        return value.strip() if isinstance(value, str) else value

    @property
    def fast_model(self) -> str:
        """Model for the cheap roles (intent, explanation, answers); defaults to ``llm_model``."""
        return self.llm_fast_model.strip() or self.llm_model

    @property
    def fallback_models(self) -> list[str]:
        """Same-provider models tried in order when the primary model errors (e.g. HTTP 429)."""
        return _split_csv(self.llm_fallback_models)

    @property
    def resolved_database_path(self) -> Path:
        """Absolute path of the SQLite database (``DATABASE_PATH`` or the bundled default)."""
        if self.database_path.strip():
            return Path(self.database_path.strip()).expanduser().resolve()
        from app.db.database import DEFAULT_DB_PATH  # local import: keeps config free of import cycles

        return Path(DEFAULT_DB_PATH)

    @property
    def resolved_static_dir(self) -> Path | None:
        """Directory of the built SPA, or ``None`` when no build is available."""
        if self.static_dir.strip():
            explicit = Path(self.static_dir.strip()).expanduser().resolve()
            return explicit if explicit.is_dir() else None
        for candidate in (REPO_ROOT / "frontend" / "dist", _CONTAINER_STATIC_DIR):
            if candidate.is_dir():
                return candidate
        return None

    @property
    def cors_origin_list(self) -> list[str]:
        """Allowed CORS origins as a list."""
        return _split_csv(self.cors_origins)


def _split_csv(value: str) -> list[str]:
    """Split a comma-separated setting into trimmed, non-empty items."""
    return [item.strip() for item in value.split(",") if item.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings (cached; call ``get_settings.cache_clear()`` in tests)."""
    return Settings()
