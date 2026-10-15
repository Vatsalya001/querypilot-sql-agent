#!/usr/bin/env bash
# Run the QueryPilot dev stack: FastAPI (uvicorn --reload) on :8000 and Vite on :5173, which proxies
# /api to the backend. Each output line is prefixed with [api] or [web]; Ctrl+C stops both servers.
#
# Usage: scripts/dev.sh        (or: make dev)
set -euo pipefail

# shellcheck source=SCRIPTDIR/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

API_PORT=8000
WEB_PORT=5173
BACKEND_DIR="$QP_ROOT/backend"
FRONTEND_DIR="$QP_ROOT/frontend"
PY="$BACKEND_DIR/.venv/bin/python"
API_PID=""
WEB_PID=""

check_port() {
  local port="$1" pid
  command -v lsof >/dev/null 2>&1 || return 0
  pid="$(lsof -nP -iTCP:"$port" -sTCP:LISTEN -t 2>/dev/null | head -n 1 || true)"
  if [ -n "$pid" ]; then
    die "port $port is already in use by PID $pid ($(ps -o comm= -p "$pid" 2>/dev/null || echo unknown))." \
      "Stop that process (kill $pid) and retry."
  fi
}

# Warn (never fail) when the configured local LLM is not ready: the UI still starts and shows the error.
check_llm() {
  local provider="${LLM_PROVIDER:-$QP_DEFAULT_PROVIDER}" base model models
  if [ "$provider" != "ollama" ]; then
    log_info "LLM provider: $provider (${LLM_MODEL:-default model})"
    return 0
  fi
  base="${OLLAMA_BASE_URL:-$QP_DEFAULT_OLLAMA_URL}"
  base="${base%/}"
  if ! ollama_reachable "$base"; then
    log_warn "Ollama is not reachable at $base, so chat requests will fail until it runs. Fix: make ollama"
    return 0
  fi
  models="${LLM_MODEL:-$QP_DEFAULT_OLLAMA_MODEL}"
  if [ -n "${LLM_FAST_MODEL:-}" ] && [ "$LLM_FAST_MODEL" != "$models" ]; then
    models="$models $LLM_FAST_MODEL"
  fi
  for model in $models; do
    if ollama_has_model "$base" "$model"; then
      log_ok "Ollama is running at $base with $model"
    else
      log_warn "Ollama model $model is not pulled, so chat requests will fail. Fix: make ollama"
    fi
  done
}

check_prerequisites() {
  [ -x "$PY" ] || die "backend/.venv is missing. Run: make setup"
  [ -d "$FRONTEND_DIR/node_modules" ] || die "frontend/node_modules is missing. Run: make setup"
  check_port "$API_PORT"
  check_port "$WEB_PORT"
  check_llm
}

# prefix_output TAG COLOR: copy stdin to stdout with "[TAG] " in front of every line, flushing per line.
# It ignores SIGINT so the servers' shutdown messages still get printed after Ctrl+C.
prefix_output() {
  trap '' INT
  awk -v tag="$2[$1]$QP_RESET " '{ print tag $0; fflush() }'
}

start_servers() {
  local color_flag=()
  if [ -n "$QP_RESET" ]; then
    color_flag=(--use-colors)
    export FORCE_COLOR=1
  fi
  # The prefixers are children of this shell (fds 3 and 4), not of the servers, so stopping a server's
  # process tree never cuts off its log output. Servers read stdin from /dev/null so they never compete
  # for the terminal.
  exec 3> >(prefix_output api "$QP_CYAN")
  (cd "$BACKEND_DIR" && exec "$PY" -m uvicorn app.main:app --reload --reload-dir app \
    --host 127.0.0.1 --port "$API_PORT" "${color_flag[@]+"${color_flag[@]}"}") </dev/null >&3 2>&1 3>&- &
  API_PID=$!

  exec 4> >(prefix_output web "$QP_MAGENTA")
  (cd "$FRONTEND_DIR" && exec npm run dev -- --port "$WEB_PORT" --strictPort) </dev/null >&4 2>&1 3>&- 4>&- &
  WEB_PID=$!
}

wait_until_ready() {
  local _
  for _ in $(seq 1 240); do
    kill -0 "$API_PID" 2>/dev/null || die "the backend exited during startup (see the [api] lines above)"
    kill -0 "$WEB_PID" 2>/dev/null || die "the frontend exited during startup (see the [web] lines above)"
    if curl -fsS --max-time 2 "http://127.0.0.1:$API_PORT/api/health" >/dev/null 2>&1 &&
      curl -fsS --max-time 2 "http://localhost:$WEB_PORT/" >/dev/null 2>&1; then
      printf '\n'
      log_ok "${QP_BOLD}QueryPilot is running${QP_RESET}"
      printf '     UI:   http://localhost:%s\n' "$WEB_PORT"
      printf '     API:  http://localhost:%s/api/health\n' "$API_PORT"
      printf '     Press Ctrl+C to stop both servers.\n\n'
      return 0
    fi
    sleep 0.5
  done
  log_warn "the servers are not answering after 2 minutes; still running, check the logs above"
}

# Exit as soon as either server stops (bash 3.2 has no `wait -n`, so poll).
supervise() {
  while kill -0 "$API_PID" 2>/dev/null && kill -0 "$WEB_PID" 2>/dev/null; do
    sleep 1
  done
  if kill -0 "$API_PID" 2>/dev/null; then
    die "the frontend dev server stopped; shutting down the backend"
  fi
  die "the backend stopped; shutting down the frontend"
}

# process_tree PID: print PID and all of its descendants (collected before anything is killed, because
# orphans are re-parented and could no longer be found).
# shellcheck disable=SC2329 # called from cleanup
process_tree() {
  local child
  kill -0 "$1" 2>/dev/null || return 0
  printf '%s ' "$1"
  for child in $(pgrep -P "$1" 2>/dev/null || true); do
    process_tree "$child"
  done
}

# Stop both servers (EXIT trap): SIGTERM every process in their trees, then SIGKILL whatever is left.
# shellcheck disable=SC2329 # invoked through `trap cleanup EXIT`
cleanup() {
  local status=$? pids="" survivors="" pid _
  set +e
  trap - EXIT INT TERM HUP
  for pid in $API_PID $WEB_PID; do
    pids="$pids $(process_tree "$pid")"
  done
  pids="$(printf '%s' "$pids" | xargs)"
  if [ -n "$pids" ]; then
    printf '\n'
    log_info "Stopping the dev servers..."
    # shellcheck disable=SC2086 # word splitting of the PID list is intended
    kill -TERM $pids 2>/dev/null
    for _ in $(seq 1 40); do
      survivors=""
      for pid in $pids; do
        if kill -0 "$pid" 2>/dev/null; then survivors="$survivors $pid"; fi
      done
      if [ -z "$survivors" ]; then break; fi
      sleep 0.25
    done
    if [ -n "$survivors" ]; then
      # shellcheck disable=SC2086
      kill -KILL $survivors 2>/dev/null
    fi
  fi
  exec 3>&- 4>&-
  exit "$status"
}

main() {
  load_env
  check_prerequisites
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  trap 'exit 129' HUP
  log_info "Starting the backend on :$API_PORT and the frontend on :$WEB_PORT..."
  start_servers
  wait_until_ready
  supervise
}

main "$@"
