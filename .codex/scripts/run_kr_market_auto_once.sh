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
API_BASE="${KIS_VPS_STRATEGY_API:-http://127.0.0.1:8081}"
export KIS_STRATEGY_API="$API_BASE"
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
run_id="${4:-}"
shift $(( $# >= 4 ? 4 : $# ))
extra_args=("$@")
today="$(date +%Y%m%d)"

if [[ "$today" != "$scheduled_date" ]]; then
  echo "skip: today=$today scheduled=$scheduled_date session=$session_date"
  exit 0
fi

status="$(curl -fsS "$API_BASE/api/auth/status" 2>/dev/null || true)"
if [[ -z "$status" ]]; then
  (cd "$PROJECT_ROOT" && docker compose --env-file .env.production -f compose.yml up -d builder-backend-vps builder-frontend caddy) \
    >> "$LOG_DIR/backend.log" 2>&1
  sleep 5
  status="$(curl -fsS "$API_BASE/api/auth/status" 2>/dev/null || true)"
fi
if ! printf '%s' "$status" | grep -q '"mode"[[:space:]]*:[[:space:]]*"vps"'; then
  echo "error: Strategy Builder vps endpoint is not available at $API_BASE: $status" >&2
  exit 1
fi

lock_file="$LOG_DIR/kr_market_auto.lock"
exec 9>"$lock_file"
if ! flock -n 9; then
  if [[ -n "$run_id" ]]; then
    cd "$PROJECT_ROOT/strategy_builder"
    "$UV_BIN" run "$PROJECT_ROOT/.codex/scripts/kr_market_auto_run.py" \
      --slot "$slot" --date "$session_date" --run-id "$run_id" \
      --record-skip skipped_overlap
  fi
  echo "skip: overlapping KR automation run session=$session_date run_id=${run_id:-legacy}"
  exit 0
fi

cd "$PROJECT_ROOT/strategy_builder"
args=(--slot "$slot" --date "$session_date")
if [[ -n "$run_id" ]]; then
  args+=(--run-id "$run_id")
fi
args+=("${extra_args[@]}")
"$UV_BIN" run "$PROJECT_ROOT/.codex/scripts/kr_market_auto_run.py" "${args[@]}"
