#!/usr/bin/env python3
"""Scheduled KRX trading run.

Automation for the user's Korean-market workflow. It keeps the strategy and
risk rules local, records the required pre-order table, and uses the Strategy
Builder API for signals, orders, reservations, and protection.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from xml.etree import ElementTree

import prod_telegram_approval
import requests
from market_candidate_selector import select_kr_candidates
from organic_strategy_router import (
    execute_strategy_pool,
    explain_order_decisions,
    select_strategy_candidates,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNTIME_DIR = PROJECT_ROOT / ".codex" / "runtime" / "kr_market_auto"
PROD_RUNTIME_DIR = PROJECT_ROOT / ".codex" / "runtime" / "kr_market_auto_prod"
PROD_AUTO_CONFIRM_VALUE = "I_UNDERSTAND_REAL_ORDERS"

STATIC_CANDIDATES = [
    ("005930", "삼성전자", "대형주"),
    ("000660", "SK하이닉스", "대형주"),
    ("005380", "현대차", "대형주"),
    ("373220", "LG에너지솔루션", "대형주"),
    ("000270", "기아", "대형주"),
    ("105560", "KB금융", "대형주"),
    ("055550", "신한지주", "대형주"),
    ("066570", "LG전자", "대형주"),
    ("069500", "KODEX 200", "ETF"),
    ("091160", "KODEX 반도체", "ETF"),
]


def env_fraction(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = float(raw.strip().rstrip("%"))
    return value / 100 if value > 1 else value


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw.strip())


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "y", "on", "allow"}


TOTAL_BUY_PCT = env_fraction("KR_MARKET_TOTAL_BUY_PCT", 0.10)
DAILY_LOSS_PCT = env_fraction("KR_MARKET_DAILY_LOSS_PCT", 0.005)
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.06
MIN_BUY_STRENGTH = 0.70
MAX_NEW_BUY_SYMBOLS = env_int("KR_MARKET_MAX_NEW_BUY_SYMBOLS", 2)
MAX_PER_SYMBOL_BUY_PCT = env_fraction("KR_MARKET_MAX_PER_SYMBOL_BUY_PCT", 0.01)
ORGANIC_STRATEGY_MAX_SYMBOLS = env_int("KR_MARKET_ORGANIC_MAX_SYMBOLS", 3)
RISK_CONTROL_BLOCKS_NEW_BUYS = True
LLM_DECIDER_PATH = PROJECT_ROOT / ".codex" / "scripts" / "kr_market_llm_decider.py"
CALENDAR_PATH = PROJECT_ROOT / ".codex" / "scripts" / "kr_market_calendar.py"


def api_base_for(trade_mode: str) -> str:
    if trade_mode == "prod":
        return os.environ.get("KIS_PROD_STRATEGY_API") or os.environ.get("KIS_STRATEGY_API") or "http://127.0.0.1:8083"
    return os.environ.get("KIS_VPS_STRATEGY_API") or os.environ.get("KIS_STRATEGY_API") or "http://127.0.0.1:8081"


API_BASE = api_base_for(os.environ.get("KIS_TRADE_MODE", "vps"))


def runtime_dir_for(trade_mode: str) -> Path:
    return PROD_RUNTIME_DIR if trade_mode == "prod" else PROJECT_ROOT / ".codex" / "runtime" / "kr_market_auto"


def prod_auto_confirmed(args_confirmed: bool) -> bool:
    return args_confirmed or os.environ.get("KIS_PROD_AUTO_CONFIRM") == PROD_AUTO_CONFIRM_VALUE


def prod_telegram_approval_enabled() -> bool:
    return prod_telegram_approval.telegram_approval_enabled()


def order_execution_enabled(trade_mode: str, prod_confirmed: bool, telegram_approval: bool = False) -> bool:
    return trade_mode != "prod" or prod_confirmed or telegram_approval


def prod_llm_orders_enabled(trade_mode: str, llm_mode: str) -> bool:
    return True


def strategy_sell_execution_enabled(trade_mode: str) -> bool:
    return trade_mode != "prod" or env_truthy("KR_PROD_ALLOW_STRATEGY_SELL")


def is_live_llm_mode(mode: str) -> bool:
    return mode in {"live-vps", "live-prod"}


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


def api(method: str, path: str, **kwargs) -> Any:
    response = requests.request(method, f"{API_BASE}{path}", timeout=90, **kwargs)
    response.raise_for_status()
    return response.json()


def fetch_headlines() -> list[dict[str, str]]:
    queries = [
        "한국 증시 오늘 코스피 환율 금리 반도체 자동차 when:1d",
        "코스피 장중 외국인 기관 반도체 배터리 자동차 금융 when:1d",
        "한국 시장 매크로 원달러 환율 국채 금리 수급 when:1d",
    ]
    headers = {"User-Agent": "Mozilla/5.0 KIS KRX paper trading"}
    headlines: list[dict[str, str]] = []
    seen: set[str] = set()
    for query in queries:
        url = (
            "https://news.google.com/rss/search?"
            f"q={quote_plus(query)}&hl=ko&gl=KR&ceid=KR:ko"
        )
        try:
            response = requests.get(url, headers=headers, timeout=12)
            response.raise_for_status()
            root = ElementTree.fromstring(response.content)
        except Exception as exc:
            headlines.append({"title": f"뉴스 조회 실패: {query}", "source": str(exc), "link": ""})
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
        "semis": ["반도체", "하이닉스", "삼성전자", "ai", "칩"],
        "autos": ["자동차", "현대차", "기아"],
        "rates_fx": ["환율", "금리", "원달러", "국채", "한국은행"],
        "risk": ["급락", "관세", "전쟁", "침체", "변동성", "매도"],
    }
    scores = {
        name: sum(text.count(keyword) for keyword in keywords)
        for name, keywords in buckets.items()
    }
    if scores["risk"] >= 3 or scores["rates_fx"] >= 5:
        regime = "risk_control"
    elif scores["semis"] >= scores["autos"] and scores["semis"] > 0:
        regime = "semiconductor_momentum"
    elif scores["autos"] > 0:
        regime = "auto_momentum"
    else:
        regime = "large_cap_rebound"
    return {"regime": regime, "scores": scores}


def signal_groups(results: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped = {"BUY": [], "SELL": [], "HOLD": []}
    for item in results:
        grouped.setdefault(item.get("action", "HOLD"), []).append(item)
    for items in grouped.values():
        items.sort(key=lambda row: float(row.get("strength") or 0), reverse=True)
    return grouped


def session_state_path(session_date: str) -> Path:
    return RUNTIME_DIR / f"{session_date}.json"


def load_state(session_date: str) -> dict[str, Any]:
    path = session_state_path(session_date)
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"runs": [], "orders": []}


def save_state(session_date: str, state: dict[str, Any]) -> None:
    path = session_state_path(session_date)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def run_already_recorded(state: dict[str, Any], run_id: str | None) -> bool:
    if not run_id:
        return False
    return any(run.get("run_id") == run_id for run in state.get("runs", []) if isinstance(run, dict))


def record_skip_event(session_date: str, run_id: str, status: str, slot: str) -> None:
    state = load_state(session_date)
    state.setdefault("events", []).append({
        "run_id": run_id,
        "slot": slot,
        "status": status,
        "at": datetime.now().isoformat(timespec="seconds"),
    })
    save_state(session_date, state)


def account_snapshot() -> dict[str, Any]:
    api("POST", "/api/orders/account/clear-cache")
    time.sleep(1.0)
    account = api("GET", "/api/orders/account")
    if not account.get("deposit", {}).get("total_eval"):
        time.sleep(3.0)
        api("POST", "/api/orders/account/clear-cache")
        time.sleep(1.0)
        account = api("GET", "/api/orders/account")
    time.sleep(1.0)
    pending = api("GET", "/api/orders/pending")
    time.sleep(1.0)
    try:
        reservations = api("GET", "/api/orders/reservations?market=domestic")
    except Exception as exc:
        reservations = {"status": "error", "message": str(exc), "orders": []}
    time.sleep(1.0)
    protective = api("GET", "/api/orders/protective")
    return {
        "account": account,
        "pending": pending,
        "reservations": reservations,
        "protective": protective,
    }


def evaluate_market_risk(news_summary: dict[str, Any] | None) -> dict[str, Any]:
    summary = news_summary or {}
    regime = str(summary.get("regime") or "unknown")
    reasons: list[str] = []
    if RISK_CONTROL_BLOCKS_NEW_BUYS and regime == "risk_control":
        reasons.append("risk_control news regime blocks new buys")
    return {
        "regime": regime,
        "risk_gate_open": not reasons,
        "reasons": reasons,
        "risk_control_blocks_new_buys": RISK_CONTROL_BLOCKS_NEW_BUYS,
    }


def build_buy_orders(
    results: list[dict[str, Any]],
    account: dict[str, Any],
    state: dict[str, Any],
    *,
    market_regime: str = "neutral",
    risk_gate_open: bool = True,
) -> list[dict[str, Any]]:
    if not risk_gate_open or (RISK_CONTROL_BLOCKS_NEW_BUYS and market_regime == "risk_control"):
        return []

    total_eval = float(account.get("deposit", {}).get("total_eval") or 0)
    cash = float(account.get("deposit", {}).get("deposit") or 0)
    bought_today = sum(float(order.get("amount") or 0) for order in state.get("orders", []))
    total_budget = max(0.0, total_eval * TOTAL_BUY_PCT - bought_today)
    loss_budget = max(0.0, total_eval * DAILY_LOSS_PCT - bought_today * STOP_LOSS_PCT)
    usable_budget = min(total_budget, loss_budget / STOP_LOSS_PCT if STOP_LOSS_PCT else 0, cash)

    candidates = [
        row for row in results
        if row.get("action") == "BUY"
        and float(row.get("strength") or 0) >= MIN_BUY_STRENGTH
        and float(row.get("target_price") or 0) > 0
    ]
    candidates.sort(key=lambda row: float(row.get("strength") or 0), reverse=True)
    selected_candidates = candidates[:MAX_NEW_BUY_SYMBOLS]
    total_strength = sum(float(row.get("strength") or 0) for row in selected_candidates) or 1.0

    def make_order(row: dict[str, Any], qty: int) -> dict[str, Any]:
        price = float(row["target_price"])
        amount = qty * price
        return {
            **row,
            "quantity": qty,
            "amount": amount,
            "weight": amount / max(1.0, usable_budget),
            "risk_amount": amount * STOP_LOSS_PCT,
            "take_profit": math.floor(price * (1 + TAKE_PROFIT_PCT)),
            "stop_loss": math.floor(price * (1 - STOP_LOSS_PCT)),
            "order_type": "market",
            "order_decision": "주문",
        }

    if not selected_candidates or usable_budget <= 0:
        return []

    orders: list[dict[str, Any]] = []
    remaining = usable_budget
    per_symbol_cap = max(0.0, total_eval * MAX_PER_SYMBOL_BUY_PCT)
    for row in selected_candidates:
        price = float(row["target_price"])
        target_amount = min(
            usable_budget * float(row.get("strength") or 0) / total_strength,
            per_symbol_cap,
            remaining,
        )
        qty = int(target_amount // price)
        if qty <= 0:
            continue
        order = make_order(row, qty)
        orders.append(order)
        remaining -= float(order["amount"])
    return orders


def load_llm_decider():
    spec = importlib.util.spec_from_file_location("kr_market_llm_decider", LLM_DECIDER_PATH)
    if not spec or not spec.loader:
        raise RuntimeError(f"LLM decider not loadable: {LLM_DECIDER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_calendar():
    spec = importlib.util.spec_from_file_location("kr_market_calendar", CALENDAR_PATH)
    if not spec or not spec.loader:
        raise RuntimeError(f"calendar guard not loadable: {CALENDAR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def trading_day_status(date: str) -> dict[str, Any]:
    try:
        calendar = load_calendar()
        return calendar.market_status(date)
    except Exception as exc:
        return {
            "status": "error",
            "date": date,
            "is_open": False,
            "source": "exception",
            "error": str(exc)[:1000],
        }


def run_llm_decision(mode: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    if mode == "off":
        return None
    try:
        decider = load_llm_decider()
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


def apply_llm_decision(
    planned_buys: list[dict[str, Any]],
    llm_result: dict[str, Any] | None,
    mode: str,
) -> list[dict[str, Any]]:
    effective_mode, _ = normalize_llm_mode(mode)
    if effective_mode in {"off", "shadow"}:
        return planned_buys
    raise RuntimeError(f"invalid llm mode: {mode}")


def annotate_buys_for_report(
    planned_buys: list[dict[str, Any]],
    executable_buys: list[dict[str, Any]],
    mode: str,
) -> list[dict[str, Any]]:
    effective_mode, _ = normalize_llm_mode(mode)
    if effective_mode in {"off", "shadow"}:
        return planned_buys
    raise RuntimeError(f"invalid llm mode: {mode}")


def holding_by_code(account: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("stock_code")): row for row in account.get("holdings", [])}


def telegram_skip_status(approval: dict[str, Any] | None) -> str:
    status = (approval or {}).get("status") or "error"
    if status in {"rejected", "timeout", "hash_mismatch", "error"}:
        return f"telegram_{status}"
    return f"telegram_{status}"


def prod_order_payload_after_approval(
    payload: dict[str, Any],
    approval_details: dict[str, Any],
    trade_mode: str,
    prod_confirmed: bool,
    telegram_enabled: bool,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    if trade_mode != "prod":
        return {**payload, "confirm_prod": False}, None

    if telegram_enabled:
        try:
            approval = prod_telegram_approval.request_approval(
                {**payload, "confirm_prod": False},
                approval_details,
                store_dir=RUNTIME_DIR / "telegram_approvals",
            )
        except Exception as exc:
            approval = {"status": "error", "message": str(exc)[:1000]}
        if approval.get("status") != "approved":
            return None, approval
        expected_hash = approval.get("payload_hash")
        actual_hash = prod_telegram_approval.payload_hash({**payload, "confirm_prod": False})
        if expected_hash != actual_hash:
            approval = {
                **approval,
                "status": "hash_mismatch",
                "expected_payload_hash": expected_hash,
                "actual_payload_hash": actual_hash,
            }
            return None, approval
        return {**payload, "confirm_prod": True}, approval

    if not prod_confirmed:
        return None, {"status": "blocked", "message": "one_time_prod_approval_missing"}
    return {**payload, "confirm_prod": True}, None


def buy_protection_summary(order: dict[str, Any]) -> str:
    return (
        "매수 후 앱 레벨 감시 설정 예정: "
        f"익절 {float(order.get('take_profit') or 0):,.0f}원 지정가, "
        f"손절 {float(order.get('stop_loss') or 0):,.0f}원 시장가"
    )


def place_sells(
    results: list[dict[str, Any]],
    holdings: dict[str, dict[str, Any]],
    trade_mode: str,
    prod_confirmed: bool,
    telegram_enabled: bool = False,
) -> list[dict[str, Any]]:
    submitted = []
    for signal in results:
        if signal.get("action") != "SELL" or float(signal.get("strength") or 0) < 0.5:
            continue
        holding = holdings.get(str(signal.get("code")))
        qty = int(float((holding or {}).get("quantity") or 0))
        if qty <= 0:
            signal["order_status"] = "skipped_no_holding"
            submitted.append(signal)
            continue
        payload = {
            "stock_code": signal["code"],
            "stock_name": signal.get("name") or signal["code"],
            "action": "SELL",
            "order_type": "market",
            "price": float(signal.get("target_price") or 0),
            "quantity": qty,
            "signal_reason": signal.get("reason") or "SELL signal",
            "market": "domestic",
        }
        approval_details = {
            "market_label": "국내",
            "action": "SELL",
            "stock_code": payload["stock_code"],
            "stock_name": payload["stock_name"],
            "quantity": payload["quantity"],
            "order_type": payload["order_type"],
            "price": payload["price"],
            "estimated_amount": float(payload["price"] or 0) * qty if payload["price"] else None,
            "signal_strength": f"{float(signal.get('strength') or 0):.2f}",
            "reason": payload["signal_reason"],
            "protection_summary": "해당 없음",
        }
        approved_payload, approval = prod_order_payload_after_approval(
            payload,
            approval_details,
            trade_mode,
            prod_confirmed,
            telegram_enabled,
        )
        if approval:
            signal["telegram_approval"] = approval
        if approved_payload is None:
            signal["order_status"] = telegram_skip_status(approval)
            submitted.append(signal)
            continue
        result = api("POST", "/api/orders/execute", json=approved_payload)
        signal["order_status"] = result.get("status")
        signal["order_result"] = result
        submitted.append(signal)
        time.sleep(1.0)
    return submitted


def place_buys(
    orders: list[dict[str, Any]],
    trade_mode: str,
    prod_confirmed: bool,
    telegram_enabled: bool = False,
) -> list[dict[str, Any]]:
    submitted = []
    for order in orders:
        payload = {
            "stock_code": order["code"],
            "stock_name": order.get("name") or order["code"],
            "action": "BUY",
            "order_type": "market",
            "price": float(order["target_price"]),
            "quantity": int(order["quantity"]),
            "signal_reason": order.get("reason") or "BUY signal",
            "market": "domestic",
        }
        approval_details = {
            "market_label": "국내",
            "action": "BUY",
            "stock_code": payload["stock_code"],
            "stock_name": payload["stock_name"],
            "quantity": payload["quantity"],
            "order_type": payload["order_type"],
            "price": payload["price"],
            "estimated_amount": order.get("amount"),
            "signal_strength": f"{float(order.get('strength') or 0):.2f}",
            "reason": payload["signal_reason"],
            "protection_summary": buy_protection_summary(order),
        }
        approved_payload, approval = prod_order_payload_after_approval(
            payload,
            approval_details,
            trade_mode,
            prod_confirmed,
            telegram_enabled,
        )
        if approval:
            order["telegram_approval"] = approval
        if approved_payload is None:
            order["order_status"] = telegram_skip_status(approval)
            order["order_decision"] = f"{order['order_status']}/미주문"
            submitted.append(order)
            continue
        result = api("POST", "/api/orders/execute", json=approved_payload)
        order["order_status"] = result.get("status")
        order["order_result"] = result
        if approval and approval.get("status") == "approved":
            order["order_decision"] = "telegram_approved/주문제출"
        submitted.append(order)
        time.sleep(1.0)
    return submitted


def register_protection_for_holdings(
    before_codes: set[str],
    after_account: dict[str, Any],
    trade_mode: str,
    prod_confirmed: bool,
    candidate_codes: set[str],
    telegram_enabled: bool = False,
    buy_approved_codes: set[str] | None = None,
) -> list[dict[str, Any]]:
    protections = []
    buy_approved_codes = buy_approved_codes or set()
    for row in after_account.get("holdings", []):
        code = str(row.get("stock_code"))
        if before_codes and code not in before_codes and code not in candidate_codes:
            continue
        qty = int(float(row.get("quantity") or 0))
        entry = float(row.get("avg_price") or row.get("current_price") or 0)
        if qty <= 0 or entry <= 0:
            continue
        payload = {
            "stock_code": code,
            "stock_name": row.get("stock_name") or code,
            "quantity": qty,
            "entry_price": entry,
            "enabled": True,
            "take_profit_enabled": True,
            "take_profit_trigger_price": math.floor(entry * (1 + TAKE_PROFIT_PCT)),
            "take_profit_order_type": "limit",
            "take_profit_limit_price": math.floor(entry * (1 + TAKE_PROFIT_PCT)),
            "stop_loss_enabled": True,
            "stop_loss_trigger_price": math.floor(entry * (1 - STOP_LOSS_PCT)),
            "stop_loss_order_type": "market",
            "stop_loss_limit_price": None,
            "market": "domestic",
            "exchange": None,
            "currency": "KRW",
        }
        reuses_buy_approval = trade_mode == "prod" and telegram_enabled and code in buy_approved_codes
        approval = None
        approved_payload = {**payload, "confirm_prod": trade_mode == "prod" and prod_confirmed}
        if reuses_buy_approval:
            approval = {
                "status": "approved_reused_buy_approval",
                "message": "BUY approval covered immediate post-buy protection setup.",
                "stock_code": code,
            }
            approved_payload = {**payload, "confirm_prod": True}
        else:
            approval_details = {
                "market_label": "국내",
                "action": "PROTECTIVE_SELL",
                "stock_code": payload["stock_code"],
                "stock_name": payload["stock_name"],
                "quantity": payload["quantity"],
                "order_type": "앱 레벨 손익절 감시",
                "price": payload["entry_price"],
                "estimated_amount": payload["entry_price"] * qty,
                "signal_strength": "-",
                "reason": "보유종목 손익절 감시 설정",
                "protection_summary": (
                    f"익절 {payload['take_profit_trigger_price']:,.0f}원 지정가, "
                    f"손절 {payload['stop_loss_trigger_price']:,.0f}원 시장가"
                ),
            }
            approved_payload, approval = prod_order_payload_after_approval(
                payload,
                approval_details,
                trade_mode,
                prod_confirmed,
                telegram_enabled,
            )
        if approved_payload is None:
            protections.append({
                "status": telegram_skip_status(approval),
                "stock_code": code,
                "stock_name": payload["stock_name"],
                "telegram_approval": approval,
            })
            continue
        try:
            result = api("POST", "/api/orders/protective", json=approved_payload)
            if approval:
                result["telegram_approval"] = approval
            protections.append(result)
        except Exception as exc:
            protections.append({
                "status": "error",
                "stock_code": code,
                "message": str(exc),
                "telegram_approval": approval,
            })
        time.sleep(1.0)
    return protections


def write_report(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        f"# KRX Market Auto Run - {payload['slot']}",
        "",
        f"- Time: {payload['started_at']}",
        f"- Mode: {payload.get('trade_mode', payload.get('safety', {}).get('mode', 'vps'))}",
        f"- Order execution enabled: {payload.get('order_execution_enabled', True)}",
        f"- Order block reasons: {', '.join(payload.get('order_block_reasons') or []) or '-'}",
        f"- Strategy SELL execution enabled: {payload.get('strategy_sell_execution_enabled', True)}",
        f"- Regime: {payload['news_summary']['regime']}",
        f"- LLM mode: {payload.get('llm_mode', 'off')}",
        f"- Effective LLM mode: {payload.get('effective_llm_mode', payload.get('llm_mode', 'off'))}",
        f"- Equity: {payload['account_before']['account'].get('deposit', {}).get('total_eval', 0):,}원",
    ]
    market_risk = payload.get("market_risk") or {}
    if market_risk:
        lines.extend([
            "",
            "## Market Risk",
            f"- Risk gate open: {market_risk.get('risk_gate_open', True)}",
            f"- New buys blocked in risk_control: {market_risk.get('risk_control_blocks_new_buys', False)}",
            f"- Reasons: {'; '.join(market_risk.get('reasons') or []) or '-'}",
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
            f"- Target count: {strategy_orchestration.get('target_strategy_count', {}).get('min', 8)}"
            f"-{strategy_orchestration.get('target_strategy_count', {}).get('max', 12)}",
            f"- Symbol limit: {strategy_orchestration.get('symbol_limit', '-')}",
            f"- Risk gate open: {strategy_orchestration.get('risk_gate_open', True)}",
            f"- Warnings: {'; '.join(strategy_orchestration.get('warnings') or []) or '-'}",
            "",
            "| 전략 | 계열 | 가중치 | 선택 이유 |",
            "|---|---|---:|---|",
        ])
        for item in enabled:
            lines.append(
                f"| {item.get('name', item.get('id'))} ({item.get('id')}) | "
                f"{item.get('family', '-')} | {float(item.get('weight') or 0):.2f} | "
                f"{item.get('reason', '-')} |"
            )
        if disabled:
            lines.extend(["", "### Disabled Strategy Candidates", "| 전략 | 이유 |", "|---|---|"])
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
            "| 전략 | 상태 | 결과 수 | 메시지 |",
            "|---|---|---:|---|",
        ])
        for item in strategy_run.get("runs") or []:
            lines.append(
                f"| {item.get('strategy_id')} | {item.get('status')} | "
                f"{int(item.get('result_count') or 0)} | {item.get('message') or '-'} |"
            )
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
            f"- Warnings: {'; '.join(candidate_selection.get('warnings') or []) or '-'}",
            "",
            "| 종목 | 구분 | 점수 | 원천 | 선정 이유 |",
            "|---|---|---:|---|---|",
        ])
        for item in selected_candidates:
            lines.append(
                f"| {item.get('name', item.get('code'))}({item.get('code')}) | {item.get('category', '-')} | "
                f"{float(item.get('score') or 0):.2f} | {', '.join(item.get('sources') or [])} | "
                f"{', '.join(item.get('reasons') or [])} |"
            )

    if not payload.get("trading_day", {}).get("is_open", True):
        lines.extend([
            "",
            "## Market Closed",
            f"- Source: {payload.get('trading_day', {}).get('source')}",
            f"- Status: {payload.get('trading_day', {}).get('status')}",
            f"- Reason: {payload.get('trading_day', {}).get('record') or payload.get('trading_day', {}).get('error', '')}",
        ])
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    lines.extend(["", "## Signals", "| 종목 | 신호 | 강도 | 현재가 | 이유 |", "|---|---:|---:|---:|---|"])
    for signal in payload["signals"]:
        lines.append(
            f"| {signal.get('name', signal.get('code'))}({signal.get('code')}) | {signal.get('action')} | "
            f"{float(signal.get('strength') or 0):.2f} | {float(signal.get('target_price') or 0):,.0f} | "
            f"{signal.get('reason', '')} |"
        )

    order_decisions = payload.get("order_decisions") or []
    if order_decisions:
        lines.extend([
            "",
            "## Order Gate Decisions",
            "| 종목 | 신호 | 강도 | 상태 | 이유 |",
            "|---|---:|---:|---|---|",
        ])
        for item in order_decisions:
            lines.append(
                f"| {item.get('name', item.get('code'))}({item.get('code')}) | {item.get('action')} | "
                f"{float(item.get('strength') or 0):.2f} | {item.get('status')} | "
                f"{'; '.join(item.get('reasons') or []) or '-'} |"
            )

    llm_result = payload.get("llm_result")
    if llm_result:
        decision = llm_result.get("decision", {})
        lines.extend([
            "",
            "## LLM Decision",
            f"- Status: {llm_result.get('status')}",
            f"- Model: {llm_result.get('model', '-')}",
            f"- Market regime: {decision.get('market_regime', '-')}",
            f"- Risk level: {decision.get('risk_level', '-')}",
            f"- Should trade: {decision.get('should_trade', False)}",
            f"- Notes: {decision.get('notes', '')}",
            "",
            "| 종목 | 승인수량 | 신뢰도 | 이유 |",
            "|---|---:|---:|---|",
        ])
        for item in decision.get("approved_buys", []):
            lines.append(
                f"| {item.get('code')} | {item.get('max_quantity', 0)} | "
                f"{float(item.get('confidence') or 0):.2f} | {item.get('reason', '')} |"
            )
        for item in decision.get("blocked_symbols", []):
            lines.append(f"| {item.get('code')} | 0 | 0.00 | BLOCKED: {item.get('reason', '')} |")

    lines.extend([
        "",
        "## Pre Order Table",
        "| 종목 | 구분 | 신호 | 강도 | 현재가 | 예상 수량 | 예상 금액 | 배분 비중 | 익절가(+6%) | 손절가(-3%) | 주문 방식 | 주문 여부 |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|",
    ])
    candidate_categories = {
        str(item.get("code")): item.get("category", "대형주")
        for item in selected_candidates
    }
    for order in payload["planned_buys"]:
        kind = candidate_categories.get(str(order["code"]), "대형주")
        lines.append(
            f"| {order.get('name')}({order['code']}) | {kind} | BUY | {order['strength']:.2f} | "
            f"{order['target_price']:,.0f} | {order['quantity']} | {order['amount']:,.0f} | "
            f"{order['weight']*100:.1f}% | {order['take_profit']:,.0f} | {order['stop_loss']:,.0f} | "
            f"일반 {payload.get('trade_mode', 'vps')} 시장가 | {order['order_decision']} |"
        )

    telegram_rows = []
    for section, items in (
        ("SELL", payload.get("submitted_sells") or []),
        ("BUY", payload.get("submitted_buys") or []),
        ("PROTECTION", payload.get("protections") or []),
    ):
        for item in items:
            approval = item.get("telegram_approval")
            if not approval:
                continue
            code = item.get("code") or item.get("stock_code") or approval.get("stock_code") or "-"
            name = item.get("name") or item.get("stock_name") or code
            telegram_rows.append(
                (
                    section,
                    f"{name}({code})",
                    approval.get("approval_id", "-"),
                    approval.get("status", "-"),
                    item.get("order_status") or item.get("status") or "-",
                )
            )
    if telegram_rows:
        lines.extend([
            "",
            "## Prod Telegram Approvals",
            "| 구분 | 종목 | 승인 ID | 승인 상태 | 주문/설정 상태 |",
            "|---|---|---|---|---|",
        ])
        for section, stock, approval_id, approval_status, order_status in telegram_rows:
            lines.append(f"| {section} | {stock} | {approval_id} | {approval_status} | {order_status} |")

    lines.extend([
        "",
        "## Post Fill Status",
        "| 종목 | 체결 수량 | 평균단가 | 익절 지정가 | 손절 지정가 | 보호주문 상태 | 예약/미체결 상태 |",
        "|---|---:|---:|---:|---:|---|---|",
    ])
    protective_orders = payload["account_after"].get("protective", {}).get("orders", [])
    pending_count = payload["account_after"].get("pending", {}).get("total_count", 0)
    reservation_status = payload["account_after"].get("reservations", {}).get("status", "unknown")
    for row in payload["account_after"].get("account", {}).get("holdings", []):
        code = str(row.get("stock_code"))
        protection = next((item for item in reversed(protective_orders) if str(item.get("stock_code")) == code), {})
        lines.append(
            f"| {row.get('stock_name')}({code}) | {int(float(row.get('quantity') or 0))} | "
            f"{float(row.get('avg_price') or 0):,.0f} | {float(protection.get('take_profit_limit_price') or 0):,.0f} | "
            f"{float(protection.get('stop_loss_price') or 0):,.0f} | {protection.get('status', '-')} | "
            f"예약조회={reservation_status}, 미체결={pending_count} |"
        )

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


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
            errors.append(f"{signal.get('code') or signal.get('symbol')}: {signal.get('reason')}")
    for order in [*payload.get("submitted_sells", []), *payload.get("submitted_buys", [])]:
        status = str(order.get("order_status") or "")
        if status in {"failed", "error", "reservation_failed"} and (order.get("last_error") or order.get("message")):
            errors.append(
                f"{order.get('code') or order.get('stock_code') or order.get('symbol')}: "
                f"{order.get('last_error') or order.get('message')}"
            )
        result = order.get("order_result") if isinstance(order.get("order_result"), dict) else {}
        if result.get("status") == "error" and result.get("message"):
            errors.append(f"{order.get('code') or order.get('stock_code')}: {result.get('message')}")
    for item in payload.get("account_after", {}).get("reservations", {}).get("errors", []):
        errors.append(f"reservations: {item}")
    protective_health = payload.get("account_after", {}).get("protective", {}).get("health", {})
    if protective_health.get("status") in {"degraded", "stale"}:
        errors.append(f"protective monitor: {protective_health.get('status')}")
    return list(dict.fromkeys(errors))


def compact_account(snapshot: dict[str, Any]) -> dict[str, Any]:
    account = snapshot.get("account", {}) if isinstance(snapshot, dict) else {}
    deposit = account.get("deposit", {}) if isinstance(account, dict) else {}
    holdings = account.get("holdings", []) if isinstance(account, dict) else []
    total_eval = float(deposit.get("total_eval") or 0)
    cash = float(deposit.get("deposit") or deposit.get("available_amount") or 0)
    return {
        "equity": round(total_eval, 2),
        "risk_equity": round(total_eval, 2),
        "cash": round(cash, 2),
        "holdings_count": len(holdings) if isinstance(holdings, list) else 0,
    }


def run_summary(payload: dict[str, Any]) -> dict[str, Any]:
    signals = payload.get("signals") or []
    orders = [*payload.get("submitted_sells", []), *payload.get("submitted_buys", [])]
    success_statuses = {"success", "submitted", "reservation_submitted"}
    failed_statuses = {"failed", "error", "reservation_failed"}
    return {
        "run_id": payload.get("run_id"),
        "slot": payload.get("slot"),
        "scheduled_at_kst": payload.get("scheduled_at_kst"),
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
            "submitted": sum(1 for order in orders if order.get("order_status") in success_statuses),
            "filled": sum(1 for order in orders if float(order.get("filled_quantity") or 0) > 0),
            "failed": sum(1 for order in orders if order.get("order_status") in failed_statuses),
            "skipped": sum(1 for order in orders if str(order.get("order_status") or "").startswith("skipped")),
        },
        "buy_notional": round(sum(
            float(order.get("amount") or order.get("notional") or 0)
            for order in payload.get("submitted_buys", [])
            if order.get("order_status") in success_statuses
        ), 2),
        "account_before": compact_account(payload.get("account_before", {})),
        "account_after": compact_account(payload.get("account_after", {})),
        "pending_count": int(payload.get("account_after", {}).get("pending", {}).get("total_count") or 0),
        "app_reservation_count": int(payload.get("account_after", {}).get("reservations", {}).get("total_count") or 0),
        "protective_count": len(payload.get("account_after", {}).get("protective", {}).get("orders") or []),
        "errors": collect_payload_errors(payload),
        "json_report": f"{payload.get('run_id')}.json",
        "markdown_report": f"{payload.get('run_id')}.md",
    }


def normalize_summary_run(run: dict[str, Any]) -> dict[str, Any]:
    if run.get("run_id") and run.get("signal_counts") and run.get("order_counts"):
        return run
    report = str(run.get("report") or "")
    report_id = Path(report).stem if report else ""
    status = run.get("status") or ("market_closed" if run.get("skipped") == "market_closed" else "legacy")
    return {
        "run_id": run.get("run_id") or report_id or f"legacy_{run.get('started_at', 'unknown')}",
        "slot": run.get("slot", "legacy"),
        "scheduled_at_kst": run.get("scheduled_at_kst"),
        "started_at": run.get("started_at"),
        "finished_at": None,
        "duration_seconds": 0.0,
        "status": status,
        "report_only": False,
        "signal_counts": {"BUY": 0, "SELL": 0, "HOLD": 0, "ERROR": 0},
        "order_counts": {"submitted": 0, "filled": 0, "failed": 0, "skipped": 0},
        "buy_notional": 0.0,
        "account_before": {},
        "account_after": {},
        "pending_count": 0,
        "app_reservation_count": 0,
        "protective_count": 0,
        "errors": [],
        "json_report": f"{report_id}.json" if report_id else None,
        "markdown_report": f"{report_id}.md" if report_id else None,
    }


def write_session_summary(session_date: str, state: dict[str, Any]) -> dict[str, Any]:
    runs = [normalize_summary_run(run) for run in state.get("runs", []) if isinstance(run, dict)]
    cumulative_buy = round(sum(float(run.get("buy_notional") or 0) for run in runs), 2)
    latest_account = next((run.get("account_after") for run in reversed(runs) if run.get("account_after")), {})
    risk_equity = float((latest_account or {}).get("risk_equity") or 0)
    summary = {
        "session_date": session_date,
        "mode": "vps",
        "updated_at": datetime.now().isoformat(timespec="seconds"),
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
    (RUNTIME_DIR / f"{session_date}_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    lines = [
        f"# KR Automation Session - {session_date}",
        "",
        "- Mode: vps only",
        f"- Updated: {summary['updated_at']}",
        f"- Runs: {summary['run_count']}",
        f"- Cumulative buys: {cumulative_buy:,.0f}원",
        f"- Remaining buy budget: {summary['remaining_buy_budget']:,.0f}원",
        f"- Remaining loss budget: {summary['remaining_loss_budget']:,.0f}원",
        "",
        "## Timeline",
        "| KST Time | Run | Status | Duration | BUY/SELL/HOLD | Submitted/Filled/Failed | Errors |",
        "|---|---|---|---:|---|---|---:|",
    ]
    for run in runs:
        counts = run.get("signal_counts", {})
        order_counts = run.get("order_counts", {})
        lines.append(
            f"| {run.get('scheduled_at_kst') or '-'} | {run.get('run_id')} | {run.get('status')} | "
            f"{float(run.get('duration_seconds') or 0):.1f}s | "
            f"{counts.get('BUY', 0)}/{counts.get('SELL', 0)}/{counts.get('HOLD', 0)} | "
            f"{order_counts.get('submitted', 0)}/{order_counts.get('filled', 0)}/{order_counts.get('failed', 0)} | "
            f"{len(run.get('errors') or [])} |"
        )
    (RUNTIME_DIR / f"{session_date}_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


def telegram_message(payload: dict[str, Any], summary: dict[str, Any] | None = None) -> str:
    if payload.get("status") == "market_closed":
        return f"KR paper automation {payload.get('date')}: market closed. No orders."
    run = run_summary(payload)
    counts = run["signal_counts"]
    order_counts = run["order_counts"]
    report_url = (
        os.environ.get("KR_MARKET_REPORT_URL")
        or os.environ.get("US_MARKET_REPORT_URL")
        or ""
    ).rstrip("/")
    link = f"\n{report_url}/automation" if report_url else ""
    text = (
        f"KR paper {run.get('run_id')} {run.get('status')}\n"
        f"Signals B/S/H {counts['BUY']}/{counts['SELL']}/{counts['HOLD']}\n"
        f"Orders submitted/filled/failed "
        f"{order_counts['submitted']}/{order_counts['filled']}/{order_counts['failed']}\n"
        f"Buy {run['buy_notional']:,.0f}원, errors {len(run['errors'])}{link}"
    )
    if summary is not None:
        text += (
            f"\nSession total: {summary['run_count']} runs, "
            f"buys {summary['cumulative_buy_notional']:,.0f}원, "
            f"remaining risk {summary['remaining_loss_budget']:,.0f}원"
        )
    return text


def session_telegram_message(summary: dict[str, Any]) -> str:
    report_url = (
        os.environ.get("KR_MARKET_REPORT_URL")
        or os.environ.get("US_MARKET_REPORT_URL")
        or ""
    ).rstrip("/")
    link = f"\n{report_url}/automation" if report_url else ""
    totals = summary.get("totals", {})
    return (
        f"KR paper session {summary.get('session_date')} complete\n"
        f"Runs {summary.get('run_count', 0)}, submitted/filled/failed "
        f"{totals.get('submitted', 0)}/{totals.get('filled', 0)}/{totals.get('failed', 0)}\n"
        f"Buys {float(summary.get('cumulative_buy_notional') or 0):,.0f}원, "
        f"remaining loss budget {float(summary.get('remaining_loss_budget') or 0):,.0f}원, "
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


def main() -> int:
    global API_BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True, choices=["hourly", "open", "mid", "close", "manual"])
    parser.add_argument("--date", required=True)
    parser.add_argument("--run-id", help="Stable run identifier, normally YYYYMMDD_HHMM_KST.")
    parser.add_argument("--report-only", action="store_true", help="Generate reports without submitting orders.")
    parser.add_argument("--record-skip", choices=["skipped_overlap", "skipped_duplicate", "skipped_not_scheduled"])
    parser.add_argument(
        "--trade-mode",
        choices=["vps", "prod"],
        default=os.environ.get("KIS_TRADE_MODE", "vps"),
    )
    parser.add_argument(
        "--llm-mode",
        choices=["off", "shadow", "live-vps", "live-prod"],
        default=os.environ.get("KR_MARKET_LLM_MODE", "off"),
    )
    parser.add_argument(
        "--prod-auto-confirm",
        action="store_true",
        help="Allow prod order submission for this run. Prefer KIS_PROD_AUTO_CONFIRM for cron.",
    )
    args = parser.parse_args()
    API_BASE = api_base_for(args.trade_mode)
    effective_llm_mode, llm_warnings = normalize_llm_mode(args.llm_mode)

    global RUNTIME_DIR
    RUNTIME_DIR = runtime_dir_for(args.trade_mode)
    prod_confirmed = prod_auto_confirmed(args.prod_auto_confirm)
    monotonic_started = time.monotonic()
    prod_telegram_enabled = prod_telegram_approval_enabled()
    order_block_reasons = []
    if args.report_only:
        order_block_reasons.append("report_only")
    if args.trade_mode == "prod" and not prod_confirmed and not prod_telegram_enabled:
        order_block_reasons.append("one_time_prod_approval_missing")
    can_submit_orders = (
        order_execution_enabled(args.trade_mode, prod_confirmed, prod_telegram_enabled)
        and not args.report_only
    )
    strategy_sell_enabled = strategy_sell_execution_enabled(args.trade_mode)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    run_id = args.run_id or f"{now.strftime('%Y%m%d_%H%M%S')}_{args.slot}"
    report_base = RUNTIME_DIR / run_id
    state = load_state(args.date)

    if args.record_skip:
        record_skip_event(args.date, run_id, args.record_skip, args.slot)
        write_session_summary(args.date, load_state(args.date))
        return 0
    if run_already_recorded(state, run_id):
        record_skip_event(args.date, run_id, "skipped_duplicate", args.slot)
        write_session_summary(args.date, load_state(args.date))
        return 0

    trading_day = trading_day_status(args.date)
    schedule_entry = next(
        (item for item in trading_day.get("scheduled_runs", []) if item.get("run_id") == run_id),
        {},
    )
    if args.slot == "hourly" and trading_day.get("is_open") and args.run_id and not schedule_entry:
        record_skip_event(args.date, run_id, "skipped_not_scheduled", args.slot)
        write_session_summary(args.date, load_state(args.date))
        return 0

    if not trading_day.get("is_open"):
        finished = datetime.now()
        payload = {
            "run_id": run_id,
            "slot": args.slot,
            "date": args.date,
            "started_at": now.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "duration_seconds": round(time.monotonic() - monotonic_started, 2),
            "status": "market_closed",
            "report_only": True,
            "scheduled_at_kst": schedule_entry.get("scheduled_at_kst"),
            "headlines": [],
            "news_summary": {"regime": "market_closed", "scores": {}},
            "signals": [],
            "signal_groups": {"BUY": [], "SELL": [], "HOLD": []},
            "planned_buys": [],
            "raw_planned_buys": [],
            "executable_buys": [],
            "submitted_buys": [],
            "submitted_sells": [],
            "protections": [],
            "account_before": {"account": {"deposit": {}}, "pending": {}, "reservations": {}, "protective": {}},
            "account_after": {"account": {"deposit": {}}, "pending": {}, "reservations": {}, "protective": {}},
            "llm_mode": args.llm_mode,
            "effective_llm_mode": effective_llm_mode,
            "llm_warnings": llm_warnings,
            "llm_result": None,
            "trade_mode": args.trade_mode,
            "prod_auto_confirmed": prod_confirmed,
            "prod_telegram_approval_enabled": prod_telegram_enabled,
            "order_execution_enabled": can_submit_orders,
            "order_block_reasons": order_block_reasons,
            "strategy_sell_execution_enabled": strategy_sell_enabled,
            "trading_day": trading_day,
            "candidate_selection": {
                "mode": os.environ.get("KR_MARKET_CANDIDATE_MODE", "dynamic"),
                "selected": [],
                "fallback_used": False,
                "errors": [],
                "warnings": [],
                "generated_at": now.isoformat(timespec="seconds"),
            },
            "market_risk": {
                "regime": "market_closed",
                "risk_gate_open": False,
                "reasons": ["market closed"],
                "risk_control_blocks_new_buys": RISK_CONTROL_BLOCKS_NEW_BUYS,
            },
            "strategy_orchestration": select_strategy_candidates(
                market="kr",
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
            "safety": {
                "mode": args.trade_mode,
                "total_new_buy_pct": TOTAL_BUY_PCT,
                "daily_loss_pct": DAILY_LOSS_PCT,
                "take_profit_pct": TAKE_PROFIT_PCT,
                "stop_loss_pct": STOP_LOSS_PCT,
                "min_buy_strength": MIN_BUY_STRENGTH,
                "max_new_buy_symbols": MAX_NEW_BUY_SYMBOLS,
                "max_per_symbol_buy_pct": MAX_PER_SYMBOL_BUY_PCT,
                "risk_control_blocks_new_buys": RISK_CONTROL_BLOCKS_NEW_BUYS,
                "risk_gate_open": False,
                "risk_gate_reasons": ["market closed"],
                "bought_today_before": sum(float(order.get("amount") or 0) for order in state.get("orders", [])),
                "order_block_reasons": order_block_reasons,
                "prod_telegram_approval_enabled": prod_telegram_enabled,
                "strategy_sell_execution_enabled": strategy_sell_enabled,
                "llm_warnings": llm_warnings,
            },
        }
        state.setdefault("runs", []).append({
            **run_summary(payload),
            "report": str(report_base.with_suffix(".md")),
            "skipped": "market_closed",
        })
        save_state(args.date, state)
        summary = write_session_summary(args.date, state)
        payload["telegram"] = send_telegram(telegram_message(payload, summary))
        report_base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        write_report(report_base.with_suffix(".md"), payload)
        return 0

    auth = api("POST", "/api/auth/login", json={"mode": args.trade_mode})
    if auth.get("mode") != args.trade_mode:
        raise RuntimeError(f"not {args.trade_mode} mode: {auth}")

    headlines = fetch_headlines()
    news_summary = summarize_news(headlines)
    market_risk = evaluate_market_risk(news_summary)
    account_before = account_snapshot()
    before_holding_codes = set(holding_by_code(account_before["account"]).keys())
    candidate_selection = select_kr_candidates(
        api_get=lambda path: api("GET", path),
        account=account_before["account"],
        static_candidates=STATIC_CANDIDATES,
    )

    stock_codes = [
        str(item["code"])
        for item in candidate_selection.get("selected", [])[:ORGANIC_STRATEGY_MAX_SYMBOLS]
    ]
    strategy_orchestration = select_strategy_candidates(
        market="kr",
        regime=market_risk["regime"],
        risk_gate_open=market_risk["risk_gate_open"],
    )
    strategy_orchestration["symbol_limit"] = ORGANIC_STRATEGY_MAX_SYMBOLS
    strategy_orchestration["symbols"] = stock_codes
    strategy_run = execute_strategy_pool(
        api,
        stock_codes,
        strategy_orchestration,
        market="domestic",
    )
    signals = strategy_run.get("merged_signals", [])
    groups = signal_groups(signals)
    sells = []
    if can_submit_orders and strategy_sell_enabled:
        sells = place_sells(
            groups.get("SELL", []),
            holding_by_code(account_before["account"]),
            args.trade_mode,
            prod_confirmed,
            prod_telegram_enabled,
        )
    planned_buys = build_buy_orders(
        signals,
        account_before["account"],
        state,
        market_regime=market_risk["regime"],
        risk_gate_open=market_risk["risk_gate_open"],
    )
    order_decisions = explain_order_decisions(
        signals,
        planned_buys,
        min_buy_strength=MIN_BUY_STRENGTH,
        risk_gate_open=market_risk["risk_gate_open"],
        risk_reasons=market_risk["reasons"],
        order_execution_enabled=can_submit_orders,
        order_block_reasons=order_block_reasons,
    )
    llm_context = {
        "run_id": run_id,
        "slot": args.slot,
        "date": args.date,
        "started_at": now.isoformat(timespec="seconds"),
        "scheduled_at_kst": schedule_entry.get("scheduled_at_kst"),
        "headlines": headlines,
        "news_summary": news_summary,
        "market_risk": market_risk,
        "strategy_orchestration": strategy_orchestration,
        "strategy_run": {
            key: value
            for key, value in strategy_run.items()
            if key != "merged_signals"
        },
        "signals": signals,
        "candidate_selection": candidate_selection,
        "planned_buys": planned_buys,
        "order_decisions": order_decisions,
        "submitted_sells": sells,
        "account_before": account_before,
        "safety": {
            "mode": args.trade_mode,
            "prod_auto_confirmed": prod_confirmed,
            "prod_telegram_approval_enabled": prod_telegram_enabled,
            "order_execution_enabled": can_submit_orders,
            "order_block_reasons": order_block_reasons,
            "risk_control_blocks_new_buys": RISK_CONTROL_BLOCKS_NEW_BUYS,
            "risk_gate_open": market_risk["risk_gate_open"],
            "risk_gate_reasons": market_risk["reasons"],
            "strategy_sell_execution_enabled": strategy_sell_enabled,
            "total_new_buy_pct": TOTAL_BUY_PCT,
            "daily_loss_pct": DAILY_LOSS_PCT,
            "take_profit_pct": TAKE_PROFIT_PCT,
            "stop_loss_pct": STOP_LOSS_PCT,
            "min_buy_strength": MIN_BUY_STRENGTH,
                "max_new_buy_symbols": MAX_NEW_BUY_SYMBOLS,
                "max_per_symbol_buy_pct": MAX_PER_SYMBOL_BUY_PCT,
                "organic_strategy_max_symbols": ORGANIC_STRATEGY_MAX_SYMBOLS,
                "bought_today_before": sum(float(order.get("amount") or 0) for order in state.get("orders", [])),
            },
    }
    llm_result = run_llm_decision(effective_llm_mode, llm_context)
    executable_buys = apply_llm_decision(planned_buys, llm_result, effective_llm_mode)
    if not can_submit_orders:
        executable_buys = []
    report_buys = annotate_buys_for_report(planned_buys, executable_buys, effective_llm_mode)
    if args.report_only:
        report_buys = [
            {**order, "order_decision": "리포트 전용/미주문"}
            for order in report_buys
        ]
    if not can_submit_orders and args.trade_mode == "prod":
        report_buys = [
            {**order, "order_decision": f"실전 주문 차단({','.join(order_block_reasons)})/미주문"}
            for order in report_buys
        ]
    buys = place_buys(
        executable_buys,
        args.trade_mode,
        prod_confirmed,
        prod_telegram_enabled,
    ) if can_submit_orders else []

    api("POST", "/api/orders/account/clear-cache")
    time.sleep(3.0)
    account_mid = account_snapshot()
    buy_approved_codes = {
        str(order.get("code"))
        for order in buys
        if order.get("order_status") == "success"
        and (order.get("telegram_approval") or {}).get("status") == "approved"
    }
    should_register_protection = can_submit_orders
    if args.trade_mode == "prod" and prod_telegram_enabled:
        should_register_protection = bool(buy_approved_codes)
    protections = (
        register_protection_for_holdings(
            before_holding_codes,
            account_mid["account"],
            args.trade_mode,
            prod_confirmed,
            set(stock_codes),
            prod_telegram_enabled,
            buy_approved_codes,
        )
        if should_register_protection
        else []
    )
    time.sleep(1.0)
    account_after = account_snapshot()

    payload = {
        "run_id": run_id,
        "slot": args.slot,
        "date": args.date,
        "started_at": now.isoformat(timespec="seconds"),
        "scheduled_at_kst": schedule_entry.get("scheduled_at_kst"),
        "headlines": headlines,
        "news_summary": news_summary,
        "market_risk": market_risk,
        "strategy_orchestration": strategy_orchestration,
        "strategy_run": {
            key: value
            for key, value in strategy_run.items()
            if key != "merged_signals"
        },
        "signals": signals,
        "signal_groups": groups,
        "candidate_selection": candidate_selection,
        "order_decisions": order_decisions,
        "planned_buys": report_buys,
        "raw_planned_buys": planned_buys,
        "executable_buys": executable_buys,
        "submitted_buys": buys,
        "submitted_sells": sells,
        "protections": protections,
        "account_before": account_before,
        "account_after": account_after,
        "llm_mode": args.llm_mode,
        "effective_llm_mode": effective_llm_mode,
        "llm_warnings": llm_warnings,
        "llm_result": llm_result,
        "trade_mode": args.trade_mode,
        "prod_auto_confirmed": prod_confirmed,
        "prod_telegram_approval_enabled": prod_telegram_enabled,
        "order_execution_enabled": can_submit_orders,
        "order_block_reasons": order_block_reasons,
        "strategy_sell_execution_enabled": strategy_sell_enabled,
        "trading_day": trading_day,
        "status": "report_only" if args.report_only else "completed",
        "report_only": args.report_only,
        "safety": {
            "mode": args.trade_mode,
            "prod_auto_confirmed": prod_confirmed,
            "prod_telegram_approval_enabled": prod_telegram_enabled,
            "order_execution_enabled": can_submit_orders,
            "order_block_reasons": order_block_reasons,
            "risk_control_blocks_new_buys": RISK_CONTROL_BLOCKS_NEW_BUYS,
            "risk_gate_open": market_risk["risk_gate_open"],
            "risk_gate_reasons": market_risk["reasons"],
            "strategy_sell_execution_enabled": strategy_sell_enabled,
            "llm_warnings": llm_warnings,
            "total_new_buy_pct": TOTAL_BUY_PCT,
            "daily_loss_pct": DAILY_LOSS_PCT,
            "take_profit_pct": TAKE_PROFIT_PCT,
            "stop_loss_pct": STOP_LOSS_PCT,
            "min_buy_strength": MIN_BUY_STRENGTH,
            "max_new_buy_symbols": MAX_NEW_BUY_SYMBOLS,
            "max_per_symbol_buy_pct": MAX_PER_SYMBOL_BUY_PCT,
            "organic_strategy_max_symbols": ORGANIC_STRATEGY_MAX_SYMBOLS,
            "bought_today_before": sum(float(order.get("amount") or 0) for order in state.get("orders", [])),
        },
    }
    finished = datetime.now()
    payload["finished_at"] = finished.isoformat(timespec="seconds")
    payload["duration_seconds"] = round(time.monotonic() - monotonic_started, 2)
    payload["errors"] = collect_payload_errors(payload)
    summary_row = run_summary(payload)
    state.setdefault("runs", []).append({**summary_row, "report": str(report_base.with_suffix(".md"))})
    state.setdefault("orders", []).extend(
        order for order in buys if order.get("order_status") == "success"
    )
    save_state(args.date, state)
    summary = write_session_summary(args.date, state)
    is_last_run = bool(schedule_entry) and schedule_entry == trading_day.get("scheduled_runs", [])[-1]
    payload["telegram"] = {
        "run": send_telegram(telegram_message(payload)),
    }
    if is_last_run:
        payload["telegram"]["session"] = send_telegram(session_telegram_message(summary))
    report_base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_report(report_base.with_suffix(".md"), payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
