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
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd
import exchange_calendars as xcals
import requests

from core import data_fetcher, overseas_data_fetcher
from core.data_fetcher import cancel_order, get_holdings, get_pending_orders
from core.order_executor import OrderExecutor
from core.signal import Action, Signal

logger = logging.getLogger(__name__)
US_EXCHANGE_CALENDAR = xcals.get_calendar("XNYS")
KR_EXCHANGE_CALENDAR = xcals.get_calendar("XKRX")

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
EXIT_PENDING_REPRICE_SECONDS = 60
US_STOP_LOSS_MARKETABLE_LIMIT_BUFFER_PCT = 2.0
US_TAKE_PROFIT_MARKETABLE_LIMIT_BUFFER_PCT = 0.3
US_EXIT_REPRICE_STEP_PCT = 0.75
US_EXIT_MAX_OFFSET_PCT = 5.0
MIN_US_EXIT_LIMIT_BUFFER_PCT = 0.0
MAX_US_EXIT_LIMIT_BUFFER_PCT = 10.0
PROTECTIVE_KIS_CALL_INTERVAL_SECONDS = float(
    os.environ.get("KIS_PROTECTIVE_CALL_INTERVAL_SECONDS", "0.85")
)
PROTECTIVE_EXIT_ALERT_AFTER_SECONDS = int(
    os.environ.get("KIS_PROTECTIVE_EXIT_ALERT_AFTER_SECONDS", "180")
)
PROTECTIVE_ALERT_COOLDOWN_SECONDS = int(
    os.environ.get("KIS_PROTECTIVE_ALERT_COOLDOWN_SECONDS", "900")
)
US_PAPER_RATE_LIMIT_BASE_SECONDS = 1
US_PAPER_RATE_LIMIT_MAX_SECONDS = 60
US_PAPER_DIRECT_SELL_UNSUPPORTED_PATH = "us_paper_direct_sell"
US_PAPER_LOCAL_RESERVATION_MARKERS = (
    "90000000",
    "40490000",
    "EGW00201",
    "모의투자에서는 해당업무가 제공되지 않습니다",
    "모의투자 예약주문시간",
    "초당 거래건수",
)
US_PAPER_LOCAL_RESERVATION_RETRY_STATUS = "waiting_retry"

_monitor_task: Optional[asyncio.Task] = None
_state_lock = asyncio.Lock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _is_us_paper_order(order: dict[str, Any]) -> bool:
    return str(order.get("market") or "domestic") == "us" and order.get("env_dv") not in ("prod", "real")


def _normalize_unsupported_paths(value: Any) -> list[str]:
    if isinstance(value, list):
        return sorted({str(item) for item in value if item})
    if isinstance(value, str) and value:
        return [value]
    return []


def _normalize_runtime_order(order: dict[str, Any]) -> dict[str, Any]:
    order["retry_count"] = max(0, int(order.get("retry_count") or 0))
    order["exit_reprice_count"] = max(0, int(order.get("exit_reprice_count") or 0))
    order["last_error_code"] = order.get("last_error_code") or None
    order["next_retry_at"] = order.get("next_retry_at") or None
    order["unsupported_paths"] = _normalize_unsupported_paths(order.get("unsupported_paths"))
    reservation = order.get("app_exit_reservation")
    if isinstance(reservation, dict):
        if _is_us_paper_order(order) and reservation.get("status") == "broker_submitted":
            reservation["status"] = "waiting_retry"
            reservation["last_error"] = (
                "Legacy paper broker reservation is no longer treated as an exit; "
                "the app will retry a normal limit sell during US regular hours."
            )
            reservation.pop("reservation_order_no", None)
            order["status"] = "active"
            order["app_exit_reservation_status"] = "waiting_retry"
            order["exit_org_no"] = None
            order["next_retry_at"] = _next_us_regular_session_retry_at().isoformat(timespec="seconds")
            reservation["next_retry_at"] = order["next_retry_at"]
        reservation.setdefault("retry_count", order["retry_count"])
        reservation.setdefault("last_error_code", order["last_error_code"])
        reservation.setdefault("next_retry_at", order["next_retry_at"])
        reservation.setdefault("unsupported_paths", list(order["unsupported_paths"]))
    return order


def _error_has(error: Optional[str], *markers: str) -> bool:
    text = str(error or "")
    return any(marker in text for marker in markers)


def _us_paper_error_code(error: Optional[str]) -> Optional[str]:
    text = str(error or "")
    if "EGW00201" in text or "초당 거래건수" in text:
        return "EGW00201"
    if "40490000" in text or "모의투자 예약주문시간" in text:
        return "40490000"
    if "90000000" in text or "모의투자에서는 해당업무가 제공되지 않습니다" in text:
        return "90000000"
    return None


def _is_us_paper_direct_sell_unsupported(error: Optional[str]) -> bool:
    return _error_has(error, "90000000", "모의투자에서는 해당업무가 제공되지 않습니다")


def _is_us_paper_reservation_time_error(error: Optional[str]) -> bool:
    return _error_has(error, "40490000", "모의투자 예약주문시간")


def _is_us_paper_rate_limit_error(error: Optional[str]) -> bool:
    return _error_has(error, "EGW00201", "초당 거래건수")


def _next_weekday_at(now: datetime, hour: int, minute: int) -> datetime:
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    while target.weekday() >= 5:
        target += timedelta(days=1)
    return target


def _us_reservation_cutoff_time(now: datetime):
    try:
        kst_now = now.replace(tzinfo=ZoneInfo("Asia/Seoul"))
        ny_now = kst_now.astimezone(ZoneInfo("America/New_York"))
        is_dst = bool(ny_now.dst())
    except Exception:
        is_dst = True
    return datetime.strptime("22:20" if is_dst else "23:20", "%H:%M").time()


