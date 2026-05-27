#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="${PROJECT_ROOT:-/home/from0to01/open-trading-api}"
PORT="${PORT:-8001}"

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
export KIS_TOKEN_ROOT="${KIS_TOKEN_ROOT:-$HOME/KIS/config-vps}"
export KIS_MODE_FILE="${KIS_MODE_FILE:-$KIS_TOKEN_ROOT/KIS_MODE}"
export KIS_RUNTIME_DIR="${KIS_RUNTIME_DIR:-$PROJECT_ROOT/strategy_builder/.runtime-vps}"
export KIS_DEFAULT_MODE="vps"

mkdir -p "$KIS_TOKEN_ROOT" "$KIS_RUNTIME_DIR"
printf "vps" > "$KIS_MODE_FILE"

cd "$PROJECT_ROOT/strategy_builder"
exec "$UV_BIN" run uvicorn backend.main:app --host 127.0.0.1 --port "$PORT"
