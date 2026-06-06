#!/usr/bin/env bash
set -euo pipefail

slot="${1:-hourly}"
today="$(TZ=Asia/Seoul date +%Y%m%d)"
PROJECT_ROOT="/home/from0to01/open-trading-api"
LOG_DIR="$PROJECT_ROOT/.codex/runtime/us_market_auto"
UV_BIN="/home/from0to01/.local/bin/uv"
LOCAL_ENV="$PROJECT_ROOT/.codex/local/us_market_auto.env"
if [[ -f "$LOCAL_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$LOCAL_ENV"
fi
export US_MARKET_LLM_MODE="${US_MARKET_LLM_MODE:-off}"
mkdir -p "$LOG_DIR"

if [[ "$slot" != "hourly" ]]; then
  case "$slot" in
    open) session_date="$today" ;;
    mid|close) session_date="$(TZ=Asia/Seoul date -d "$today -1 day" +%Y%m%d)" ;;
    *) echo "invalid slot: $slot" >&2; exit 2 ;;
  esac
  exec "$PROJECT_ROOT/.codex/scripts/run_us_market_auto_once.sh" "$slot" "$today" "$session_date"
fi

resolution="$(
  cd "$PROJECT_ROOT/strategy_builder"
  "$UV_BIN" run "$PROJECT_ROOT/.codex/scripts/us_market_calendar.py" --resolve-now
)"
printf '%s\n' "$resolution" >> "$LOG_DIR/calendar_daily.log"

readarray -t resolved < <(
  printf '%s' "$resolution" | "$UV_BIN" run --project "$PROJECT_ROOT/strategy_builder" python -c \
    'import json,sys; d=json.load(sys.stdin); print(d.get("resolution","")); print(d.get("date","")); print(d.get("run_id","")); print("1" if d.get("first_closed_slot") else "0")'
)
resolution_status="${resolved[0]:-}"
session_date="${resolved[1]:-}"
run_id="${resolved[2]:-}"
first_closed_slot="${resolved[3]:-0}"

if [[ "$resolution_status" == "scheduled" ]]; then
  exec "$PROJECT_ROOT/.codex/scripts/run_us_market_auto_once.sh" hourly "$today" "$session_date" "$run_id"
fi

if [[ "$resolution_status" == "market_closed" && "$first_closed_slot" == "1" ]]; then
  cd "$PROJECT_ROOT/strategy_builder"
  exec "$UV_BIN" run "$PROJECT_ROOT/.codex/scripts/us_market_auto_run.py" \
    --slot hourly --date "$session_date" --run-id "${session_date}_closed" --market-closed
fi

echo "skip: no US automation slot local=$today resolution=$resolution_status session=$session_date"