def _next_us_paper_reservation_retry_at(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now()
    start = datetime.strptime("10:00", "%H:%M").time()
    settle_start = datetime.strptime("16:30", "%H:%M").time()
    settle_end = datetime.strptime("16:45", "%H:%M").time()
    cutoff = _us_reservation_cutoff_time(now)
    current = now.time()

    if now.weekday() >= 5 or current >= cutoff:
        return _next_weekday_at(now, 10, 0)
    if current < start:
        return now.replace(hour=10, minute=0, second=0, microsecond=0)
    if settle_start <= current < settle_end:
        return now.replace(hour=16, minute=45, second=0, microsecond=0)
    return now + timedelta(minutes=30)


def _rate_limit_retry_seconds(retry_count: int) -> int:
    retry_count = max(1, int(retry_count or 1))
    return min(US_PAPER_RATE_LIMIT_MAX_SECONDS, US_PAPER_RATE_LIMIT_BASE_SECONDS * (2 ** (retry_count - 1)))


def _set_next_retry_at(order: dict[str, Any], next_retry_at: Optional[datetime]) -> None:
    order["next_retry_at"] = next_retry_at.isoformat(timespec="seconds") if next_retry_at else None
    reservation = order.get("app_exit_reservation")
    if isinstance(reservation, dict):
        reservation["next_retry_at"] = order["next_retry_at"]


def _set_unsupported_path(order: dict[str, Any], path: str) -> None:
    paths = set(_normalize_unsupported_paths(order.get("unsupported_paths")))
    paths.add(path)
    order["unsupported_paths"] = sorted(paths)
    reservation = order.get("app_exit_reservation")
    if isinstance(reservation, dict):
        reservation["unsupported_paths"] = list(order["unsupported_paths"])


def _clear_submit_retry_state(order: dict[str, Any]) -> None:
    order["retry_count"] = 0
    order["last_error_code"] = None
    _set_next_retry_at(order, None)
    reservation = order.get("app_exit_reservation")
    if isinstance(reservation, dict):
        reservation["retry_count"] = 0
        reservation["last_error_code"] = None


def _default_settings() -> dict[str, Any]:
    return {
        "monitor_interval_seconds": DEFAULT_MONITOR_INTERVAL_SECONDS,
        "price_source": "websocket",
        "us_stop_loss_limit_offset_pct": US_STOP_LOSS_MARKETABLE_LIMIT_BUFFER_PCT,
        "us_take_profit_limit_offset_pct": US_TAKE_PROFIT_MARKETABLE_LIMIT_BUFFER_PCT,
        "exit_reprice_interval_seconds": EXIT_PENDING_REPRICE_SECONDS,
        "us_exit_reprice_step_pct": US_EXIT_REPRICE_STEP_PCT,
        "us_exit_max_offset_pct": US_EXIT_MAX_OFFSET_PCT,
    }


def _apply_us_paper_submit_error_policy(order: dict[str, Any], error: Optional[str]) -> None:
    if not _is_us_paper_order(order):
        return
    _normalize_runtime_order(order)
    if _is_us_paper_direct_sell_unsupported(error):
        _set_unsupported_path(order, US_PAPER_DIRECT_SELL_UNSUPPORTED_PATH)

    code = _us_paper_error_code(error)
    order["last_error_code"] = code
    order["retry_count"] = int(order.get("retry_count") or 0) + 1

    if _is_us_paper_rate_limit_error(error):
        delay = _rate_limit_retry_seconds(order["retry_count"])
        next_retry = datetime.now() + timedelta(seconds=delay)
    elif _is_us_paper_reservation_time_error(error):
        next_retry = _next_us_paper_reservation_retry_at()
    else:
        next_retry = datetime.now() + timedelta(seconds=US_PAPER_LOCAL_RESERVATION_RETRY_SECONDS)
    _set_next_retry_at(order, next_retry)

    reservation = order.get("app_exit_reservation")
    if isinstance(reservation, dict):
        reservation["retry_count"] = order["retry_count"]
        reservation["last_error_code"] = order["last_error_code"]
        reservation["unsupported_paths"] = list(order["unsupported_paths"])


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
            "settings": _default_settings(),
            "health": {},
        }
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("orders"), list):
            settings = data.get("settings") if isinstance(data.get("settings"), dict) else {}
            interval = settings.get("monitor_interval_seconds", DEFAULT_MONITOR_INTERVAL_SECONDS)
            data["orders"] = [
                _normalize_runtime_order(order)
                for order in data.get("orders", [])
                if isinstance(order, dict)
            ]
            data["settings"] = {
                **settings,
                "monitor_interval_seconds": _normalize_monitor_interval(interval),
                "price_source": settings.get("price_source") or "websocket",
                "us_stop_loss_limit_offset_pct": _normalize_us_exit_offset(
                    settings.get("us_stop_loss_limit_offset_pct"),
                    US_STOP_LOSS_MARKETABLE_LIMIT_BUFFER_PCT,
                ),
                "us_take_profit_limit_offset_pct": _normalize_us_exit_offset(
                    settings.get("us_take_profit_limit_offset_pct"),
                    US_TAKE_PROFIT_MARKETABLE_LIMIT_BUFFER_PCT,
                ),
                "exit_reprice_interval_seconds": _normalize_monitor_interval(
                    settings.get("exit_reprice_interval_seconds", EXIT_PENDING_REPRICE_SECONDS)
                ),
                "us_exit_reprice_step_pct": _normalize_us_exit_offset(
                    settings.get("us_exit_reprice_step_pct"),
                    US_EXIT_REPRICE_STEP_PCT,
                ),
                "us_exit_max_offset_pct": _normalize_us_exit_offset(
                    settings.get("us_exit_max_offset_pct"),
                    US_EXIT_MAX_OFFSET_PCT,
                ),
            }
            data["health"] = data.get("health") if isinstance(data.get("health"), dict) else {}
            return data
    except Exception as exc:
        logger.warning("protective order state load failed: %s", exc)
    return {
        "orders": [],
        "settings": _default_settings(),
        "health": {},
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


async def _merge_updated_orders(
    updated_orders: list[dict[str, Any]],
    state_patch: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
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
        if state_patch:
            latest.update(state_patch)
        await asyncio.to_thread(_save_state_sync, latest)
        return latest


def _normalize_monitor_interval(value: Any) -> int:
    try:
        interval = int(value)
    except (TypeError, ValueError):
        interval = DEFAULT_MONITOR_INTERVAL_SECONDS
    return max(MIN_MONITOR_INTERVAL_SECONDS, min(MAX_MONITOR_INTERVAL_SECONDS, interval))


def _normalize_us_exit_offset(value: Any, default: float) -> float:
    try:
        offset = float(value)
    except (TypeError, ValueError):
        offset = default
    return round(max(MIN_US_EXIT_LIMIT_BUFFER_PCT, min(MAX_US_EXIT_LIMIT_BUFFER_PCT, offset)), 2)


def _settings_sync() -> dict[str, Any]:
    state = _load_state_sync()
    return state.get("settings") if isinstance(state.get("settings"), dict) else {}


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


def _aware_kst(value: Optional[datetime] = None) -> datetime:
    if value is None:
        return datetime.now(ZoneInfo("Asia/Seoul"))
    if value.tzinfo is None:
        return value.replace(tzinfo=ZoneInfo("Asia/Seoul"))
    return value.astimezone(ZoneInfo("Asia/Seoul"))


def _is_us_regular_session_now(now: Optional[datetime] = None) -> bool:
    current = _aware_kst(now)
    session_label = pd.Timestamp(current.astimezone(ZoneInfo("America/New_York")).date())
    if not US_EXCHANGE_CALENDAR.is_session(session_label):
        return False
    opened = pd.Timestamp(US_EXCHANGE_CALENDAR.session_open(session_label)).to_pydatetime()
    closed = pd.Timestamp(US_EXCHANGE_CALENDAR.session_close(session_label)).to_pydatetime()
    current_utc = current.astimezone(ZoneInfo("UTC"))
    return opened <= current_utc <= closed


def _is_domestic_regular_session_now(now: Optional[datetime] = None) -> bool:
    current = _aware_kst(now)
    session_label = pd.Timestamp(current.date())
    if not KR_EXCHANGE_CALENDAR.is_session(session_label):
        return False
    opened = pd.Timestamp(KR_EXCHANGE_CALENDAR.session_open(session_label)).to_pydatetime()
    closed = pd.Timestamp(KR_EXCHANGE_CALENDAR.session_close(session_label)).to_pydatetime()
    current_utc = current.astimezone(ZoneInfo("UTC"))
    return opened <= current_utc <= closed


def _is_order_market_open(order: dict[str, Any], now: Optional[datetime] = None) -> bool:
    if str(order.get("market") or "domestic") == "us":
        return _is_us_regular_session_now(now)
    return _is_domestic_regular_session_now(now)


def _next_us_regular_session_retry_at(now: Optional[datetime] = None) -> datetime:
    current = _aware_kst(now)
    current_utc = current.astimezone(ZoneInfo("UTC"))
    session_label = pd.Timestamp(current.astimezone(ZoneInfo("America/New_York")).date())

    if US_EXCHANGE_CALENDAR.is_session(session_label):
        opened = pd.Timestamp(US_EXCHANGE_CALENDAR.session_open(session_label)).to_pydatetime()
        closed = pd.Timestamp(US_EXCHANGE_CALENDAR.session_close(session_label)).to_pydatetime()
        if opened <= current_utc <= closed:
            return current.replace(tzinfo=None)
        if current_utc < opened:
            target = opened
        else:
            target = pd.Timestamp(
                US_EXCHANGE_CALENDAR.session_open(
                    US_EXCHANGE_CALENDAR.next_session(session_label)
                )
            ).to_pydatetime()
    else:
        next_session = US_EXCHANGE_CALENDAR.date_to_session(session_label, direction="next")
        target = pd.Timestamp(US_EXCHANGE_CALENDAR.session_open(next_session)).to_pydatetime()

    return target.astimezone(ZoneInfo("Asia/Seoul")).replace(tzinfo=None)


def _exit_submit_retry_due(order: dict[str, Any]) -> bool:
    _normalize_runtime_order(order)
    if order.get("exit_submit_blocked"):
        return False
    app_reservation = order.get("app_exit_reservation") if isinstance(order.get("app_exit_reservation"), dict) else {}
    retry_at = _parse_time(order.get("next_retry_at"))
    if retry_at:
        return datetime.now() >= retry_at
    value = order.get("exit_submit_failed_at")
    if not value:
        return True
    try:
        failed_at = datetime.fromisoformat(str(value))
    except ValueError:
        return True
    retry_seconds = EXIT_SUBMIT_RETRY_SECONDS
    last_error = str(order.get("last_error") or "")
    if order.get("market") == "us" and order.get("env_dv") not in ("prod", "real"):
        if (
            app_reservation.get("status") == "waiting_retry"
            or "모의투자에서는 해당업무가 제공되지 않습니다" in last_error
            or "모의투자 예약주문시간" in last_error
            or "초당 거래건수" in last_error
            or "EGW00201" in last_error
        ):
            retry_seconds = US_PAPER_LOCAL_RESERVATION_RETRY_SECONDS
    return datetime.now() - failed_at >= timedelta(seconds=retry_seconds)


def _should_create_local_us_paper_reservation(env_dv: str, market: str, error: Optional[str]) -> bool:
    return market == "us" and env_dv not in ("prod", "real") and bool(error)


def _mark_local_us_paper_reservation(
    order: dict[str, Any],
    *,
    exit_reason: str,
    order_type: str,
    price: Optional[float],
    current_price: float,
    error: str,
) -> None:
    _apply_us_paper_submit_error_policy(order, error)
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
        "last_error_code": order.get("last_error_code"),
        "next_retry_at": order.get("next_retry_at"),
        "retry_count": int(order.get("retry_count") or 0),
        "unsupported_paths": list(order.get("unsupported_paths") or []),
        "note": "The normal paper sell was not accepted; Strategy Builder will retry with a refreshed marketable limit.",
    }
    order["status"] = "active"
    order["app_exit_reservation"] = reservation
    order["app_exit_reservation_status"] = "waiting_retry"
    order["app_exit_reserved_at"] = reserved_at
    order["app_exit_reason"] = exit_reason
    order["exit_submit_failed_at"] = _now()
    order.pop("last_error", None)
    order.pop("exit_submit_blocked", None)


def _mark_us_paper_exit_submitted(
    order: dict[str, Any],
    *,
    exit_reason: str,
    order_type: str,
    price: Optional[float],
    current_price: float,
    order_no: Optional[str],
) -> None:
    _normalize_runtime_order(order)
    existing = order.get("app_exit_reservation") if isinstance(order.get("app_exit_reservation"), dict) else {}
    reserved_at = existing.get("reserved_at") or _now()
    reservation = {
        "status": "submitted_unconfirmed",
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
        "submitted_order_no": str(order_no or ""),
        "last_error_code": None,
        "next_retry_at": None,
        "retry_count": 0,
        "unsupported_paths": list(order.get("unsupported_paths") or []),
        "note": "The app submitted a normal paper sell and will verify holdings, cancel stale pending orders, and reprice until filled.",
    }
    order["app_exit_reservation"] = reservation
    order["app_exit_reservation_status"] = "submitted_unconfirmed"
    order["app_exit_reserved_at"] = reserved_at
    order["app_exit_reason"] = exit_reason
    order["exit_order_no"] = str(order_no or "")
    order["exit_reason"] = exit_reason
    order["exit_order_type"] = order_type
    _clear_submit_retry_state(order)
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


def _snapshot_key(
    env_dv: str,
    market: str,
    exchange: Optional[str] = None,
    stock_code: Optional[str] = None,
) -> str:
    return "|".join((
        str(env_dv or "vps"),
        str(market or "domestic"),
        str(exchange or ""),
        str(stock_code or ""),
    ))


def _paced_monitor_call(snapshot: dict[str, Any], callback):
    last_call_at = float(snapshot.get("last_call_at") or 0)
    elapsed = time.monotonic() - last_call_at
    if last_call_at and elapsed < PROTECTIVE_KIS_CALL_INTERVAL_SECONDS:
        time.sleep(PROTECTIVE_KIS_CALL_INTERVAL_SECONDS - elapsed)
    result = callback()
    snapshot["last_call_at"] = time.monotonic()
    snapshot["api_calls"] = int(snapshot.get("api_calls") or 0) + 1
    return result


def _build_monitor_snapshot_sync(orders: list[dict[str, Any]]) -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "holdings": {},
        "pending": {},
        "prices": {},
        "api_calls": 0,
        "last_call_at": 0.0,
        "errors": [],
    }
    holding_keys = {
        _snapshot_key(
            str(order.get("env_dv") or "vps"),
            str(order.get("market") or "domestic"),
        )
        for order in orders
    }
    pending_keys = {
        _snapshot_key(
            str(order.get("env_dv") or "vps"),
            str(order.get("market") or "domestic"),
            order.get("exchange"),
        )
        for order in orders
    }
    price_keys = {
        _snapshot_key(
            str(order.get("env_dv") or "vps"),
            str(order.get("market") or "domestic"),
            order.get("exchange"),
            str(order.get("stock_code") or ""),
        )
        for order in orders
        if order.get("stock_code") and _is_order_market_open(order)
    }

    for key in sorted(holding_keys):
        env_dv, market, _, _ = key.split("|", 3)
        try:
            snapshot["holdings"][key] = _paced_monitor_call(
                snapshot,
                lambda env_dv=env_dv, market=market: _get_holding_map(env_dv, market),
            )
        except Exception as exc:
            snapshot["holdings"][key] = None
            snapshot["errors"].append(f"holdings {env_dv}/{market}: {exc}")

    for key in sorted(pending_keys):
        env_dv, market, exchange, _ = key.split("|", 3)
        try:
            pending, ok = _paced_monitor_call(
                snapshot,
                lambda env_dv=env_dv, market=market, exchange=exchange: _get_pending_order_state(
                    env_dv,
                    market,
                    exchange or None,
                ),
            )
            snapshot["pending"][key] = {"orders": pending, "ok": ok}
            if not ok:
                snapshot["errors"].append(f"pending {env_dv}/{market}/{exchange or '-'}: query failed")
        except Exception as exc:
            snapshot["pending"][key] = {"orders": {}, "ok": False}
            snapshot["errors"].append(f"pending {env_dv}/{market}/{exchange or '-'}: {exc}")

    for key in sorted(price_keys):
        env_dv, market, exchange, stock_code = key.split("|", 3)
        try:
            if market == "us":
                price_info = _paced_monitor_call(
                    snapshot,
                    lambda env_dv=env_dv, exchange=exchange, stock_code=stock_code:
                        overseas_data_fetcher.get_current_price(
                            stock_code,
                            env_dv,
                            exchange or None,
                        ),
                )
            else:
                price_info = _paced_monitor_call(
                    snapshot,
                    lambda env_dv=env_dv, stock_code=stock_code:
                        data_fetcher.get_current_price(stock_code, env_dv),
                )
            snapshot["prices"][key] = float(price_info.get("price") or 0)
        except Exception as exc:
            snapshot["prices"][key] = 0.0
            snapshot["errors"].append(f"price {stock_code}: {exc}")

    snapshot.pop("last_call_at", None)
    return snapshot


