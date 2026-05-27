#!/usr/bin/env python3
"""Scheduled US market news/signal/order run for KIS paper trading.

This script is intentionally vps-only. It fetches recent market headlines,
builds a short-term news-aware signal model, sizes BUY candidates under daily
risk limits, submits paper overseas limit orders, and registers app-level
protective sell triggers after fills are visible in holdings.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from xml.etree import ElementTree

import requests


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STRATEGY_BUILDER = PROJECT_ROOT / "strategy_builder"
RUNTIME_DIR = PROJECT_ROOT / ".codex" / "runtime" / "us_market_auto"
CANDIDATES = [
    ("SPY", "NYSE"), ("QQQ", "NASD"), ("DIA", "NYSE"), ("IWM", "NYSE"),
    ("NVDA", "NASD"), ("MSFT", "NASD"), ("AVGO", "NASD"), ("AMD", "NASD"),
    ("AMZN", "NASD"), ("GOOGL", "NASD"), ("META", "NASD"), ("AAPL", "NASD"),
    ("JPM", "NYSE"), ("V", "NYSE"), ("XOM", "NYSE"), ("CVX", "NYSE"),
    ("LLY", "NYSE"), ("COST", "NASD"),
]

TOTAL_BUY_PCT = 0.03
PER_SYMBOL_PCT = 0.01
DAILY_LOSS_PCT = 0.005
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.06
MIN_BUY_STRENGTH = 0.70
MIN_SECONDS_BETWEEN_KIS_CALLS = 0.85


def _json_default(value: Any) -> Any:
    try:
        import numpy as np

        if isinstance(value, (np.integer, np.floating)):
            return value.item()
    except Exception:
        pass
    return str(value)


def load_modules():
    sys.path.insert(0, str(STRATEGY_BUILDER))
    import kis_auth as ka
    from core import indicators, overseas_data_fetcher
    from backend.services.protective_orders import upsert_existing_position_protection

    return ka, indicators, overseas_data_fetcher, upsert_existing_position_protection


def fetch_headlines() -> list[dict[str, str]]:
    queries = [
        "US stock market today macro Fed yields oil AI",
        "Wall Street today S&P 500 Nasdaq Dow market news",
        "US market today Nvidia tech energy banks",
    ]
    headlines: list[dict[str, str]] = []
    seen: set[str] = set()
    headers = {"User-Agent": "Mozilla/5.0 KIS paper trading news summarizer"}

    for query in queries:
        url = (
            "https://news.google.com/rss/search?"
            f"q={quote_plus(query + ' when:1d')}&hl=en-US&gl=US&ceid=US:en"
        )
        try:
            response = requests.get(url, headers=headers, timeout=12)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
        except Exception as exc:
            headlines.append({"title": f"news fetch failed: {query}", "source": str(exc), "link": ""})
            continue

        for item in root.findall(".//item")[:8]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            source = item.findtext("source") or "Google News"
            if not title or title in seen:
                continue
            seen.add(title)
            headlines.append({"title": title, "source": source, "link": link})

    return headlines[:18]


def summarize_news(headlines: list[dict[str, str]]) -> dict[str, Any]:
    text = " ".join(item["title"].lower() for item in headlines)
    buckets = {
        "rates_fed": ["fed", "rate", "yield", "treasury", "inflation", "jobs"],
        "ai_tech": ["nvidia", "ai", "semiconductor", "nasdaq", "tech", "chip"],
        "energy": ["oil", "energy", "crude", "opec", "xom", "chevron"],
        "risk": ["selloff", "tariff", "war", "recession", "debt", "volatility"],
    }
    scores = {
        name: sum(text.count(keyword) for keyword in keywords)
        for name, keywords in buckets.items()
    }
    if scores["risk"] >= 3 or scores["rates_fed"] >= 5:
        regime = "risk_control"
    elif scores["ai_tech"] >= scores["energy"] and scores["ai_tech"] > 0:
        regime = "ai_tech_momentum"
    elif scores["energy"] > 0:
        regime = "energy_momentum"
    else:
        regime = "broad_momentum"
    return {"regime": regime, "scores": scores}


def signal_for(symbol: str, exchange: str, odf, indicators) -> dict[str, Any]:
    df = odf.get_daily_prices(symbol, days=100, env_dv="vps", exchange=exchange)
    time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
    price_info = odf.get_current_price(symbol, env_dv="vps", exchange=exchange)
    time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)

    if df.empty or len(df) < 55:
        return {
            "symbol": symbol,
            "exchange": exchange,
            "action": "ERROR",
            "strength": 0.0,
            "reason": f"일봉 데이터 부족: {len(df)} rows",
            "price": float(price_info.get("price") or 0),
        }

    ema20 = float(indicators.calc_ema(df, 20).iloc[-1])
    ema50 = float(indicators.calc_ema(df, 50).iloc[-1])
    roc20 = float(indicators.calc_roc(df, 20).iloc[-1])
    rsi14 = float(indicators.calc_rsi(df, 14).iloc[-1])
    price = float(price_info.get("price") or df["close"].iloc[-1])

    buy = ema20 > ema50 and roc20 > 4 and 50 < rsi14 < 72
    sell = ema20 < ema50 or roc20 < -3 or rsi14 > 78
    if buy:
        strength = min(0.95, 0.55 + min(max((roc20 - 4) / 20, 0), 0.25) + min(max((rsi14 - 50) / 44, 0), 0.15))
        action = "BUY"
        reason = f"EMA20 {ema20:.2f}>EMA50 {ema50:.2f}, ROC20 {roc20:.2f}%>4, RSI14 {rsi14:.2f}"
    elif sell:
        strength = 0.65
        action = "SELL"
        reason = f"Exit filter: EMA20 {ema20:.2f}, EMA50 {ema50:.2f}, ROC20 {roc20:.2f}%, RSI14 {rsi14:.2f}"
    else:
        strength = 0.25
        action = "HOLD"
        reason = f"No entry: EMA20 {ema20:.2f}, EMA50 {ema50:.2f}, ROC20 {roc20:.2f}%, RSI14 {rsi14:.2f}"

    return {
        "symbol": symbol,
        "exchange": exchange,
        "action": action,
        "strength": round(strength, 2),
        "reason": reason,
        "price": round(price, 2),
        "take_profit": round(price * (1 + TAKE_PROFIT_PCT), 2),
        "stop_loss": round(price * (1 - STOP_LOSS_PCT), 2),
        "ema20": round(ema20, 2),
        "ema50": round(ema50, 2),
        "roc20": round(roc20, 2),
        "rsi14": round(rsi14, 2),
    }


def today_state_path(now: datetime) -> Path:
    return RUNTIME_DIR / f"{now.strftime('%Y%m%d')}.json"


def load_today_state(path: Path) -> dict[str, Any]:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"runs": [], "orders": []}


def save_today_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")


def account_equity(odf) -> tuple[float, float, list[dict[str, Any]]]:
    deposit = odf.get_deposit("vps") or {}
    time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
    holdings_df = odf.get_holdings("vps")
    time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
    holdings = [] if holdings_df.empty else holdings_df.to_dict("records")
    holdings_value = sum(float(row.get("eval_amount") or 0) for row in holdings)
    cash = float(deposit.get("deposit") or 0)
    total_eval = float(deposit.get("total_eval") or 0)
    equity = max(cash + holdings_value, total_eval, holdings_value)
    return equity, cash, holdings


def build_orders(signals: list[dict[str, Any]], equity: float, cash: float, state: dict[str, Any]) -> list[dict[str, Any]]:
    bought_today = sum(float(order.get("notional") or 0) for order in state.get("orders", []))
    total_budget = max(0.0, equity * TOTAL_BUY_PCT - bought_today)
    risk_budget = max(0.0, equity * DAILY_LOSS_PCT - (bought_today * STOP_LOSS_PCT))
    risk_capital = risk_budget / STOP_LOSS_PCT if STOP_LOSS_PCT else 0
    usable_budget = min(total_budget, risk_capital, cash)
    per_symbol_cap = equity * PER_SYMBOL_PCT

    buy_signals = [
        signal for signal in signals
        if signal["action"] == "BUY"
        and float(signal.get("strength") or 0) >= MIN_BUY_STRENGTH
        and float(signal.get("price") or 0) > 0
    ]
    buy_signals.sort(key=lambda item: float(item["strength"]), reverse=True)
    total_strength = sum(float(item["strength"]) for item in buy_signals) or 1

    orders: list[dict[str, Any]] = []
    remaining = usable_budget
    for signal in buy_signals:
        price = float(signal["price"])
        target_notional = min(
            per_symbol_cap,
            usable_budget * float(signal["strength"]) / total_strength,
            remaining,
        )
        qty = int(target_notional // price)
        if qty <= 0:
            continue
        notional = round(qty * price, 2)
        remaining -= notional
        orders.append({
            **signal,
            "quantity": qty,
            "notional": notional,
            "risk_amount": round(notional * STOP_LOSS_PCT, 2),
            "limit_price": price,
        })
    return orders


def wait_for_holding(odf, symbol: str, exchange: str, min_qty: int, attempts: int = 8) -> dict[str, Any] | None:
    for _ in range(attempts):
        holdings_df = odf.get_holdings("vps")
        if not holdings_df.empty:
            for _, row in holdings_df.iterrows():
                data = row.to_dict()
                if str(data.get("stock_code")).upper() == symbol and int(float(data.get("quantity") or 0)) >= min_qty:
                    return data
        time.sleep(2.0)
    return None


async def place_orders(orders: list[dict[str, Any]], odf, upsert_protection) -> list[dict[str, Any]]:
    submitted: list[dict[str, Any]] = []
    for order in orders:
        symbol = order["symbol"]
        exchange = order["exchange"]
        qty = int(order["quantity"])
        price = float(order["limit_price"])
        buyable = odf.get_buyable_amount(symbol, price, env_dv="vps", exchange=exchange)
        time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
        buyable_qty = int(float(buyable.get("quantity") or 0))
        buyable_amount = float(buyable.get("amount") or 0)
        capped_qty = min(qty, buyable_qty, int(buyable_amount // price) if price > 0 else 0)
        if capped_qty <= 0:
            order["order_status"] = "skipped_no_buyable_amount"
            order["buyable_amount"] = round(buyable_amount, 2)
            order["buyable_quantity"] = buyable_qty
            submitted.append(order)
            continue

        if capped_qty < qty:
            qty = capped_qty
            order["quantity"] = qty
            order["notional"] = round(qty * price, 2)
            order["risk_amount"] = round(order["notional"] * STOP_LOSS_PCT, 2)
            order["buyable_amount"] = round(buyable_amount, 2)
            order["buyable_quantity"] = buyable_qty

        result = odf.execute_order(
            symbol=symbol,
            action="BUY",
            quantity=qty,
            price=price,
            env_dv="vps",
            exchange=exchange,
        )
        time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
        if result.empty:
            order["order_status"] = "failed"
            submitted.append(order)
            continue

        row = result.iloc[0].to_dict()
        order_no = str(row.get("ODNO", row.get("odno", "")))
        order["order_status"] = "submitted"
        order["order_no"] = order_no

        holding = wait_for_holding(odf, symbol, exchange, qty)
        if not holding:
            order["protection_status"] = "pending_fill_not_registered"
            submitted.append(order)
            continue

        entry_price = float(holding.get("avg_price") or price)
        held_qty = int(float(holding.get("quantity") or qty))
        protection = await upsert_protection(
            env_dv="vps",
            stock_code=symbol,
            stock_name=str(holding.get("stock_name") or symbol),
            quantity=held_qty,
            entry_price=entry_price,
            enabled=True,
            take_profit_enabled=True,
            take_profit_trigger_price=round(entry_price * (1 + TAKE_PROFIT_PCT), 2),
            take_profit_order_type="limit",
            take_profit_limit_price=round(entry_price * (1 + TAKE_PROFIT_PCT), 2),
            stop_loss_enabled=True,
            stop_loss_trigger_price=round(entry_price * (1 - STOP_LOSS_PCT), 2),
            stop_loss_order_type="limit",
            stop_loss_limit_price=round(entry_price * (1 - STOP_LOSS_PCT), 2),
            market="us",
            exchange=exchange,
            currency="USD",
        )
        order["filled_avg_price"] = entry_price
        order["filled_quantity"] = held_qty
        order["take_profit"] = protection.get("take_profit_price")
        order["stop_loss"] = protection.get("stop_loss_price")
        order["protection_status"] = protection.get("status")
        order["protection_id"] = protection.get("id")
        submitted.append(order)
    return submitted


def write_report(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        f"# US Market Auto Run - {payload['slot']}",
        "",
        f"- Time: {payload['started_at']}",
        f"- Regime: {payload['news_summary']['regime']}",
        f"- Equity: ${payload['account']['equity']:,.2f}",
        f"- Cash: ${payload['account']['cash']:,.2f}",
        "",
        "## Headlines",
    ]
    for item in payload["headlines"][:10]:
        lines.append(f"- {item['title']} ({item['source']})")
    lines.extend(["", "## Signals", "| Symbol | Action | Strength | Price | Reason |", "|---|---:|---:|---:|---|"])
    for signal in payload["signals"]:
        lines.append(
            f"| {signal['symbol']} | {signal['action']} | {signal.get('strength', 0):.2f} | "
            f"${float(signal.get('price') or 0):.2f} | {signal.get('reason', '')} |"
        )
    lines.extend(["", "## Orders", "| Symbol | Qty | Notional | Status | TP | SL | Protection |", "|---|---:|---:|---|---:|---:|---|"])
    for order in payload["orders"]:
        lines.append(
            f"| {order['symbol']} | {order.get('quantity', 0)} | ${order.get('notional', 0):,.2f} | "
            f"{order.get('order_status')} | ${float(order.get('take_profit') or 0):.2f} | "
            f"${float(order.get('stop_loss') or 0):.2f} | {order.get('protection_status', '')} |"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cleanup_cron_if_done(state: dict[str, Any]) -> None:
    slots = {run.get("slot") for run in state.get("runs", [])}
    if not {"mid", "close"}.issubset(slots):
        return
    os.system("(crontab -l 2>/dev/null | grep -v 'KIS_US_MARKET_AUTO_20260522') | crontab -")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True, choices=["mid", "close", "manual"])
    parser.add_argument("--date", default="20260522")
    args = parser.parse_args()

    now = datetime.now()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    report_base = RUNTIME_DIR / f"{now.strftime('%Y%m%d_%H%M%S')}_{args.slot}"
    state_path = today_state_path(now)
    state = load_today_state(state_path)

    ka, indicators, odf, upsert_protection = load_modules()
    ka.auth(svr="vps")

    headlines = fetch_headlines()
    news_summary = summarize_news(headlines)
    signals = []
    for symbol, exchange in CANDIDATES:
        try:
            signals.append(signal_for(symbol, exchange, odf, indicators))
        except Exception as exc:
            signals.append({
                "symbol": symbol,
                "exchange": exchange,
                "action": "ERROR",
                "strength": 0.0,
                "reason": str(exc),
                "price": 0.0,
            })

    equity, cash, holdings = account_equity(odf)
    planned_orders = build_orders(signals, equity, cash, state)
    submitted_orders = await place_orders(planned_orders, odf, upsert_protection)

    payload = {
        "slot": args.slot,
        "started_at": now.isoformat(timespec="seconds"),
        "headlines": headlines,
        "news_summary": news_summary,
        "strategy": {
            "name": "US news-aware EMA/ROC/RSI trend filter",
            "entry": "EMA20 > EMA50 and ROC20 > 4 and 50 < RSI14 < 72",
            "exit": "EMA20 < EMA50 or ROC20 < -3 or RSI14 > 78",
            "risk": {"take_profit_pct": 6, "stop_loss_pct": 3},
        },
        "account": {"equity": equity, "cash": cash, "holdings": holdings},
        "signals": signals,
        "orders": submitted_orders,
    }

    state.setdefault("runs", []).append({
        "slot": args.slot,
        "started_at": payload["started_at"],
        "report": str(report_base.with_suffix(".md")),
        "orders": submitted_orders,
    })
    state.setdefault("orders", []).extend(
        order for order in submitted_orders if order.get("order_status") == "submitted"
    )
    save_today_state(state_path, state)
    report_base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    write_report(report_base.with_suffix(".md"), payload)
    cleanup_cron_if_done(state)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
