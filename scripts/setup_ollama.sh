#!/usr/bin/env bash
# Prepare the local LLM for QueryPilot. Every step is skipped when it is already done, so re-running is
# fast and safe:
#   1. install the Ollama CLI (Homebrew when available, otherwise print instructions);
#   2. start the Ollama server (brew services, the macOS app, or `ollama serve` in the background);
#   3. pull the model (LLM_MODEL from the environment or .env, default qwen3.5:9b) if it is missing;
#   4. run a tiny smoke prompt and a tool-calling check (the app relies on tool calling).
# Models are never deleted or modified.
set -euo pipefail

# shellcheck source=SCRIPTDIR/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

SERVE_LOG="${TMPDIR:-/tmp}/querypilot-ollama-serve.log"
SMOKE=1
MODEL_ARG=""
BASE_URL=""
MODELS=()

usage() {
  cat <<'EOF'
Usage: scripts/setup_ollama.sh [--no-smoke] [MODEL]

Installs and starts Ollama, pulls MODEL if it is missing, and smoke-tests it.
MODEL defaults to LLM_MODEL (environment or .env), else qwen3.5:9b. When LLM_FAST_MODEL is set to a
different model, that model is pulled as well. OLLAMA_BASE_URL selects the server
(default http://localhost:11434).

Options:
  --no-smoke   skip the smoke prompt and the tool-calling check
  -h, --help   show this help
EOF
}

parse_args() {
  while [ "$#" -gt 0 ]; do
    case "$1" in
      --no-smoke) SMOKE=0 ;;
      -h | --help)
        usage
        exit 0
        ;;
      -*) die "unknown option: $1 (see --help)" ;;
      *)
        [ -z "$MODEL_ARG" ] || die "only one MODEL argument is allowed"
        MODEL_ARG="$1"
        ;;
    esac
    shift
  done
}

resolve_models() {
  local provider="${LLM_PROVIDER:-$QP_DEFAULT_PROVIDER}"
  if [ -n "$MODEL_ARG" ]; then
    MODELS=("$MODEL_ARG")
  elif [ "$provider" = "ollama" ]; then
    MODELS=("${LLM_MODEL:-$QP_DEFAULT_OLLAMA_MODEL}")
    if [ -n "${LLM_FAST_MODEL:-}" ] && [ "$LLM_FAST_MODEL" != "${MODELS[0]}" ]; then
      MODELS+=("$LLM_FAST_MODEL")
    fi
  else
    log_warn "LLM_PROVIDER is '$provider', so the app will not use Ollama until LLM_PROVIDER=ollama."
    log_warn "Preparing the default local model $QP_DEFAULT_OLLAMA_MODEL anyway."
    MODELS=("$QP_DEFAULT_OLLAMA_MODEL")
  fi
}

ensure_cli() {
  if command -v ollama >/dev/null 2>&1; then
    log_ok "Ollama CLI found at $(command -v ollama)"
  elif command -v brew >/dev/null 2>&1; then
    log_info "Installing Ollama with Homebrew (brew install ollama)..."
    brew install ollama
  else
    die "Ollama is not installed. Get it from https://ollama.com/download" \
      "(Linux: curl -fsSL https://ollama.com/install.sh | sh), then re-run this script."
  fi
}

is_local_url() {
  case "$1" in
    http://localhost | http://localhost:* | http://127.0.0.1 | http://127.0.0.1:*) return 0 ;;
    *) return 1 ;;
  esac
}

server_version() {
  curl -fsS --max-time 3 "$BASE_URL/api/version" 2>/dev/null | sed -n 's/.*"version":"\([^"]*\)".*/\1/p'
}

