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

import requests

from market_candidate_selector import select_kr_candidates


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


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "y", "on", "allow"}


TOTAL_BUY_PCT = env_fraction("KR_MARKET_TOTAL_BUY_PCT", 0.10)
DAILY_LOSS_PCT = env_fraction("KR_MARKET_DAILY_LOSS_PCT", 0.005)
STOP_LOSS_PCT = 0.03
TAKE_PROFIT_PCT = 0.06
MIN_BUY_STRENGTH = 0.70
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


def order_execution_enabled(trade_mode: str, prod_confirmed: bool) -> bool:
    return trade_mode != "prod" or prod_confirmed


def prod_llm_orders_enabled(trade_mode: str, llm_mode: str) -> bool:
    return trade_mode != "prod" or llm_mode == "live-prod"


def strategy_sell_execution_enabled(trade_mode: str) -> bool:
    return trade_mode != "prod" or env_truthy("KR_PROD_ALLOW_STRATEGY_SELL")


def is_live_llm_mode(mode: str) -> bool:
    return mode in {"live-vps", "live-prod"}


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


def build_buy_orders(results: list[dict[str, Any]], account: dict[str, Any], state: dict[str, Any]) -> list[dict[str, Any]]:
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
    remaining = usable_budget
    orders: list[dict[str, Any]] = []

    for row in candidates:
        price = float(row["target_price"])
        qty = int(remaining // price)
        if qty <= 0:
            continue
        amount = qty * price
        remaining -= amount
        orders.append({
            **row,
            "quantity": qty,
            "amount": amount,
            "weight": amount / max(1.0, usable_budget),
            "take_profit": math.floor(price * (1 + TAKE_PROFIT_PCT)),
            "stop_loss": math.floor(price * (1 - STOP_LOSS_PCT)),
            "order_type": "market",
            "order_decision": "주문",
        })
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
                "should_trade": False,
                "approved_buys": [],
                "blocked_symbols": [],
                "notes": "LLM decision failed; live-vps mode must block buys.",
            },
        }


def apply_llm_decision(
    planned_buys: list[dict[str, Any]],
    llm_result: dict[str, Any] | None,
    mode: str,
) -> list[dict[str, Any]]:
    if mode in {"off", "shadow"}:
        return planned_buys
    if not is_live_llm_mode(mode):
        raise RuntimeError(f"invalid llm mode: {mode}")
    if not llm_result or llm_result.get("status") != "success":
        return []

    decision = llm_result.get("decision", {})
    if not decision.get("should_trade"):
        return []

    approvals = {
        str(item.get("code")): item
        for item in decision.get("approved_buys", [])
        if item.get("code")
    }
    approved_orders = []
    for order in planned_buys:
        code = str(order.get("code"))
        approval = approvals.get(code)
        if not approval:
            continue
        qty = min(int(order.get("quantity") or 0), int(approval.get("max_quantity") or 0))
        if qty <= 0:
            continue
        adjusted = {**order}
        if qty != int(order.get("quantity") or 0):
            price = float(order.get("target_price") or 0)
            adjusted["quantity"] = qty
            adjusted["amount"] = qty * price
            adjusted["weight"] = 0.0
        adjusted["llm_approved"] = True
        adjusted["llm_confidence"] = float(approval.get("confidence") or 0)
        adjusted["llm_reason"] = approval.get("reason", "")
        approved_orders.append(adjusted)
    return approved_orders


def annotate_buys_for_report(
    planned_buys: list[dict[str, Any]],
    executable_buys: list[dict[str, Any]],
    mode: str,
) -> list[dict[str, Any]]:
    if not is_live_llm_mode(mode):
        return planned_buys

    executable_by_code = {str(order.get("code")): order for order in executable_buys}
    reported = []
    for order in planned_buys:
        code = str(order.get("code"))
        executable = executable_by_code.get(code)
        if executable:
            reported.append({
                **order,
                **{
                    key: executable[key]
                    for key in ("quantity", "amount", "weight", "llm_confidence", "llm_reason")
                    if key in executable
                },
                "order_decision": "LLM 승인/주문",
            })
        else:
            reported.append({
                **order,
                "quantity": 0,
                "amount": 0,
                "weight": 0,
                "order_decision": "LLM 차단/미주문",
            })
    return reported


def holding_by_code(account: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(row.get("stock_code")): row for row in account.get("holdings", [])}


def place_sells(
    results: list[dict[str, Any]],
    holdings: dict[str, dict[str, Any]],
    trade_mode: str,
    prod_confirmed: bool,
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
            "confirm_prod": trade_mode == "prod" and prod_confirmed,
        }
        result = api("POST", "/api/orders/execute", json=payload)
        signal["order_status"] = result.get("status")
        signal["order_result"] = result
        submitted.append(signal)
        time.sleep(1.0)
    return submitted


