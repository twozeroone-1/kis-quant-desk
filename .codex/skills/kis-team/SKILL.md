---
name: kis-team
description: Orchestrate full KIS pipeline from strategy design to backtest to execution. Use when user asks for end-to-end flow like "전략부터 주문까지", "다 해줘", or full pipeline automation with stage-by-stage confirmation.
---

# KIS Team Orchestrator

Coordinate 3 stages with explicit user confirmation between stages.

## Stages

1. Strategy design (`kis-strategy-builder`)
2. Backtest validation (`kis-backtester`)
3. Signal and order execution (`kis-order-executor`)

## Rules

- Before stage 1, check current auth status.
- Carry a single `kis_context` through all stages: `strategy_id`, `yaml_path`, `symbols`, `market`, `timeframe`, entry/exit summary, risk settings, backtest job/result, and execution mode.
- Do not let the strategy used for live signals drift from the strategy that was backtested. If params or symbols change, ask whether to re-run the backtest.
- Before stage 3, show the validated strategy conditions, latest backtest summary, and current auth mode.
- If mode is `prod`, present stock, side, quantity, estimated amount, risk orders, and a clear warning; require explicit user confirmation.
- Stop on any stage failure, report cause, and ask whether to retry/adjust.
- Do not auto-advance without user confirmation.

## Handoff Shape

```json
{
  "strategy_id": "rsi_oversold",
  "yaml_path": "strategies/custom/rsi_oversold.kis.yaml",
  "symbols": ["005930"],
  "market": "domestic",
  "entry": "RSI < 30",
  "exit": "RSI > 70",
  "risk": {"stop_loss_pct": 3.0, "take_profit_pct": 8.0},
  "backtest": {"job_id": "...", "total_return_pct": 0.0, "max_drawdown": 0.0, "sharpe_ratio": 0.0},
  "execution_mode": "vps"
}
```
