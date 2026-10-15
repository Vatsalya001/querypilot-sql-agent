# syntax=docker/dockerfile:1

# QueryPilot production image: one container serves the FastAPI API and the built React UI from the same
# origin. Configure it with environment variables (see .env.example and docs/DEPLOYMENT.md):
#   docker build -t querypilot .
#   docker run --rm -p 8000:8000 -e LLM_PROVIDER=mock querypilot      # offline smoke test, no LLM
#   docker run --rm -p 8000:8000 -e LLM_PROVIDER=groq -e LLM_MODEL=openai/gpt-oss-120b \
#     -e LLM_FAST_MODEL=openai/gpt-oss-20b -e GROQ_API_KEY querypilot  # passes the key from your shell

# ---- Stage 1: build the frontend ---------------------------------------------------------------------
FROM node:22-alpine AS frontend
WORKDIR /build/frontend
# Dependencies first, so this layer stays cached until package.json or the lockfile changes.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: Python runtime -------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Unprivileged runtime user; uid 1000 is also what Hugging Face Spaces runs containers as.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app

# Dependencies before code, so this layer stays cached until requirements.txt changes.
COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/app ./app
COPY backend/scripts ./scripts
COPY backend/evals ./evals

# Precompile the app (the runtime user cannot write __pycache__, so cold starts would otherwise recompile
# it) and bake the deterministic sample database into /app/data/company.db. Both stay owned by root: the
# runtime user can read the database but never modify it (on top of the read-only connection and the
# SQLite authorizer).
RUN python -m compileall -q app \
    && python -m app.db.seed --reset

COPY --from=frontend /build/frontend/dist ./static

# The UI is served from the same origin as the API, so no cross-origin access is allowed by default.
ENV STATIC_DIR=/app/static \
    CORS_ORIGINS="" \
    PORT=8000

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('PORT', '8000'), timeout=4)" || exit 1

# A single worker on purpose: conversation memory and rate-limit counters live in process memory.
# Hosts such as Render inject PORT. Behind their proxy, --proxy-headers restores the client address
# from X-Forwarded-For. `exec` makes uvicorn PID 1 so it receives SIGTERM and shuts down cleanly.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --proxy-headers --forwarded-allow-ips='*'"]
