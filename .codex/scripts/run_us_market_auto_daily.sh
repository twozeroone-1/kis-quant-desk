#!/usr/bin/env bash
set -euo pipefail

slot="${1:?slot required}"
today="$(TZ=Asia/Seoul date +%Y%m%d)"
PROJECT_ROOT="/home/from0to01/open-trading-api"
LOG_DIR="$PROJECT_ROOT/.codex/runtime/us_market_auto"
UV_BIN="/home/from0to01/.local/bin/uv"
LOCAL_ENV="$PROJECT_ROOT/.codex/local/kr_market_auto.env"
if [[ -f "$LOCAL_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$LOCAL_ENV"
fi
export US_MARKET_LLM_MODE="${US_MARKET_LLM_MODE:-live-vps}"
mkdir -p "$LOG_DIR"

case "$slot" in
  open)
    session_date="$today"
    ;;
  mid|close)
    session_date="$(TZ=Asia/Seoul date -d "$today -1 day" +%Y%m%d)"
    ;;
  *)
    echo "invalid slot: $slot" >&2
    exit 2
    ;;
esac

if ! (cd "$PROJECT_ROOT" && "$UV_BIN" run "$PROJECT_ROOT/.codex/scripts/us_market_calendar.py" --date "$session_date" --check-open) >> "$LOG_DIR/calendar_daily.log" 2>&1; then
  echo "skip: US market closed session=$session_date local=$today slot=$slot"
  exit 0
fi

exec "$PROJECT_ROOT/.codex/scripts/run_us_market_auto_once.sh" "$slot" "$today" "$session_date"
