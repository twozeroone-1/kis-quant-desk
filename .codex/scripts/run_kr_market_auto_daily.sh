#!/usr/bin/env bash
set -euo pipefail

slot="${1:-hourly}"
today="$(TZ=Asia/Seoul date +%Y%m%d)"
weekday="$(TZ=Asia/Seoul date +%u)"
export KR_MARKET_LLM_MODE="${KR_MARKET_LLM_MODE:-off}"
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

if [[ "$slot" != "hourly" ]]; then
  if ! (cd "$PROJECT_ROOT" && "$UV_BIN" run "$PROJECT_ROOT/.codex/scripts/kr_market_calendar.py" --date "$today" --check-open) >> "$LOG_DIR/calendar_daily.log" 2>&1; then
    echo "skip: market closed today=$today slot=$slot"
    exit 0
  fi
  exec "$PROJECT_ROOT/.codex/scripts/run_kr_market_auto_once.sh" "$slot" "$today" "$today"
fi

resolution="$(
  cd "$PROJECT_ROOT/strategy_builder"
  "$UV_BIN" run "$PROJECT_ROOT/.codex/scripts/kr_market_calendar.py" --resolve-now
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
  exec "$PROJECT_ROOT/.codex/scripts/run_kr_market_auto_once.sh" hourly "$today" "$session_date" "$run_id"
fi

if [[ "$resolution_status" == "market_closed" && "$first_closed_slot" == "1" ]]; then
  exec "$PROJECT_ROOT/.codex/scripts/run_kr_market_auto_once.sh" hourly "$today" "$session_date" "${session_date}_closed"
fi

echo "skip: no KR automation slot local=$today resolution=$resolution_status session=$session_date"
