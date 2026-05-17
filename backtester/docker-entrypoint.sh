#!/usr/bin/env bash
set -euo pipefail

workspace="/app/.lean-workspace"
symbol_props="$workspace/data/symbol-properties/symbol-properties-database.csv"
market_hours="$workspace/data/market-hours/market-hours-database.json"

mkdir -p "$workspace"

docker pull quantconnect/lean:latest

if [[ ! -f "$symbol_props" || ! -f "$market_hours" ]]; then
  bash /app/scripts/setup_lean_data.sh
fi

python /app/scripts/download_master.py

exec /app/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8002
