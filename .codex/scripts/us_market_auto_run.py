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
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timedelta
from datetime import time as dt_time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests
from market_candidate_selector import select_us_candidates
from organic_strategy_router import (
    execute_strategy_pool,
    explain_order_decisions,
    merge_us_anchor_signals,
    select_strategy_candidates,
)

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
MAX_BUY_ROC20_PCT = 25.0
MAX_INTRADAY_BUY_DROP_PCT = 3.0
CRASH_EXIT_DROP_PCT = 5.0
MAX_NEW_BUY_SYMBOLS = 2
MAX_PER_SYMBOL_BUY_PCT = 0.01
MAX_TOTAL_HOLDINGS_EXPOSURE_PCT = 0.75
MAX_SECTOR_EXPOSURE_PCT = 0.25
MAX_SESSION_EQUITY_DEVIATION_PCT = 0.50
RAW_BALANCE_ANOMALY_MULTIPLIER = 3.0
MARKET_BENCHMARK_DROP_PCT = 2.0
MARKET_SINGLE_CRASH_PCT = 3.0
MARKET_BREADTH_DROP_PCT = 2.0
MARKET_BREADTH_RISK_RATIO = 0.50
MIN_MARKET_BREADTH_SYMBOLS = 4
MARKETABLE_SELL_LIMIT_BUFFER_PCT = 0.02
MIN_SECONDS_BETWEEN_KIS_CALLS = 0.85
US_EXCHANGES = ("NASD", "NYSE", "AMEX")
ACTIVE_PROTECTION_STATUSES = {"active", "exit_submitted"}
ACTIVE_APP_RESERVATION_STATUSES = {"scheduled", "submitting"}
ACTIVE_PENDING_ACTIONS = {"BUY", "SELL"}
DETAIL_RETENTION_DAYS = 30
DEFAULT_REPORT_URL = "http://ww.tailea9a3f.ts.net:8081"
US_SECTOR_BY_SYMBOL = {
    "SPY": "broad_etf",
    "QQQ": "broad_etf",
    "DIA": "broad_etf",
    "IWM": "broad_etf",
    "NVDA": "technology",
    "MSFT": "technology",
    "AVGO": "technology",
    "AMD": "technology",
    "GOOGL": "technology",
    "META": "technology",
    "AAPL": "technology",
    "ORCL": "technology",
    "NOW": "technology",
    "DELL": "technology",
    "MU": "technology",
    "MRVL": "technology",
    "AMZN": "consumer",
    "COST": "consumer",
    "TSLA": "consumer",
    "JPM": "financials",
    "V": "financials",
    "XOM": "energy",
    "CVX": "energy",
    "LLY": "healthcare",
}


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


US_ORGANIC_STRATEGY_MAX_SYMBOLS = env_int("US_MARKET_ORGANIC_MAX_SYMBOLS", 3)


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


def strategy_api_base() -> str:
    return (
        os.environ.get("KIS_VPS_STRATEGY_API")
        or os.environ.get("KIS_STRATEGY_API")
        or "http://127.0.0.1:8081"
    ).rstrip("/")


def strategy_api(method: str, path: str, **kwargs) -> dict[str, Any]:
    response = requests.request(method, f"{strategy_api_base()}{path}", timeout=90, **kwargs)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {"status": "error", "message": "non-object API response"}


def load_modules():
    sys.path.insert(0, str(STRATEGY_BUILDER))
    import kis_auth as ka
    from backend.services.app_reservations import create_app_reservation, list_app_reservations
    from backend.services.protective_orders import (
        list_protective_orders,
        run_monitor_cycle,
        upsert_existing_position_protection,
    )
    from core import indicators, overseas_data_fetcher

    return (
        ka,
        indicators,
        overseas_data_fetcher,
        create_app_reservation,
        upsert_existing_position_protection,
        list_protective_orders,
        list_app_reservations,
        run_monitor_cycle,
    )


def load_helper(module_name: str, filename: str):
    path = PROJECT_ROOT / ".codex" / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def us_regular_hours_now(now: datetime | None = None) -> bool:
    """Legacy manual-slot regular-session approximation.

    Hourly cron runs are resolved by exchange_calendars before this helper is
    reached. This remains for open/mid/close/manual compatibility.
    """
    kst_now = now.astimezone(ZoneInfo("Asia/Seoul")) if now else datetime.now(ZoneInfo("Asia/Seoul"))
    current = kst_now.time()
    return current >= dt_time(22, 30) or current <= dt_time(6, 0)


