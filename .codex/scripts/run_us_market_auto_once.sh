#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/from0to01/open-trading-api"
LOG_DIR="$PROJECT_ROOT/.codex/runtime/us_market_auto"
UV_BIN="/home/from0to01/.local/bin/uv"
mkdir -p "$LOG_DIR"

slot="${1:?slot required}"
run_date="${2:?YYYYMMDD date required}"
today="$(date +%Y%m%d)"

if [[ "$today" != "$run_date" ]]; then
  echo "skip: today=$today target=$run_date"
  exit 0
fi

if ! curl -fsS http://127.0.0.1:8000/api/auth/status >/dev/null 2>&1; then
  nohup bash -lc "cd '$PROJECT_ROOT/strategy_builder' && '$UV_BIN' run python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000" \
    >> "$LOG_DIR/backend.log" 2>&1 &
  sleep 5
fi

cd "$PROJECT_ROOT"
cd "$PROJECT_ROOT/strategy_builder"
"$UV_BIN" run "$PROJECT_ROOT/.codex/scripts/us_market_auto_run.py" --slot "$slot" --date "$run_date"
