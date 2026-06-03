#!/usr/bin/env python3
"""Scheduled US market news/signal/order run for KIS paper trading.

This script is intentionally vps-only. It fetches recent market headlines,
builds a short-term news-aware signal model, sizes BUY candidates under session
risk limits, submits paper overseas limit orders, and registers app-level
protective sell triggers after fills are visible in holdings.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import math
import os
import re
import sys
import time
from datetime import datetime, time as dt_time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests

from market_candidate_selector import select_us_candidates


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STRATEGY_BUILDER = PROJECT_ROOT / "strategy_builder"
RUNTIME_DIR = PROJECT_ROOT / ".codex" / "runtime" / "us_market_auto"
STATIC_CANDIDATES = [
    ("SPY", "NYSE"), ("QQQ", "NASD"), ("DIA", "NYSE"), ("IWM", "NYSE"),
    ("NVDA", "NASD"), ("MSFT", "NASD"), ("AVGO", "NASD"), ("AMD", "NASD"),
    ("AMZN", "NASD"), ("GOOGL", "NASD"), ("META", "NASD"), ("AAPL", "NASD"),
    ("JPM", "NYSE"), ("V", "NYSE"), ("XOM", "NYSE"), ("CVX", "NYSE"),
    ("LLY", "NYSE"), ("COST", "NASD"),
]

TOTAL_BUY_PCT = 0.10
DAILY_LOSS_PCT = 0.005
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.06
MIN_BUY_STRENGTH = 0.70
MIN_SELL_STRENGTH = 0.50
MARKETABLE_SELL_LIMIT_BUFFER_PCT = 0.02
MIN_SECONDS_BETWEEN_KIS_CALLS = 0.85
US_EXCHANGES = ("NASD", "NYSE", "AMEX")
ACTIVE_PROTECTION_STATUSES = {"active", "exit_submitted"}


def normalize_llm_mode(mode: str | None) -> tuple[str, list[str]]:
    raw = (mode or "off").strip().lower()
    if raw in {"", "off"}:
        return "off", []
    if raw == "shadow":
        return "shadow", []
    if raw in {"live-vps", "live-prod"}:
        return "shadow", [
            f"{raw} is deprecated; treating it as shadow. LLM output is report-only and does not gate orders."
        ]
    raise RuntimeError(f"invalid llm mode: {mode}")


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
    from core import indicators, overseas_data_fetcher, reserved_orders
    from backend.services.protective_orders import list_protective_orders, upsert_existing_position_protection

    return ka, indicators, overseas_data_fetcher, reserved_orders, upsert_existing_position_protection, list_protective_orders


def load_helper(module_name: str, filename: str):
    path = PROJECT_ROOT / ".codex" / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def us_regular_hours_now(now: datetime | None = None) -> bool:
    """Approximate US regular session in KST.

    The user schedule is KST-based and intentionally works in both DST and
    standard-time seasons: 23:45 is after the regular open in either season,
    and 04:45 is near the end during DST.
    """
    kst_now = now.astimezone(ZoneInfo("Asia/Seoul")) if now else datetime.now(ZoneInfo("Asia/Seoul"))
    current = kst_now.time()
    return current >= dt_time(22, 30) or current <= dt_time(6, 0)


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


def session_state_path(session_date: str) -> Path:
    return RUNTIME_DIR / f"{session_date}.json"


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


def holdings_by_symbol(holdings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("stock_code")).upper(): row for row in holdings}


def marketable_sell_limit(price: float) -> float:
    return round(float(price) * (1 - MARKETABLE_SELL_LIMIT_BUFFER_PCT), 2) if price > 0 else 0.0


def clear_balance_cache(odf) -> None:
    clear = getattr(odf, "clear_balance_cache", None)
    if callable(clear):
        clear()


def list_pending_by_exchange(odf) -> dict[str, Any]:
    data: dict[str, Any] = {"status": "success", "orders": [], "errors": []}
    for exchange in US_EXCHANGES:
        df, ok = odf.get_pending_orders("vps", exchange=exchange)
        time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
        if not ok:
            data["errors"].append({"exchange": exchange, "message": "pending query failed"})
            continue
        if not df.empty:
            data["orders"].extend(df.to_dict("records"))
    if data["errors"]:
        data["status"] = "partial_error" if data["orders"] else "error"
    data["total_count"] = len(data["orders"])
    return data


def list_reservations_by_exchange(reserved_orders) -> dict[str, Any]:
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    data: dict[str, Any] = {"status": "success", "orders": [], "errors": []}
    for exchange in US_EXCHANGES:
        result = reserved_orders.list_us_reservations(
            start_date=today,
            end_date=today,
            exchange=exchange,
            env_dv="vps",
        )
        time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
        if not result.success:
            data["errors"].append({
                "exchange": exchange,
                "error_code": result.error_code,
                "message": result.display_error(),
                "api_url": result.api_url,
                "tr_id": result.tr_id,
            })
            continue
        for record in result.records():
            data["orders"].append({"exchange": exchange, **record})
    if data["errors"]:
        data["status"] = "partial_error" if data["orders"] else "error"
    data["total_count"] = len(data["orders"])
    return data


async def account_status(odf, reserved_orders, list_protective_orders) -> dict[str, Any]:
    equity, cash, holdings = account_equity(odf)
    pending = list_pending_by_exchange(odf)
    reservations = list_reservations_by_exchange(reserved_orders)
    protective = await list_protective_orders()
    return {
        "equity": equity,
        "cash": cash,
        "holdings": holdings,
        "pending": pending,
        "reservations": reservations,
        "protective": protective,
    }


def build_orders(signals: list[dict[str, Any]], equity: float, cash: float, state: dict[str, Any]) -> list[dict[str, Any]]:
    bought_today = sum(float(order.get("notional") or 0) for order in state.get("orders", []))
    total_budget = max(0.0, equity * TOTAL_BUY_PCT - bought_today)
    risk_budget = max(0.0, equity * DAILY_LOSS_PCT - (bought_today * STOP_LOSS_PCT))
    risk_capital = risk_budget / STOP_LOSS_PCT if STOP_LOSS_PCT else 0
    usable_budget = min(total_budget, risk_capital, cash)
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
            "weight": round(float(signal["strength"]) / total_strength, 4),
            "risk_amount": round(notional * STOP_LOSS_PCT, 2),
            "limit_price": price,
        })
    return orders


def apply_llm_decision(
    planned_orders: list[dict[str, Any]],
    llm_result: dict[str, Any],
    live_mode: bool,
) -> list[dict[str, Any]]:
    return planned_orders


def run_llm_decision(mode: str, payload: dict[str, Any]) -> dict[str, Any]:
    if mode == "off":
        return {"status": "disabled", "decision": {"should_trade": bool(payload.get("planned_buys")), "approved_buys": []}}
    try:
        decider = load_helper("us_market_llm_decider", "us_market_llm_decider.py")
        return decider.call_llm(payload)
    except Exception as exc:
        message = str(exc)
        message = re.sub(r"(?i)(api key:\s*)[A-Za-z0-9._-]+", r"\1<redacted>", message)
        message = re.sub(r"Bearer\s+[A-Za-z0-9._-]+", "Bearer <redacted>", message)
        return {
            "status": "error",
            "provider": "cliproxyapi",
            "error": message[:1000],
            "decision": {
                "market_regime": "unknown",
                "risk_level": "high",
                "should_trade": True,
                "approved_buys": [],
                "blocked_symbols": [],
                "notes": "LLM decision failed; automation continues without LLM gating.",
            },
        }


def annotate_llm_decisions(
    planned_orders: list[dict[str, Any]],
    executable_orders: list[dict[str, Any]],
    llm_mode: str,
    llm_result: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    effective_mode, _ = normalize_llm_mode(llm_mode)
    executable = {str(order.get("symbol")).upper(): order for order in executable_orders}
    approved_symbols = {
        str(item.get("symbol")).upper()
        for item in (llm_result or {}).get("decision", {}).get("approved_buys", [])
        if item.get("symbol")
    }
    rows = []
    for order in planned_orders:
        symbol = str(order.get("symbol")).upper()
        if effective_mode == "off":
            rows.append({**order, "order_decision": "주문"})
        elif symbol in executable and symbol in approved_symbols:
            rows.append({**executable[symbol], "order_decision": "LLM shadow 승인/주문"})
        elif symbol in executable:
            rows.append({**executable[symbol], "order_decision": "주문"})
        else:
            rows.append({**order, "order_decision": "주문 차단/미주문"})
    return rows


def place_sells(signals: list[dict[str, Any]], holdings: list[dict[str, Any]], odf) -> list[dict[str, Any]]:
    held = holdings_by_symbol(holdings)
    submitted = []
    for signal in signals:
        if signal.get("action") != "SELL" or float(signal.get("strength") or 0) < MIN_SELL_STRENGTH:
            continue
        symbol = str(signal.get("symbol")).upper()
        holding = held.get(symbol)
        qty = int(float((holding or {}).get("quantity") or 0))
        if qty <= 0:
            signal["order_status"] = "skipped_no_holding"
            submitted.append(signal)
            continue
        reference_price = float(signal.get("price") or (holding or {}).get("current_price") or 0)
        price = marketable_sell_limit(reference_price)
        if price <= 0:
            signal["order_status"] = "skipped_no_price"
            submitted.append(signal)
            continue
        result = odf.submit_order(
            symbol=symbol,
            action="SELL",
            quantity=qty,
            price=price,
            env_dv="vps",
            exchange=signal.get("exchange"),
        )
        row = result.dataframe.iloc[0].to_dict() if result.success and not result.dataframe.empty else {}
        submitted.append({
            **signal,
            "quantity": qty,
            "limit_price": price,
            "order_status": "submitted" if result.success else "failed",
            "order_no": str(row.get("ODNO") or row.get("odno") or ""),
            "last_error": None if result.success else result.display_error(),
        })
        time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
    return submitted


def wait_for_holding(odf, symbol: str, exchange: str, min_qty: int, attempts: int = 30) -> dict[str, Any] | None:
    for _ in range(attempts):
        clear_balance_cache(odf)
        holdings_df = odf.get_holdings("vps")
        if not holdings_df.empty:
            for _, row in holdings_df.iterrows():
                data = row.to_dict()
                if str(data.get("stock_code")).upper() == symbol and int(float(data.get("quantity") or 0)) >= min_qty:
                    return data
        time.sleep(3.0)
    return None


async def place_orders(
    orders: list[dict[str, Any]],
    odf,
    reserved_orders,
    upsert_protection,
    *,
    use_reservations: bool,
) -> list[dict[str, Any]]:
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

        if use_reservations:
            result = reserved_orders.submit_us_reservation(
                symbol=symbol,
                action="BUY",
                quantity=qty,
                price=price,
                order_type="limit",
                env_dv="vps",
                exchange=exchange,
            )
            time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
            order["order_status"] = "reservation_submitted" if result.success else "reservation_failed"
            order["order_method"] = "미국 모의 예약매수 limit"
            order["reservation_result"] = {
                "status": "success" if result.success else "error",
                "message": None if result.success else result.display_error(),
                "api_url": result.api_url,
                "tr_id": result.tr_id,
                "error_code": result.error_code,
                "records": result.records(),
            }
            order["protection_status"] = "not_registered_until_fill"
            submitted.append(order)
            continue

        result = odf.submit_order(
            symbol=symbol,
            action="BUY",
            quantity=qty,
            price=price,
            env_dv="vps",
            exchange=exchange,
        )
        time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
        if not result.success or result.dataframe.empty:
            order["order_status"] = "failed"
            order["last_error"] = result.display_error()
            submitted.append(order)
            continue

        row = result.dataframe.iloc[0].to_dict()
        order_no = str(row.get("ODNO", row.get("odno", "")))
        order["order_status"] = "submitted"
        order["order_method"] = "미국 모의 일반 지정가"
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


def state_order_symbols(state: dict[str, Any]) -> set[str]:
    return {
        str(order.get("symbol")).upper()
        for order in state.get("orders", [])
        if order.get("order_status") in {"submitted", "reservation_submitted"}
        and order.get("symbol")
    }


def active_protection_symbols(protective_payload: dict[str, Any]) -> set[str]:
    orders = protective_payload.get("orders", []) if isinstance(protective_payload, dict) else []
    return {
        str(order.get("stock_code")).upper()
        for order in orders
        if str(order.get("market") or "domestic") == "us"
        and str(order.get("env_dv") or "vps") == "vps"
        and order.get("status") in ACTIVE_PROTECTION_STATUSES
        and order.get("stop_loss_enabled")
        and order.get("stock_code")
    }


async def register_missing_protection_for_holdings(
    holdings: list[dict[str, Any]],
    protective_payload: dict[str, Any],
    upsert_protection,
    eligible_symbols: set[str],
) -> list[dict[str, Any]]:
    protected = active_protection_symbols(protective_payload)
    protections: list[dict[str, Any]] = []
    for row in holdings:
        symbol = str(row.get("stock_code")).upper()
        if not symbol or symbol in protected or symbol not in eligible_symbols:
            continue
        qty = int(float(row.get("quantity") or 0))
        entry_price = float(row.get("avg_price") or row.get("current_price") or 0)
        if qty <= 0 or entry_price <= 0:
            continue
        exchange = str(row.get("exchange") or "NASD")
        try:
            protection = await upsert_protection(
                env_dv="vps",
                stock_code=symbol,
                stock_name=str(row.get("stock_name") or symbol),
                quantity=qty,
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
            protections.append({"status": "success", "stock_code": symbol, "protection": protection})
            protected.add(symbol)
        except Exception as exc:
            protections.append({"status": "error", "stock_code": symbol, "message": str(exc)})
        time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
    return protections


def write_report(path: Path, payload: dict[str, Any]) -> None:
    planned_rows = payload.get("planned_buys_display", payload.get("planned_buys", []))
    post_rows = payload.get("account_after", {}).get("holdings", payload.get("account", {}).get("holdings", []))
    protective_orders = payload.get("account_after", {}).get("protective", {}).get("orders", [])
    pending_count = payload.get("account_after", {}).get("pending", {}).get("total_count", 0)
    reservation_status = payload.get("account_after", {}).get("reservations", {}).get("status", "unknown")
    catchup_protections = payload.get("catchup_protections", [])
    lines = [
        f"# US Market Auto Run - {payload['slot']}",
        "",
        f"- Time: {payload['started_at']}",
        f"- US session date: {payload['date']}",
        f"- Regime: {payload['news_summary']['regime']}",
        f"- LLM mode: {payload.get('llm_mode', 'off')}",
        f"- Effective LLM mode: {payload.get('effective_llm_mode', payload.get('llm_mode', 'off'))}",
        f"- Order mode: vps only",
        f"- Equity: ${payload['account']['equity']:,.2f}",
        f"- Cash: ${payload['account']['cash']:,.2f}",
        f"- Protective orders are app-level monitoring, not KIS server OCO; backend/auth/network health matters.",
    ]
    if payload.get("llm_warnings"):
        lines.extend(["", "## LLM Warnings"])
        lines.extend(f"- {warning}" for warning in payload.get("llm_warnings") or [])
    lines.extend(["", "## Headlines"])
    for item in payload["headlines"][:10]:
        lines.append(f"- {item['title']} ({item['source']})")
    candidate_selection = payload.get("candidate_selection") or {}
    selected_candidates = candidate_selection.get("selected") or []
    if candidate_selection:
        lines.extend([
            "",
            "## Candidate Selection",
            f"- Mode: {candidate_selection.get('mode', '-')}",
            f"- Fallback used: {candidate_selection.get('fallback_used', False)}",
            f"- Generated at: {candidate_selection.get('generated_at', '-')}",
            f"- Errors: {'; '.join(candidate_selection.get('errors') or []) or '-'}",
            "",
            "| Symbol | Exchange | Category | Score | Sources | Reasons |",
            "|---|---|---|---:|---|---|",
        ])
        for item in selected_candidates:
            lines.append(
                f"| {item.get('symbol')} | {item.get('exchange', '')} | {item.get('category', '-')} | "
                f"{float(item.get('score') or 0):.2f} | {', '.join(item.get('sources') or [])} | "
                f"{', '.join(item.get('reasons') or [])} |"
            )
    for action in ("BUY", "SELL", "HOLD", "ERROR"):
        rows = [signal for signal in payload["signals"] if signal.get("action") == action]
        if not rows:
            continue
        lines.extend(["", f"## {action} Signals", "| Symbol | Exchange | Action | Strength | Price | Reason |", "|---|---|---:|---:|---:|---|"])
        for signal in rows:
            lines.append(
                f"| {signal['symbol']} | {signal.get('exchange', '')} | {signal['action']} | "
                f"{signal.get('strength', 0):.2f} | ${float(signal.get('price') or 0):.2f} | {signal.get('reason', '')} |"
            )

    lines.extend([
        "",
        "## Pre-Buy Table",
        "| 종목 | 구분 | 신호 | 강도 | 현재가 | 예상 수량 | 예상 금액 | 배분 비중 | 익절가(+6%) | 손절가(-3%) | 주문 방식 | 주문 여부 |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ])
    if not planned_rows:
        lines.append("| - | - | - | - | - | - | - | - | - | - | - | 매수 후보 없음 |")
    for order in planned_rows:
        weight = float(order.get("weight") or 0)
        lines.append(
            f"| {order['symbol']} | {order.get('exchange', '')} | BUY | {float(order.get('strength') or 0):.2f} | "
            f"${float(order.get('price') or order.get('limit_price') or 0):.2f} | {int(order.get('quantity') or 0)} | "
            f"${float(order.get('notional') or 0):,.2f} | {weight*100:.1f}% | "
            f"${float(order.get('take_profit') or 0):.2f} | ${float(order.get('stop_loss') or 0):.2f} | "
            f"{order.get('order_method') or payload.get('planned_order_method', '미국 모의 지정가')} | {order.get('order_decision', '주문')} |"
        )

    lines.extend(["", "## Submitted Orders", "| Symbol | Qty | Notional | Status | TP | SL | Protection | Error |", "|---|---:|---:|---|---:|---:|---|---|"])
    for order in payload["orders"]:
        lines.append(
            f"| {order['symbol']} | {order.get('quantity', 0)} | ${order.get('notional', 0):,.2f} | "
            f"{order.get('order_status')} | ${float(order.get('take_profit') or 0):.2f} | "
            f"${float(order.get('stop_loss') or 0):.2f} | {order.get('protection_status', '')} | {order.get('last_error') or ''} |"
        )
    if catchup_protections:
        lines.extend(["", "## Catch-Up Protections", "| Symbol | Status | Protection Status | Error |", "|---|---|---|---|"])
        for item in catchup_protections:
            protection = item.get("protection") if isinstance(item.get("protection"), dict) else {}
            lines.append(
                f"| {item.get('stock_code')} | {item.get('status')} | "
                f"{protection.get('status', '')} | {item.get('message', '')} |"
            )
    lines.extend([
        "",
        "## Post-Fill Status",
        "| 종목 | 체결 수량 | 평균단가 | 익절 지정가 | 손절 지정가 | 보호주문 상태 | 예약/미체결 상태 |",
        "|---|---:|---:|---:|---:|---|---|",
    ])
    for row in post_rows:
        symbol = str(row.get("stock_code") or "").upper()
        protection = next((item for item in reversed(protective_orders) if str(item.get("stock_code")).upper() == symbol), {})
        lines.append(
            f"| {symbol} | {int(float(row.get('quantity') or 0))} | ${float(row.get('avg_price') or 0):.2f} | "
            f"${float(protection.get('take_profit_price') or protection.get('take_profit_limit_price') or 0):.2f} | "
            f"${float(protection.get('stop_loss_price') or protection.get('stop_loss_limit_price') or 0):.2f} | "
            f"{protection.get('status', 'none')} | reservations={reservation_status}, pending={pending_count} |"
        )
    reservation_errors = payload.get("account_after", {}).get("reservations", {}).get("errors", [])
    if reservation_errors:
        lines.extend(["", "## Reservation API Errors"])
        for error in reservation_errors:
            lines.append(f"- {error}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_market_closed_report(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        f"# US Market Auto Run - {payload['slot']}",
        "",
        f"- Time: {payload['started_at']}",
        f"- US session date: {payload['date']}",
        "- Regime: market_closed",
        f"- LLM mode: {payload.get('llm_mode', 'off')}",
        f"- Effective LLM mode: {payload.get('effective_llm_mode', payload.get('llm_mode', 'off'))}",
        "- Order mode: vps only",
        "",
        "## Market Closed",
        f"- Source: {payload.get('trading_day', {}).get('source')}",
        f"- Status: {payload.get('trading_day', {}).get('status')}",
        f"- Reason: {payload.get('trading_day', {}).get('record')}",
    ]
    if payload.get("llm_warnings"):
        lines.extend(["", "## LLM Warnings"])
        lines.extend(f"- {warning}" for warning in payload.get("llm_warnings") or [])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def cleanup_cron_if_done(state: dict[str, Any], session_date: str) -> None:
    slots = {run.get("slot") for run in state.get("runs", [])}
    if not {"open", "mid", "close"}.issubset(slots):
        return
    os.system(f"(crontab -l 2>/dev/null | grep -v 'KIS_US_MARKET_AUTO_{session_date}') | crontab -")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True, choices=["open", "mid", "close", "manual"])
    parser.add_argument("--date", required=True, help="US session date key in YYYYMMDD, shared by all slots")
    parser.add_argument("--llm-mode", choices=["off", "shadow", "live-vps", "live-prod"], default=os.environ.get("US_MARKET_LLM_MODE", "off"))
    args = parser.parse_args()
    effective_llm_mode, llm_warnings = normalize_llm_mode(args.llm_mode)

    now = datetime.now(ZoneInfo("Asia/Seoul"))
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    report_base = RUNTIME_DIR / f"{now.strftime('%Y%m%d_%H%M%S')}_{args.slot}"
    state_path = session_state_path(args.date)
    state = load_today_state(state_path)

    calendar = load_helper("us_market_calendar", "us_market_calendar.py")
    trading_day = calendar.market_status(args.date)
    if not trading_day.get("is_open"):
        payload = {
            "slot": args.slot,
            "date": args.date,
            "started_at": now.isoformat(timespec="seconds"),
            "llm_mode": args.llm_mode,
            "effective_llm_mode": effective_llm_mode,
            "llm_warnings": llm_warnings,
            "trading_day": trading_day,
            "news_summary": {"regime": "market_closed"},
            "headlines": [],
            "signals": [],
            "planned_buys": [],
            "orders": [],
            "candidate_selection": {
                "mode": os.environ.get("US_MARKET_CANDIDATE_MODE", "dynamic"),
                "selected": [],
                "fallback_used": False,
                "errors": [],
                "generated_at": now.isoformat(timespec="seconds"),
            },
        }
        state.setdefault("runs", []).append({
            "slot": args.slot,
            "started_at": payload["started_at"],
            "report": str(report_base.with_suffix(".md")),
            "market_closed": True,
            "trading_day": trading_day,
        })
        save_today_state(state_path, state)
        report_base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        write_market_closed_report(report_base.with_suffix(".md"), payload)
        cleanup_cron_if_done(state, args.date)
        return 0

    ka, indicators, odf, reserved_orders, upsert_protection, list_protective_orders = load_modules()
    ka.auth(svr="vps")

    headlines = fetch_headlines()
    news_summary = summarize_news(headlines)
    account_before = await account_status(odf, reserved_orders, list_protective_orders)
    equity = float(account_before["equity"])
    cash = float(account_before["cash"])
    holdings = account_before["holdings"]
    before_symbols = set(holdings_by_symbol(holdings).keys())
    candidate_selection = select_us_candidates(
        ranking_fetcher=odf,
        holdings=holdings,
        static_candidates=STATIC_CANDIDATES,
    )
    signals = []
    for candidate in candidate_selection.get("selected", []):
        symbol = str(candidate.get("symbol") or candidate.get("code")).upper()
        exchange = str(candidate.get("exchange") or "NASD")
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
    submitted_sells = place_sells(signals, holdings, odf)
    planned_orders = build_orders(signals, equity, cash, state)

    pre_payload = {
        "slot": args.slot,
        "date": args.date,
        "started_at": now.isoformat(timespec="seconds"),
        "headlines": headlines,
        "news_summary": news_summary,
        "llm_mode": args.llm_mode,
        "effective_llm_mode": effective_llm_mode,
        "llm_warnings": llm_warnings,
        "trading_day": trading_day,
        "strategy": {
            "name": "US news-aware EMA/ROC/RSI trend filter",
            "entry": "EMA20 > EMA50 and ROC20 > 4 and 50 < RSI14 < 72",
            "exit": "EMA20 < EMA50 or ROC20 < -3 or RSI14 > 78",
            "risk": {
                "total_new_buy_pct": round(TOTAL_BUY_PCT * 100, 2),
                "daily_loss_pct": round(DAILY_LOSS_PCT * 100, 2),
                "take_profit_pct": round(TAKE_PROFIT_PCT * 100, 2),
                "stop_loss_pct": round(STOP_LOSS_PCT * 100, 2),
                "per_symbol_cap": None,
            },
        },
        "account": {"equity": equity, "cash": cash, "holdings": holdings},
        "account_before": account_before,
        "candidate_selection": candidate_selection,
        "signals": signals,
        "submitted_sells": submitted_sells,
        "planned_buys": planned_orders,
        "orders": [],
    }
    llm_result = run_llm_decision(effective_llm_mode, pre_payload)
    executable_orders = apply_llm_decision(planned_orders, llm_result, live_mode=False)

    regular_hours = us_regular_hours_now(now)
    submitted_orders = await place_orders(
        executable_orders,
        odf,
        reserved_orders,
        upsert_protection,
        use_reservations=not regular_hours,
    )
    clear_balance_cache(odf)
    account_after = await account_status(odf, reserved_orders, list_protective_orders)
    eligible_protection_symbols = (
        before_symbols
        | {str(item.get("symbol") or item.get("code")).upper() for item in candidate_selection.get("selected", [])}
        | state_order_symbols(state)
        | {str(order.get("symbol")).upper() for order in submitted_orders if order.get("symbol")}
    )
    catchup_protections = await register_missing_protection_for_holdings(
        account_after["holdings"],
        account_after.get("protective", {}),
        upsert_protection,
        eligible_protection_symbols,
    )
    if catchup_protections:
        clear_balance_cache(odf)
        account_after = await account_status(odf, reserved_orders, list_protective_orders)
    planned_display = annotate_llm_decisions(planned_orders, executable_orders, effective_llm_mode, llm_result)

    payload = {
        **pre_payload,
        "llm": llm_result,
        "planned_order_method": "미국 모의 일반 지정가" if regular_hours else "미국 모의 예약매수 limit",
        "planned_buys_display": planned_display,
        "orders": submitted_orders,
        "catchup_protections": catchup_protections,
        "account_after": account_after,
    }

    state.setdefault("runs", []).append({
        "slot": args.slot,
        "started_at": payload["started_at"],
        "report": str(report_base.with_suffix(".md")),
        "orders": submitted_orders,
    })
    state.setdefault("orders", []).extend(
        order for order in submitted_orders if order.get("order_status") in {"submitted", "reservation_submitted"}
    )
    save_today_state(state_path, state)
    report_base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
    write_report(report_base.with_suffix(".md"), payload)
    cleanup_cron_if_done(state, args.date)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
