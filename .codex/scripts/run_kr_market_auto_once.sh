#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/from0to01/open-trading-api"
LOG_DIR="$PROJECT_ROOT/.codex/runtime/kr_market_auto"
UV_BIN="/home/from0to01/.local/bin/uv"
LOCAL_ENV="$PROJECT_ROOT/.codex/local/kr_market_auto.env"
if [[ -f "$LOCAL_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$LOCAL_ENV"
fi
export KIS_CONFIG_ROOT="/home/from0to01/KIS/config"
export KIS_TOKEN_ROOT="/home/from0to01/.local/state/kis-stack/token-vps"
export KIS_MODE_FILE="$KIS_TOKEN_ROOT/KIS_MODE"
export KIS_RUNTIME_DIR="/home/from0to01/.local/state/kis-stack/runtime-vps"
export KIS_DEFAULT_MODE="vps"
export KIS_LOCK_MODE="vps"
mkdir -p "$LOG_DIR"

slot="${1:?slot required}"
scheduled_date="${2:?YYYYMMDD scheduled local date required}"
session_date="${3:-$scheduled_date}"
today="$(date +%Y%m%d)"

if [[ "$today" != "$scheduled_date" ]]; then
  echo "skip: today=$today scheduled=$scheduled_date session=$session_date"
  exit 0
fi

if ! curl -fsS http://127.0.0.1:8000/api/auth/status >/dev/null 2>&1; then
  setsid bash -c "cd '$PROJECT_ROOT/strategy_builder' && export KIS_LOCK_MODE=vps KIS_DEFAULT_MODE=vps && exec '$UV_BIN' run uvicorn backend.main:app --host 127.0.0.1 --port 8000" \
    >> "$LOG_DIR/backend.log" 2>&1 < /dev/null &
  sleep 5
fi

cd "$PROJECT_ROOT/strategy_builder"
"$UV_BIN" run "$PROJECT_ROOT/.codex/scripts/kr_market_auto_run.py" --slot "$slot" --date "$session_date"
