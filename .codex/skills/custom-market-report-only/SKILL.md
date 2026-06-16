---
name: custom-market-report-only
description: Use when the user wants vps-only report-only automation reports from arbitrary Korean or US stock candidates, such as "005930,000660 리포트만", "NVDA,VRT report-only", "한국/미국 후보군 자동화 리포트", or "임의 후보군으로 주문 없이 자동화 리포트".
---

# Custom Market Report Only

Create Strategy Builder automation reports from explicit KR/US candidate symbols without submitting orders.

## Core Rules

- Use only paper trading mode: `vps`.
- Use only the vps Strategy Builder endpoint: `http://127.0.0.1:8081`.
- Always pass `--report-only`; never submit live or paper orders from this skill.
- Never use `prod`, `8083`, broker reservations, or prod protective-order actions.
- Do not read or modify `~/KIS/config/kis_devlp.yaml`.
- Do not persist candidate symbols into `.codex/local/*_market_auto.env`; use inline env only.
- KR symbols must be comma-separated 6-digit stock codes.
- US symbols must be comma-separated tickers; normalize them to uppercase.
- Reuse `.codex/scripts/kr_market_auto_run.py` and `.codex/scripts/us_market_auto_run.py`; do not duplicate trading logic.

## Preferred Command

Use the bundled wrapper:

```bash
uv run python .codex/skills/custom-market-report-only/scripts/run_custom_report_only.py \
  --kr 005930,000660,005380 \
  --us NVDA,VRT,AVGO
```

Useful options:

- `--kr SYMBOLS`: Korean 6-digit codes.
- `--us SYMBOLS`: US tickers.
- `--date YYYYMMDD`: Apply one date to both markets.
- `--kr-date YYYYMMDD`: Korean session date, defaults to today in KST.
- `--us-date YYYYMMDD`: US session date, defaults to today in America/New_York.
- `--run-tag custom_report`: Safe suffix used in generated run IDs.
- `--skip-calendar-check`: Skip market-open guard only when intentionally creating a closed-market report.

## Workflow

1. Verify 8081 vps auth:
   - `curl -s http://127.0.0.1:8081/api/auth/status`
   - Require `authenticated=true` and `mode=vps`.
2. Validate symbols:
   - KR: `^[0-9]{6}$`
   - US: `^[A-Z0-9][A-Z0-9.-]{0,9}$`
3. Check market calendar unless the user asks to skip it:
   - KR: `.codex/scripts/kr_market_calendar.py --date YYYYMMDD --check-open`
   - US: `.codex/scripts/us_market_calendar.py --date YYYYMMDD --check-open`
4. Run each requested market with inline env:
   - KR: `KR_MARKET_CANDIDATE_SYMBOLS=...`
   - US: `US_MARKET_CANDIDATE_SYMBOLS=...`
   - Common: `KIS_TRADE_MODE=vps`, `KIS_VPS_STRATEGY_API=http://127.0.0.1:8081`
5. Read generated JSON reports and, when available, the `/api/automation/{kr|us}/runs/{run_id}` endpoint.
6. Final response should include:
   - markets and symbols used
   - run IDs
   - JSON/Markdown report paths
   - `candidate_selection.mode`, expected to be `custom`
   - `report_only`, expected to be `true`
   - submitted order count, expected to be `0`
   - warnings or errors exactly enough to debug