def _snapshot_holdings(
    snapshot: Optional[dict[str, Any]],
    env_dv: str,
    market: str,
) -> Optional[dict[str, dict[str, Any]]]:
    if snapshot is None:
        return _get_holding_map(env_dv, market)
    return snapshot.get("holdings", {}).get(_snapshot_key(env_dv, market))


def _snapshot_pending(
    snapshot: Optional[dict[str, Any]],
    env_dv: str,
    market: str,
    exchange: Optional[str],
) -> tuple[dict[str, dict[str, Any]], bool]:
    if snapshot is None:
        return _get_pending_order_state(env_dv, market, exchange)
    value = snapshot.get("pending", {}).get(_snapshot_key(env_dv, market, exchange))
    if not isinstance(value, dict):
        return {}, False
    return value.get("orders", {}), bool(value.get("ok"))


def _current_order_price(
    order: dict[str, Any],
    env_dv: str,
    holding: dict[str, Any],
    snapshot: Optional[dict[str, Any]] = None,
) -> float:
    market = str(order.get("market") or "domestic")
    if snapshot is not None:
        current_price = float(
            snapshot.get("prices", {}).get(
                _snapshot_key(
                    env_dv,
                    market,
                    order.get("exchange"),
                    str(order["stock_code"]),
                ),
                0,
            )
            or holding.get("current_price")
            or order.get("last_price")
            or 0
        )
    elif market == "us":
        price_info = overseas_data_fetcher.get_current_price(
            str(order["stock_code"]),
            env_dv,
            order.get("exchange"),
        )
        current_price = float(
            price_info.get("price")
            or holding.get("current_price")
            or order.get("last_price")
            or 0
        )
    else:
        price_info = data_fetcher.get_current_price(str(order["stock_code"]), env_dv)
        current_price = float(
            price_info.get("price")
            or holding.get("current_price")
            or order.get("last_price")
            or 0
        )
    if current_price > 0:
        order["last_price"] = current_price
        order["last_checked_at"] = _now()
    return current_price


