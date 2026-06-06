---
name: us-market-auto-run
description: Use when the user asks to run or schedule US-market paper-trading automation through Open Trading API/KIS, especially "미국장 3번 실행", "미국 모의 자동매매", "미국장 예약주문", or requests combining US market news, signals, vps-only orders, reservation checks, and protective stop/take-profit monitoring.
---

# US Market Auto Run

Run the US-market intraday paper-trading workflow through the local Open Trading API/KIS Strategy Builder backend.

## Core Rules

- Always use overseas paper trading only: `vps`.
- Never submit `prod` orders, `prod` reservation orders, or `prod` protective orders.
- Start or verify the backend with `KIS_LOCK_MODE=vps`.
- Do not read or modify `~/KIS/config/kis_devlp.yaml` unless the user explicitly asks.
- Do not commit or print API keys, app secrets, tokens, or raw account identifiers.
- Use `.codex/runtime/us_market_auto/YYYYMMDD.json` as the source of truth for the US session's automated new-buy total.
- Today/session new buys must stay within 10% of total account evaluation.
- Size US orders only from verified USD buyable cash plus holdings market value. Ignore anomalous raw deposit/total-evaluation values for risk sizing.
- `risk_control` blocks all new BUY orders.
- Exclude leveraged/inverse ETFs from new-buy candidates.
- Block entries when ROC20 is above 25%, current price is below EMA20, or the live price is down 3% or more from the latest daily close.
- Limit each symbol to 1% of verified session equity and at most two new symbols per run.
- Block new buys when SPY/QQQ or broad candidate breadth confirms a market selloff; headline risk alone is advisory unless price action confirms it.
- Keep total holdings below 75% of verified equity and each sector below 25%.
- Reject dynamic candidates that have only short-term volume signals without trade-value or market-cap liquidity evidence.
- Daily loss budget is 0.5% of total account evaluation, modeled with the -3% stop loss on automated buys.
- Only BUY signals with strength >= 0.70 are eligible.
- SELL signals require actual US holdings.
- Take profit is +6%, stop loss is -3%.
- US protective sell orders are limit orders.
- App-level protective orders are not KIS server OCO. Explain that they depend on the backend, auth, and network.
- Do not use KIS broker reservation APIs for paper protective exits. Keep them in the Strategy Builder app queue and retry normal paper limit sells.

## Scripts

- Single slot: `.codex/scripts/run_us_market_auto_once.sh <hourly|open|mid|close|manual> YYYYMMDD [us_session_date] [run_id]`
- Hourly wrapper: `.codex/scripts/run_us_market_auto_daily.sh hourly`
- Install daily cron: `.codex/scripts/install_us_market_auto_daily_cron.sh`
- Main implementation: `.codex/scripts/us_market_auto_run.py`
- LLM decision layer: `.codex/scripts/us_market_llm_decider.py`
- Trading-day guard: `.codex/scripts/us_market_calendar.py`

Prefer running or patching these scripts instead of rewriting the workflow in a response.

## Standard Schedule

- Cron: `45 0-6,22-23 * * *` in `Asia/Seoul`.
- `exchange_calendars` XNYS sessions resolve actual runs at `09:45, 10:45, ... 15:45` ET.
- DST, exchange holidays, and early closes are derived from the exchange calendar.
- Legacy `open`, `mid`, and `close` inputs remain available for manual and historical compatibility.

## Execution Workflow

1. Confirm the US session is an NYSE/Nasdaq open day:
   - `.codex/scripts/us_market_calendar.py --date YYYYMMDD --check-open`
   - If closed, fail closed: no orders, no LLM trading decision, write a market-closed report.
2. Confirm backend/auth:
   - `curl -s http://127.0.0.1:8081/api/auth/status`
   - If down, start the `builder-backend-vps` service with `KIS_LOCK_MODE=vps`.
   - Authenticate with vps only.
3. Fetch and summarize US-market news using the script's Google News RSS queries.
4. Run the US news-aware EMA/ROC/RSI trend filter over the large-cap/ETF candidate basket.
5. Split signals into BUY / SELL / HOLD / ERROR.
6. Before any BUY order, show this table:

   `| 종목 | 구분 | 신호 | 강도 | 현재가 | 예상 수량 | 예상 금액 | 배분 비중 | 익절가(+6%) | 손절가(-3%) | 주문 방식 | 주문 여부 |`

7. During regular US hours, use normal overseas vps limit orders.
8. Outside regular hours, use Strategy Builder app-level US vps reservation BUY limit orders scheduled for the next XNYS open. Never call broker reservation APIs in vps.
9. US reservation SELL requires holdings first. US reservation modify is cancel-and-replace.
10. After fills, requery US holdings and pending orders.
11. Register protective orders for full held quantity:
    - take-profit trigger/order: limit
    - stop-loss trigger/order: limit
12. Requery reservations, pending orders, holdings, and protective orders.
13. Save reports under `.codex/runtime/us_market_auto/`.

## Daily Automation

For hourly US regular-session automation, install cron:

```bash
.codex/scripts/install_us_market_auto_daily_cron.sh
```

Daily runs default to `US_MARKET_LLM_MODE=off`. `shadow` logs CLIProxyAPI/OpenAI-compatible LLM decisions without affecting orders. Legacy `live-vps`/`live-prod` values are accepted only as compatibility aliases for `shadow`; LLM output is never an order approval gate. Hard deterministic risk gates still apply.

Use the untracked `.codex/local/us_market_auto.env` for Telegram settings and local US automation overrides. Do not commit API keys, bot tokens, or user-local key file paths.

## Reporting

Final response should include:

- Current mode and whether `prod` was avoided.
- What ran now and what was scheduled.
- Orders or reservations submitted, if any.
- Post-fill table:

  `| 종목 | 체결 수량 | 평균단가 | 익절 지정가 | 손절 지정가 | 보호주문 상태 | 예약/미체결 상태 |`

- Reservation API errors exactly as returned.
- Report/log file paths.