ensure_server() {
  if ollama_reachable "$BASE_URL"; then
    log_ok "Ollama server $(server_version) is running at $BASE_URL"
    return 0
  fi
  is_local_url "$BASE_URL" ||
    die "No Ollama server answers at $BASE_URL. Start it on that host or fix OLLAMA_BASE_URL."

  if command -v brew >/dev/null 2>&1 && brew list --formula ollama >/dev/null 2>&1 &&
    [ "${BASE_URL##*:}" = "11434" ]; then
    log_info "Starting Ollama: brew services start ollama"
    brew services start ollama >/dev/null
  elif [ -d /Applications/Ollama.app ]; then
    log_info "Starting the Ollama app"
    open -a Ollama
  else
    log_info "Starting 'ollama serve' in the background (log: $SERVE_LOG)"
    OLLAMA_HOST="$BASE_URL" nohup ollama serve >"$SERVE_LOG" 2>&1 &
  fi

  local _
  for _ in $(seq 1 60); do
    if ollama_reachable "$BASE_URL"; then
      log_ok "Ollama server $(server_version) is running at $BASE_URL"
      return 0
    fi
    sleep 0.5
  done
  die "Ollama did not come up at $BASE_URL within 30 s (check $SERVE_LOG or 'brew services info ollama')."
}

ensure_model() {
  local model="$1"
  if ollama_has_model "$BASE_URL" "$model"; then
    log_ok "Model $model is already pulled"
    return 0
  fi
  log_info "Pulling $model (one-time download; qwen3.5:9b is about 7.6 GB)..."
  OLLAMA_HOST="$BASE_URL" ollama pull "$model"
  log_ok "Model $model pulled"
}

# chat MODEL PROMPT [EXTRA_JSON]: send one non-streaming chat request with thinking disabled (as the app
# does) and print the raw JSON response. EXTRA_JSON is appended to the request object, e.g. tools.
chat() {
  local body
  body="{\"model\":\"$1\",\"stream\":false,\"think\":false,\"options\":{\"temperature\":0,\"num_predict\":64},"
  body+="\"messages\":[{\"role\":\"user\",\"content\":\"$2\"}]${3:-}}"
  curl -fsS --max-time 300 -X POST "$BASE_URL/api/chat" -H 'Content-Type: application/json' -d "$body"
}

json_string() { sed -n "s/.*\"$1\":\"\([^\"]*\)\".*/\1/p" | head -n 1; }
json_number() { sed -n "s/.*\"$1\":\([0-9][0-9]*\).*/\1/p" | head -n 1; }
seconds() { awk -v ns="${1:-0}" 'BEGIN { printf "%.1f", ns / 1e9 }'; }

smoke_test() {
  local model="$1" response reply ping_tool
  log_info "Smoke prompt to $model (the first call after a restart loads the model into memory)..."
  response="$(chat "$model" "Reply with the single word OK.")" ||
    die "The smoke prompt failed. Check 'ollama ps' and the Ollama server log."
  reply="$(printf '%s' "$response" | json_string content)"
  log_ok "Model replied \"${reply}\" in $(seconds "$(printf '%s' "$response" | json_number total_duration)") s" \
    "(model load $(seconds "$(printf '%s' "$response" | json_number load_duration)") s)"

  ping_tool=',"tools":[{"type":"function","function":{"name":"ping","description":"Health check.",'
  ping_tool+='"parameters":{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}}}]'
  response="$(chat "$model" "Call the ping tool with ok set to true." "$ping_tool")" ||
    die "The tool-calling check failed. Check 'ollama ps' and the Ollama server log."
  if printf '%s' "$response" | grep -q '"tool_calls"'; then
    log_ok "Tool calling works (the app uses it for structured output)"
  else
    log_warn "$model answered without a tool call; the app will fall back to plain-JSON extraction."
  fi
}

main() {
  parse_args "$@"
  load_env
  BASE_URL="${OLLAMA_BASE_URL:-$QP_DEFAULT_OLLAMA_URL}"
  BASE_URL="${BASE_URL%/}"
  resolve_models

  ensure_cli
  ensure_server
  local model
  for model in "${MODELS[@]}"; do
    ensure_model "$model"
  done
  if [ "$SMOKE" -eq 1 ]; then
    smoke_test "${MODELS[0]}"
  fi
  log_ok "${QP_BOLD}Ollama is ready.${QP_RESET} Next: make dev"
}

main "$@"
