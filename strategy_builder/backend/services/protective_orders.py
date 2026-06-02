"""Application-level protective orders.

KIS order-cash submits one normal order at a time. This service emulates OCO for
domestic stocks by keeping a local protection group:

- submit/retry a take-profit limit sell when possible
- monitor current price for stop-loss
- cancel the take-profit order before submitting the stop-loss market sell

It is intentionally scoped to already-authenticated Strategy Builder sessions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from core import data_fetcher, overseas_data_fetcher, reserved_orders
from core.data_fetcher import cancel_order, get_holdings, get_pending_orders
from core.order_executor import OrderExecutor
from core.signal import Action, Signal

logger = logging.getLogger(__name__)

RUNTIME_DIR = Path(
    os.environ.get(
        "KIS_RUNTIME_DIR",
        str(Path(__file__).resolve().parents[2] / ".runtime"),
    )
)
STATE_FILE = RUNTIME_DIR / "protective_orders.json"
DEFAULT_MONITOR_INTERVAL_SECONDS = 15
MIN_MONITOR_INTERVAL_SECONDS = 5
MAX_MONITOR_INTERVAL_SECONDS = 300
REALTIME_FRESH_SECONDS = 30
POSITION_MISSING_CONFIRMATIONS = 3
EXIT_SUBMIT_RETRY_SECONDS = 60
US_PAPER_LOCAL_RESERVATION_RETRY_SECONDS = 60
US_PAPER_BROKER_RESERVATION_RETRY_SECONDS = 6 * 60 * 60
US_PAPER_BROKER_RESERVATION_REGULAR_RETRY_SECONDS = 5 * 60
EXIT_PENDING_REPRICE_SECONDS = 60
US_STOP_LOSS_MARKETABLE_LIMIT_BUFFER_PCT = 2.0
US_PAPER_LOCAL_RESERVATION_MARKERS = (
    "90000000",
    "40490000",
    "모의투자에서는 해당업무가 제공되지 않습니다",
    "모의투자 예약주문시간",
)
US_PAPER_LOCAL_RESERVATION_RETRY_STATUS = "waiting_retry"

_monitor_task: Optional[asyncio.Task] = None
_state_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _get_tick_size(price: int) -> int:
    if price < 2000:
        return 1
    if price < 5000:
        return 5
    if price < 20000:
        return 10
    if price < 50000:
        return 50
    if price < 200000:
        return 100
    if price < 500000:
        return 500
    return 1000


def _round_up_to_tick(price: float) -> int:
    value = int(math.ceil(price))
    tick = _get_tick_size(value)
    return int(math.ceil(value / tick) * tick)


def _round_down_to_tick(price: float) -> int:
    value = int(math.floor(price))
    tick = _get_tick_size(value)
    return int(math.floor(value / tick) * tick)


def _round_price(market: str, price: float, direction: str) -> float:
    if market == "us":
        return round(float(price), 2)
    if direction == "up":
        return float(_round_up_to_tick(price))
    return float(_round_down_to_tick(price))


def _load_state_sync() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {
            "orders": [],
            "settings": {
                "monitor_interval_seconds": DEFAULT_MONITOR_INTERVAL_SECONDS,
                "price_source": "websocket",
            },
        }
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("orders"), list):
            settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
            interval = settings.get("monitor_interval_seconds", DEFAULT_MONITOR_INTERVAL_SECONDS)
            data["settings"] = {
                **settings,
                "monitor_interval_seconds": _normalize_monitor_interval(interval),
                "price_source": settings.get("price_source") or "websocket",
            }
            return data
    except Exception as exc:
        logger.warning("protective order state load failed: %s", exc)
    return {
        "orders": [],
        "settings": {
            "monitor_interval_seconds": DEFAULT_MONITOR_INTERVAL_SECONDS,
            "price_source": "websocket",
        },
    }


def _save_state_sync(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


async def _load_state() -> dict[str, Any]:
    async with _state_lock:
        return await asyncio.to_thread(_load_state_sync)


async def _save_state(state: dict[str, Any]) -> None:
    async with _state_lock:
        await asyncio.to_thread(_save_state_sync, state)


async def _merge_updated_orders(updated_orders: list[dict[str, Any]]) -> dict[str, Any]:
    """Save monitor updates without dropping orders added by concurrent UI/API saves."""
    async with _state_lock:
        latest = await asyncio.to_thread(_load_state_sync)
        latest_orders = latest.get("orders", [])
        updated_by_id = {
            order.get("id"): order
            for order in updated_orders
            if order.get("id")
        }

        merged_orders = []
        for order in latest_orders:
            order_id = order.get("id")
            merged_orders.append(updated_by_id.get(order_id, order))

        latest["orders"] = merged_orders
        await asyncio.to_thread(_save_state_sync, latest)
        return latest


def _normalize_monitor_interval(value: Any) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        interval = DEFAULT_MONITOR_INTERVAL_SECONDS
    return max(MIN_MONITOR_INTERVAL_SECONDS, min(MAX_MONITOR_INTERVAL_SECONDS, interval))


async def _get_monitor_interval_seconds() -> int:
    state = await _load_state()
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    return _normalize_monitor_interval(settings.get("monitor_interval_seconds"))


def _is_recent_realtime(order: dict[str, Any]) -> bool:
    value = order.get("last_realtime_at")
    if not value:
        return False
    try:
        checked_at = datetime.fromisoformat(str(value))
    except ValueError:
        return False
    return (datetime.now() - checked_at).total_seconds() <= REALTIME_FRESH_SECONDS


def _is_us_regular_session_now() -> bool:
    kst_now = datetime.now(ZoneInfo("Asia/Seoul"))
    current = kst_now.time()
    weekday = kst_now.weekday()
    if current >= datetime.strptime("22:30", "%H:%M").time():
        return weekday <= 4
    if current <= datetime.strptime("06:00", "%H:%M").time():
        return 1 <= weekday <= 5
    return False


def _exit_submit_retry_due(order: dict[str, Any]) -> bool:
    if order.get("exit_submit_blocked"):
        return False
    value = order.get("exit_submit_failed_at")
    if not value:
        return True
    try:
        failed_at = datetime.fromisoformat(str(value))
    except ValueError:
        return True
    retry_seconds = EXIT_SUBMIT_RETRY_SECONDS
    last_error = str(order.get("last_error") or "")
    app_reservation = order.get("app_exit_reservation") if isinstance(order.get("app_exit_reservation"), dict) else {}
    if order.get("market") == "us" and order.get("env_dv") not in ("prod", "real"):
        if app_reservation.get("status") == "broker_submitted":
            retry_seconds = US_PAPER_BROKER_RESERVATION_RETRY_SECONDS
            if _is_us_regular_session_now():
                retry_seconds = US_PAPER_BROKER_RESERVATION_REGULAR_RETRY_SECONDS
        elif (
            app_reservation.get("status") == "waiting_retry"
            or "모의투자에서는 해당업무가 제공되지 않습니다" in last_error
            or "모의투자 예약주문시간" in last_error
            or "초당 거래건수" in last_error
            or "EGW00201" in last_error
        ):
            retry_seconds = US_PAPER_LOCAL_RESERVATION_RETRY_SECONDS
    return datetime.now() - failed_at >= timedelta(seconds=retry_seconds)


def _should_create_local_us_paper_reservation(env_dv: str, market: str, error: Optional[str]) -> bool:
    if market != "us" or env_dv in ("prod", "real") or not error:
        return False
    return any(marker in error for marker in US_PAPER_LOCAL_RESERVATION_MARKERS)


def _mark_local_us_paper_reservation(
    order: dict[str, Any],
    *,
    exit_reason: str,
    order_type: str,
    price: Optional[float],
    current_price: float,
    error: str,
) -> None:
    existing = order.get("app_exit_reservation") if isinstance(order.get("app_exit_reservation"), dict) else {}
    reserved_at = existing.get("reserved_at") or _now()
    reservation = {
        "status": "waiting_retry",
        "market": "us",
        "env_dv": order.get("env_dv") or "vps",
        "stock_code": order.get("stock_code"),
        "exchange": order.get("exchange"),
        "quantity": int(order.get("quantity") or 0),
        "exit_reason": exit_reason,
        "order_type": order_type,
        "limit_price": price if order_type == "limit" else None,
        "current_price": current_price,
        "reserved_at": reserved_at,
        "last_attempt_at": _now(),
        "last_error": error,
        "note": "KIS paper US sell/reservation was unavailable; Strategy Builder will retry from the app monitor.",
    }
    order["status"] = "active"
    order["app_exit_reservation"] = reservation
    order["app_exit_reservation_status"] = "waiting_retry"
    order["app_exit_reserved_at"] = reserved_at
    order["app_exit_reason"] = exit_reason
    order["exit_submit_failed_at"] = _now()
    order.pop("last_error", None)
    order.pop("exit_submit_blocked", None)


def _mark_broker_us_paper_reservation(
    order: dict[str, Any],
    *,
    exit_reason: str,
    order_type: str,
    price: Optional[float],
    current_price: float,
    order_no: Optional[str],
) -> None:
    existing = order.get("app_exit_reservation") if isinstance(order.get("app_exit_reservation"), dict) else {}
    reserved_at = existing.get("reserved_at") or _now()
    reservation = {
        "status": "broker_submitted",
        "market": "us",
        "env_dv": order.get("env_dv") or "vps",
        "stock_code": order.get("stock_code"),
        "exchange": order.get("exchange"),
        "quantity": int(order.get("quantity") or 0),
        "exit_reason": exit_reason,
        "order_type": order_type,
        "limit_price": price if order_type == "limit" else None,
        "current_price": current_price,
        "reserved_at": reserved_at,
        "last_attempt_at": _now(),
        "reservation_order_no": str(order_no or ""),
        "note": "KIS paper US reservation was accepted; Strategy Builder will keep monitoring until the holding disappears.",
    }
    order["status"] = "active"
    order["app_exit_reservation"] = reservation
    order["app_exit_reservation_status"] = "broker_submitted"
    order["app_exit_reserved_at"] = reserved_at
    order["app_exit_reason"] = exit_reason
    order["exit_order_no"] = str(order_no or "")
    order["exit_org_no"] = "reservation"
    order["exit_reason"] = exit_reason
    order["exit_order_type"] = order_type
    order["exit_submit_failed_at"] = _now()
    order.pop("closed_at", None)
    order.pop("last_error", None)
    order.pop("exit_submit_blocked", None)


def _retry_local_exit_reservation(order: dict[str, Any], env_dv: str, current_price: float) -> dict[str, Any] | None:
    reservation = order.get("app_exit_reservation") if isinstance(order.get("app_exit_reservation"), dict) else {}
    if reservation.get("status") != US_PAPER_LOCAL_RESERVATION_RETRY_STATUS:
        return None
    exit_reason = str(reservation.get("exit_reason") or order.get("app_exit_reason") or "stop_loss")
    order_type = str(reservation.get("order_type") or order.get("stop_loss_order_type") or "limit")
    if order_type not in {"market", "limit"}:
        order_type = "limit"
    price = reservation.get("limit_price")
    if price in (None, ""):
        price = current_price
    return _submit_triggered_exit(
        order,
        env_dv,
        reason=(
            f"미국 모의 로컬 예약매도 재시도: {exit_reason}, "
            f"현재가 {current_price}"
        ),
        exit_reason=exit_reason,
        order_type=order_type,
        price=float(price) if order_type == "limit" else None,
        current_price=current_price,
    )


def _order_timestamp(order: dict[str, Any], *keys: str) -> Optional[datetime]:
    for key in keys:
        value = order.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            continue
    return None


def _current_order_price(order: dict[str, Any], env_dv: str, holding: dict[str, Any]) -> float:
    market = str(order.get("market") or "domestic")
    if market == "us":
        price_info = overseas_data_fetcher.get_current_price(
            str(order["stock_code"]),
            env_dv,
            order.get("exchange"),
        )
    else:
        price_info = data_fetcher.get_current_price(str(order["stock_code"]), env_dv)
    current_price = float(price_info.get("price") or holding.get("current_price") or order.get("last_price") or 0)
    if current_price > 0:
        order["last_price"] = current_price
        order["last_checked_at"] = _now()
    return current_price


def _us_stop_loss_order_price(configured_price: Optional[float], current_price: float) -> float:
    if current_price <= 0:
        return float(configured_price or 0)
    marketable_price = current_price * (1 - US_STOP_LOSS_MARKETABLE_LIMIT_BUFFER_PCT / 100)
    if configured_price and configured_price > 0:
        marketable_price = min(float(configured_price), marketable_price)
    return _round_price("us", marketable_price, "down")


def _reconcile_exit_submitted_sync(order: dict[str, Any], env_dv: str) -> dict[str, Any]:
    if order.get("status") != "exit_submitted":
        return order

    market = str(order.get("market") or "domestic")
    holdings = _get_holding_map(env_dv, market)
    if not holdings:
        order["last_checked_at"] = _now()
        order["last_error"] = "holdings unavailable or empty; submitted exit preserved"
        return order

    holding = holdings.get(str(order.get("stock_code")))
    if not holding or int(holding.get("quantity") or 0) <= 0:
        order["status"] = "closed"
        order["closed_at"] = _now()
        order["last_checked_at"] = _now()
        order.pop("last_error", None)
        order.setdefault("events", []).append({"type": "position_closed_after_exit_submit", "at": _now()})
        return order

    order["quantity"] = min(int(order.get("quantity") or 0), int(holding.get("quantity") or 0))
    order["last_checked_at"] = _now()
    if market == "us" and env_dv not in ("prod", "real") and order.get("exit_org_no") == "reservation":
        previous_exit_attempt_at = (
            order.get("exit_submitted_at")
            or order.get("closed_at")
            or order.get("exit_submit_failed_at")
        )
        _mark_broker_us_paper_reservation(
            order,
            exit_reason=str(order.get("exit_reason") or "stop_loss"),
            order_type=str(order.get("exit_order_type") or order.get("stop_loss_order_type") or "limit"),
            price=(
                float(order.get("stop_loss_limit_price") or order.get("stop_loss_price") or 0)
                if order.get("exit_order_type", order.get("stop_loss_order_type")) == "limit"
                else None
            ),
            current_price=float(holding.get("current_price") or order.get("last_price") or 0),
            order_no=str(order.get("exit_order_no") or ""),
        )
        order["exit_submit_failed_at"] = previous_exit_attempt_at or _now()
        order.setdefault("events", []).append({
            "type": "exit_reopened_position_still_held",
            "at": _now(),
            "current_price": float(holding.get("current_price") or order.get("last_price") or 0),
            "reason": "reservation exit was submitted but holding is still present",
        })
        return order

    pending = _get_pending_order_map(env_dv, market, order.get("exchange"))
    exit_order_no = str(order.get("exit_order_no") or "")
    pending_exit = pending.get(exit_order_no)
    exit_reason = str(order.get("exit_reason") or "stop_loss")
    current_price = _current_order_price(order, env_dv, holding)

    if pending_exit and exit_reason == "stop_loss":
        submitted_at = _order_timestamp(order, "exit_submitted_at", "closed_at")
        if submitted_at and datetime.now() - submitted_at < timedelta(seconds=EXIT_PENDING_REPRICE_SECONDS):
            order["last_error"] = "exit order pending; waiting before repricing"
            return order

        if market == "us":
            cancel_result = overseas_data_fetcher.cancel_order(
                order_no=exit_order_no,
                symbol=str(order["stock_code"]),
                qty=int(order["quantity"]),
                env_dv=env_dv,
                exchange=order.get("exchange"),
            )
        else:
            cancel_result = cancel_order(
                order_no=exit_order_no,
                org_no=str(order.get("exit_org_no") or ""),
                stock_code=str(order["stock_code"]),
                qty=int(order["quantity"]),
                env_dv=env_dv,
            )
        order.setdefault("events", []).append({
            "type": "exit_pending_cancel_for_reprice",
            "at": _now(),
            "order_no": exit_order_no,
            "current_price": current_price,
            "result": cancel_result,
        })
        if not cancel_result.get("success"):
            order["last_error"] = f"exit pending cancel failed: {cancel_result.get('message')}"
            return order

    if exit_reason == "stop_loss" and current_price > 0:
        order["status"] = "active"
        order["exit_submit_failed_at"] = None
        order.pop("closed_at", None)
        order.setdefault("events", []).append({
            "type": "exit_retry_position_still_held",
            "at": _now(),
            "current_price": current_price,
            "reason": "exit submitted but holding is still present",
        })
        return _submit_triggered_exit(
            order,
            env_dv,
            reason=(
                f"보호주문 손절 재시도: 현재가 {current_price}, "
                "기존 청산 주문 후에도 보유 잔량 확인"
            ),
            exit_reason="stop_loss",
            order_type=str(order.get("stop_loss_order_type") or order.get("exit_order_type") or "market"),
            price=float(order.get("stop_loss_limit_price") or order.get("stop_loss_price") or current_price),
            current_price=current_price,
        )

    order["last_error"] = "exit submitted but holding is still present"
    return order


def _is_non_retryable_submit_error(error: Optional[str]) -> bool:
    if not error:
        return False
    if "reservation sell failed:" in error:
        reservation_error = error.split("reservation sell failed:", 1)[1]
        if "EGW00201" in reservation_error or "초당 거래건수" in reservation_error:
            return False
        error = reservation_error
    if "미국주식 주간거래" in error and "제공하지 않습니다" in error:
        return False
    if "모의투자에서는 해당업무가 제공되지 않습니다" in error and "reservation sell failed" not in error:
        return False
    return any(
        marker in error
        for marker in (
            "모의투자에서는 해당업무가 제공되지 않습니다",
            "해당업무가 제공되지 않습니다",
            "제공하지 않습니다",
            "not supported",
            "not provided",
        )
    )


def _strip_submit_blocked_prefix(error: str) -> str:
    marker = " sell submit blocked: "
    while marker in error:
        error = error.split(marker, 1)[1]
    return error


def _non_retryable_submit_error(order: dict[str, Any]) -> Optional[str]:
    last_error = _strip_submit_blocked_prefix(str(order.get("last_error") or ""))
    if _is_non_retryable_submit_error(last_error):
        return last_error

    for event in reversed(order.get("events") or []):
        if not str(event.get("type") or "").endswith("_submit_failed"):
            continue
        error = _strip_submit_blocked_prefix(str(event.get("error") or ""))
        if _is_non_retryable_submit_error(error):
            return error
    return None


async def _sync_realtime_subscriptions(state: dict[str, Any] | None = None) -> None:
    try:
        from backend.services.realtime_price_stream import get_realtime_price_stream

        state = state or await _load_state()
        orders = [
            {
                "market": order.get("market", "domestic"),
                "stock_code": order.get("stock_code"),
                "exchange": order.get("exchange"),
            }
            for order in state.get("orders", [])
            if order.get("status") == "active"
        ]
        await get_realtime_price_stream().set_protective_subscriptions(orders)
    except Exception:
        logger.exception("realtime subscription sync failed")


def _get_holding_map(env_dv: str, market: str = "domestic") -> dict[str, dict[str, Any]]:
    df = overseas_data_fetcher.get_holdings(env_dv) if market == "us" else get_holdings(env_dv)
    if df.empty:
        return {}
    return {
        str(row.get("stock_code")): row.to_dict()
        for _, row in df.iterrows()
    }


def _get_pending_order_map(env_dv: str, market: str = "domestic", exchange: str | None = None) -> dict[str, dict[str, Any]]:
    if market == "us":
        df, ok = overseas_data_fetcher.get_pending_orders(env_dv, exchange or "NASD")
    else:
        df, ok = get_pending_orders(env_dv)
    if not ok or df.empty:
        return {}
    return {
        str(row.get("order_no")): row.to_dict()
        for _, row in df.iterrows()
    }


def _submit_exit_order(
    env_dv: str,
    stock_code: str,
    stock_name: str,
    quantity: int,
    reason: str,
    order_type: str,
    price: Optional[float] = None,
    market: str = "domestic",
    exchange: str | None = None,
) -> tuple[Optional[str], Optional[str], bool, Optional[str]]:
    if market == "us":
        order_price = float(price or 0)
        if order_price <= 0:
            price_info = overseas_data_fetcher.get_current_price(stock_code, env_dv, exchange)
            order_price = float(price_info.get("price") or 0)
        if order_price <= 0:
            return None, None, False, "current price unavailable"
        result = overseas_data_fetcher.submit_order(
            symbol=stock_code,
            action="SELL",
            quantity=quantity,
            price=round(order_price, 2),
            env_dv=env_dv,
            exchange=exchange,
        )
        if not result.success or result.dataframe.empty:
            regular_error = result.display_error()
            if env_dv not in ("prod", "real"):
                reservation = reserved_orders.submit_us_reservation(
                    symbol=stock_code,
                    action="SELL",
                    quantity=quantity,
                    price=round(order_price, 2),
                    order_type="limit",
                    env_dv=env_dv,
                    exchange=exchange,
                )
                if reservation.success and not reservation.dataframe.empty:
                    data = reserved_orders.first_normalized_record(reservation)
                    return (
                        str(data.get("reservation_order_no") or ""),
                        "reservation",
                        True,
                        None,
                    )
                return (
                    None,
                    None,
                    False,
                    f"regular sell failed: {regular_error}; "
                    f"reservation sell failed: {reservation.display_error()}",
                )
            return None, None, False, regular_error
        row = result.dataframe.iloc[0]
        return (
            str(row.get("ODNO", row.get("odno", ""))),
            str(row.get("KRX_FWDG_ORD_ORGNO", row.get("ord_gno_brno", ""))),
            True,
            None,
        )

    signal = Signal(
        stock_code=stock_code,
        stock_name=stock_name,
        action=Action.SELL,
        strength=1.0 if order_type == "market" else 0.7,
        reason=reason,
        target_price=price if order_type == "limit" else None,
        quantity=quantity,
    )
    result = OrderExecutor(env_dv=env_dv).execute_signal(signal)
    if result.empty:
        return None, None, False, None

    row = result.iloc[0]
    return (
        str(row.get("ODNO", "")),
        str(row.get("KRX_FWDG_ORD_ORGNO", "")),
        True,
        None,
    )


def _is_order_pending(order_no: Optional[str], pending: dict[str, dict[str, Any]]) -> bool:
    return bool(order_no and order_no in pending)


def _cancel_take_profit(order: dict[str, Any], env_dv: str) -> None:
    order_no = order.get("take_profit_order_no")
    if not order_no:
        return
    if order.get("market") == "us":
        result = overseas_data_fetcher.cancel_order(
            order_no=str(order_no),
            symbol=str(order["stock_code"]),
            qty=int(order["quantity"]),
            env_dv=env_dv,
            exchange=order.get("exchange"),
        )
    else:
        result = cancel_order(
            order_no=str(order_no),
            org_no=str(order.get("take_profit_org_no") or ""),
            stock_code=str(order["stock_code"]),
            qty=int(order["quantity"]),
            env_dv=env_dv,
        )
    order.setdefault("events", []).append({
        "type": "take_profit_cancel",
        "at": _now(),
        "result": result,
    })


def _ensure_take_profit(order: dict[str, Any], env_dv: str, pending: dict[str, dict[str, Any]]) -> None:
    if not order.get("take_profit_enabled"):
        return
    if order.get("take_profit_submit_mode") == "on_trigger":
        return
    if _is_order_pending(order.get("take_profit_order_no"), pending):
        return

    order_no, org_no, ok, error = _submit_exit_order(
        env_dv=env_dv,
        stock_code=str(order["stock_code"]),
        stock_name=str(order["stock_name"]),
        quantity=int(order["quantity"]),
        order_type="limit",
        price=float(order["take_profit_price"]),
        market=str(order.get("market") or "domestic"),
        exchange=order.get("exchange"),
        reason=(
            "보호주문 익절 지정가 "
            f"{order['take_profit_pct']}% @ {order['take_profit_price']}"
        ),
    )
    order["take_profit_last_attempt_at"] = _now()
    if ok:
        order["take_profit_order_no"] = order_no
        order["take_profit_org_no"] = org_no
        order["take_profit_status"] = "pending"
        order.setdefault("events", []).append({
            "type": "take_profit_submitted",
            "at": _now(),
            "order_no": order_no,
        })
    else:
        order["take_profit_status"] = "submit_failed"
        if error:
            order["last_error"] = f"take_profit sell submit failed: {error}"


def _submit_triggered_exit(
    order: dict[str, Any],
    env_dv: str,
    *,
    reason: str,
    exit_reason: str,
    order_type: str,
    price: Optional[float],
    current_price: float,
) -> dict[str, Any]:
    order_type = "market" if order_type not in {"market", "limit"} else order_type
    market = str(order.get("market") or "domestic")
    last_error = str(order.get("last_error") or "")
    if (
        market == "us"
        and env_dv not in ("prod", "real")
        and last_error.startswith(f"{exit_reason} sell submit blocked:")
        and (
            "해당업무" in last_error
            or "미국주식 주간거래" in last_error
            or "EGW00201" in last_error
            or "초당 거래건수" in last_error
        )
    ):
        order.pop("exit_submit_blocked", None)
        order.pop("last_error", None)

    non_retryable_error = _non_retryable_submit_error(order)
    if non_retryable_error:
        order["exit_submit_blocked"] = True
        order["last_error"] = f"{exit_reason} sell submit blocked: {non_retryable_error}"
        return order
    order.pop("exit_submit_blocked", None)

    if not _exit_submit_retry_due(order):
        return order

    if market == "us" and order_type == "market":
        order_type = "limit"
        price = current_price
    if market == "us" and exit_reason == "stop_loss" and order_type == "limit":
        price = _us_stop_loss_order_price(float(price or 0), current_price)

    order_no, org_no, ok, error = _submit_exit_order(
        env_dv=env_dv,
        stock_code=str(order["stock_code"]),
        stock_name=str(order["stock_name"]),
        quantity=int(order["quantity"]),
        order_type=order_type,
        price=price if order_type == "limit" else None,
        market=market,
        exchange=order.get("exchange"),
        reason=reason,
    )
    if ok:
        if market == "us" and env_dv not in ("prod", "real") and org_no == "reservation":
            _mark_broker_us_paper_reservation(
                order,
                exit_reason=exit_reason,
                order_type=order_type,
                price=price if order_type == "limit" else None,
                current_price=current_price,
                order_no=order_no,
            )
            order.setdefault("events", []).append({
                "type": f"{exit_reason}_reservation_submitted",
                "at": _now(),
                "order_no": order_no,
                "order_type": order_type,
                "order_price": price if order_type == "limit" else None,
                "current_price": current_price,
            })
            return order

        order["status"] = "exit_submitted"
        order["exit_order_no"] = order_no
        order["exit_org_no"] = org_no
        order["exit_reason"] = exit_reason
        order["exit_order_type"] = order_type
        order["exit_submitted_at"] = _now()
        order.pop("last_error", None)
        order.pop("exit_submit_failed_at", None)
        order.pop("exit_submit_blocked", None)
        order.pop("closed_at", None)
        order.pop("app_exit_reservation", None)
        order.pop("app_exit_reservation_status", None)
        order.pop("app_exit_reserved_at", None)
        order.pop("app_exit_reason", None)
        order.setdefault("events", []).append({
            "type": f"{exit_reason}_submitted",
            "at": _now(),
            "order_no": order_no,
            "order_type": order_type,
            "order_price": price if order_type == "limit" else None,
            "current_price": current_price,
        })
    else:
        if _should_create_local_us_paper_reservation(env_dv, market, error):
            detail = str(error or "")
            _mark_local_us_paper_reservation(
                order,
                exit_reason=exit_reason,
                order_type=order_type,
                price=price if order_type == "limit" else None,
                current_price=current_price,
                error=detail,
            )
            order.setdefault("events", []).append({
                "type": f"{exit_reason}_app_reserved",
                "at": _now(),
                "order_type": order_type,
                "order_price": price if order_type == "limit" else None,
                "current_price": current_price,
                "error": detail,
            })
            return order

        detail = f": {error}" if error else ""
        order["last_error"] = f"{exit_reason} sell submit failed{detail}"
        order["exit_submit_failed_at"] = _now()
        if _is_non_retryable_submit_error(error):
            order["exit_submit_blocked"] = True
        order.setdefault("events", []).append({
            "type": f"{exit_reason}_submit_failed",
            "at": _now(),
            "order_type": order_type,
            "order_price": price if order_type == "limit" else None,
            "current_price": current_price,
            "error": error,
        })
    return order


def _check_order_sync(order: dict[str, Any], env_dv: str) -> dict[str, Any]:
    if order.get("status") != "active":
        return order

    market = str(order.get("market") or "domestic")
    holdings = _get_holding_map(env_dv, market)
    pending = _get_pending_order_map(env_dv, market, order.get("exchange"))

    if not holdings:
        order["last_checked_at"] = _now()
        order["last_error"] = "holdings unavailable or empty; active protection preserved"
        return order

    holding = holdings.get(str(order["stock_code"]))

    if not holding or int(holding.get("quantity") or 0) <= 0:
        missing_count = int(order.get("position_missing_count") or 0) + 1
        order["position_missing_count"] = missing_count
        order["last_checked_at"] = _now()
        order["last_error"] = (
            "position not found in holdings "
            f"({missing_count}/{POSITION_MISSING_CONFIRMATIONS})"
        )
        if missing_count < POSITION_MISSING_CONFIRMATIONS:
            return order

        order["status"] = "closed"
        order["closed_at"] = _now()
        order.setdefault("events", []).append({"type": "position_closed", "at": _now()})
        return order

    order.pop("position_missing_count", None)
    if str(order.get("last_error") or "").startswith(("holdings unavailable", "position not found")):
        order.pop("last_error", None)

    order["quantity"] = min(int(order["quantity"]), int(holding.get("quantity") or 0))
    _ensure_take_profit(order, env_dv, pending)

    if market == "us":
        price_info = overseas_data_fetcher.get_current_price(
            str(order["stock_code"]),
            env_dv,
            order.get("exchange"),
        )
    else:
        price_info = data_fetcher.get_current_price(str(order["stock_code"]), env_dv)
    current_price = float(price_info.get("price") or holding.get("current_price") or 0)
    if current_price <= 0:
        order["last_error"] = "current price unavailable"
        return order

    order["last_price"] = current_price
    order["last_checked_at"] = _now()
    retried = _retry_local_exit_reservation(order, env_dv, current_price)
    if retried is not None:
        return retried

    take_profit_trigger = float(order.get("take_profit_trigger_price") or order.get("take_profit_price") or 0)
    if (
        order.get("take_profit_enabled")
        and order.get("take_profit_submit_mode") == "on_trigger"
        and take_profit_trigger > 0
        and current_price >= take_profit_trigger
    ):
        _cancel_take_profit(order, env_dv)
        return _submit_triggered_exit(
            order,
            env_dv,
            reason=(
                f"보호주문 익절 {order.get('take_profit_order_type', 'limit')} "
                f"{order.get('take_profit_pct')}%: 현재가 {current_price} >= {take_profit_trigger}"
            ),
            exit_reason="take_profit",
            order_type=str(order.get("take_profit_order_type") or "limit"),
            price=float(order.get("take_profit_limit_price") or order.get("take_profit_price") or take_profit_trigger),
            current_price=current_price,
        )

    stop_loss_price = float(order["stop_loss_price"])
    if order.get("stop_loss_enabled") and current_price <= stop_loss_price:
        _cancel_take_profit(order, env_dv)
        return _submit_triggered_exit(
            order,
            env_dv,
            reason=(
                f"보호주문 손절 {order.get('stop_loss_order_type', 'market')} "
                f"{order['stop_loss_pct']}%: 현재가 {current_price} <= {stop_loss_price}"
            ),
            exit_reason="stop_loss",
            order_type=str(order.get("stop_loss_order_type") or "market"),
            price=float(order.get("stop_loss_limit_price") or stop_loss_price),
            current_price=current_price,
        )

    return order


def _check_realtime_trigger_sync(order: dict[str, Any], env_dv: str, current_price: float) -> dict[str, Any]:
    if order.get("status") != "active" or current_price <= 0:
        return order

    order["last_price"] = current_price
    order["last_checked_at"] = _now()
    order["last_realtime_at"] = _now()
    retried = _retry_local_exit_reservation(order, env_dv, current_price)
    if retried is not None:
        return retried

    take_profit_trigger = float(order.get("take_profit_trigger_price") or order.get("take_profit_price") or 0)
    if (
        order.get("take_profit_enabled")
        and order.get("take_profit_submit_mode") == "on_trigger"
        and take_profit_trigger > 0
        and current_price >= take_profit_trigger
    ):
        _cancel_take_profit(order, env_dv)
        return _submit_triggered_exit(
            order,
            env_dv,
            reason=(
                f"실시간 보호주문 익절 {order.get('take_profit_order_type', 'limit')} "
                f"{order.get('take_profit_pct')}%: 현재가 {current_price} >= {take_profit_trigger}"
            ),
            exit_reason="take_profit",
            order_type=str(order.get("take_profit_order_type") or "limit"),
            price=float(order.get("take_profit_limit_price") or order.get("take_profit_price") or take_profit_trigger),
            current_price=current_price,
        )

    stop_loss_price = float(order.get("stop_loss_price") or 0)
    if order.get("stop_loss_enabled") and stop_loss_price > 0 and current_price <= stop_loss_price:
        _cancel_take_profit(order, env_dv)
        return _submit_triggered_exit(
            order,
            env_dv,
            reason=(
                f"실시간 보호주문 손절 {order.get('stop_loss_order_type', 'market')} "
                f"{order.get('stop_loss_pct')}%: 현재가 {current_price} <= {stop_loss_price}"
            ),
            exit_reason="stop_loss",
            order_type=str(order.get("stop_loss_order_type") or "market"),
            price=float(order.get("stop_loss_limit_price") or stop_loss_price),
            current_price=current_price,
        )

    return order


async def register_after_buy(
    *,
    env_dv: str,
    stock_code: str,
    stock_name: str,
    quantity: int,
    entry_price: float,
    take_profit_pct: Optional[float],
    stop_loss_pct: Optional[float],
    source_order_no: Optional[str] = None,
    market: str = "domestic",
    exchange: Optional[str] = None,
    currency: str = "KRW",
) -> dict[str, Any]:
    """Create a protection group after a successful BUY order."""
    if quantity <= 0 or entry_price <= 0:
        raise ValueError("quantity and entry_price must be positive")

    take_profit_enabled = take_profit_pct is not None and take_profit_pct > 0
    stop_loss_enabled = stop_loss_pct is not None and stop_loss_pct > 0
    if not take_profit_enabled and not stop_loss_enabled:
        raise ValueError("take_profit_pct or stop_loss_pct is required")

    protection = {
        "id": uuid4().hex,
        "status": "active",
        "env_dv": env_dv,
        "market": market,
        "exchange": exchange,
        "currency": currency,
        "stock_code": stock_code,
        "stock_name": stock_name,
        "quantity": quantity,
        "entry_price": entry_price,
        "source_order_no": source_order_no,
        "source": "after_buy",
        "take_profit_enabled": take_profit_enabled,
        "take_profit_pct": take_profit_pct,
        "take_profit_price": _round_price(market, entry_price * (1 + (take_profit_pct or 0) / 100), "up"),
        "take_profit_trigger_price": _round_price(market, entry_price * (1 + (take_profit_pct or 0) / 100), "up"),
        "take_profit_limit_price": _round_price(market, entry_price * (1 + (take_profit_pct or 0) / 100), "up"),
        "take_profit_order_type": "limit",
        "take_profit_submit_mode": "resting_limit",
        "take_profit_status": "not_submitted",
        "take_profit_order_no": None,
        "take_profit_org_no": None,
        "stop_loss_enabled": stop_loss_enabled,
        "stop_loss_pct": stop_loss_pct,
        "stop_loss_price": _round_price(market, entry_price * (1 - (stop_loss_pct or 0) / 100), "down"),
        "stop_loss_limit_price": _round_price(market, entry_price * (1 - (stop_loss_pct or 0) / 100), "down"),
        "stop_loss_order_type": "market",
        "created_at": _now(),
        "last_checked_at": None,
        "events": [{"type": "created", "at": _now()}],
    }

    state = await _load_state()
    state["orders"] = [
        order for order in state.get("orders", [])
        if not (
            order.get("status") == "active"
            and order.get("stock_code") == stock_code
            and order.get("env_dv") == env_dv
        )
    ]
    state["orders"].append(protection)
    await _save_state(state)
    await _sync_realtime_subscriptions(state)

    updated = await asyncio.to_thread(_check_order_sync, protection, env_dv)
    state = await _load_state()
    for index, order in enumerate(state.get("orders", [])):
        if order.get("id") == updated["id"]:
            state["orders"][index] = updated
            break
    await _save_state(state)
    await _sync_realtime_subscriptions(state)
    return updated


async def upsert_existing_position_protection(
    *,
    env_dv: str,
    stock_code: str,
    stock_name: str,
    quantity: int,
    entry_price: float,
    enabled: bool,
    take_profit_enabled: bool,
    take_profit_trigger_price: Optional[float],
    take_profit_order_type: str = "limit",
    take_profit_limit_price: Optional[float] = None,
    stop_loss_enabled: bool = True,
    stop_loss_trigger_price: Optional[float] = None,
    stop_loss_order_type: str = "market",
    stop_loss_limit_price: Optional[float] = None,
    market: str = "domestic",
    exchange: Optional[str] = None,
    currency: str = "KRW",
) -> dict[str, Any]:
    """Create or replace a trigger-based protection group for an existing holding."""
    if quantity <= 0:
        raise ValueError("quantity must be positive")
    if entry_price <= 0:
        raise ValueError("entry_price must be positive")

    state = await _load_state()
    existing_orders = state.get("orders", [])

    if enabled and not take_profit_enabled and not stop_loss_enabled:
        raise ValueError("take_profit or stop_loss must be enabled")

    tp_trigger = float(take_profit_trigger_price or 0)
    sl_trigger = float(stop_loss_trigger_price or 0)
    if enabled and take_profit_enabled and tp_trigger <= 0:
        raise ValueError("take_profit_trigger_price must be positive")
    if enabled and stop_loss_enabled and sl_trigger <= 0:
        raise ValueError("stop_loss_trigger_price must be positive")

    take_profit_order_type = take_profit_order_type if take_profit_order_type in {"market", "limit"} else "limit"
    stop_loss_order_type = stop_loss_order_type if stop_loss_order_type in {"market", "limit"} else "market"
    if market == "us":
        take_profit_order_type = "limit"
        stop_loss_order_type = "limit"
    tp_pct = ((tp_trigger / entry_price) - 1) * 100 if take_profit_enabled else None
    sl_pct = (1 - (sl_trigger / entry_price)) * 100 if stop_loss_enabled else None
    tp_price = _round_price(market, tp_trigger, "up") if take_profit_enabled and tp_trigger > 0 else None
    tp_limit = (
        _round_price(market, take_profit_limit_price or tp_trigger, "down")
        if take_profit_enabled and (take_profit_limit_price or tp_trigger) else None
    )
    sl_price = _round_price(market, sl_trigger, "down") if stop_loss_enabled and sl_trigger > 0 else None
    sl_limit = (
        _round_price(market, stop_loss_limit_price or sl_trigger, "down")
        if stop_loss_enabled and (stop_loss_limit_price or sl_trigger) else None
    )
    status = "active" if enabled else "disabled"
    now = _now()

    protection = {
        "id": uuid4().hex,
        "status": status,
        "env_dv": env_dv,
        "market": market,
        "exchange": exchange,
        "currency": currency,
        "stock_code": stock_code,
        "stock_name": stock_name,
        "quantity": quantity,
        "entry_price": entry_price,
        "source": "review",
        "source_order_no": None,
        "take_profit_enabled": take_profit_enabled,
        "take_profit_pct": round(tp_pct, 2) if tp_pct is not None else None,
        "take_profit_price": tp_price,
        "take_profit_trigger_price": tp_price,
        "take_profit_limit_price": tp_limit,
        "take_profit_order_type": take_profit_order_type,
        "take_profit_submit_mode": "on_trigger",
        "take_profit_status": "waiting_trigger",
        "take_profit_order_no": None,
        "take_profit_org_no": None,
        "stop_loss_enabled": stop_loss_enabled,
        "stop_loss_pct": round(sl_pct, 2) if sl_pct is not None else None,
        "stop_loss_price": sl_price,
        "stop_loss_limit_price": sl_limit,
        "stop_loss_order_type": stop_loss_order_type,
        "created_at": now,
        "last_checked_at": None,
        "events": [{"type": "created_from_review" if enabled else "saved_disabled_from_review", "at": now}],
    }
    if not enabled:
        protection["closed_at"] = now

    state["orders"] = [
        order for order in existing_orders
        if not (
            order.get("status") in {"active", "disabled"}
            and order.get("stock_code") == stock_code
            and order.get("env_dv") == env_dv
            and order.get("market", "domestic") == market
            and (order.get("exchange") or None) == (exchange or None)
        )
    ]
    state["orders"].append(protection)
    await _save_state(state)
    await _sync_realtime_subscriptions(state)
    return protection


async def run_monitor_cycle() -> None:
    state = await _load_state()
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    price_source = settings.get("price_source") or "websocket"
    changed = False
    updated_orders = []

    for order in state.get("orders", []):
        if order.get("status") == "exit_submitted":
            try:
                before = json.dumps(order, sort_keys=True, default=str)
                updated = await asyncio.to_thread(
                    _reconcile_exit_submitted_sync,
                    order,
                    str(order.get("env_dv") or "vps"),
                )
                updated_orders.append(updated)
                changed = changed or json.dumps(updated, sort_keys=True, default=str) != before
            except Exception as exc:
                order["last_error"] = str(exc)
                order["last_checked_at"] = _now()
                updated_orders.append(order)
                changed = True
                logger.exception("protective exit reconciliation failed")
            continue

        if order.get("status") != "active":
            updated_orders.append(order)
            continue

        if (
            price_source == "websocket"
            and order.get("source") == "review"
            and order.get("take_profit_submit_mode") == "on_trigger"
            and _is_recent_realtime(order)
        ):
            updated_orders.append(order)
            continue

        try:
            before = json.dumps(order, sort_keys=True, default=str)
            updated = await asyncio.to_thread(_check_order_sync, order, str(order.get("env_dv") or "vps"))
            updated_orders.append(updated)
            changed = changed or json.dumps(updated, sort_keys=True, default=str) != before
        except Exception as exc:
            order["last_error"] = str(exc)
            order["last_checked_at"] = _now()
            updated_orders.append(order)
            changed = True
            logger.exception("protective order check failed")

    if changed:
        state = await _merge_updated_orders(updated_orders)
        await _sync_realtime_subscriptions(state)


async def handle_realtime_tick(tick: dict[str, Any]) -> None:
    state = await _load_state()
    changed = False
    updated_orders = []
    tick_market = str(tick.get("market") or "domestic")
    tick_code = str(tick.get("stock_code") or "")
    tick_exchange = tick.get("exchange")
    current_price = float(tick.get("price") or 0)

    for order in state.get("orders", []):
        if order.get("status") != "active":
            updated_orders.append(order)
            continue
        if str(order.get("market") or "domestic") != tick_market:
            updated_orders.append(order)
            continue
        if str(order.get("stock_code") or "") != tick_code:
            updated_orders.append(order)
            continue
        if tick_market == "us" and (order.get("exchange") or None) != (tick_exchange or None):
            updated_orders.append(order)
            continue

        try:
            before = json.dumps(order, sort_keys=True, default=str)
            updated = await asyncio.to_thread(
                _check_realtime_trigger_sync,
                order,
                str(order.get("env_dv") or "vps"),
                current_price,
            )
            updated_orders.append(updated)
            changed = changed or json.dumps(updated, sort_keys=True, default=str) != before
        except Exception as exc:
            order["last_error"] = str(exc)
            order["last_checked_at"] = _now()
            updated_orders.append(order)
            changed = True
            logger.exception("realtime protective order check failed")

    if changed:
        state = await _merge_updated_orders(updated_orders)
        await _sync_realtime_subscriptions(state)


async def _monitor_loop() -> None:
    while True:
        try:
            await run_monitor_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("protective order monitor cycle failed")
        await asyncio.sleep(await _get_monitor_interval_seconds())


async def start_monitor() -> None:
    global _monitor_task
    try:
        from backend.services.realtime_price_stream import get_realtime_price_stream

        stream = get_realtime_price_stream()
        stream.set_tick_handler(handle_realtime_tick)
        await stream.start()
        await _sync_realtime_subscriptions()
    except Exception:
        logger.exception("realtime price stream startup failed")

    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(_monitor_loop())
        logger.info("protective order monitor started")


async def stop_monitor() -> None:
    global _monitor_task
    if _monitor_task is None:
        return
    _monitor_task.cancel()
    try:
        await _monitor_task
    except asyncio.CancelledError:
        pass
    _monitor_task = None
    try:
        from backend.services.realtime_price_stream import get_realtime_price_stream

        await get_realtime_price_stream().stop()
    except Exception:
        logger.exception("realtime price stream shutdown failed")


async def list_protective_orders() -> dict[str, Any]:
    return await _load_state()


async def update_monitor_settings(*, monitor_interval_seconds: int) -> dict[str, Any]:
    state = await _load_state()
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    settings["monitor_interval_seconds"] = _normalize_monitor_interval(monitor_interval_seconds)
    state["settings"] = settings
    await _save_state(state)
    await _sync_realtime_subscriptions(state)
    return settings