def _us_stop_loss_order_price(
    configured_price: Optional[float],
    current_price: float,
    offset_pct: float = US_STOP_LOSS_MARKETABLE_LIMIT_BUFFER_PCT,
) -> float:
    if current_price <= 0:
        return float(configured_price or 0)
    marketable_price = current_price * (1 - _normalize_us_exit_offset(
        offset_pct,
        US_STOP_LOSS_MARKETABLE_LIMIT_BUFFER_PCT,
    ) / 100)
    if configured_price and configured_price > 0:
        marketable_price = min(float(configured_price), marketable_price)
    return _round_price("us", marketable_price, "down")


def _us_triggered_exit_order_price(
    configured_price: Optional[float],
    current_price: float,
    exit_reason: str,
    settings: Optional[dict[str, Any]] = None,
    reprice_count: int = 0,
) -> float:
    settings = settings or _settings_sync()
    setting_key = (
        "us_stop_loss_limit_offset_pct"
        if exit_reason == "stop_loss"
        else "us_take_profit_limit_offset_pct"
    )
    default = (
        US_STOP_LOSS_MARKETABLE_LIMIT_BUFFER_PCT
        if exit_reason == "stop_loss"
        else US_TAKE_PROFIT_MARKETABLE_LIMIT_BUFFER_PCT
    )
    base_offset = _normalize_us_exit_offset(settings.get(setting_key), default)
    step_offset = _normalize_us_exit_offset(
        settings.get("us_exit_reprice_step_pct"),
        US_EXIT_REPRICE_STEP_PCT,
    )
    max_offset = _normalize_us_exit_offset(
        settings.get("us_exit_max_offset_pct"),
        US_EXIT_MAX_OFFSET_PCT,
    )
    adaptive_offset = min(
        max(max_offset, base_offset),
        base_offset + max(0, int(reprice_count or 0)) * step_offset,
    )
    return _us_stop_loss_order_price(
        configured_price,
        current_price,
        adaptive_offset,
    )


