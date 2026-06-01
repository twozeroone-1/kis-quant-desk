#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="/home/from0to01/open-trading-api"
LOG_DIR="$PROJECT_ROOT/.codex/runtime/kr_market_auto_prod"
APPROVAL_DIR="$LOG_DIR/approvals"
APPROVAL_VALUE="I_UNDERSTAND_REAL_ORDERS"

date_key="${1:?YYYYMMDD required}"
slot="${2:?slot required: open|mid|close|manual}"

case "$slot" in
  open|mid|close|manual) ;;
  *)
    echo "invalid slot: $slot" >&2
    exit 2
    ;;
esac

if [[ ! "$date_key" =~ ^[0-9]{8}$ ]]; then
  echo "invalid date: $date_key" >&2
  exit 2
fi

mkdir -p "$APPROVAL_DIR"
approval_file="$APPROVAL_DIR/${date_key}_${slot}.approved"
printf '%s\n' "$APPROVAL_VALUE" > "$approval_file"
chmod 600 "$approval_file"
echo "$approval_file"
