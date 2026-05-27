#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/from0to01/open-trading-api}"
PORT="${PORT:-8000}"

if [[ -z "${UV_BIN:-}" ]]; then
  if command -v uv >/dev/null 2>&1; then
    UV_BIN="$(command -v uv)"
  elif [[ -x "$HOME/.local/bin/uv" ]]; then
    UV_BIN="$HOME/.local/bin/uv"
  else
    echo "uv not found. Set UV_BIN=/path/to/uv." >&2
    exit 127
  fi
fi

export KIS_CONFIG_ROOT="${KIS_CONFIG_ROOT:-$HOME/KIS/config}"
export KIS_TOKEN_ROOT="${KIS_TOKEN_ROOT:-$HOME/KIS/config-prod}"
export KIS_MODE_FILE="${KIS_MODE_FILE:-$KIS_TOKEN_ROOT/KIS_MODE}"
export KIS_RUNTIME_DIR="${KIS_RUNTIME_DIR:-$PROJECT_ROOT/strategy_builder/.runtime-prod}"
export KIS_DEFAULT_MODE="prod"

mkdir -p "$KIS_TOKEN_ROOT" "$KIS_RUNTIME_DIR"
printf "prod" > "$KIS_MODE_FILE"

if [[ ! -f "$KIS_RUNTIME_DIR/protective_orders.json" && -f "$PROJECT_ROOT/strategy_builder/.runtime/protective_orders.json" ]]; then
  cp "$PROJECT_ROOT/strategy_builder/.runtime/protective_orders.json" "$KIS_RUNTIME_DIR/protective_orders.json"
fi

cd "$PROJECT_ROOT/strategy_builder"
exec "$UV_BIN" run uvicorn backend.main:app --host 127.0.0.1 --port "$PORT"
