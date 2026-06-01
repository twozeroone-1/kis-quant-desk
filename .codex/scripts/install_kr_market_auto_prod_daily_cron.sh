#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/from0to01/open-trading-api"
LOG_DIR="$PROJECT_ROOT/.codex/runtime/kr_market_auto_prod"
CRON_LOG="$LOG_DIR/cron_daily.log"
PROD_ENV="$PROJECT_ROOT/.codex/local/kr_market_auto_prod.env"
mkdir -p "$LOG_DIR"

if [[ ! -f "$PROD_ENV" ]] || ! grep -q '^export KIS_PROD_AUTO_CONFIRM=I_UNDERSTAND_REAL_ORDERS$' "$PROD_ENV"; then
  cat >&2 <<EOF
Refusing to install prod daily cron.

Create $PROD_ENV with:
export KIS_PROD_AUTO_CONFIRM=I_UNDERSTAND_REAL_ORDERS

This cron can submit real KIS orders once BUY/SELL signals pass risk gates.
EOF
  exit 1
fi

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
