#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/from0to01/open-trading-api"
LOG_DIR="$PROJECT_ROOT/.codex/runtime/kr_market_auto_prod"
CRON_LOG="$LOG_DIR/cron_daily.log"
mkdir -p "$LOG_DIR"

tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

(crontab -l 2>/dev/null | grep -v 'KIS_KR_MARKET_AUTO_PROD_DAILY' || true) > "$tmp"
if ! grep -q '^CRON_TZ=Asia/Seoul$' "$tmp"; then
  {
    echo 'CRON_TZ=Asia/Seoul'
    cat "$tmp"
  } > "$tmp.with_tz"
  mv "$tmp.with_tz" "$tmp"
fi

cat >> "$tmp" <<EOF
20 9 * * 1-5 $PROJECT_ROOT/.codex/scripts/run_kr_market_auto_prod_daily.sh open >> $CRON_LOG 2>&1 # KIS_KR_MARKET_AUTO_PROD_DAILY
EOF

crontab "$tmp"
crontab -l | grep 'KIS_KR_MARKET_AUTO_PROD_DAILY'
cat <<EOF

Installed prod KRX cron in report-only mode by default.
To allow one real-order run, create:
  $LOG_DIR/approvals/YYYYMMDD_<open|mid|close|manual>.approved
with exactly:
  I_UNDERSTAND_REAL_ORDERS
The approval file is consumed before that run starts.
Helper:
  $PROJECT_ROOT/.codex/scripts/approve_kr_market_auto_prod_once.sh YYYYMMDD open
EOF