def _reconcile_exit_submitted_sync(
    order: dict[str, Any],
    env_dv: str,
    snapshot: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if order.get("status") != "exit_submitted":
        return order

    market = str(order.get("market") or "domestic")
    holdings = _snapshot_holdings(snapshot, env_dv, market)
    if holdings is None:
        return _mark_submitted_exit_missing_position(order, "holdings unavailable or empty")

    holding = holdings.get(str(order.get("stock_code")))
    if not holding or int(holding.get("quantity") or 0) <= 0:
        return _mark_submitted_exit_missing_position(order, "position not found in holdings")

    order["quantity"] = min(int(order.get("quantity") or 0), int(holding.get("quantity") or 0))
    order["last_checked_at"] = _now()
    pending, pending_query_ok = _snapshot_pending(
        snapshot,
        env_dv,
        market,
        order.get("exchange"),
    )
    if not pending_query_ok:
        order["last_error"] = "pending order query failed; exit reconciliation deferred"
        return order
    exit_order_no = str(order.get("exit_order_no") or "")
    pending_exit = pending.get(exit_order_no)
    exit_reason = str(order.get("exit_reason") or "stop_loss")
    if not _is_order_market_open(order):
        order["last_error"] = "exit submitted; waiting for regular market session before repricing"
        return order

    current_price = (
        _current_order_price(order, env_dv, holding)
        if snapshot is None
        else _current_order_price(order, env_dv, holding, snapshot)
    )

    is_us_paper = market == "us" and env_dv not in ("prod", "real")
    settings = _settings_sync()
    reprice_seconds = _normalize_monitor_interval(
        settings.get("exit_reprice_interval_seconds", EXIT_PENDING_REPRICE_SECONDS)
    )
    submitted_at = _order_timestamp(order, "exit_submitted_at", "closed_at")
    if (
        is_us_paper
        and submitted_at
        and datetime.now() - submitted_at < timedelta(seconds=reprice_seconds)
    ):
        order["last_error"] = "exit submitted; waiting for fill reconciliation before repricing"
        return order

    if pending_exit and (exit_reason == "stop_loss" or is_us_paper):
        if submitted_at and datetime.now() - submitted_at < timedelta(seconds=reprice_seconds):
            order["last_error"] = "exit order pending; waiting before repricing"
            return order

        cancel_quantity = max(
            1,
            int(
                pending_exit.get("unfilled_qty")
                or pending_exit.get("order_qty")
                or order["quantity"]
            ),
        )
        if market == "us":
            cancel_result = overseas_data_fetcher.cancel_order(
                order_no=exit_order_no,
                symbol=str(order["stock_code"]),
                qty=cancel_quantity,
                env_dv=env_dv,
                exchange=order.get("exchange"),
            )
        else:
            cancel_result = cancel_order(
                order_no=exit_order_no,
                org_no=str(order.get("exit_org_no") or ""),
                stock_code=str(order["stock_code"]),
                qty=cancel_quantity,
                env_dv=env_dv,
            )
        order.setdefault("events", []).append({
            "type": "exit_pending_cancel_for_reprice",
            "at": _now(),
            "order_no": exit_order_no,
            "cancel_quantity": cancel_quantity,
            "current_price": current_price,
            "result": cancel_result,
        })
        if not cancel_result.get("success"):
            order["last_error"] = f"exit pending cancel failed: {cancel_result.get('message')}"
            return order
        order["exit_reprice_count"] = int(order.get("exit_reprice_count") or 0) + 1

    if (exit_reason == "stop_loss" or is_us_paper) and current_price > 0:
        if not pending_exit:
            order["exit_reprice_count"] = int(order.get("exit_reprice_count") or 0) + 1
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
            reason=f"보호주문 {exit_reason} 재시도: 현재가 {current_price}, 기존 청산 주문 후에도 보유 잔량 확인",
            exit_reason=exit_reason,
            order_type=str(
                order.get(f"{exit_reason}_order_type")
                or order.get("exit_order_type")
                or "limit"
            ),
            price=float(
                order.get(f"{exit_reason}_limit_price")
                or order.get(f"{exit_reason}_price")
                or current_price
            ),
            current_price=current_price,
        )

    order["last_error"] = "exit submitted but holding is still present"
    return order


