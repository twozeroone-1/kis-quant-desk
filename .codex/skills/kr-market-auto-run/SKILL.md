---
name: kr-market-auto-run
description: Use when the user asks to run or schedule Korean-market paper-trading automation through Open Trading API/KIS, especially "한국장 3번 실행", "장 초반/중반/막판", "국내 모의 자동매매", "매일 AI 트레이딩", or requests that combine Korean market news, signals, vps-only orders, reservation checks, and protective stop/take-profit monitoring.
---

# Korean Market Auto Run

Run the Korean-market intraday paper-trading workflow through the local Open Trading API Strategy Builder backend.

## Core Rules

- Always use domestic paper trading only: `vps`.
- Never submit `prod` orders, `prod` reservation orders, or `prod` protective orders.
- Start or verify the backend with `KIS_LOCK_MODE=vps`.
- Do not read or modify `~/KIS/config/kis_devlp.yaml` unless the user explicitly asks.
- Mask account identifiers if they appear.
- Use `.codex/runtime/kr_market_auto/YYYYMMDD.json` as the source of truth for today's automated new-buy total.
- Today new buys must stay within 10% of total account evaluation.
- Daily loss budget is 0.5% of total account evaluation, modeled with the -3% stop loss on today's automated buys.
- Only BUY signals with strength >= 0.70 are eligible.
- SELL signals require actual holdings.
- Take profit is +6%, stop loss is -3%.
- App-level protective orders are not KIS server OCO. Explain that they depend on the backend, auth, and network.
- If KIS paper reservation APIs return unsupported errors such as `OPSQ0002 없는 서비스 코드 입니다`, report the original error and continue with holdings, pending, and protective-order checks.

## Scripts

- Single slot: `.codex/scripts/run_kr_market_auto_once.sh <open|mid|close|manual> YYYYMMDD [session_date]`
- Daily slot wrapper: `.codex/scripts/run_kr_market_auto_daily.sh <open|mid|close>`
- Install daily cron: `.codex/scripts/install_kr_market_auto_daily_cron.sh`
- Main implementation: `.codex/scripts/kr_market_auto_run.py`
- LLM decision layer: `.codex/scripts/kr_market_llm_decider.py`
- Trading-day guard: `.codex/scripts/kr_market_calendar.py`

Do not rewrite the workflow in the response. Prefer running or patching these scripts.

## Standard Schedule

Korea time:

- open: 09:10
- mid: 12:30
- close: 15:10

For a same-day request:

1. Check current KST time with `date`.
2. Check KRX open status with `.codex/scripts/kr_market_calendar.py --date YYYYMMDD --check-open`.
3. If the market is closed, skip orders and write a market-closed report.
4. If a requested slot time has already passed, run that slot immediately as a catch-up unless the user said not to.
5. Register remaining slots for today with cron, or install the daily cron if the user asks for every trading day.
6. Remove stale one-off `KIS_KR_MARKET_AUTO_YYYYMMDD` cron entries for past dates when replacing schedules.

## Execution Workflow

1. Confirm the date is a KRX open day:
   - `.codex/scripts/kr_market_calendar.py --date YYYYMMDD --check-open`
   - If closed or unknown, fail closed: no orders, no LLM trading decision, report the original calendar status/error.
2. Confirm backend/auth:
   - `curl -s http://127.0.0.1:8000/api/auth/status`
   - If down, start with `KIS_LOCK_MODE=vps`.
   - Authenticate with `POST /api/auth/login {"mode":"vps"}`.
3. Fetch and summarize Korean-market news using the script's Google News RSS queries.
4. Run `custom:today_krx_macro_rebound` over the domestic large-cap/ETF candidate basket.
5. Split signals into BUY / SELL / HOLD.
6. Before any BUY order, show this table:

   `| 종목 | 구분 | 신호 | 강도 | 현재가 | 예상 수량 | 예상 금액 | 배분 비중 | 익절가(+6%) | 손절가(-3%) | 주문 방식 | 주문 여부 |`

7. During regular hours, prefer normal domestic paper orders. Use reservation orders only when trading is unavailable or a waiting limit strategy is more appropriate.
8. After fills, requery account holdings and pending orders.
9. Register protective orders for full held quantity:
   - take-profit trigger/order: limit
   - stop-loss trigger: -3%, domestic sell order type may be market or limit
10. Requery reservations, pending orders, holdings, and protective orders.
11. Save reports under `.codex/runtime/kr_market_auto/`.

## Daily Automation

For "매일 하루 3번" automation, install cron:

```bash
.codex/scripts/install_kr_market_auto_daily_cron.sh
```

This schedules Monday-Friday KST runs. Each run checks the KIS domestic holiday API and skips closed days. It does not make investment guarantees and does not replace monitoring. The backend and protective-order monitor must remain healthy for app-level stops to work.

Daily runs default to `KR_MARKET_LLM_MODE=live-vps`, so CLIProxyAPI/OpenAI-compatible LLM approval is required before BUY orders. The LLM can only approve, reduce, or block deterministic BUY candidates; hard risk gates still apply. Override with `KR_MARKET_LLM_MODE=shadow` to log LLM decisions without affecting orders, or `off` to disable the LLM layer.

For local CLIProxyAPI credentials, prefer an untracked `.codex/local/kr_market_auto.env` file that exports `CLIPROXY_API_KEY_FILE`, `CLIPROXY_API_BASE`, and optionally `KR_MARKET_LLM_MODEL`. Do not commit API keys or user-local key file paths.

## Reporting

Final response should include:

- Current mode and whether `prod` was avoided.
- What ran now and what was scheduled.
- Orders submitted, if any.
- Post-fill table:

  `| 종목 | 체결 수량 | 평균단가 | 익절 지정가 | 손절 지정가 | 보호주문 상태 | 예약/미체결 상태 |`

- Reservation API errors exactly as returned.
- Report/log file paths.
