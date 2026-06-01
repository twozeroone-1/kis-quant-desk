#!/usr/bin/env bash
set -euo pipefail

slot="${1:?slot required}"
today="$(TZ=Asia/Seoul date +%Y%m%d)"
weekday="$(TZ=Asia/Seoul date +%u)"
export KR_MARKET_LLM_MODE="${KR_MARKET_LLM_MODE:-live-vps}"
PROJECT_ROOT="/home/from0to01/open-trading-api"
LOG_DIR="$PROJECT_ROOT/.codex/runtime/kr_market_auto"
UV_BIN="/home/from0to01/.local/bin/uv"
LOCAL_ENV="$PROJECT_ROOT/.codex/local/kr_market_auto.env"
if [[ -f "$LOCAL_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$LOCAL_ENV"
fi
mkdir -p "$LOG_DIR"

if [[ "$weekday" -gt 5 ]]; then
  echo "skip: weekend today=$today slot=$slot"
  exit 0
fi

if ! (cd "$PROJECT_ROOT" && "$UV_BIN" run "$PROJECT_ROOT/.codex/scripts/kr_market_calendar.py" --date "$today" --check-open) >> "$LOG_DIR/calendar_daily.log" 2>&1; then
  echo "skip: market closed today=$today slot=$slot"
  exit 0
fi

exec "$PROJECT_ROOT/.codex/scripts/run_kr_market_auto_once.sh" "$slot" "$today" "$today"