def _mark_submitted_exit_missing_position(order: dict[str, Any], reason: str) -> dict[str, Any]:
    missing_count = int(order.get("position_missing_count") or 0) + 1
    now = _now()
    order["position_missing_count"] = missing_count
    order["last_checked_at"] = now
    order["last_error"] = f"{reason}; submitted exit close confirmation ({missing_count}/{POSITION_MISSING_CONFIRMATIONS})"
    if missing_count < POSITION_MISSING_CONFIRMATIONS:
        return order

    order["status"] = "closed"
    order["closed_at"] = now
    order.pop("last_error", None)
    reservation = order.get("app_exit_reservation")
    if isinstance(reservation, dict):
        reservation["status"] = "filled"
        reservation["filled_at"] = now
        order["app_exit_reservation_status"] = "filled"
    order.setdefault("events", []).append({
        "type": "position_closed_after_exit_submit",
        "at": now,
        "reason": reason,
    })
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
    pending, _ = _get_pending_order_state(env_dv, market, exchange)
    return pending


def _get_pending_order_state(
    env_dv: str,
    market: str = "domestic",
    exchange: str | None = None,
) -> tuple[dict[str, dict[str, Any]], bool]:
    if market == "us":
        df, ok = overseas_data_fetcher.get_pending_orders(env_dv, exchange or "NASD")
    else:
        df, ok = get_pending_orders(env_dv)
    if not ok or df.empty:
        return {}, bool(ok)
    return ({
        str(row.get("order_no")): row.to_dict()
        for _, row in df.iterrows()
    }, True)


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
    skip_direct_us_sell: bool = False,
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
        if result.success and not result.dataframe.empty:
            row = result.dataframe.iloc[0]
            return (
                str(row.get("ODNO", row.get("odno", ""))),
                str(row.get("KRX_FWDG_ORD_ORGNO", row.get("ord_gno_brno", ""))),
                True,
                None,
            )
        return None, None, False, result.display_error() or "overseas sell failed"

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
    _normalize_runtime_order(order)
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
    if market == "us" and order_type == "limit":
        price = _us_triggered_exit_order_price(
            float(price or 0),
            current_price,
            exit_reason,
            reprice_count=int(order.get("exit_reprice_count") or 0),
        )
    if market == "us" and env_dv not in ("prod", "real") and not _is_us_regular_session_now():
        detail = "미국 정규장 외 시간이라 앱 보호매도를 다음 정규장까지 대기합니다"
        _mark_local_us_paper_reservation(
            order,
            exit_reason=exit_reason,
            order_type=order_type,
            price=price if order_type == "limit" else None,
            current_price=current_price,
            error=detail,
        )
        _set_next_retry_at(order, _next_us_regular_session_retry_at())
        order["app_exit_reservation"]["last_error"] = detail
        order.setdefault("events", []).append({
            "type": f"{exit_reason}_app_waiting_regular_session",
            "at": _now(),
            "order_type": order_type,
            "order_price": price if order_type == "limit" else None,
            "current_price": current_price,
            "next_retry_at": order.get("next_retry_at"),
        })
        return order

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
        skip_direct_us_sell=False,
    )
    if ok:
        order["status"] = "exit_submitted"
        order["exit_order_no"] = order_no
        order["exit_org_no"] = org_no
        order["exit_reason"] = exit_reason
        order["exit_order_type"] = order_type
        order["exit_submitted_at"] = _now()
        order["exit_order_price"] = price if order_type == "limit" else None
        _clear_submit_retry_state(order)
        order.pop("last_error", None)
        order.pop("exit_submit_failed_at", None)
        order.pop("exit_submit_blocked", None)
        order.pop("closed_at", None)
        if market == "us" and env_dv not in ("prod", "real"):
            _mark_us_paper_exit_submitted(
                order,
                exit_reason=exit_reason,
                order_type=order_type,
                price=price if order_type == "limit" else None,
                current_price=current_price,
                order_no=order_no,
            )
        else:
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

        _apply_us_paper_submit_error_policy(order, error)
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


