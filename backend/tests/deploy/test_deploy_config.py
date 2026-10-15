"""Consistency checks for the deployment files: render.yaml, the Space guide, .env.example and the image.

A misspelled variable in render.yaml would be ignored silently and the service would start with the
defaults (``LLM_PROVIDER=ollama`` on a host without Ollama), so every configured key must be a setting,
and the model ids must agree across render.yaml, the Hugging Face guide and docs/DEPLOYMENT.md.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from app.config import REPO_ROOT, Settings
from app.main import create_app
from tests.deploy.conftest import FALLBACK, FAST, PRIMARY

LLM_KEYS = ("LLM_PROVIDER", "LLM_MODEL", "LLM_FAST_MODEL", "LLM_FALLBACK_MODELS")


def _render_service() -> dict[str, Any]:
    blueprint = yaml.safe_load((REPO_ROOT / "render.yaml").read_text(encoding="utf-8"))
    [service] = blueprint["services"]
    return service


def _render_values() -> dict[str, str]:
    return {item["key"]: str(item["value"]) for item in _render_service()["envVars"] if "value" in item}


def test_render_blueprint_is_a_free_docker_web_service_with_a_real_health_check(
    tmp_path: Path, db_path: Path
) -> None:
    service = _render_service()

    assert (service["type"], service["runtime"], service["plan"]) == ("web", "docker", "free")
    assert (REPO_ROOT / service.get("dockerfilePath", "./Dockerfile")).is_file()
    settings = Settings(_env_file=None, llm_provider="mock", static_dir=str(tmp_path), database_path=str(db_path))
    with TestClient(create_app(settings)) as client:
        assert client.get(service["healthCheckPath"]).status_code == 200


def test_render_secret_is_prompted_for_and_never_committed() -> None:
    [secret] = [item for item in _render_service()["envVars"] if item["key"] == "GROQ_API_KEY"]

    assert secret.get("sync") is False and "value" not in secret


def test_render_variables_are_settings_and_select_the_documented_groq_models() -> None:
    keys = {item["key"] for item in _render_service()["envVars"]}
    unknown = {key for key in keys if key.lower() not in Settings.model_fields}
    assert not unknown, f"render.yaml sets variables the app does not read: {unknown}"

    values = {key.lower(): value for key, value in _render_values().items()}
    settings = Settings(_env_file=None, **values, groq_api_key="gsk_offline_test_key_not_real")
    assert (settings.llm_provider, settings.llm_model, settings.fast_model) == ("groq", PRIMARY, FAST)
    assert settings.fallback_models == [FALLBACK]


def test_hugging_face_guide_uses_the_render_configuration() -> None:
    guide = (REPO_ROOT / "deploy" / "huggingface" / "README.md").read_text(encoding="utf-8")
    space_env = dict(re.findall(r"-e ([A-Z_]+)=(\S+)", guide))
    render = _render_values()

    for key, value in space_env.items():
        assert render.get(key) == value, f"{key}: Space guide {value!r} != render.yaml {render.get(key)!r}"
    assert set(LLM_KEYS) <= set(space_env)
    assert re.search(r"^app_port: 8000$", guide, re.MULTILINE)


@pytest.mark.parametrize("key", LLM_KEYS[1:])
def test_deployment_guide_names_the_configured_models(key: str) -> None:
    guide = (REPO_ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")

    assert f"`{_render_values()[key]}`" in guide


def test_env_example_only_documents_real_settings() -> None:
    text = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    keys = set(re.findall(r"^#? ?([A-Z][A-Z0-9_]+)=", text, re.MULTILINE))

    assert {"LLM_PROVIDER", "GROQ_API_KEY", "DATABASE_PATH"} <= keys
    assert not {key for key in keys if key.lower() not in Settings.model_fields}
    groq_block = dict(re.findall(r"^# (LLM_[A-Z_]+)=(\S+)$", text.split("# --- Groq", 1)[1].split("# ---", 1)[0], re.M))
    assert groq_block == {key: _render_values()[key] for key in LLM_KEYS}


def test_constraints_pin_every_requirement_inside_its_range() -> None:
    backend = REPO_ROOT / "backend"
    pins = {}
    for line in (backend / "constraints.txt").read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#"):
            name, version = line.split("==")
            pins[canonicalize_name(name)] = Version(version)
    requirements = [
        Requirement(line.split("#")[0])
        for path in ("requirements.txt", "requirements-dev.txt")
        for line in (backend / path).read_text(encoding="utf-8").splitlines()
        if line.split("#")[0].strip() and not line.startswith("-r")
    ]

    assert requirements
    for requirement in requirements:
        pinned = pins.get(canonicalize_name(requirement.name))
        assert pinned is not None, f"{requirement.name} is not pinned in backend/constraints.txt"
        assert requirement.specifier.contains(pinned), f"{requirement.name}=={pinned} violates {requirement}"


def test_image_runs_unprivileged_on_the_injected_port_without_secrets_in_the_context() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    runtime_stage = dockerfile.rsplit("\nFROM ", 1)[1]
    ignored = set((REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").split())

    assert re.search(r"^USER (?!root)\w+", runtime_stage, re.MULTILINE)
    assert '${PORT:-8000}' in runtime_stage and "--workers" not in runtime_stage  # one worker: in-memory state
    assert "-c constraints.txt" in runtime_stage  # the tested versions, not the newest ones
    assert {"**/.env", "**/.env.*", "**/.venv", "**/node_modules", "backend/data/*.db"} <= ignored
