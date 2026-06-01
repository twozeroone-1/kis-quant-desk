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
- Daily loss budget is 0.5% of total account evaluation, modeled with the -3% stop loss on automated buys.
- Only BUY signals with strength >= 0.70 are eligible.
- SELL signals require actual US holdings.
- Take profit is +6%, stop loss is -3%.
- US protective sell orders are limit orders.
- App-level protective orders are not KIS server OCO. Explain that they depend on the backend, auth, and network.
- If KIS paper reservation APIs return unsupported errors, report the original error and continue with holdings, pending, and protective-order checks.

## Scripts

- Single slot: `.codex/scripts/run_us_market_auto_once.sh <open|mid|close|manual> YYYYMMDD [us_session_date]`
- Daily slot wrapper: `.codex/scripts/run_us_market_auto_daily.sh <open|mid|close>`
- Install daily cron: `.codex/scripts/install_us_market_auto_daily_cron.sh`
- Main implementation: `.codex/scripts/us_market_auto_run.py`
- LLM decision layer: `.codex/scripts/us_market_llm_decider.py`
- Trading-day guard: `.codex/scripts/us_market_calendar.py`

Prefer running or patching these scripts instead of rewriting the workflow in a response.

## Standard Schedule

Korea time:

- open: 23:45 Monday-Friday, US session date is the same KST date.
- mid: 02:45 Tuesday-Saturday, US session date is the previous KST date.
- close: 04:45 Tuesday-Saturday, US session date is the previous KST date.

This schedule intentionally works across US daylight-saving and standard-time seasons. `23:45` KST is after the regular open in either season.

## Execution Workflow

1. Confirm the US session is an NYSE/Nasdaq open day:
   - `.codex/scripts/us_market_calendar.py --date YYYYMMDD --check-open`
   - If closed, fail closed: no orders, no LLM trading decision, write a market-closed report.
2. Confirm backend/auth:
   - `curl -s http://127.0.0.1:8000/api/auth/status`
   - If down, start with `KIS_LOCK_MODE=vps`.
   - Authenticate with vps only.
3. Fetch and summarize US-market news using the script's Google News RSS queries.
4. Run the US news-aware EMA/ROC/RSI trend filter over the large-cap/ETF candidate basket.
5. Split signals into BUY / SELL / HOLD / ERROR.
6. Before any BUY order, show this table:

   `| 종목 | 구분 | 신호 | 강도 | 현재가 | 예상 수량 | 예상 금액 | 배분 비중 | 익절가(+6%) | 손절가(-3%) | 주문 방식 | 주문 여부 |`

7. During regular US hours, use normal overseas vps limit orders.
8. Outside regular hours, use US vps reservation BUY limit orders when a waiting strategy is appropriate.
9. US reservation SELL requires holdings first. US reservation modify is cancel-and-replace.
10. After fills, requery US holdings and pending orders.
11. Register protective orders for full held quantity:
    - take-profit trigger/order: limit
    - stop-loss trigger/order: limit
12. Requery reservations, pending orders, holdings, and protective orders.
13. Save reports under `.codex/runtime/us_market_auto/`.

## Daily Automation

For "매일 하루 3번" US-market automation, install cron:

```bash
.codex/scripts/install_us_market_auto_daily_cron.sh
```

Daily runs default to `US_MARKET_LLM_MODE=live-vps`, so CLIProxyAPI/OpenAI-compatible LLM approval is required before BUY orders. The LLM can only approve, reduce, or block deterministic BUY candidates; hard risk gates still apply. Override with `US_MARKET_LLM_MODE=shadow` to log LLM decisions without affecting orders, or `off` to disable the LLM layer.

For local CLIProxyAPI credentials, prefer an untracked `.codex/local/kr_market_auto.env` file that exports `CLIPROXY_API_KEY_FILE`, `CLIPROXY_API_BASE`, and optionally `US_MARKET_LLM_MODEL` or `KR_MARKET_LLM_MODEL`. Do not commit API keys or user-local key file paths.

## Reporting

Final response should include:

- Current mode and whether `prod` was avoided.
- What ran now and what was scheduled.
- Orders or reservations submitted, if any.
- Post-fill table:

  `| 종목 | 체결 수량 | 평균단가 | 익절 지정가 | 손절 지정가 | 보호주문 상태 | 예약/미체결 상태 |`

- Reservation API errors exactly as returned.
- Report/log file paths.