def _check_order_sync(
    order: dict[str, Any],
    env_dv: str,
    snapshot: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    if order.get("status") != "active":
        return order

    market = str(order.get("market") or "domestic")
    holdings = _snapshot_holdings(snapshot, env_dv, market)
    pending, pending_query_ok = _snapshot_pending(
        snapshot,
        env_dv,
        market,
        order.get("exchange"),
    )

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
    if pending_query_ok:
        _ensure_take_profit(order, env_dv, pending)
    elif order.get("take_profit_submit_mode") != "on_trigger":
        order["last_error"] = "pending order query failed; take-profit reconciliation deferred"

    if snapshot is not None:
        current_price = float(
            snapshot.get("prices", {}).get(
                _snapshot_key(
                    env_dv,
                    market,
                    order.get("exchange"),
                    str(order["stock_code"]),
                ),
                0,
            )
            or holding.get("current_price")
            or 0
        )
    elif market == "us":
        price_info = overseas_data_fetcher.get_current_price(
            str(order["stock_code"]),
            env_dv,
            order.get("exchange"),
        )
        current_price = float(price_info.get("price") or holding.get("current_price") or 0)
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
        "next_retry_at": None,
        "retry_count": 0,
        "last_error_code": None,
        "unsupported_paths": [],
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
        "next_retry_at": None,
        "retry_count": 0,
        "last_error_code": None,
        "unsupported_paths": [],
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


def _monitor_health(
    *,
    orders: list[dict[str, Any]],
    snapshot: dict[str, Any],
    started_at: datetime,
    processing_errors: list[str],
) -> dict[str, Any]:
    now = datetime.now()
    rate_limited = [
        order for order in orders
        if order.get("last_error_code") == "EGW00201"
        or _is_us_paper_rate_limit_error(str(order.get("last_error") or ""))
    ]
    overdue = []
    for order in orders:
        if order.get("status") not in {"exit_submitted", "active"}:
            continue
        reservation = order.get("app_exit_reservation")
        waiting = (
            order.get("status") == "exit_submitted"
            or (
                isinstance(reservation, dict)
                and reservation.get("status") in {"waiting_retry", "submitted_unconfirmed"}
            )
        )
        if not waiting:
            continue
        if not _is_order_market_open(order):
            continue
        started = _order_timestamp(
            order,
            "exit_submitted_at",
            "app_exit_reserved_at",
            "exit_submit_failed_at",
        )
        if isinstance(reservation, dict) and reservation.get("status") == "waiting_retry":
            retry_at = _order_timestamp(order, "next_retry_at")
            if retry_at and (started is None or retry_at > started):
                started = retry_at
        if started and (now - started).total_seconds() >= PROTECTIVE_EXIT_ALERT_AFTER_SECONDS:
            overdue.append({
                "stock_code": order.get("stock_code"),
                "status": order.get("status"),
                "age_seconds": int((now - started).total_seconds()),
            })

    snapshot_errors = list(snapshot.get("errors") or [])
    errors = snapshot_errors + processing_errors
    status = "degraded" if errors or rate_limited or overdue else "healthy"
    return {
        "status": status,
        "last_cycle_started_at": started_at.isoformat(timespec="seconds"),
        "last_cycle_completed_at": now.isoformat(timespec="seconds"),
        "last_cycle_duration_seconds": round((now - started_at).total_seconds(), 2),
        "snapshot_api_calls": int(snapshot.get("api_calls") or 0),
        "snapshot_errors": snapshot_errors[-20:],
        "processing_errors": processing_errors[-20:],
        "rate_limited_order_count": len(rate_limited),
        "overdue_exit_count": len(overdue),
        "overdue_exits": overdue[:20],
        "active_order_count": sum(order.get("status") == "active" for order in orders),
        "submitted_exit_count": sum(order.get("status") == "exit_submitted" for order in orders),
    }


def _send_monitor_health_alert_sync(
    health: dict[str, Any],
    previous_health: dict[str, Any],
) -> dict[str, Any]:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_ALLOWED_CHAT_ID", "").strip()
    if not token or not chat_id:
        return {"status": "disabled"}

    is_degraded = health.get("status") == "degraded"
    previous_status = previous_health.get("status")
    signature = "|".join((
        str(health.get("status") or ""),
        str(health.get("rate_limited_order_count") or 0),
        str(health.get("overdue_exit_count") or 0),
        str(len(health.get("snapshot_errors") or [])),
        str(len(health.get("processing_errors") or [])),
    ))
    previous_signature = str(previous_health.get("last_alert_signature") or "")
    last_alert_at = _parse_time(previous_health.get("last_alert_at"))
    cooldown_elapsed = (
        last_alert_at is None
        or (datetime.now() - last_alert_at).total_seconds() >= PROTECTIVE_ALERT_COOLDOWN_SECONDS
    )
    should_send = (
        (is_degraded and (previous_status != "degraded" or cooldown_elapsed))
        or (not is_degraded and previous_status == "degraded")
    )
    if not should_send:
        return {"status": "skipped", "signature": previous_signature}

    report_url = os.environ.get("US_MARKET_REPORT_URL", "").rstrip("/")
    link = f"\n{report_url}/review" if report_url else ""
    if is_degraded:
        overdue_symbols = ", ".join(
            str(item.get("stock_code"))
            for item in health.get("overdue_exits", [])[:8]
        ) or "-"
        text = (
            "KIS protective monitor degraded\n"
            f"API calls {health.get('snapshot_api_calls', 0)}, "
            f"rate limits {health.get('rate_limited_order_count', 0)}, "
            f"overdue exits {health.get('overdue_exit_count', 0)}\n"
            f"Overdue symbols: {overdue_symbols}\n"
            f"Snapshot errors: {len(health.get('snapshot_errors') or [])}, "
            f"processing errors: {len(health.get('processing_errors') or [])}{link}"
        )
    else:
        text = f"KIS protective monitor recovered{link}"

    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
            timeout=12,
        )
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        if response.ok and body.get("ok"):
            return {
                "status": "sent",
                "signature": signature,
                "sent_at": _now(),
            }
        return {
            "status": "error",
            "signature": previous_signature,
            "message": str(body.get("description") or "telegram send failed")[:500],
        }
    except Exception as exc:
        return {
            "status": "error",
            "signature": previous_signature,
            "message": f"{type(exc).__name__}: {exc}"[:500],
        }