def place_buys(
    orders: list[dict[str, Any]],
    trade_mode: str,
    prod_confirmed: bool,
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
            "confirm_prod": trade_mode == "prod" and prod_confirmed,
        }
        result = api("POST", "/api/orders/execute", json=payload)
        order["order_status"] = result.get("status")
        order["order_result"] = result
        submitted.append(order)
        time.sleep(1.0)
    return submitted


def register_protection_for_holdings(
    before_codes: set[str],
    after_account: dict[str, Any],
    trade_mode: str,
    prod_confirmed: bool,
    candidate_codes: set[str],
) -> list[dict[str, Any]]:
    protections = []
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
            "confirm_prod": trade_mode == "prod" and prod_confirmed,
        }
        try:
            protections.append(api("POST", "/api/orders/protective", json=payload))
        except Exception as exc:
            protections.append({"status": "error", "stock_code": code, "message": str(exc)})
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
        f"- Equity: {payload['account_before']['account'].get('deposit', {}).get('total_eval', 0):,}원",
        "",
        "## Headlines",
    ]
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


def main() -> int:
    global API_BASE
    parser = argparse.ArgumentParser()
    parser.add_argument("--slot", required=True, choices=["open", "mid", "close", "manual"])
    parser.add_argument("--date", required=True)
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

    global RUNTIME_DIR
    RUNTIME_DIR = runtime_dir_for(args.trade_mode)
    prod_confirmed = prod_auto_confirmed(args.prod_auto_confirm)
    order_block_reasons = []
    if args.trade_mode == "prod" and not prod_confirmed:
        order_block_reasons.append("one_time_prod_approval_missing")
    if not prod_llm_orders_enabled(args.trade_mode, args.llm_mode):
        order_block_reasons.append("prod_requires_live_prod_llm")
    can_submit_orders = order_execution_enabled(args.trade_mode, prod_confirmed) and prod_llm_orders_enabled(args.trade_mode, args.llm_mode)
    strategy_sell_enabled = strategy_sell_execution_enabled(args.trade_mode)
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    report_base = RUNTIME_DIR / f"{now.strftime('%Y%m%d_%H%M%S')}_{args.slot}"
    state = load_state(args.date)
    trading_day = trading_day_status(args.date)

    if not trading_day.get("is_open"):
        payload = {
            "slot": args.slot,
            "date": args.date,
            "started_at": now.isoformat(timespec="seconds"),
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
            "llm_result": None,
            "trade_mode": args.trade_mode,
            "prod_auto_confirmed": prod_confirmed,
            "order_execution_enabled": can_submit_orders,
            "order_block_reasons": order_block_reasons,
            "strategy_sell_execution_enabled": strategy_sell_enabled,
            "trading_day": trading_day,
            "candidate_selection": {
                "mode": os.environ.get("KR_MARKET_CANDIDATE_MODE", "dynamic"),
                "selected": [],
                "fallback_used": False,
                "errors": [],
                "generated_at": now.isoformat(timespec="seconds"),
            },
            "safety": {
                "mode": args.trade_mode,
                "total_new_buy_pct": TOTAL_BUY_PCT,
                "daily_loss_pct": DAILY_LOSS_PCT,
                "take_profit_pct": TAKE_PROFIT_PCT,
                "stop_loss_pct": STOP_LOSS_PCT,
                "min_buy_strength": MIN_BUY_STRENGTH,
                "bought_today_before": sum(float(order.get("amount") or 0) for order in state.get("orders", [])),
                "order_block_reasons": order_block_reasons,
                "strategy_sell_execution_enabled": strategy_sell_enabled,
            },
        }
        state.setdefault("runs", []).append({
            "slot": args.slot,
            "started_at": payload["started_at"],
            "report": str(report_base.with_suffix(".md")),
            "skipped": "market_closed",
        })
        save_state(args.date, state)
        report_base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        write_report(report_base.with_suffix(".md"), payload)
        return 0

    auth = api("POST", "/api/auth/login", json={"mode": args.trade_mode})
    if auth.get("mode") != args.trade_mode:
        raise RuntimeError(f"not {args.trade_mode} mode: {auth}")

    headlines = fetch_headlines()
    news_summary = summarize_news(headlines)
    account_before = account_snapshot()
    before_holding_codes = set(holding_by_code(account_before["account"]).keys())
    candidate_selection = select_kr_candidates(
        api_get=lambda path: api("GET", path),
        account=account_before["account"],
        static_candidates=STATIC_CANDIDATES,
    )

    stock_codes = [str(item["code"]) for item in candidate_selection.get("selected", [])]
    signals_response = api("POST", "/api/strategies/execute", json={
        "strategy_id": "custom:today_krx_macro_rebound",
        "stocks": stock_codes,
        "params": {},
        "market": "domestic",
    })
    signals = signals_response.get("results", [])
    groups = signal_groups(signals)
    sells = []
    if can_submit_orders and strategy_sell_enabled:
        sells = place_sells(
            groups.get("SELL", []),
            holding_by_code(account_before["account"]),
            args.trade_mode,
            prod_confirmed,
        )
    planned_buys = build_buy_orders(signals, account_before["account"], state)
    llm_context = {
        "slot": args.slot,
        "date": args.date,
        "started_at": now.isoformat(timespec="seconds"),
        "headlines": headlines,
        "news_summary": news_summary,
        "signals": signals,
        "candidate_selection": candidate_selection,
        "planned_buys": planned_buys,
        "submitted_sells": sells,
        "account_before": account_before,
        "safety": {
            "mode": args.trade_mode,
            "prod_auto_confirmed": prod_confirmed,
            "order_execution_enabled": can_submit_orders,
            "order_block_reasons": order_block_reasons,
            "strategy_sell_execution_enabled": strategy_sell_enabled,
            "total_new_buy_pct": TOTAL_BUY_PCT,
            "daily_loss_pct": DAILY_LOSS_PCT,
            "take_profit_pct": TAKE_PROFIT_PCT,
            "stop_loss_pct": STOP_LOSS_PCT,
            "min_buy_strength": MIN_BUY_STRENGTH,
            "bought_today_before": sum(float(order.get("amount") or 0) for order in state.get("orders", [])),
        },
    }
    llm_result = run_llm_decision(args.llm_mode, llm_context)
    executable_buys = apply_llm_decision(planned_buys, llm_result, args.llm_mode)
    if not can_submit_orders:
        executable_buys = []
    report_buys = annotate_buys_for_report(planned_buys, executable_buys, args.llm_mode)
    if not can_submit_orders and args.trade_mode == "prod":
        report_buys = [
            {**order, "order_decision": f"실전 주문 차단({','.join(order_block_reasons)})/미주문"}
            for order in report_buys
        ]
    buys = place_buys(executable_buys, args.trade_mode, prod_confirmed) if can_submit_orders else []

    api("POST", "/api/orders/account/clear-cache")
    time.sleep(3.0)
    account_mid = account_snapshot()
    protections = (
        register_protection_for_holdings(
            before_holding_codes,
            account_mid["account"],
            args.trade_mode,
            prod_confirmed,
            set(stock_codes),
        )
        if can_submit_orders
        else []
    )
    time.sleep(1.0)
    account_after = account_snapshot()

    payload = {
        "slot": args.slot,
        "date": args.date,
        "started_at": now.isoformat(timespec="seconds"),
        "headlines": headlines,
        "news_summary": news_summary,
        "signals": signals,
        "signal_groups": groups,
        "candidate_selection": candidate_selection,
        "planned_buys": report_buys,
        "raw_planned_buys": planned_buys,
        "executable_buys": executable_buys,
        "submitted_buys": buys,
        "submitted_sells": sells,
        "protections": protections,
        "account_before": account_before,
        "account_after": account_after,
        "llm_mode": args.llm_mode,
        "llm_result": llm_result,
        "trade_mode": args.trade_mode,
        "prod_auto_confirmed": prod_confirmed,
        "order_execution_enabled": can_submit_orders,
        "order_block_reasons": order_block_reasons,
        "strategy_sell_execution_enabled": strategy_sell_enabled,
        "trading_day": trading_day,
        "safety": {
            "mode": args.trade_mode,
            "prod_auto_confirmed": prod_confirmed,
            "order_execution_enabled": can_submit_orders,
            "order_block_reasons": order_block_reasons,
            "strategy_sell_execution_enabled": strategy_sell_enabled,
            "total_new_buy_pct": TOTAL_BUY_PCT,
            "daily_loss_pct": DAILY_LOSS_PCT,
            "take_profit_pct": TAKE_PROFIT_PCT,
            "stop_loss_pct": STOP_LOSS_PCT,
            "min_buy_strength": MIN_BUY_STRENGTH,
            "bought_today_before": sum(float(order.get("amount") or 0) for order in state.get("orders", [])),
        },
    }
    state.setdefault("runs", []).append({
        "slot": args.slot,
        "started_at": payload["started_at"],
        "report": str(report_base.with_suffix(".md")),
    })
    state.setdefault("orders", []).extend(
        order for order in buys if order.get("order_status") == "success"
    )
    save_state(args.date, state)
    report_base.with_suffix(".json").write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    write_report(report_base.with_suffix(".md"), payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
