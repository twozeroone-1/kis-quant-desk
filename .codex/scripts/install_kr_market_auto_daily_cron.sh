#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/from0to01/open-trading-api"
CRON_LOG="$PROJECT_ROOT/.codex/runtime/kr_market_auto/cron_daily.log"
mkdir -p "$(dirname "$CRON_LOG")"

tmp="$(mktemp)"
crontab -l 2>/dev/null | grep -v 'KIS_KR_MARKET_AUTO_' > "$tmp" || true

if ! grep -q '^CRON_TZ=Asia/Seoul$' "$tmp"; then
  printf 'CRON_TZ=Asia/Seoul\n' | cat - "$tmp" > "$tmp.new"
  mv "$tmp.new" "$tmp"
fi

cat >> "$tmp" <<EOF
10 9-15 * * 1-5 $PROJECT_ROOT/.codex/scripts/run_kr_market_auto_daily.sh hourly >> $CRON_LOG 2>&1 # KIS_KR_MARKET_AUTO_DAILY
EOF

awk '!seen[$0]++' "$tmp" | crontab -
rm -f "$tmp"
crontab -l