async def run_monitor_cycle() -> None:
    started_at = datetime.now()
    state = await _load_state()
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    price_source = settings.get("price_source") or "websocket"
    changed = False
    updated_orders = []
    processing_errors: list[str] = []
    snapshot_targets = [
        order
        for order in state.get("orders", [])
        if order.get("status") == "exit_submitted"
        or (
            order.get("status") == "active"
            and _is_order_market_open(order)
            and not (
                price_source == "websocket"
                and order.get("source") == "review"
                and order.get("take_profit_submit_mode") == "on_trigger"
                and _is_recent_realtime(order)
            )
        )
    ]
    snapshot = await asyncio.to_thread(_build_monitor_snapshot_sync, snapshot_targets)

    for order in state.get("orders", []):
        if order.get("status") == "exit_submitted":
            try:
                before = json.dumps(order, sort_keys=True, default=str)
                updated = await asyncio.to_thread(
                    _reconcile_exit_submitted_sync,
                    order,
                    str(order.get("env_dv") or "vps"),
                    snapshot,
                )
                updated_orders.append(updated)
                changed = changed or json.dumps(updated, sort_keys=True, default=str) != before
            except Exception as exc:
                order["last_error"] = str(exc)
                order["last_checked_at"] = _now()
                updated_orders.append(order)
                changed = True
                processing_errors.append(f"{order.get('stock_code')}: {exc}")
                logger.exception("protective exit reconciliation failed")
            continue

        if order.get("status") != "active":
            updated_orders.append(order)
            continue

        if not _is_order_market_open(order):
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
            updated = await asyncio.to_thread(
                _check_order_sync,
                order,
                str(order.get("env_dv") or "vps"),
                snapshot,
            )
            updated_orders.append(updated)
            changed = changed or json.dumps(updated, sort_keys=True, default=str) != before
        except Exception as exc:
            order["last_error"] = str(exc)
            order["last_checked_at"] = _now()
            updated_orders.append(order)
            changed = True
            processing_errors.append(f"{order.get('stock_code')}: {exc}")
            logger.exception("protective order check failed")

    health = _monitor_health(
        orders=updated_orders,
        snapshot=snapshot,
        started_at=started_at,
        processing_errors=processing_errors,
    )
    alert = await asyncio.to_thread(
        _send_monitor_health_alert_sync,
        health,
        state.get("health") if isinstance(state.get("health"), dict) else {},
    )
    health["alert_status"] = alert.get("status")
    if alert.get("status") == "sent":
        health["last_alert_signature"] = alert.get("signature")
        health["last_alert_at"] = alert.get("sent_at")
    else:
        previous_health = state.get("health") if isinstance(state.get("health"), dict) else {}
        health["last_alert_signature"] = previous_health.get("last_alert_signature")
        health["last_alert_at"] = previous_health.get("last_alert_at")
        if alert.get("message"):
            health["alert_error"] = alert["message"]

    state = await _merge_updated_orders(updated_orders, {"health": health})
    if changed:
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
    state = await _load_state()
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    health = dict(state.get("health") if isinstance(state.get("health"), dict) else {})
    completed_at = _parse_time(health.get("last_cycle_completed_at"))
    stale_after = max(
        90,
        _normalize_monitor_interval(settings.get("monitor_interval_seconds")) * 3,
    )
    health["monitor_running"] = bool(_monitor_task is not None and not _monitor_task.done())
    health["stale"] = bool(
        completed_at is None
        or (datetime.now() - completed_at).total_seconds() > stale_after
    )
    health["stale_after_seconds"] = stale_after
    if health["stale"] and health.get("status") != "degraded":
        health["status"] = "stale"
    state["health"] = health
    return state


async def list_protective_app_reservations() -> list[dict[str, Any]]:
    state = await _load_state()
    rows = []
    for order in state.get("orders", []):
        if not _is_us_paper_order(order):
            continue
        reservation = order.get("app_exit_reservation")
        if not isinstance(reservation, dict):
            continue
        reservation_status = str(reservation.get("status") or "")
        if order.get("status") == "closed" and reservation_status != "filled":
            reservation_status = "closed"
        created_at = reservation.get("reserved_at") or order.get("created_at") or _now()
        action = "SELL"
        rows.append({
            **reservation,
            "status": reservation_status,
            "id": f"protective:{order.get('id')}",
            "reservation_order_no": f"protective:{order.get('id')}",
            "reservation_order_date": str(created_at)[:10].replace("-", ""),
            "reservation_order_org_no": "app",
            "reservation_source": "app",
            "reservation_kind": "protective_exit",
            "cancellable": False,
            "market": "us",
            "action": action,
            "stock_code": order.get("stock_code"),
            "stock_name": order.get("stock_name"),
            "exchange": order.get("exchange"),
            "quantity": order.get("quantity"),
            "price": reservation.get("limit_price"),
            "order_type": reservation.get("order_type") or "limit",
            "scheduled_at": reservation.get("next_retry_at") or reservation.get("reserved_at"),
            "expires_at": None,
            "submitted_order_no": reservation.get("submitted_order_no"),
            "SLL_BUY_DVSN_NAME": "매도",
            "ORD_QTY": order.get("quantity"),
            "ORD_UNPR": reservation.get("limit_price"),
            "RSVN_ORD_PRCS_STAT_NAME": {
                "waiting_retry": "보호매도 재시도 대기",
                "submitted_unconfirmed": "보호매도 체결 확인 중",
                "filled": "보호매도 체결 완료",
                "closed": "보호매도 종료",
            }.get(reservation_status, reservation_status or "-"),
        })
    return rows


async def update_monitor_settings(
    *,
    monitor_interval_seconds: int,
    us_stop_loss_limit_offset_pct: float | None = None,
    us_take_profit_limit_offset_pct: float | None = None,
    exit_reprice_interval_seconds: int | None = None,
    us_exit_reprice_step_pct: float | None = None,
    us_exit_max_offset_pct: float | None = None,
) -> dict[str, Any]:
    state = await _load_state()
    settings = state.get("settings") if isinstance(state.get("settings"), dict) else {}
    settings["monitor_interval_seconds"] = _normalize_monitor_interval(monitor_interval_seconds)
    if us_stop_loss_limit_offset_pct is not None:
        settings["us_stop_loss_limit_offset_pct"] = _normalize_us_exit_offset(
            us_stop_loss_limit_offset_pct,
            US_STOP_LOSS_MARKETABLE_LIMIT_BUFFER_PCT,
        )
    if us_take_profit_limit_offset_pct is not None:
        settings["us_take_profit_limit_offset_pct"] = _normalize_us_exit_offset(
            us_take_profit_limit_offset_pct,
            US_TAKE_PROFIT_MARKETABLE_LIMIT_BUFFER_PCT,
        )
    if exit_reprice_interval_seconds is not None:
        settings["exit_reprice_interval_seconds"] = _normalize_monitor_interval(
            exit_reprice_interval_seconds
        )
    if us_exit_reprice_step_pct is not None:
        settings["us_exit_reprice_step_pct"] = _normalize_us_exit_offset(
            us_exit_reprice_step_pct,
            US_EXIT_REPRICE_STEP_PCT,
        )
    if us_exit_max_offset_pct is not None:
        settings["us_exit_max_offset_pct"] = _normalize_us_exit_offset(
            us_exit_max_offset_pct,
            US_EXIT_MAX_OFFSET_PCT,
        )
    if (
        float(settings.get("us_exit_max_offset_pct") or US_EXIT_MAX_OFFSET_PCT)
        < max(
            float(settings.get("us_stop_loss_limit_offset_pct") or 0),
            float(settings.get("us_take_profit_limit_offset_pct") or 0),
        )
    ):
        settings["us_exit_max_offset_pct"] = max(
            float(settings.get("us_stop_loss_limit_offset_pct") or 0),
            float(settings.get("us_take_profit_limit_offset_pct") or 0),
        )
    state["settings"] = settings
    await _save_state(state)
    await _sync_realtime_subscriptions(state)
    return settings
