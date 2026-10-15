# QueryPilot developer commands. Run `make` (or `make help`) to list them.
# Written for GNU make 3.81 (the version macOS ships) and bash. The backend reads .env itself
# (backend/app/config.py), and variables set in your shell take precedence over it.

SHELL := /bin/bash
.DEFAULT_GOAL := help

BACKEND := backend
FRONTEND := frontend
VENV_PY := $(BACKEND)/.venv/bin/python
# Recipes that run Python cd into backend/ first, so this path is relative to it.
PY := .venv/bin/python
IMAGE ?= querypilot:latest
# Extra arguments for pytest (test, test-live), the eval runner (evals) or the graph export (graph),
# e.g. ARGS="-k validator".
ARGS ?=

.PHONY: help setup ollama ollama-reset seed dev backend frontend test test-live lint build serve evals graph \
	docker-build docker-run clean

help: ## Show this list of targets
	@echo "Usage: make <target> [ARGS='...']"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) | awk 'BEGIN { FS = ":.*## " } { printf "  %-13s %s\n", $$1, $$2 }'
	@echo
	@echo "First run: make setup && make ollama && make dev, then open http://localhost:5173"

setup: ## Create backend/.venv (Python 3.12 via uv), install all dependencies, create .env, seed the DB
	@command -v uv >/dev/null || { echo "uv is required: brew install uv (https://docs.astral.sh/uv/)"; exit 1; }
	@command -v npm >/dev/null || { echo "Node.js 22+ is required: brew install node"; exit 1; }
	@if [ -x $(VENV_PY) ]; then echo "Using the existing $(BACKEND)/.venv"; else uv venv --python 3.12 $(BACKEND)/.venv; fi
	uv pip install --python $(VENV_PY) -r $(BACKEND)/requirements-dev.txt -c $(BACKEND)/constraints.txt
	cd $(FRONTEND) && npm install
	@if [ -f .env ]; then echo ".env exists, leaving it unchanged"; else cp .env.example .env && echo "Created .env from .env.example"; fi
	@$(MAKE) --no-print-directory seed

ollama: ## Install/start Ollama and pull the local model (LLM_MODEL, default qwen3.5:9b); idempotent
	@scripts/setup_ollama.sh

ollama-reset: ## Free the RAM held by the local model (its MLX prompt cache can reach ~8 GiB); restart the app to re-warm
	@source scripts/lib/common.sh && load_env && model="$${LLM_MODEL:-$$QP_DEFAULT_OLLAMA_MODEL}" && \
		ollama stop "$$model" && echo "Unloaded $$model. Restart the backend (make dev or make serve) to re-warm its prompt cache."

seed: ## Rebuild the sample SQLite database (deterministic)
	cd $(BACKEND) && $(PY) -m app.db.seed --reset

dev: ## Run backend (:8000, auto-reload) and frontend (:5173) together; Ctrl+C stops both
	@scripts/dev.sh

backend: ## Run only the FastAPI backend on :8000 with auto-reload
	cd $(BACKEND) && $(PY) -m uvicorn app.main:app --reload --reload-dir app --port 8000

frontend: ## Run only the Vite dev server on :5173 (it proxies /api to :8000)
	cd $(FRONTEND) && npm run dev -- --port 5173 --strictPort

test: ## Run backend tests (offline, LLM_PROVIDER=mock) and frontend unit tests (vitest)
	cd $(BACKEND) && LLM_PROVIDER=mock $(PY) -m pytest $(ARGS)
	cd $(FRONTEND) && CI=true npm test

test-live: ## Run live integration tests against the configured LLM (RUN_LIVE_LLM=1; needs Ollama or a key)
	cd $(BACKEND) && RUN_LIVE_LLM=1 $(PY) -m pytest tests/integration $(ARGS)

lint: ## Lint the backend (ruff) and type-check the frontend (tsc)
	cd $(BACKEND) && .venv/bin/ruff check .
	cd $(FRONTEND) && npm run typecheck

build: ## Build the production frontend into frontend/dist
	cd $(FRONTEND) && npm run build

serve: build ## Build the UI, then serve UI + API from one process on http://localhost:8000 (like production)
	cd $(BACKEND) && STATIC_DIR="$(CURDIR)/$(FRONTEND)/dist" $(PY) -m uvicorn app.main:app --port 8000

evals: ## Run the golden-set evaluation against the configured LLM
	cd $(BACKEND) && $(PY) -m evals.run_evals $(ARGS)

graph: ## Export the LangGraph workflow as Mermaid (docs/graph.mmd)
	cd $(BACKEND) && $(PY) scripts/export_graph.py $(ARGS)

docker-build: ## Build the production Docker image (IMAGE=querypilot:latest)
	@command -v docker >/dev/null || { echo "Docker is not installed: https://docs.docker.com/get-docker/"; exit 1; }
	docker build -t $(IMAGE) .

# Same overrides as docker-compose.yml: Ollama is reached on the host, and paths and the port are pinned
# to the image layout so host values in .env cannot break the container.
docker-run: ## Run the image on :8000 with your .env; the LLM stays on the host (Ollama via host.docker.internal)
	@command -v docker >/dev/null || { echo "Docker is not installed: https://docs.docker.com/get-docker/"; exit 1; }
	docker run --rm --name querypilot -p 8000:8000 $$([ -f .env ] && echo "--env-file .env") \
		-e OLLAMA_BASE_URL=$${DOCKER_OLLAMA_BASE_URL:-http://host.docker.internal:11434} \
		-e DATABASE_PATH= -e STATIC_DIR=/app/static -e PORT=8000 \
		--add-host host.docker.internal:host-gateway $(IMAGE)

clean: ## Remove build output and caches (frontend/dist, __pycache__, .pytest_cache, .ruff_cache)
	rm -rf $(FRONTEND)/dist $(BACKEND)/.pytest_cache $(BACKEND)/.ruff_cache
	find $(BACKEND) -path $(BACKEND)/.venv -prune -o -type d -name __pycache__ -prune -exec rm -rf {} +
