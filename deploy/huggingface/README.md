---
title: QueryPilot
colorFrom: indigo
colorTo: blue
sdk: docker
app_port: 8000
fullWidth: true
pinned: false
short_description: Natural language to validated, read-only SQL
tags:
  - text-to-sql
  - langgraph
  - fastapi
  - sqlite
---

# QueryPilot: SQL Query AI Agent

QueryPilot turns plain-English questions into SQL for a sample company database. It validates every
query before returning it (syntax, tables, columns, joins, read-only), explains it in plain English,
optimizes and debugs SQL you paste in, keeps conversation context for follow-up questions, runs queries
read-only against the bundled SQLite database, and politely refuses anything outside SQL and databases.

This Space runs the project's `Dockerfile`: one container serves the FastAPI API and the React UI on
port 8000. The language model is Groq's free hosted API, because a free CPU Space is far too slow for a
local 9B model.

> This file is the Space card. In the source repository it lives at `deploy/huggingface/README.md` and
> is uploaded as the Space's `README.md`. The full guide, including the primary Render deployment, is
> `docs/DEPLOYMENT.md`.

## Deploy this Space (free, about 15 minutes)

Hugging Face Spaces is the alternative free host. Its free `cpu-basic` hardware (2 vCPU, 16 GB RAM) is
much larger than Render's free instance, and it only goes to sleep after a long period without visitors
(48 hours at the time of writing).

You need:

- a free Hugging Face account and an access token with **write** permission
  (<https://huggingface.co/settings/tokens>);
- a free Groq API key (<https://console.groq.com/keys>);
- the `hf` command-line tool: `uv tool install huggingface_hub` (or `pip install -U huggingface_hub`).

Run these commands from the repository root. Replace `<user>` with your Hugging Face username.

1. **Log in.**

   ```bash
   hf auth login
   ```

2. **Create the Space with its configuration.** The Groq key is read from your environment (`-s`), so it
   never appears in your shell history. The other settings are plain variables (`-e`).

   ```bash
   read -rs GROQ_API_KEY && export GROQ_API_KEY      # paste the key, then press Enter
   hf repos create <user>/querypilot --type space --sdk docker --flavor cpu-basic --public \
     -s GROQ_API_KEY \
     -e LLM_PROVIDER=groq \
     -e LLM_MODEL=openai/gpt-oss-120b \
     -e LLM_FAST_MODEL=openai/gpt-oss-20b \
     -e LLM_FALLBACK_MODELS=qwen/qwen3.8-27b \
     -e RATE_LIMIT_PER_MINUTE=10 \
     -e RATE_LIMIT_PER_DAY=300
   ```

   You can also create the Space in the web UI (**New Space**, SDK **Docker**, template **Blank**,
   hardware **CPU basic**) and add the same values under **Settings > Variables and secrets**, with
   `GROQ_API_KEY` as a **secret**.

3. **Upload the Space card first**, so the Space knows the app listens on port 8000:

   ```bash
   hf upload <user>/querypilot deploy/huggingface/README.md README.md --type space
   ```

4. **Upload the code the image needs.** These patterns upload the Dockerfile, `backend/` and `frontend/`,
   and skip local environments, build output, caches, the database and `.env` files:

   ```bash
   hf upload <user>/querypilot . . --type space \
     --include "Dockerfile" --include ".dockerignore" --include "backend/*" --include "frontend/*" \
     --exclude "*/.venv/*" --exclude "*/node_modules/*" --exclude "*/__pycache__/*" --exclude "*_cache/*" \
     --exclude "frontend/dist/*" --exclude "backend/data/*.db*" --exclude "backend/evals/results/*" \
     --exclude "*.env" --exclude "*.env.*" --exclude "*.DS_Store" \
     --commit-message "Deploy QueryPilot"
   ```

5. **Wait for the build** (about 5 minutes) on the Space page, or from the terminal:

   ```bash
   hf spaces wait <user>/querypilot --timeout 15m   # exits 0 once the Space is RUNNING
   hf spaces logs <user>/querypilot --build         # the build log, if it ends in BUILD_ERROR
   hf spaces logs <user>/querypilot                 # the app's log, if it ends in RUNTIME_ERROR
   ```

6. **Check it.** Open `https://<user>-querypilot.hf.space/api/health`. It should report
   `"provider": "groq"` and `"configured": true`. Then open the Space and try "Show all employees hired
   after January 2024."

To deploy a new version, repeat step 4. Each upload is a new commit and triggers a rebuild.

## Notes

- **Port and user.** The image listens on `PORT` (default 8000, matching `app_port` above) and runs as
  the unprivileged user with uid 1000, which is what Spaces expects.
- **Memory.** Conversation memory lives in the container's process memory. After the Space sleeps or
  restarts, the browser re-sends recent turns and the server rebuilds the conversation from them.
- **Rate limits.** `RATE_LIMIT_PER_MINUTE` and `RATE_LIMIT_PER_DAY` are per-client limits that protect
  the shared free Groq quota. Requests over the limit get HTTP 429 with a `retry_after` hint.
- **Secrets.** Never put the Groq key in the code, the Space card or a variable. Store it only as a
  Space secret.
