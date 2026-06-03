#!/usr/bin/env bash
set -euo pipefail

slot="${1:?slot required}"
today="$(TZ=Asia/Seoul date +%Y%m%d)"
weekday="$(TZ=Asia/Seoul date +%u)"
PROJECT_ROOT="/home/from0to01/open-trading-api"
LOG_DIR="$PROJECT_ROOT/.codex/runtime/kr_market_auto_prod"
UV_BIN="/home/from0to01/.local/bin/uv"
COMMON_ENV="$PROJECT_ROOT/.codex/local/kr_market_auto.env"
PROD_ENV="$PROJECT_ROOT/.codex/local/kr_market_auto_prod.env"

if [[ -f "$COMMON_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$COMMON_ENV"
fi
if [[ -f "$PROD_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$PROD_ENV"
fi

export KIS_TRADE_MODE="prod"
export KR_MARKET_LLM_MODE="${KR_MARKET_LLM_MODE:-off}"
export KR_MARKET_TOTAL_BUY_PCT="${KR_MARKET_TOTAL_BUY_PCT:-100}"
export KR_MARKET_DAILY_LOSS_PCT="${KR_MARKET_DAILY_LOSS_PCT:-3}"
mkdir -p "$LOG_DIR"

if [[ "$weekday" -gt 5 ]]; then
  echo "skip: weekend today=$today slot=$slot"
  exit 0
fi

if ! (cd "$PROJECT_ROOT" && "$UV_BIN" run "$PROJECT_ROOT/.codex/scripts/kr_market_calendar.py" --date "$today" --check-open) >> "$LOG_DIR/calendar_daily.log" 2>&1; then
  echo "skip: market closed today=$today slot=$slot"
  exit 0
fi

exec "$PROJECT_ROOT/.codex/scripts/run_kr_market_auto_prod_once.sh" "$slot" "$today" "$today"
