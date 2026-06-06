#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/from0to01/open-trading-api"
LOG_DIR="$PROJECT_ROOT/.codex/runtime/us_market_auto"
mkdir -p "$LOG_DIR"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

(crontab -l 2>/dev/null | grep -v 'KIS_US_MARKET_AUTO_DAILY' || true) > "$tmp"
if ! grep -q '^CRON_TZ=Asia/Seoul$' "$tmp"; then
  {
    echo 'CRON_TZ=Asia/Seoul'
    cat "$tmp"
  } > "$tmp.with_tz"
  mv "$tmp.with_tz" "$tmp"
fi

cat >> "$tmp" <<EOF
45 0-6,22-23 * * * $PROJECT_ROOT/.codex/scripts/run_us_market_auto_daily.sh hourly >> $LOG_DIR/cron_daily.log 2>&1 # KIS_US_MARKET_AUTO_DAILY
EOF

crontab "$tmp"
crontab -l | grep 'KIS_US_MARKET_AUTO_DAILY'
