# shellcheck shell=bash disable=SC2034  # the variables below are used by the scripts that source this file
# Shared helpers for the developer scripts in scripts/. Source this file; do not execute it.
# Written for the macOS system bash 3.2: no associative arrays, no ${var,,}, no `wait -n`.

# Absolute path of the repository root (this file lives in <root>/scripts/lib).
QP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

# Defaults shared with backend/app/config.py and .env.example.
QP_DEFAULT_PROVIDER="ollama"
QP_DEFAULT_OLLAMA_MODEL="qwen3.5:9b"
QP_DEFAULT_OLLAMA_URL="http://localhost:11434"

# Colors only when stdout is a terminal and NO_COLOR (https://no-color.org) is unset.
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  QP_RED=$'\033[31m' QP_GREEN=$'\033[32m' QP_YELLOW=$'\033[33m' QP_BLUE=$'\033[34m'
  QP_MAGENTA=$'\033[35m' QP_CYAN=$'\033[36m' QP_BOLD=$'\033[1m' QP_RESET=$'\033[0m'
else
  QP_RED='' QP_GREEN='' QP_YELLOW='' QP_BLUE='' QP_MAGENTA='' QP_CYAN='' QP_BOLD='' QP_RESET=''
fi

log_info() { printf '%s==>%s %s\n' "$QP_BLUE" "$QP_RESET" "$*"; }
log_ok() { printf '%s ok%s %s\n' "$QP_GREEN" "$QP_RESET" "$*"; }
log_warn() { printf '%swarning:%s %s\n' "$QP_YELLOW" "$QP_RESET" "$*" >&2; }
die() {
  printf '%serror:%s %s\n' "$QP_RED" "$QP_RESET" "$*" >&2
  exit 1
}

# load_env_file FILE
# Read the KEY=VALUE pairs of a dotenv file into shell variables, with the precedence the backend uses
# (pydantic-settings with python-dotenv): variables that are already set win, and within the file the
# last occurrence of a key wins. Nothing is exported: the backend reads the same files itself, so its
# parser stays the single source of truth for the app. The file is parsed, never executed: no command
# substitution or variable expansion happens. Supported syntax: comments, blank lines, an optional
# `export ` prefix, single- or double-quoted values (text after the closing quote is ignored), and
# ` # comments` after unquoted values.
load_env_file() {
  local file="$1" line key value from_file=" "
  [ -f "$file" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    case "$line" in '' | '#'*) continue ;; esac
    line="${line#export }"
    case "$line" in *=*) ;; *) continue ;; esac
    key="${line%%=*}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${line#*=}"
    value="${value#"${value%%[![:space:]]*}"}"
    case "$key" in
      '' | [0-9]* | *[!A-Za-z0-9_]*)
        log_warn "ignoring malformed line in $file: $line"
        continue
        ;;
    esac
    case "$value" in
      \"*)
        value="${value#\"}"
        value="${value%%\"*}"
        ;;
      \'*)
        value="${value#\'}"
        value="${value%%\'*}"
        ;;
      *)
        value="${value%%[[:space:]]#*}"
        value="${value%"${value##*[![:space:]]}"}"
        ;;
    esac
    case "$from_file" in
      *" $key "*) printf -v "$key" '%s' "$value" ;;
      *)
        if [ -z "${!key+x}" ]; then
          printf -v "$key" '%s' "$value"
          from_file="$from_file$key "
        fi
        ;;
    esac
  done <"$file"
}

# load_env: read the dotenv files the backend reads (backend/app/config.py), in its order of precedence:
# the environment, then backend/.env, then .env in the repository root.
load_env() {
  load_env_file "$QP_ROOT/backend/.env"
  load_env_file "$QP_ROOT/.env"
}

# ollama_reachable BASE_URL: succeed when an Ollama server answers at BASE_URL.
ollama_reachable() {
  curl -fsS --max-time 3 "$1/api/version" >/dev/null 2>&1
}

# ollama_has_model BASE_URL MODEL: succeed when MODEL is already pulled (never triggers a download).
ollama_has_model() {
  curl -fsS --max-time 10 -X POST "$1/api/show" -H 'Content-Type: application/json' \
    -d "{\"model\":\"$2\"}" >/dev/null 2>&1
}
