"""Strategy Builder app-level reservation orders for vps paper trading.

These are not broker-side reservations. The Strategy Builder backend persists
the schedule locally and submits a normal order when the configured KST time is
due.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd

from backend import get_current_mode, is_authenticated
from backend.services.audit_log import write_order_audit
from core import overseas_data_fetcher
from core.data_fetcher import get_holdings
from core.order_executor import OrderExecutor
from core.signal import Action, Signal

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
RUNTIME_DIR = Path(
    os.environ.get(
        "KIS_RUNTIME_DIR",
        str(Path(__file__).resolve().parents[2] / ".runtime"),
    )
)
STATE_FILE = RUNTIME_DIR / "app_reservations.json"
MONITOR_INTERVAL_SECONDS = 10
DEFAULT_EXPIRY_MINUTES = 30
MAX_ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (10, 30, 60)
ACTIVE_STATUSES = {"scheduled", "submitting"}
FINAL_STATUSES = {"submitted", "failed", "cancelled", "expired"}

_state_lock = asyncio.Lock()
_monitor_task: Optional[asyncio.Task] = None


def _now_dt() -> datetime:
    return datetime.now(KST)


def _now() -> str:
    return _now_dt().isoformat(timespec="seconds")


def _parse_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


def _format_time(value: datetime) -> str:
    return value.astimezone(KST).isoformat(timespec="seconds")


def _event(event_type: str, **payload: Any) -> dict[str, Any]:
    return {"type": event_type, "at": _now(), **payload}


def _load_state_sync() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"reservations": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        reservations = data.get("reservations") if isinstance(data, dict) else []
        if not isinstance(reservations, list):
            reservations = []
        return {
            "reservations": [
                _normalize_reservation(item)
                for item in reservations
                if isinstance(item, dict)
            ],
        }
    except Exception as exc:
        logger.warning("app reservation state load failed: %s", exc)
        return {"reservations": []}


def _save_state_sync(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


async def _load_state() -> dict[str, Any]:
    async with _state_lock:
        return await asyncio.to_thread(_load_state_sync)


async def _save_state(state: dict[str, Any]) -> None:
    async with _state_lock:
        await asyncio.to_thread(_save_state_sync, state)


def _normalize_reservation(item: dict[str, Any]) -> dict[str, Any]:
    item["reservation_source"] = "app"
    item["id"] = str(item.get("id") or uuid4().hex)
    item["env_dv"] = str(item.get("env_dv") or "vps")
    item["market"] = str(item.get("market") or "domestic").strip().lower()
    item["exchange"] = item.get("exchange") or None
    item["stock_code"] = str(item.get("stock_code") or "").strip().upper() if item["market"] == "us" else str(item.get("stock_code") or "").strip()
    item["stock_name"] = str(item.get("stock_name") or item.get("stock_code") or "").strip()
    item["action"] = str(item.get("action") or "").strip().upper()
    item["quantity"] = int(item.get("quantity") or 0)
    item["price"] = float(item.get("price") or 0)
    item["order_type"] = str(item.get("order_type") or "limit").strip().lower()
    item["status"] = str(item.get("status") or "scheduled")
    item["attempt_count"] = int(item.get("attempt_count") or 0)
    item["events"] = item.get("events") if isinstance(item.get("events"), list) else []
    item.setdefault("created_at", _now())
    item.setdefault("updated_at", item["created_at"])
    return item


def _validate_app_reservation_request(
    *,
    env_dv: str,
    market: str,
    action: str,
    quantity: int,
    price: float,
    order_type: str,
    scheduled_at: str | None,
    expires_at: str | None,
) -> tuple[datetime, datetime]:
    if env_dv != "vps":
        raise ValueError("앱 예약주문은 8081 모의투자(vps)에서만 사용할 수 있습니다")
    if market not in {"domestic", "us"}:
        raise ValueError("시장 구분이 올바르지 않습니다")
    if action not in {"BUY", "SELL"}:
        raise ValueError("예약주문 방향이 올바르지 않습니다")
    if quantity <= 0:
        raise ValueError("예약주문 수량이 올바르지 않습니다")
    if market == "domestic":
        if order_type not in {"limit", "market"}:
            raise ValueError("앱 예약은 국내 지정가 또는 시장가만 지원합니다")
    else:
        if order_type != "limit":
            raise ValueError("앱 예약은 미국 지정가 주문만 지원합니다")
    if order_type == "limit" and price <= 0:
        raise ValueError("지정가 앱 예약에는 가격이 필요합니다")

    scheduled = _parse_time(scheduled_at)
    if scheduled is None:
        raise ValueError("앱 예약 실행시각이 필요합니다")
    expires = _parse_time(expires_at) if expires_at else scheduled + timedelta(minutes=DEFAULT_EXPIRY_MINUTES)
    if expires is None:
        raise ValueError("앱 예약 만료시각이 올바르지 않습니다")
    if expires <= _now_dt():
        raise ValueError("앱 예약 만료시각은 현재 이후여야 합니다")
    if expires <= scheduled:
        raise ValueError("앱 예약 만료시각은 실행시각 이후여야 합니다")
    return scheduled, expires


def _assert_sellable(order: dict[str, Any]) -> None:
    if order.get("action") != "SELL":
        return
    env_dv = str(order.get("env_dv") or "vps")
    market = str(order.get("market") or "domestic")
    stock_code = str(order.get("stock_code") or "")
    quantity = int(order.get("quantity") or 0)
    if market == "us":
        holdings = overseas_data_fetcher.get_holdings(env_dv)
        row = holdings[holdings["stock_code"].astype(str) == stock_code] if not holdings.empty else pd.DataFrame()
        message = "미국 보유수량을 확인할 수 없어 앱 예약매도를 차단했습니다"
    else:
        holdings = get_holdings(env_dv)
        row = holdings[holdings["stock_code"].astype(str) == stock_code] if not holdings.empty else pd.DataFrame()
        message = "국내 보유수량을 확인할 수 없어 앱 예약매도를 차단했습니다"
    if row.empty:
        raise ValueError(message)
    holding_qty = int(float(row.iloc[0].get("quantity") or 0))
    if holding_qty < quantity:
        raise ValueError(f"앱 예약매도 수량이 보유수량({holding_qty})을 초과합니다")


async def create_app_reservation(
    *,
    market: str,
    stock_code: str,
    stock_name: str,
    action: str,
    quantity: int,
    price: float,
    order_type: str,
    exchange: str | None,
    scheduled_at: str | None,
    expires_at: str | None = None,
    authenticated_user: str = "unknown",
) -> dict[str, Any]:
    env_dv = get_current_mode()
    market = market.strip().lower()
    action = action.strip().upper()
    order_type = order_type.strip().lower()
    stock_code = stock_code.strip().upper() if market == "us" else stock_code.strip()
    stock_name = stock_name.strip() or stock_code
    exchange = exchange.strip().upper() if exchange else None
    scheduled, expires = _validate_app_reservation_request(
        env_dv=env_dv,
        market=market,
        action=action,
        quantity=quantity,
        price=price,
        order_type=order_type,
        scheduled_at=scheduled_at,
        expires_at=expires_at,
    )
    order = _normalize_reservation({
        "id": uuid4().hex,
        "reservation_source": "app",
        "env_dv": env_dv,
        "market": market,
        "exchange": exchange,
        "stock_code": stock_code,
        "stock_name": stock_name,
        "action": action,
        "quantity": int(quantity),
        "price": float(price),
        "order_type": order_type,
        "scheduled_at": _format_time(scheduled),
        "expires_at": _format_time(expires),
        "status": "scheduled",
        "attempt_count": 0,
        "next_retry_at": None,
        "submitted_order_no": None,
        "last_error": None,
        "created_at": _now(),
        "updated_at": _now(),
        "events": [_event("created")],
    })
    _assert_sellable(order)

    state = await _load_state()
    state.setdefault("reservations", []).append(order)
    await _save_state(state)
    write_order_audit({
        "authenticated_user": authenticated_user,
        "mode": env_dv,
        "action": "app_reservation_create",
        "stock_code": stock_code,
        "quantity": quantity,
        "price": price,
        "order_type": order_type,
        "result": "success",
        "order_id": order["id"],
        "error_message": None,
    })
    return order


def _date_in_range(order: dict[str, Any], start_date: str | None, end_date: str | None) -> bool:
    scheduled = _parse_time(order.get("scheduled_at"))
    if scheduled is None:
        return True
    compact = scheduled.strftime("%Y%m%d")
    if start_date and compact < start_date:
        return False
    if end_date and compact > end_date:
        return False
    return True


async def list_app_reservations(
    *,
    market: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    include_cancelled: bool = True,
) -> list[dict[str, Any]]:
    state = await _load_state()
    orders = []
    for item in state.get("reservations", []):
        if market and item.get("market") != market:
            continue
        if not include_cancelled and item.get("status") == "cancelled":
            continue
        if not _date_in_range(item, start_date, end_date):
            continue
        orders.append(_record_for_api(item))
    return sorted(orders, key=lambda row: str(row.get("scheduled_at") or ""), reverse=True)


async def cancel_app_reservation(
    *,
    reservation_id: str,
    authenticated_user: str = "unknown",
) -> dict[str, Any]:
    state = await _load_state()
    for item in state.get("reservations", []):
        if str(item.get("id")) != reservation_id:
            continue
        if item.get("status") in FINAL_STATUSES:
            raise ValueError("이미 제출, 실패, 취소 또는 만료된 앱 예약은 취소할 수 없습니다")
        item["status"] = "cancelled"
        item["updated_at"] = _now()
        item.setdefault("events", []).append(_event("cancelled"))
        await _save_state(state)
        write_order_audit({
            "authenticated_user": authenticated_user,
            "mode": item.get("env_dv"),
            "action": "app_reservation_cancel",
            "stock_code": item.get("stock_code"),
            "quantity": item.get("quantity"),
            "price": item.get("price"),
            "order_type": item.get("order_type"),
            "result": "success",
            "order_id": item.get("id"),
            "error_message": None,
        })
        return _record_for_api(item)
    raise ValueError("앱 예약주문을 찾을 수 없습니다")


def _record_for_api(order: dict[str, Any]) -> dict[str, Any]:
    scheduled = _parse_time(order.get("scheduled_at"))
    created = _parse_time(order.get("created_at"))
    order_date = (created or scheduled or _now_dt()).strftime("%Y%m%d")
    action = str(order.get("action") or "")
    return {
        **order,
        "reservation_source": "app",
        "reservation_order_no": order.get("id"),
        "reservation_order_date": order_date,
        "reservation_order_org_no": "app",
        "SLL_BUY_DVSN_NAME": "매수" if action == "BUY" else "매도" if action == "SELL" else action,
        "ORD_QTY": order.get("quantity"),
        "ORD_UNPR": order.get("price"),
        "RSVN_ORD_PRCS_STAT_NAME": _status_label(str(order.get("status") or "")),
    }


def _status_label(status: str) -> str:
    return {
        "scheduled": "앱 예약 대기",
        "submitting": "앱 주문 제출 중",
        "submitted": "앱 주문 제출 완료",
        "failed": "앱 예약 실패",
        "cancelled": "앱 예약 취소",
        "expired": "앱 예약 만료",
    }.get(status, status or "-")


def _is_retryable_error(error: str) -> bool:
    text = str(error or "")
    retry_markers = (
        "EGW00201",
        "초당 거래건수",
        "인증",
        "timeout",
        "Timeout",
        "temporarily",
        "Temporary",
        "Connection",
        "HTTP",
        "네트워크",
        "일시",
    )
    fatal_markers = (
        "보유수량",
        "미보유",
        "수량이",
        "가격",
        "지원",
        "vps",
        "모의투자",
    )
    if any(marker in text for marker in fatal_markers):
        return False
    return any(marker in text for marker in retry_markers)


def _next_retry_time(attempt_count: int) -> str:
    index = min(max(attempt_count, 1), len(RETRY_DELAYS_SECONDS)) - 1
    return _format_time(_now_dt() + timedelta(seconds=RETRY_DELAYS_SECONDS[index]))


def _submit_order_sync(order: dict[str, Any]) -> tuple[bool, str | None, str | None]:
    if not is_authenticated():
        return False, None, "KIS API 인증이 필요합니다"
    if get_current_mode() != "vps":
        return False, None, "앱 예약주문은 vps 모드에서만 실행할 수 있습니다"
    _assert_sellable(order)

    market = str(order.get("market") or "domestic")
    action = str(order.get("action") or "")
    quantity = int(order.get("quantity") or 0)
    price = float(order.get("price") or 0)
    order_type = str(order.get("order_type") or "limit")
    stock_code = str(order.get("stock_code") or "")
    stock_name = str(order.get("stock_name") or stock_code)

    if market == "us":
        if order_type != "limit":
            return False, None, "앱 예약 미국 주문은 지정가만 지원합니다"
        result = overseas_data_fetcher.submit_order(
            symbol=stock_code,
            action=action,
            quantity=quantity,
            price=price,
            env_dv="vps",
            exchange=order.get("exchange"),
        )
        if not result.success or result.dataframe.empty:
            return False, None, result.display_error()
        row = result.dataframe.iloc[0].to_dict()
        return True, str(row.get("ODNO") or row.get("odno") or ""), None

    signal = Signal(
        stock_code=stock_code,
        stock_name=stock_name,
        action=Action.BUY if action == "BUY" else Action.SELL,
        strength=1.0 if order_type == "market" else 0.7,
        reason="앱 예약주문 실행",
        target_price=price if order_type == "limit" else None,
        quantity=quantity,
    )
    result = OrderExecutor(env_dv="vps").execute_signal(signal)
    if result.empty:
        return False, None, "국내 앱 예약주문 제출 실패"
    row = result.iloc[0].to_dict()
    return True, str(row.get("ODNO") or row.get("odno") or ""), None


async def run_due_reservations() -> dict[str, int]:
    state = await _load_state()
    now = _now_dt()
    due_ids: list[str] = []
    expired = 0
    for item in state.get("reservations", []):
        status = str(item.get("status") or "")
        if status not in ACTIVE_STATUSES:
            continue
        expires = _parse_time(item.get("expires_at"))
        if expires and now >= expires:
            item["status"] = "expired"
            item["updated_at"] = _now()
            item.setdefault("events", []).append(_event("expired"))
            expired += 1
            continue
        retry_at = _parse_time(item.get("next_retry_at"))
        scheduled = _parse_time(item.get("scheduled_at"))
        due_at = retry_at or scheduled
        if due_at and now >= due_at:
            item["status"] = "submitting"
            item["updated_at"] = _now()
            item.setdefault("events", []).append(_event("submit_attempt_started"))
            due_ids.append(str(item.get("id")))
    await _save_state(state)

    submitted = 0
    failed = 0
    for reservation_id in due_ids:
        success, order_no, error = await asyncio.to_thread(_execute_reservation_by_id_sync, reservation_id)
        if success:
            submitted += 1
        elif error:
            failed += 1
    return {"submitted": submitted, "failed": failed, "expired": expired}


def _execute_reservation_by_id_sync(reservation_id: str) -> tuple[bool, str | None, str | None]:
    state = _load_state_sync()
    order = next((item for item in state.get("reservations", []) if str(item.get("id")) == reservation_id), None)
    if not order or order.get("status") != "submitting":
        return False, None, None
    order["attempt_count"] = int(order.get("attempt_count") or 0) + 1
    order["last_attempt_at"] = _now()
    try:
        success, order_no, error = _submit_order_sync(order)
    except Exception as exc:
        success, order_no, error = False, None, str(exc)

    if success:
        order["status"] = "submitted"
        order["submitted_at"] = _now()
        order["submitted_order_no"] = order_no
        order["last_error"] = None
        order["next_retry_at"] = None
        order.setdefault("events", []).append(_event("submitted", order_no=order_no))
        write_order_audit({
            "authenticated_user": "app_reservation_monitor",
            "mode": order.get("env_dv"),
            "action": "app_reservation_execute",
            "stock_code": order.get("stock_code"),
            "quantity": order.get("quantity"),
            "price": order.get("price"),
            "order_type": order.get("order_type"),
            "result": "success",
            "order_id": order_no,
            "error_message": None,
        })
    else:
        order["last_error"] = error or "앱 예약주문 제출 실패"
        retryable = _is_retryable_error(order["last_error"]) and int(order.get("attempt_count") or 0) < MAX_ATTEMPTS
        if retryable:
            order["status"] = "scheduled"
            order["next_retry_at"] = _next_retry_time(int(order.get("attempt_count") or 1))
            event_type = "submit_retry_scheduled"
        else:
            order["status"] = "failed"
            order["next_retry_at"] = None
            event_type = "submit_failed"
        order.setdefault("events", []).append(_event(event_type, error=order["last_error"]))
        write_order_audit({
            "authenticated_user": "app_reservation_monitor",
            "mode": order.get("env_dv"),
            "action": "app_reservation_execute",
            "stock_code": order.get("stock_code"),
            "quantity": order.get("quantity"),
            "price": order.get("price"),
            "order_type": order.get("order_type"),
            "result": "error",
            "order_id": order.get("id"),
            "error_message": order["last_error"],
        })
    order["updated_at"] = _now()
    _save_state_sync(state)
    return bool(success), order_no, error


async def _monitor_loop() -> None:
    while True:
        try:
            await run_due_reservations()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("app reservation monitor cycle failed")
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


async def start_app_reservation_monitor() -> None:
    global _monitor_task
    if get_current_mode() != "vps":
        logger.info("app reservation monitor skipped outside vps mode")
        return
    if _monitor_task is None or _monitor_task.done():
        _monitor_task = asyncio.create_task(_monitor_loop())
        logger.info("app reservation monitor started")


async def stop_app_reservation_monitor() -> None:
    global _monitor_task
    if _monitor_task is None:
        return
    _monitor_task.cancel()
    try:
        await _monitor_task
    except asyncio.CancelledError:
        pass
    _monitor_task = None
