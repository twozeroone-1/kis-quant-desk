#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/from0to01/open-trading-api"
LOG_DIR="$PROJECT_ROOT/.codex/runtime/kr_market_auto_prod"
UV_BIN="/home/from0to01/.local/bin/uv"
COMMON_ENV="$PROJECT_ROOT/.codex/local/kr_market_auto.env"
PROD_ENV="$PROJECT_ROOT/.codex/local/kr_market_auto_prod.env"
APPROVAL_DIR="$LOG_DIR/approvals"
APPROVAL_VALUE="I_UNDERSTAND_REAL_ORDERS"

if [[ -f "$COMMON_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$COMMON_ENV"
fi
if [[ -f "$PROD_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$PROD_ENV"
fi

export KIS_CONFIG_ROOT="/home/from0to01/KIS/config"
export KIS_TOKEN_ROOT="/home/from0to01/.local/state/kis-stack/token-prod"
export KIS_MODE_FILE="$KIS_TOKEN_ROOT/KIS_MODE"
export KIS_RUNTIME_DIR="/home/from0to01/.local/state/kis-stack/runtime-prod"
export KIS_DEFAULT_MODE="prod"
export KIS_LOCK_MODE="prod"
export KIS_TRADE_MODE="prod"
export KR_MARKET_LLM_MODE="${KR_MARKET_LLM_MODE:-live-prod}"
export KR_MARKET_TOTAL_BUY_PCT="${KR_MARKET_TOTAL_BUY_PCT:-100}"
export KR_MARKET_DAILY_LOSS_PCT="${KR_MARKET_DAILY_LOSS_PCT:-3}"
unset KIS_PROD_AUTO_CONFIRM

mkdir -p "$LOG_DIR" "$APPROVAL_DIR"

slot="${1:?slot required}"
scheduled_date="${2:?YYYYMMDD scheduled local date required}"
session_date="${3:-$scheduled_date}"
today="$(TZ=Asia/Seoul date +%Y%m%d)"

if [[ "$today" != "$scheduled_date" ]]; then
  echo "skip: today=$today scheduled=$scheduled_date session=$session_date"
  exit 0
fi

approval_file="$APPROVAL_DIR/${session_date}_${slot}.approved"
if [[ -f "$approval_file" ]] && grep -qx "$APPROVAL_VALUE" "$approval_file"; then
  export KIS_PROD_AUTO_CONFIRM="$APPROVAL_VALUE"
  rm -f "$approval_file"
  echo "prod approval consumed: session=$session_date slot=$slot"
else
  echo "warning: one-time prod approval not found; prod run will generate a report but submit no orders"
  echo "approval file required: $approval_file"
fi

status="$(curl -fsS http://127.0.0.1:8000/api/auth/status 2>/dev/null || true)"
if [[ -z "$status" ]]; then
  setsid bash -c "cd '$PROJECT_ROOT/strategy_builder' && export KIS_LOCK_MODE=prod KIS_DEFAULT_MODE=prod KIS_TOKEN_ROOT='$KIS_TOKEN_ROOT' KIS_MODE_FILE='$KIS_MODE_FILE' KIS_RUNTIME_DIR='$KIS_RUNTIME_DIR' && exec '$UV_BIN' run uvicorn backend.main:app --host 127.0.0.1 --port 8000" \
    >> "$LOG_DIR/backend.log" 2>&1 < /dev/null &
  sleep 5
else
  if ! printf '%s' "$status" | grep -q '"mode"[[:space:]]*:[[:space:]]*"prod"'; then
    echo "error: strategy_builder backend is already running but is not in prod mode: $status" >&2
    exit 1
  fi
fi

cd "$PROJECT_ROOT/strategy_builder"
"$UV_BIN" run "$PROJECT_ROOT/.codex/scripts/kr_market_auto_run.py" \
  --slot "$slot" \
  --date "$session_date" \
  --trade-mode prod