def next_us_regular_open_kst(calendar, now: datetime | None = None) -> str:
    current = now or datetime.now(ZoneInfo("Asia/Seoul"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    current = current.astimezone(ZoneInfo("Asia/Seoul"))
    current_et = current.astimezone(ZoneInfo("America/New_York"))
    for offset in range(0, 10):
        session_date = (current_et.date() + timedelta(days=offset)).strftime("%Y%m%d")
        status = calendar.market_status(session_date)
        open_kst = status.get("record", {}).get("open_kst")
        if not status.get("is_open") or not open_kst:
            continue
        opened = datetime.fromisoformat(open_kst).astimezone(ZoneInfo("Asia/Seoul"))
        if opened > current:
            return opened.isoformat(timespec="seconds")
    raise RuntimeError("next US regular session open could not be resolved")


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


def infer_us_sector(symbol: Any, candidate: dict[str, Any] | None = None) -> str:
    normalized = str(symbol or "").strip().upper()
    if normalized in US_SECTOR_BY_SYMBOL:
        return US_SECTOR_BY_SYMBOL[normalized]
    category = str((candidate or {}).get("category") or "").strip().lower()
    if "etf" in category:
        return "broad_etf"
    return "other"


def candidate_quality(candidate: dict[str, Any]) -> tuple[bool, str]:
    sources = {str(item) for item in candidate.get("sources") or []}
    if sources & {"static", "core_etf", "holding"}:
        return True, "core/static/holding"
    liquidity_sources = {"trade_value_rank", "market_cap_rank"}
    if not sources & liquidity_sources:
        return False, "no trade-value or market-cap liquidity source"
    if sources == {"volume_surge_rank"} or sources == {"volume_power_rank"}:
        return False, "single short-term volume source"
    return True, "liquidity-ranked"


def apply_candidate_quality_gate(selection: dict[str, Any]) -> dict[str, Any]:
    accepted = []
    rejected = []
    for candidate in selection.get("selected") or []:
        allowed, reason = candidate_quality(candidate)
        item = {
            **candidate,
            "sector": infer_us_sector(candidate.get("symbol") or candidate.get("code"), candidate),
            "quality_reason": reason,
        }
        if allowed:
            accepted.append(item)
        else:
            rejected.append(item)
    return {
        **selection,
        "selected": accepted,
        "rejected": rejected,
        "quality_gate": {
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "requires_liquidity_source": True,
        },
    }


def evaluate_market_risk(
    signals: list[dict[str, Any]],
    news_summary: dict[str, Any],
) -> dict[str, Any]:
    valid = [
        signal for signal in signals
        if signal.get("action") != "ERROR"
        and isinstance(signal.get("intraday_change_pct"), (int, float))
    ]
    benchmarks = {
        str(signal.get("symbol") or "").upper(): float(signal.get("intraday_change_pct") or 0)
        for signal in valid
        if str(signal.get("symbol") or "").upper() in {"SPY", "QQQ"}
    }
    changes = [float(signal.get("intraday_change_pct") or 0) for signal in valid]
    breadth_drop_count = sum(change <= -MARKET_BREADTH_DROP_PCT for change in changes)
    breadth_ratio = breadth_drop_count / len(changes) if changes else 1.0
    reasons: list[str] = []
    warnings: list[str] = []
    benchmark_average = (
        sum(benchmarks.values()) / len(benchmarks)
        if benchmarks
        else 0.0
    )

    if len(benchmarks) < 2:
        reasons.append("SPY/QQQ benchmark data unavailable")
    if benchmarks and benchmark_average <= -MARKET_BENCHMARK_DROP_PCT:
        reasons.append(f"SPY/QQQ average drop >= {MARKET_BENCHMARK_DROP_PCT:.1f}%")
    if any(change <= -MARKET_SINGLE_CRASH_PCT for change in benchmarks.values()):
        reasons.append(f"SPY or QQQ drop >= {MARKET_SINGLE_CRASH_PCT:.1f}%")
    if len(changes) >= MIN_MARKET_BREADTH_SYMBOLS and breadth_ratio >= MARKET_BREADTH_RISK_RATIO:
        reasons.append(
            f"market breadth drop ratio {breadth_ratio * 100:.0f}% "
            f"({breadth_drop_count}/{len(changes)})"
        )
    if news_summary.get("regime") == "risk_control":
        if len(benchmarks) == 2 and benchmark_average <= -0.5:
            reasons.append("news risk confirmed by negative SPY/QQQ")
        else:
            warnings.append("news risk was not confirmed by market prices")

    fallback_regime = str(news_summary.get("regime") or "broad_momentum")
    if fallback_regime == "risk_control":
        fallback_regime = "headline_caution"
    return {
        "regime": "risk_control" if reasons else fallback_regime,
        "risk_gate_open": not reasons,
        "reasons": reasons,
        "warnings": warnings,
        "benchmark_changes": benchmarks,
        "benchmark_average_change_pct": round(benchmark_average, 2),
        "breadth_drop_count": breadth_drop_count,
        "breadth_total": len(changes),
        "breadth_drop_ratio": round(breadth_ratio, 4),
        "news_regime": news_summary.get("regime"),
    }


def _last_completed_close(df, price_info: dict[str, Any]) -> float:
    previous_close = float(
        price_info.get("previous_close")
        or price_info.get("base")
        or 0
    )
    if previous_close > 0:
        return previous_close
    price = float(price_info.get("price") or 0)
    change_rate = float(price_info.get("change_rate") or 0)
    if price > 0 and change_rate > -100:
        derived = price / (1 + change_rate / 100)
        if derived > 0 and change_rate != 0:
            return derived
    if "date" in df.columns and len(df) >= 2:
        latest_date = str(df["date"].iloc[-1]).replace("-", "")[:8]
        market_date = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
        if latest_date >= market_date:
            return float(df["close"].iloc[-2])
    return float(df["close"].iloc[-1])


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
    previous_close = _last_completed_close(df, price_info)
    intraday_change_pct = (
        ((price / previous_close) - 1) * 100
        if price > 0 and previous_close > 0
        else 0.0
    )
    distance_from_ema20_pct = (
        ((price / ema20) - 1) * 100
        if price > 0 and ema20 > 0
        else 0.0
    )

    entry_trend = ema20 > ema50 and roc20 > 4 and 50 < rsi14 < 72
    overextended = roc20 > MAX_BUY_ROC20_PCT
    below_ema20 = price < ema20
    intraday_drop = intraday_change_pct <= -MAX_INTRADAY_BUY_DROP_PCT
    crash_exit = intraday_change_pct <= -CRASH_EXIT_DROP_PCT
    buy = entry_trend and not overextended and not below_ema20 and not intraday_drop
    sell = crash_exit or ema20 < ema50 or roc20 < -3 or rsi14 > 78
    if buy:
        strength = min(0.95, 0.55 + min(max((roc20 - 4) / 20, 0), 0.25) + min(max((rsi14 - 50) / 44, 0), 0.15))
        action = "BUY"
        reason = f"EMA20 {ema20:.2f}>EMA50 {ema50:.2f}, ROC20 {roc20:.2f}%>4, RSI14 {rsi14:.2f}"
    elif sell:
        strength = 0.65
        action = "SELL"
        reason = (
            f"Exit filter: EMA20 {ema20:.2f}, EMA50 {ema50:.2f}, "
            f"ROC20 {roc20:.2f}%, RSI14 {rsi14:.2f}, intraday {intraday_change_pct:.2f}%"
        )
    else:
        strength = 0.25
        action = "HOLD"
        blocked = []
        if overextended:
            blocked.append(f"ROC20>{MAX_BUY_ROC20_PCT:.0f}%")
        if below_ema20:
            blocked.append("current<EMA20")
        if intraday_drop:
            blocked.append(f"intraday<=-{MAX_INTRADAY_BUY_DROP_PCT:.0f}%")
        blocked_text = f", blocked={'+'.join(blocked)}" if blocked else ""
        reason = (
            f"No entry: EMA20 {ema20:.2f}, EMA50 {ema50:.2f}, "
            f"ROC20 {roc20:.2f}%, RSI14 {rsi14:.2f}, intraday {intraday_change_pct:.2f}%"
            f"{blocked_text}"
        )

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
        "previous_close": round(previous_close, 2),
        "intraday_change_pct": round(intraday_change_pct, 2),
        "distance_from_ema20_pct": round(distance_from_ema20_pct, 2),
    }


def session_state_path(session_date: str) -> Path:
    return RUNTIME_DIR / f"{session_date}.json"


def load_today_state(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                state.setdefault("runs", [])
                state.setdefault("orders", [])
                state.setdefault("events", [])
                state.setdefault("active_runs", {})
                return state
        except (OSError, json.JSONDecodeError):
            pass
    return {"runs": [], "orders": [], "events": [], "active_runs": {}}


def save_today_state(path: Path, state: dict[str, Any]) -> None:
    atomic_write_json(path, state)


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def atomic_write_json(path: Path, payload: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default) + "\n",
    )


def run_already_recorded(state: dict[str, Any], run_id: str) -> bool:
    return (
        run_id in state.get("active_runs", {})
        or any(run.get("run_id") == run_id for run in state.get("runs", []))
    )


def record_skip_event(state_path: Path, run_id: str, status: str, slot: str) -> None:
    state = load_today_state(state_path)
    state.setdefault("events", []).append({
        "run_id": run_id,
        "slot": slot,
        "status": status,
        "at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
    })
    save_today_state(state_path, state)


def account_equity_snapshot(odf) -> dict[str, Any]:
    deposit = odf.get_deposit("vps") or {}
    time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
    holdings_df = odf.get_holdings("vps")
    time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
    holdings = [] if holdings_df.empty else holdings_df.to_dict("records")
    holdings_value = 0.0
    for row in holdings:
        market_value = float(row.get("quantity") or 0) * float(row.get("current_price") or 0)
        holdings_value += market_value if market_value > 0 else float(row.get("eval_amount") or 0)
    deposit_cash = float(deposit.get("deposit") or 0)
    reported_orderable_cash = float(deposit.get("available_amount") or deposit.get("orderable_amount") or 0)
    orderable_cash = 0.0
    orderable_source = None
    buyable_error = None
    try:
        buyable = odf.get_buyable_amount("NVDA", 100, env_dv="vps", exchange="NASD") or {}
        time.sleep(MIN_SECONDS_BETWEEN_KIS_CALLS)
        orderable_cash = float(buyable.get("amount") or 0)
        if orderable_cash > 0:
            orderable_source = "get_buyable_amount:NVDA"
    except Exception as exc:
        buyable_error = str(exc)
    cash = orderable_cash
    total_eval = float(deposit.get("total_eval") or 0)
    equity = cash + holdings_value
    risk_equity_trusted = orderable_cash > 0
    raw_balance_max = max(deposit_cash, reported_orderable_cash, total_eval)
    balance_anomaly = bool(
        equity > 0
        and raw_balance_max > equity * RAW_BALANCE_ANOMALY_MULTIPLIER
    )
    sources = []
    if holdings_value > 0:
        sources.append("holdings.quantity_x_current_price")
    if orderable_source:
        sources.append(orderable_source)
    return {
        "equity": equity,
        "cash": cash,
        "holdings": holdings,
        "holdings_value": holdings_value,
        "deposit_cash": deposit_cash,
        "reported_orderable_cash": reported_orderable_cash,
        "orderable_cash": orderable_cash,
        "risk_equity": equity,
        "risk_equity_sources": sources or ["unavailable"],
        "risk_equity_trusted": risk_equity_trusted,
        "balance_anomaly": balance_anomaly,
        "buyable_error": buyable_error,
        "deposit_api": deposit,
    }


def apply_session_risk_baseline(
    snapshot: dict[str, Any],
    state: dict[str, Any],
) -> dict[str, Any]:
    current = float(snapshot.get("risk_equity") or 0)
    trusted = bool(snapshot.get("risk_equity_trusted")) and current > 0
    baseline = float(state.get("validated_risk_equity") or 0)
    blocked_reason = None

    if not trusted:
        blocked_reason = "verified USD buyable amount unavailable"
    elif baseline <= 0:
        baseline = current
        state["validated_risk_equity"] = round(current, 2)
    else:
        deviation = abs(current - baseline) / baseline
        if deviation > MAX_SESSION_EQUITY_DEVIATION_PCT:
            blocked_reason = (
                f"risk equity changed {deviation * 100:.1f}% from session baseline "
                f"${baseline:,.2f}"
            )

    validated = min(current, baseline) if current > 0 and baseline > 0 else 0.0
    snapshot["validated_risk_equity"] = validated
    snapshot["risk_gate_open"] = blocked_reason is None
    snapshot["risk_gate_reason"] = blocked_reason
    return snapshot


def account_equity(odf) -> tuple[float, float, list[dict[str, Any]]]:
    snapshot = account_equity_snapshot(odf)
    return snapshot["equity"], snapshot["cash"], snapshot["holdings"]


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


def _normalized_action(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in {"BUY", "매수", "02", "2"} or "BUY" in text or "매수" in text:
        return "BUY"
    if text in {"SELL", "매도", "01", "1"} or "SELL" in text or "매도" in text:
        return "SELL"
    return text


def symbols_for_action(rows: list[dict[str, Any]], action: str) -> set[str]:
    desired = action.upper()
    symbols: set[str] = set()
    for row in rows:
        row_action = _normalized_action(
            row.get("action")
            or row.get("order_type")
            or row.get("SLL_BUY_DVSN_NAME")
            or row.get("sll_buy_dvsn_cd_name")
        )
        if row_action != desired:
            continue
        symbol = str(
            row.get("stock_code")
            or row.get("symbol")
            or row.get("pdno")
            or row.get("ovrs_pdno")
            or ""
        ).upper()
        if symbol:
            symbols.add(symbol)
    return symbols


def holding_market_value(holding: dict[str, Any]) -> float:
    quantity = float(holding.get("quantity") or 0)
    current_price = float(holding.get("current_price") or holding.get("avg_price") or 0)
    calculated = quantity * current_price
    return calculated if calculated > 0 else float(holding.get("eval_amount") or 0)


def portfolio_risk_snapshot(
    holdings: list[dict[str, Any]],
    equity: float,
) -> dict[str, Any]:
    total_value = 0.0
    sector_values: dict[str, float] = {}
    symbol_values: dict[str, float] = {}
    for holding in holdings:
        symbol = str(holding.get("stock_code") or "").upper()
        value = holding_market_value(holding)
        if not symbol or value <= 0:
            continue
        sector = infer_us_sector(symbol)
        total_value += value
        symbol_values[symbol] = symbol_values.get(symbol, 0.0) + value
        sector_values[sector] = sector_values.get(sector, 0.0) + value
    denominator = equity if equity > 0 else 1.0
    return {
        "holdings_value": round(total_value, 2),
        "holdings_exposure_pct": round(total_value / denominator * 100, 2),
        "symbol_values": {key: round(value, 2) for key, value in symbol_values.items()},
        "sector_values": {key: round(value, 2) for key, value in sector_values.items()},
        "sector_exposure_pct": {
            key: round(value / denominator * 100, 2)
            for key, value in sector_values.items()
        },
        "total_exposure_gate_open": total_value < equity * MAX_TOTAL_HOLDINGS_EXPOSURE_PCT,
    }


def compact_protective_payload(payload: dict[str, Any], event_limit: int = 20) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"orders": [], "settings": {}}
    compacted = {key: value for key, value in payload.items() if key != "orders"}
    compacted_orders = []
    for order in payload.get("orders", []):
        if not isinstance(order, dict):
            continue
        item = dict(order)
        events = item.get("events") if isinstance(item.get("events"), list) else []
        item["event_count"] = len(events)
        item["events"] = events[-event_limit:]
        compacted_orders.append(item)
    compacted["orders"] = compacted_orders
    return compacted


async def account_status(
    odf,
    list_protective_orders,
    list_app_reservations=None,
    run_protective_monitor_cycle=None,
) -> dict[str, Any]:
    equity_snapshot = account_equity_snapshot(odf)
    pending = list_pending_by_exchange(odf)
    reservations = {
        "status": "not_applicable",
        "orders": [],
        "errors": [],
        "total_count": 0,
        "policy": "vps_app_reservations_only",
    }
    app_reservations: dict[str, Any] = {"status": "success", "orders": [], "total_count": 0}
    if list_app_reservations is not None:
        try:
            app_rows = await list_app_reservations(market="us", include_cancelled=False)
            app_reservations["orders"] = [
                row for row in app_rows
                if row.get("env_dv") == "vps" and row.get("status") in ACTIVE_APP_RESERVATION_STATUSES
            ]
            app_reservations["total_count"] = len(app_reservations["orders"])
        except Exception as exc:
            app_reservations = {"status": "error", "orders": [], "total_count": 0, "errors": [str(exc)]}
    protective = compact_protective_payload(await list_protective_orders())
    health = protective.get("health") if isinstance(protective, dict) else {}
    if (
        callable(run_protective_monitor_cycle)
        and isinstance(health, dict)
        and (health.get("stale") or health.get("status") == "stale")
    ):
        try:
            await run_protective_monitor_cycle()
            protective = compact_protective_payload(await list_protective_orders())
        except Exception as exc:
            health = protective.setdefault("health", {})
            health["refresh_error"] = str(exc)
    return {
        **equity_snapshot,
        "pending": pending,
        "reservations": reservations,
        "app_reservations": app_reservations,
        "protective": protective,
    }


def build_orders(
    signals: list[dict[str, Any]],
    equity: float,
    cash: float,
    state: dict[str, Any],
    excluded_symbols: set[str] | None = None,
    holdings: list[dict[str, Any]] | None = None,
    *,
    market_regime: str = "broad_momentum",
    risk_gate_open: bool = True,
) -> list[dict[str, Any]]:
    if not risk_gate_open or market_regime == "risk_control":
        return []
    excluded_symbols = {symbol.upper() for symbol in (excluded_symbols or set())}
    bought_today = sum(float(order.get("notional") or 0) for order in state.get("orders", []))
    total_budget = max(0.0, equity * TOTAL_BUY_PCT - bought_today)
    risk_budget = max(0.0, equity * DAILY_LOSS_PCT - (bought_today * STOP_LOSS_PCT))
    risk_capital = risk_budget / STOP_LOSS_PCT if STOP_LOSS_PCT else 0
    usable_budget = min(total_budget, risk_capital, cash)
    buy_signals = [
        signal for signal in signals
        if signal["action"] == "BUY"
        and str(signal.get("symbol") or "").upper() not in excluded_symbols
        and float(signal.get("strength") or 0) >= MIN_BUY_STRENGTH
        and float(signal.get("price") or 0) > 0
    ]
    buy_signals.sort(key=lambda item: float(item["strength"]), reverse=True)
    selected_signals = buy_signals[:MAX_NEW_BUY_SYMBOLS]
    total_strength = sum(float(item["strength"]) for item in selected_signals) or 1

    orders: list[dict[str, Any]] = []
    remaining = usable_budget
    per_symbol_cap = max(0.0, equity * MAX_PER_SYMBOL_BUY_PCT)
    exposure = portfolio_risk_snapshot(holdings or [], equity)
    remaining_total_exposure = max(
        0.0,
        equity * MAX_TOTAL_HOLDINGS_EXPOSURE_PCT - float(exposure["holdings_value"]),
    )
    sector_values = dict(exposure["sector_values"])
    for signal in selected_signals:
        price = float(signal["price"])
        sector = str(signal.get("sector") or infer_us_sector(signal.get("symbol")))
        remaining_sector_exposure = max(
            0.0,
            equity * MAX_SECTOR_EXPOSURE_PCT - float(sector_values.get(sector, 0)),
        )
        target_notional = min(
            usable_budget * float(signal["strength"]) / total_strength,
            remaining,
            per_symbol_cap,
            remaining_total_exposure,
            remaining_sector_exposure,
        )
        qty = int(target_notional // price)
        if qty <= 0:
            continue
        notional = round(qty * price, 2)
        remaining -= notional
        remaining_total_exposure -= notional
        sector_values[sector] = float(sector_values.get(sector, 0)) + notional
        orders.append({
            **signal,
            "sector": sector,
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


def place_sells(
    signals: list[dict[str, Any]],
    holdings: list[dict[str, Any]],
    odf,
    pending_sell_symbols: set[str] | None = None,
) -> list[dict[str, Any]]:
    held = holdings_by_symbol(holdings)
    pending_sell_symbols = {symbol.upper() for symbol in (pending_sell_symbols or set())}
    submitted = []
    for signal in signals:
        if signal.get("action") != "SELL" or float(signal.get("strength") or 0) < MIN_SELL_STRENGTH:
            continue
        symbol = str(signal.get("symbol")).upper()
        if symbol in pending_sell_symbols:
            submitted.append({**signal, "order_status": "skipped_pending_sell"})
            continue
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
    create_app_reservation,
    upsert_protection,
    *,
    use_reservations: bool,
    reservation_scheduled_at: str | None = None,
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
            try:
                reservation = await create_app_reservation(
                    market="us",
                    stock_code=symbol,
                    stock_name=str(order.get("stock_name") or symbol),
                    action="BUY",
                    quantity=qty,
                    price=price,
                    order_type="limit",
                    exchange=exchange,
                    scheduled_at=reservation_scheduled_at,
                    authenticated_user="us_market_auto_run",
                )
                order["order_status"] = "reservation_submitted"
                order["order_method"] = "미국 모의 앱 예약매수 limit"
                order["reservation_result"] = {
                    "status": "success",
                    "reservation_source": "app",
                    "reservation_id": reservation.get("id"),
                    "scheduled_at": reservation.get("scheduled_at"),
                }
            except Exception as exc:
                order["order_status"] = "reservation_failed"
                order["order_method"] = "미국 모의 앱 예약매수 limit"
                order["last_error"] = str(exc)
                order["reservation_result"] = {
                    "status": "error",
                    "reservation_source": "app",
                    "message": str(exc),
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
        f"# US Market Auto Run - {payload.get('run_id', payload['slot'])}",
        "",
        f"- Status: {payload.get('status', 'completed')}",
        f"- Time: {payload['started_at']}",
        f"- Duration: {float(payload.get('duration_seconds') or 0):.1f}s",
        f"- US session date: {payload['date']}",
        f"- Regime: {payload.get('market_risk', {}).get('regime', payload['news_summary']['regime'])}",
        f"- LLM mode: {payload.get('llm_mode', 'off')}",
        f"- Effective LLM mode: {payload.get('effective_llm_mode', payload.get('llm_mode', 'off'))}",
        "- Order mode: vps only",
        f"- Equity: ${payload['account']['equity']:,.2f}",
        f"- Cash: ${payload['account']['cash']:,.2f}",
        f"- Risk equity source: {', '.join(payload.get('account_before', {}).get('risk_equity_sources', [])) or '-'}",
        "- Protective orders are app-level monitoring, not KIS server OCO; backend/auth/network health matters.",
    ]
    if payload.get("llm_warnings"):
        lines.extend(["", "## LLM Warnings"])
        lines.extend(f"- {warning}" for warning in payload.get("llm_warnings") or [])
    market_risk = payload.get("market_risk") or {}
    if market_risk:
        lines.extend([
            "",
            "## Market Risk Gate",
            f"- Gate open: {market_risk.get('risk_gate_open', False)}",
            f"- SPY/QQQ: {market_risk.get('benchmark_changes', {})}",
            f"- Breadth drop: {float(market_risk.get('breadth_drop_ratio') or 0) * 100:.1f}%",
            f"- Reasons: {'; '.join(market_risk.get('reasons') or []) or '-'}",
        ])
    portfolio_risk = payload.get("portfolio_risk") or {}
    if portfolio_risk:
        lines.extend([
            "",
            "## Portfolio Exposure",
            f"- Holdings exposure: {float(portfolio_risk.get('holdings_exposure_pct') or 0):.2f}%",
            f"- Sector exposure: {portfolio_risk.get('sector_exposure_pct', {})}",
            f"- Total exposure gate open: {portfolio_risk.get('total_exposure_gate_open', False)}",
        ])
    strategy_orchestration = payload.get("strategy_orchestration") or {}
    if strategy_orchestration:
        enabled = strategy_orchestration.get("enabled") or []
        disabled = strategy_orchestration.get("disabled") or []
        lines.extend([
            "",
            "## Strategy Orchestration",
            f"- Regime: {strategy_orchestration.get('regime', '-')}",
            f"- Enabled strategies: {len(enabled)}",
            f"- Target count: {strategy_orchestration.get('target_strategy_count', {}).get('min', 3)}"
            f"-{strategy_orchestration.get('target_strategy_count', {}).get('max', 5)}",
            f"- Symbol limit: {strategy_orchestration.get('symbol_limit', '-')}",
            f"- Symbols: {', '.join(strategy_orchestration.get('symbols') or []) or '-'}",
            f"- Risk gate open: {strategy_orchestration.get('risk_gate_open', True)}",
            f"- Warnings: {'; '.join(strategy_orchestration.get('warnings') or []) or '-'}",
            "",
            "| Strategy | Family | Weight | Reason |",
            "|---|---|---:|---|",
        ])
        for item in enabled:
            lines.append(
                f"| {item.get('name', item.get('id'))} ({item.get('id')}) | "
                f"{item.get('family', '-')} | {float(item.get('weight') or 0):.2f} | "
                f"{item.get('reason', '-')} |"
            )
        if disabled:
            lines.extend(["", "### Disabled Strategy Candidates", "| Strategy | Reason |", "|---|---|"])
            for item in disabled:
                lines.append(f"| {item.get('name', item.get('id'))} ({item.get('id')}) | {item.get('reason', '-')} |")

    strategy_run = payload.get("strategy_run") or {}
    if strategy_run:
        lines.extend([
            "",
            "## Strategy Run Results",
            f"- Successful: {strategy_run.get('successful_strategy_count', 0)}",
            f"- Failed: {strategy_run.get('failed_strategy_count', 0)}",
            f"- Raw signal rows: {strategy_run.get('raw_result_count', 0)}",
            f"- Errors: {'; '.join(strategy_run.get('errors') or []) or '-'}",
            "",
            "| Strategy | Status | Results | Message |",
            "|---|---|---:|---|",
        ])
        for item in strategy_run.get("runs") or []:
            lines.append(
                f"| {item.get('strategy_id')} | {item.get('status')} | "
                f"{int(item.get('result_count') or 0)} | {item.get('message') or '-'} |"
            )
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
            f"- Warnings: {'; '.join(candidate_selection.get('warnings') or []) or '-'}",
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
        rejected_candidates = candidate_selection.get("rejected") or []
        if rejected_candidates:
            lines.extend([
                "",
                "### Rejected Candidates",
                "| Symbol | Sources | Reason |",
                "|---|---|---|",
            ])
            for item in rejected_candidates:
                lines.append(
                    f"| {item.get('symbol')} | {', '.join(item.get('sources') or [])} | "
                    f"{item.get('quality_reason', '-')} |"
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

    order_decisions = payload.get("order_decisions") or []
    if order_decisions:
        lines.extend([
            "",
            "## Order Gate Decisions",
            "| Symbol | Action | Strength | Status | Reasons |",
            "|---|---:|---:|---|---|",
        ])
        for item in order_decisions:
            lines.append(
                f"| {item.get('name', item.get('code'))}({item.get('code')}) | {item.get('action')} | "
                f"{float(item.get('strength') or 0):.2f} | {item.get('status')} | "
                f"{'; '.join(item.get('reasons') or []) or '-'} |"
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
    if payload.get("errors"):
        lines.extend(["", "## Errors"])
        lines.extend(f"- {error}" for error in payload["errors"])
    atomic_write_text(path, "\n".join(lines) + "\n")


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
    atomic_write_text(path, "\n".join(lines) + "\n")


def collect_payload_errors(payload: dict[str, Any]) -> list[str]:
    errors = [str(item) for item in payload.get("errors", []) if item]
    errors.extend(
        f"candidate_selection: {item}"
        for item in payload.get("candidate_selection", {}).get("errors", [])
        if item
    )
    errors.extend(
        f"strategy_run: {item}"
        for item in payload.get("strategy_run", {}).get("errors", [])
        if item
    )
    for signal in payload.get("signals", []):
        if signal.get("action") == "ERROR" and signal.get("reason"):
            errors.append(f"{signal.get('symbol')}: {signal.get('reason')}")
    for order in [*payload.get("submitted_sells", []), *payload.get("orders", [])]:
        if order.get("order_status") in {"failed", "reservation_failed"} and order.get("last_error"):
            errors.append(f"{order.get('symbol')}: {order.get('last_error')}")
        reservation = order.get("reservation_result") or {}
        if reservation.get("status") == "error" and reservation.get("message"):
            errors.append(f"{order.get('symbol')}: {reservation.get('message')}")
    for section in ("pending", "reservations", "app_reservations"):
        for item in payload.get("account_after", {}).get(section, {}).get("errors", []):
            errors.append(f"{section}: {item}")
    protective_health = payload.get("account_after", {}).get("protective", {}).get("health", {})
    if protective_health.get("refresh_error"):
        errors.append(f"protective monitor refresh: {protective_health.get('refresh_error')}")
    if protective_health.get("status") in {"degraded", "stale"}:
        errors.append(
            "protective monitor: "
            f"{protective_health.get('status')}, "
            f"rate limits={protective_health.get('rate_limited_order_count', 0)}, "
            f"overdue exits={protective_health.get('overdue_exit_count', 0)}"
        )
    return list(dict.fromkeys(errors))


def compact_account(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "equity": round(float(account.get("equity") or 0), 2),
        "risk_equity": round(float(account.get("risk_equity") or account.get("equity") or 0), 2),
        "cash": round(float(account.get("cash") or 0), 2),
        "holdings_value": round(float(account.get("holdings_value") or 0), 2),
        "holdings_count": len(account.get("holdings") or []),
        "risk_equity_sources": account.get("risk_equity_sources") or [],
    }


def run_summary(payload: dict[str, Any]) -> dict[str, Any]:
    signals = payload.get("signals") or []
    orders = [*payload.get("submitted_sells", []), *payload.get("orders", [])]
    submitted_statuses = {"submitted", "reservation_submitted"}
    failed_statuses = {"failed", "reservation_failed"}
    return {
        "run_id": payload.get("run_id"),
        "slot": payload.get("slot"),
        "scheduled_at_et": payload.get("scheduled_at_et"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "duration_seconds": round(float(payload.get("duration_seconds") or 0), 2),
        "status": payload.get("status", "completed"),
        "report_only": bool(payload.get("report_only")),
        "signal_counts": {
            action: sum(1 for signal in signals if signal.get("action") == action)
            for action in ("BUY", "SELL", "HOLD", "ERROR")
        },
        "order_counts": {
            "submitted": sum(1 for order in orders if order.get("order_status") in submitted_statuses),
            "filled": sum(1 for order in orders if float(order.get("filled_quantity") or 0) > 0),
            "failed": sum(1 for order in orders if order.get("order_status") in failed_statuses),
            "skipped": sum(
                1 for order in orders
                if str(order.get("order_status") or "").startswith("skipped")
            ),
        },
        "buy_notional": round(sum(
            float(order.get("notional") or 0)
            for order in payload.get("orders", [])
            if order.get("order_status") in submitted_statuses
        ), 2),
        "account_before": compact_account(payload.get("account_before", {})),
        "account_after": compact_account(payload.get("account_after", {})),
        "pending_count": int(payload.get("account_after", {}).get("pending", {}).get("total_count") or 0),
        "app_reservation_count": int(payload.get("account_after", {}).get("app_reservations", {}).get("total_count") or 0),
        "protective_count": len(payload.get("account_after", {}).get("protective", {}).get("orders") or []),
        "errors": collect_payload_errors(payload),
        "json_report": f"{payload.get('run_id')}.json",
        "markdown_report": f"{payload.get('run_id')}.md",
    }


def write_session_summary(session_date: str, state: dict[str, Any]) -> dict[str, Any]:
    runs = [normalize_summary_run(run) for run in state.get("runs", []) if isinstance(run, dict)]
    cumulative_buy = round(sum(float(run.get("buy_notional") or 0) for run in runs), 2)
    latest_account = next(
        (run.get("account_after") for run in reversed(runs) if run.get("account_after")),
        {},
    )
    risk_equity = float((latest_account or {}).get("risk_equity") or 0)
    summary = {
        "session_date": session_date,
        "mode": "vps",
        "updated_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
        "run_count": len(runs),
        "runs": runs,
        "events": state.get("events", []),
        "cumulative_buy_notional": cumulative_buy,
        "session_buy_limit": round(risk_equity * TOTAL_BUY_PCT, 2),
        "remaining_buy_budget": round(max(0.0, risk_equity * TOTAL_BUY_PCT - cumulative_buy), 2),
        "session_loss_limit": round(risk_equity * DAILY_LOSS_PCT, 2),
        "remaining_loss_budget": round(max(0.0, risk_equity * DAILY_LOSS_PCT - cumulative_buy * STOP_LOSS_PCT), 2),
        "latest_account": latest_account,
        "totals": {
            "submitted": sum(int(run.get("order_counts", {}).get("submitted") or 0) for run in runs),
            "filled": sum(int(run.get("order_counts", {}).get("filled") or 0) for run in runs),
            "failed": sum(int(run.get("order_counts", {}).get("failed") or 0) for run in runs),
            "errors": sum(len(run.get("errors") or []) for run in runs),
        },
    }
    atomic_write_json(RUNTIME_DIR / f"{session_date}_summary.json", summary)

    lines = [
        f"# US Automation Session - {session_date}",
        "",
        "- Mode: vps only",
        f"- Updated: {summary['updated_at']}",
        f"- Runs: {summary['run_count']}",
        f"- Cumulative buys: ${cumulative_buy:,.2f}",
        f"- Remaining buy budget: ${summary['remaining_buy_budget']:,.2f}",
        f"- Remaining loss budget: ${summary['remaining_loss_budget']:,.2f}",
        "",
        "## Timeline",
        "| UTC+09:00 Time | Run | Status | Duration | BUY/SELL/HOLD | Submitted/Filled/Failed | Errors |",
        "|---|---|---|---:|---|---|---:|",
    ]
    for run in runs:
        counts = run.get("signal_counts", {})
        order_counts = run.get("order_counts", {})
        lines.append(
            f"| {display_kst_time(run)} | {run.get('run_id')} | {run.get('status')} | "
            f"{float(run.get('duration_seconds') or 0):.1f}s | "
            f"{counts.get('BUY', 0)}/{counts.get('SELL', 0)}/{counts.get('HOLD', 0)} | "
            f"{order_counts.get('submitted', 0)}/{order_counts.get('filled', 0)}/{order_counts.get('failed', 0)} | "
            f"{len(run.get('errors') or [])} |"
        )
    atomic_write_text(RUNTIME_DIR / f"{session_date}_summary.md", "\n".join(lines) + "\n")
    return summary


def normalize_summary_run(run: dict[str, Any]) -> dict[str, Any]:
    if run.get("run_id") and run.get("signal_counts") and run.get("order_counts"):
        return run
    report = str(run.get("report") or "")
    report_id = Path(report).stem if report else ""
    legacy_orders = run.get("orders") if isinstance(run.get("orders"), list) else []
    status = "market_closed" if run.get("market_closed") else "legacy"
    return {
        "run_id": report_id or f"legacy_{run.get('started_at', 'unknown')}",
        "slot": run.get("slot", "legacy"),
        "scheduled_at_et": None,
        "started_at": run.get("started_at"),
        "finished_at": None,
        "duration_seconds": 0.0,
        "status": status,
        "report_only": False,
        "signal_counts": {"BUY": 0, "SELL": 0, "HOLD": 0, "ERROR": 0},
        "order_counts": {
            "submitted": sum(
                1 for order in legacy_orders
                if order.get("order_status") in {"submitted", "reservation_submitted"}
            ),
            "filled": sum(1 for order in legacy_orders if float(order.get("filled_quantity") or 0) > 0),
            "failed": sum(
                1 for order in legacy_orders
                if order.get("order_status") in {"failed", "reservation_failed"}
            ),
            "skipped": sum(
                1 for order in legacy_orders
                if str(order.get("order_status") or "").startswith("skipped")
            ),
        },
        "buy_notional": round(sum(
            float(order.get("notional") or 0)
            for order in legacy_orders
            if order.get("order_status") in {"submitted", "reservation_submitted"}
        ), 2),
        "account_before": {},
        "account_after": {},
        "pending_count": 0,
        "app_reservation_count": 0,
        "protective_count": 0,
        "errors": [],
        "json_report": f"{report_id}.json" if report_id else None,
        "markdown_report": f"{report_id}.md" if report_id else None,
    }


def report_url() -> str:
    raw = (os.environ.get("US_MARKET_REPORT_URL") or DEFAULT_REPORT_URL).strip().rstrip("/")
    if raw.startswith("http://127.0.0.1") or raw.startswith("http://localhost"):
        return DEFAULT_REPORT_URL
    return raw


def display_kst_time(payload: dict[str, Any]) -> str:
    raw = payload.get("started_at") or payload.get("updated_at") or payload.get("scheduled_at_kst")
    if not raw:
        return "-"
    try:
        value = datetime.fromisoformat(str(raw)).astimezone(ZoneInfo("Asia/Seoul"))
    except ValueError:
        return str(raw)
    return value.strftime("%Y-%m-%d %H:%M UTC+09:00")


def telegram_message(payload: dict[str, Any], summary: dict[str, Any] | None = None) -> str:
    if payload.get("status") == "market_closed":
        return f"US paper automation {payload.get('date')}: market closed. No orders.\n{report_url()}/automation"
    run = run_summary(payload)
    counts = run["signal_counts"]
    order_counts = run["order_counts"]
    link = f"\n{report_url()}/automation"
    text = (
        f"US paper {display_kst_time(payload)} {run.get('status')}\n"
        f"Signals B/S/H {counts['BUY']}/{counts['SELL']}/{counts['HOLD']}\n"
        f"Orders submitted/filled/failed "
        f"{order_counts['submitted']}/{order_counts['filled']}/{order_counts['failed']}\n"
        f"Buy ${run['buy_notional']:,.2f}, errors {len(run['errors'])}{link}"
    )
    market_risk = payload.get("market_risk") or {}
    if market_risk.get("reasons"):
        text += f"\nRisk gate: {'; '.join(market_risk['reasons'])}"
    if summary is not None:
        text += (
            f"\nSession total: {summary['run_count']} runs, "
            f"buys ${summary['cumulative_buy_notional']:,.2f}, "
            f"remaining risk ${summary['remaining_loss_budget']:,.2f}"
        )
    return text


def session_telegram_message(summary: dict[str, Any]) -> str:
    link = f"\n{report_url()}/automation"
    totals = summary.get("totals", {})
    return (
        f"US paper session {summary.get('session_date')} complete\n"
        f"Updated {display_kst_time(summary)}\n"
        f"Runs {summary.get('run_count', 0)}, submitted/filled/failed "
        f"{totals.get('submitted', 0)}/{totals.get('filled', 0)}/{totals.get('failed', 0)}\n"
        f"Buys ${float(summary.get('cumulative_buy_notional') or 0):,.2f}, "
        f"remaining loss budget ${float(summary.get('remaining_loss_budget') or 0):,.2f}, "
        f"errors {totals.get('errors', 0)}{link}"
    )


def send_telegram(text: str) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    if not token or not chat_id:
        return {"status": "disabled"}
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=12,
        )
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if response.ok and body.get("ok"):
            return {"status": "sent"}
        return {
            "status": "error",
            "http_status": response.status_code,
            "message": str(body.get("description") or "telegram send failed")[:500],
        }
    except Exception as exc:
        return {"status": "error", "message": f"{type(exc).__name__}: {exc}"[:500]}


def cleanup_old_detail_reports(now: datetime | None = None) -> list[str]:
    cutoff = (now or datetime.now(ZoneInfo("Asia/Seoul"))) - timedelta(days=DETAIL_RETENTION_DAYS)
    removed: list[str] = []
    pattern = re.compile(r"^\d{8}_(?:\d{4}_ET|closed)\.(?:json|md)$")
    for path in RUNTIME_DIR.iterdir() if RUNTIME_DIR.exists() else []:
        if not path.is_file() or not pattern.match(path.name):
            continue
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=ZoneInfo("Asia/Seoul"))
        if modified < cutoff:
            path.unlink()
            removed.append(path.name)
    return removed


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True, choices=["hourly", "open", "mid", "close", "manual"])
    parser.add_argument("--date", required=True, help="US session date key in YYYYMMDD, shared by all slots")
    parser.add_argument("--run-id", help="Stable run identifier, normally YYYYMMDD_HHMM_ET.")
    parser.add_argument("--report-only", action="store_true", help="Generate reports without submitting orders.")
    parser.add_argument("--market-closed", action="store_true", help="Record the first closed-market slot.")
    parser.add_argument("--record-skip", choices=["skipped_overlap", "skipped_duplicate"])
    parser.add_argument("--llm-mode", choices=["off", "shadow", "live-vps", "live-prod"], default=os.environ.get("US_MARKET_LLM_MODE", "off"))
    args = parser.parse_args()
    effective_llm_mode, llm_warnings = normalize_llm_mode(args.llm_mode)

    now = datetime.now(ZoneInfo("Asia/Seoul"))
    monotonic_started = time.monotonic()
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    run_id = args.run_id or f"{now.strftime('%Y%m%d_%H%M%S')}_{args.slot}"
    report_base = RUNTIME_DIR / run_id
    state_path = session_state_path(args.date)
    state = load_today_state(state_path)

    if args.record_skip:
        record_skip_event(state_path, run_id, args.record_skip, args.slot)
        write_session_summary(args.date, load_today_state(state_path))
        return 0

    if run_already_recorded(state, run_id):
        record_skip_event(state_path, run_id, "skipped_duplicate", args.slot)
        write_session_summary(args.date, load_today_state(state_path))
        return 0

    calendar = load_helper("us_market_calendar", "us_market_calendar.py")
    trading_day = calendar.market_status(args.date)
    schedule_entry = next(
        (item for item in trading_day.get("scheduled_runs", []) if item.get("run_id") == run_id),
        {},
    )
    if args.slot == "hourly" and trading_day.get("is_open") and not schedule_entry:
        record_skip_event(state_path, run_id, "skipped_not_scheduled", args.slot)
        write_session_summary(args.date, load_today_state(state_path))
        return 0

    state.setdefault("active_runs", {})[run_id] = {
        "slot": args.slot,
        "status": "running",
        "started_at": now.isoformat(timespec="seconds"),
    }
    save_today_state(state_path, state)

    if args.market_closed or not trading_day.get("is_open"):
        finished = datetime.now(ZoneInfo("Asia/Seoul"))
        payload = {
            "run_id": run_id,
            "slot": args.slot,
            "date": args.date,
            "started_at": now.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_seconds": round(time.monotonic() - monotonic_started, 2),
            "status": "market_closed",
            "report_only": True,
            "llm_mode": args.llm_mode,
            "effective_llm_mode": effective_llm_mode,
            "llm_warnings": llm_warnings,
            "trading_day": trading_day,
            "news_summary": {"regime": "market_closed"},
            "headlines": [],
            "signals": [],
            "planned_buys": [],
            "orders": [],
            "account": {"equity": 0.0, "cash": 0.0, "holdings": []},
            "account_before": {},
            "account_after": {},
            "strategy_orchestration": select_strategy_candidates(
                market="us",
                regime="market_closed",
                risk_gate_open=False,
            ),
            "strategy_run": {
                "runs": [],
                "raw_result_count": 0,
                "successful_strategy_count": 0,
                "failed_strategy_count": 0,
                "errors": [],
                "merged_signals": [],
            },
            "order_decisions": [],
            "candidate_selection": {
                "mode": os.environ.get("US_MARKET_CANDIDATE_MODE", "dynamic"),
                "selected": [],
                "fallback_used": False,
                "errors": [],
                "generated_at": now.isoformat(timespec="seconds"),
            },
        }
        summary_row = run_summary(payload)
        state.get("active_runs", {}).pop(run_id, None)
        state.setdefault("runs", []).append(summary_row)
        save_today_state(state_path, state)
        summary = write_session_summary(args.date, state)
        payload["telegram"] = send_telegram(telegram_message(payload, summary))
        atomic_write_json(report_base.with_suffix(".json"), payload)
        write_market_closed_report(report_base.with_suffix(".md"), payload)
        cleanup_old_detail_reports(now)
        return 0

    payload: dict[str, Any] = {
        "run_id": run_id,
        "slot": args.slot,
        "date": args.date,
        "started_at": now.isoformat(timespec="seconds"),
        "scheduled_at_et": schedule_entry.get("scheduled_at_et"),
        "scheduled_at_kst": schedule_entry.get("scheduled_at_kst"),
        "status": "running",
        "report_only": args.report_only,
        "headlines": [],
        "news_summary": {"regime": "unknown"},
        "llm_mode": args.llm_mode,
        "effective_llm_mode": effective_llm_mode,
        "llm_warnings": llm_warnings,
        "trading_day": trading_day,
        "signals": [],
        "submitted_sells": [],
        "planned_buys": [],
        "orders": [],
        "catchup_protections": [],
        "account": {"equity": 0.0, "cash": 0.0, "holdings": []},
        "account_before": {},
        "account_after": {},
        "errors": [],
        "strategy": {
            "name": "US news-aware EMA/ROC/RSI trend filter",
            "entry": (
                "EMA20 > EMA50, 4 < ROC20 <= 25, 50 < RSI14 < 72, "
                "current >= EMA20, intraday drop < 3%"
            ),
            "exit": "EMA20 < EMA50 or ROC20 < -3 or RSI14 > 78 or intraday drop >= 5%",
            "risk": {
                "total_new_buy_pct": round(TOTAL_BUY_PCT * 100, 2),
                "daily_loss_pct": round(DAILY_LOSS_PCT * 100, 2),
                "take_profit_pct": round(TAKE_PROFIT_PCT * 100, 2),
                "stop_loss_pct": round(STOP_LOSS_PCT * 100, 2),
                "per_symbol_cap_pct": round(MAX_PER_SYMBOL_BUY_PCT * 100, 2),
                "max_total_holdings_exposure_pct": round(MAX_TOTAL_HOLDINGS_EXPOSURE_PCT * 100, 2),
                "max_sector_exposure_pct": round(MAX_SECTOR_EXPOSURE_PCT * 100, 2),
                "max_new_buy_symbols": MAX_NEW_BUY_SYMBOLS,
                "risk_control_blocks_new_buys": True,
            },
        },
        "candidate_selection": {
            "mode": os.environ.get("US_MARKET_CANDIDATE_MODE", "dynamic"),
            "selected": [],
            "fallback_used": False,
            "errors": [],
            "generated_at": now.isoformat(timespec="seconds"),
        },
    }
    exit_code = 0
    try:
        (
            ka,
            indicators,
            odf,
            create_app_reservation,
            upsert_protection,
            list_protective_orders,
            list_app_reservations,
            run_monitor_cycle,
        ) = load_modules()
        ka.auth(svr="vps")

        headlines = fetch_headlines()
        news_summary = summarize_news(headlines)
        account_before = await account_status(
            odf,
            list_protective_orders,
            list_app_reservations,
            run_monitor_cycle,
        )
        account_before = apply_session_risk_baseline(account_before, state)
        save_today_state(state_path, state)
        equity = float(account_before["validated_risk_equity"])
        cash = float(account_before["cash"])
        holdings = account_before["holdings"]
        held_symbols = set(holdings_by_symbol(holdings).keys())
        pending_buy_symbols = symbols_for_action(account_before["pending"].get("orders", []), "BUY")
        pending_sell_symbols = symbols_for_action(account_before["pending"].get("orders", []), "SELL")
        reservation_buy_symbols = symbols_for_action(account_before["reservations"].get("orders", []), "BUY")
        app_reservation_buy_symbols = symbols_for_action(account_before["app_reservations"].get("orders", []), "BUY")
        submitted_today_symbols = state_order_symbols(state)
        excluded_buy_symbols = (
            held_symbols
            | pending_buy_symbols
            | reservation_buy_symbols
            | app_reservation_buy_symbols
            | submitted_today_symbols
        )

        candidate_selection = apply_candidate_quality_gate(
            select_us_candidates(
                ranking_fetcher=odf,
                holdings=holdings,
                static_candidates=STATIC_CANDIDATES,
            )
        )
        signals = []
        for candidate in candidate_selection.get("selected", []):
            symbol = str(candidate.get("symbol") or candidate.get("code")).upper()
            exchange = str(candidate.get("exchange") or "NASD")
            try:
                signals.append({
                    **signal_for(symbol, exchange, odf, indicators),
                    "sector": candidate.get("sector") or infer_us_sector(symbol, candidate),
                    "candidate_score": candidate.get("score"),
                    "candidate_sources": candidate.get("sources") or [],
                })
            except Exception as exc:
                signals.append({
                    "symbol": symbol,
                    "exchange": exchange,
                    "action": "ERROR",
                    "strength": 0.0,
                    "reason": str(exc),
                    "price": 0.0,
                    "sector": candidate.get("sector") or infer_us_sector(symbol, candidate),
                })

        market_risk = evaluate_market_risk(signals, news_summary)
        organic_symbols = [
            str(candidate.get("symbol") or candidate.get("code") or "").upper()
            for candidate in candidate_selection.get("selected", [])[:US_ORGANIC_STRATEGY_MAX_SYMBOLS]
        ]
        organic_symbols = [symbol for symbol in organic_symbols if symbol]
        symbol_meta = {
            str(candidate.get("symbol") or candidate.get("code") or "").upper(): {
                "exchange": str(candidate.get("exchange") or "NASD"),
                "name": candidate.get("name") or candidate.get("symbol") or candidate.get("code"),
            }
            for candidate in candidate_selection.get("selected", [])
            if candidate.get("symbol") or candidate.get("code")
        }
        strategy_orchestration = select_strategy_candidates(
            market="us",
            regime=str(market_risk.get("regime") or news_summary.get("regime") or "broad_momentum"),
            risk_gate_open=bool(market_risk.get("risk_gate_open")),
        )
        strategy_orchestration["symbol_limit"] = US_ORGANIC_STRATEGY_MAX_SYMBOLS
        strategy_orchestration["symbols"] = organic_symbols
        strategy_run = execute_strategy_pool(
            strategy_api,
            organic_symbols,
            strategy_orchestration,
            market="us",
            symbol_meta={symbol: symbol_meta.get(symbol, {}) for symbol in organic_symbols},
        ) if organic_symbols else {
            "orchestration": strategy_orchestration,
            "runs": [],
            "raw_result_count": 0,
            "successful_strategy_count": 0,
            "failed_strategy_count": 0,
            "errors": [],
            "merged_signals": [],
        }
        signals = merge_us_anchor_signals(signals, strategy_run.get("merged_signals") or [])
        portfolio_risk = portfolio_risk_snapshot(holdings, equity)
        combined_risk_gate_open = bool(
            account_before.get("risk_gate_open")
            and market_risk.get("risk_gate_open")
            and portfolio_risk.get("total_exposure_gate_open")
        )
        submitted_sells = [] if args.report_only else place_sells(
            signals,
            holdings,
            odf,
            pending_sell_symbols,
        )
        planned_orders = build_orders(
            signals,
            equity,
            cash,
            state,
            excluded_buy_symbols,
            holdings,
            market_regime=str(market_risk.get("regime") or "risk_control"),
            risk_gate_open=combined_risk_gate_open,
        )
        risk_reasons = []
        if account_before.get("risk_gate_reason"):
            risk_reasons.append(str(account_before.get("risk_gate_reason")))
        risk_reasons.extend(str(item) for item in market_risk.get("reasons") or [])
        if not portfolio_risk.get("total_exposure_gate_open"):
            risk_reasons.append("total holdings exposure limit reached")
        order_decisions = explain_order_decisions(
            signals,
            planned_orders,
            min_buy_strength=MIN_BUY_STRENGTH,
            risk_gate_open=combined_risk_gate_open,
            risk_reasons=risk_reasons,
            order_execution_enabled=not args.report_only,
            order_block_reasons=["report-only mode"] if args.report_only else [],
        )
        payload.update({
            "headlines": headlines,
            "news_summary": news_summary,
            "market_risk": market_risk,
            "portfolio_risk": portfolio_risk,
            "account": {"equity": equity, "cash": cash, "holdings": holdings},
            "account_before": account_before,
            "candidate_selection": candidate_selection,
            "strategy_orchestration": strategy_orchestration,
            "strategy_run": {key: value for key, value in strategy_run.items() if key != "merged_signals"},
            "signals": signals,
            "submitted_sells": submitted_sells,
            "planned_buys": planned_orders,
            "order_decisions": order_decisions,
            "duplicate_guards": {
                "held_symbols": sorted(held_symbols),
                "pending_buy_symbols": sorted(pending_buy_symbols),
                "pending_sell_symbols": sorted(pending_sell_symbols),
                "reservation_buy_symbols": sorted(reservation_buy_symbols),
                "app_reservation_buy_symbols": sorted(app_reservation_buy_symbols),
                "submitted_today_symbols": sorted(submitted_today_symbols),
                "excluded_buy_symbols": sorted(excluded_buy_symbols),
            },
        })

        llm_result = run_llm_decision(effective_llm_mode, payload)
        executable_orders = apply_llm_decision(planned_orders, llm_result, live_mode=False)
        regular_hours = args.slot == "hourly" or us_regular_hours_now(now)
        reservation_scheduled_at = (
            None if regular_hours else next_us_regular_open_kst(calendar, now)
        )
        submitted_orders = [] if args.report_only else await place_orders(
            executable_orders,
            odf,
            create_app_reservation,
            upsert_protection,
            use_reservations=not regular_hours,
            reservation_scheduled_at=reservation_scheduled_at,
        )
        clear_balance_cache(odf)
        account_after = await account_status(
            odf,
            list_protective_orders,
            list_app_reservations,
            run_monitor_cycle,
        )
        eligible_protection_symbols = (
            held_symbols
            | {str(item.get("symbol") or item.get("code")).upper() for item in candidate_selection.get("selected", [])}
            | submitted_today_symbols
            | {str(order.get("symbol")).upper() for order in submitted_orders if order.get("symbol")}
        )
        catchup_protections = [] if args.report_only else await register_missing_protection_for_holdings(
            account_after["holdings"],
            account_after.get("protective", {}),
            upsert_protection,
            eligible_protection_symbols,
        )
        if catchup_protections:
            clear_balance_cache(odf)
            account_after = await account_status(
                odf,
                list_protective_orders,
                list_app_reservations,
                run_monitor_cycle,
            )
        planned_display = annotate_llm_decisions(
            planned_orders,
            executable_orders,
            effective_llm_mode,
            llm_result,
        )
        if args.report_only:
            planned_display = [
                {**order, "order_decision": "report-only/미주문"}
                for order in planned_display
            ]
        payload.update({
            "llm": llm_result,
            "planned_order_method": "미국 모의 일반 지정가" if regular_hours else "미국 모의 예약매수 limit",
            "planned_buys_display": planned_display,
            "orders": submitted_orders,
            "catchup_protections": catchup_protections,
            "account_after": account_after,
            "status": "report_only" if args.report_only else "completed",
        })
    except Exception as exc:
        payload["status"] = "failed"
        payload.setdefault("errors", []).append(f"{type(exc).__name__}: {exc}")
        payload["account_after"] = payload.get("account_after") or payload.get("account_before") or {}
        exit_code = 1

    finished = datetime.now(ZoneInfo("Asia/Seoul"))
    payload["finished_at"] = finished.isoformat(timespec="seconds")
    payload["duration_seconds"] = round(time.monotonic() - monotonic_started, 2)
    payload["errors"] = collect_payload_errors(payload)
    summary_row = run_summary(payload)
    state.get("active_runs", {}).pop(run_id, None)
    state.setdefault("runs", []).append(summary_row)
    state.setdefault("orders", []).extend(
        order for order in payload.get("orders", [])
        if order.get("order_status") in {"submitted", "reservation_submitted"}
    )
    save_today_state(state_path, state)
    summary = write_session_summary(args.date, state)
    is_last_run = bool(schedule_entry) and schedule_entry == trading_day.get("scheduled_runs", [])[-1]
    payload["telegram"] = {
        "run": send_telegram(telegram_message(payload)),
    }
    if is_last_run:
        payload["telegram"]["session"] = send_telegram(session_telegram_message(summary))
    atomic_write_json(report_base.with_suffix(".json"), payload)
    write_report(report_base.with_suffix(".md"), payload)
    cleanup_old_detail_reports(now)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
