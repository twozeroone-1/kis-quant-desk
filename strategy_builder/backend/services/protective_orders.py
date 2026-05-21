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
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import pandas as pd

from core import data_fetcher, overseas_data_fetcher
from core.data_fetcher import cancel_order, get_holdings, get_pending_orders
from core.order_executor import OrderExecutor
from core.signal import Action, Signal

logger = logging.getLogger(__name__)

STATE_FILE = Path(__file__).resolve().parents[2] / ".runtime" / "protective_orders.json"
MONITOR_INTERVAL_SECONDS = 15

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
        return {"orders": []}
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("orders"), list):
            return data
    except Exception as exc:
        logger.warning("protective order state load failed: %s", exc)
    return {"orders": []}


def _save_state_sync(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


async def _load_state() -> dict[str, Any]:
    async with _state_lock:
        return await asyncio.to_thread(_load_state_sync)


async def _save_state(state: dict[str, Any]) -> None:
    async with _state_lock:
        await asyncio.to_thread(_save_state_sync, state)


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
) -> tuple[Optional[str], Optional[str], bool]:
    if market == "us":
        order_price = float(price or 0)
        if order_price <= 0:
            price_info = overseas_data_fetcher.get_current_price(stock_code, env_dv, exchange)
            order_price = float(price_info.get("price") or 0)
        if order_price <= 0:
            return None, None, False
        result = overseas_data_fetcher.execute_order(
            symbol=stock_code,
            action="SELL",
            quantity=quantity,
            price=round(order_price, 2),
            env_dv=env_dv,
            exchange=exchange,
        )
        if result.empty:
            return None, None, False
        row = result.iloc[0]
        return (
            str(row.get("ODNO", row.get("odno", ""))),
            str(row.get("KRX_FWDG_ORD_ORGNO", row.get("ord_gno_brno", ""))),
            True,
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
        return None, None, False

    row = result.iloc[0]
    return (
        str(row.get("ODNO", "")),
        str(row.get("KRX_FWDG_ORD_ORGNO", "")),
        True,
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
    if _is_order_pending(order.get("take_profit_order_no"), pending):
        return

    order_no, org_no, ok = _submit_exit_order(
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


def _check_order_sync(order: dict[str, Any], env_dv: str) -> dict[str, Any]:
    if order.get("status") != "active":
        return order

    market = str(order.get("market") or "domestic")
    holdings = _get_holding_map(env_dv, market)
    pending = _get_pending_order_map(env_dv, market, order.get("exchange"))
    holding = holdings.get(str(order["stock_code"]))

    if not holding or int(holding.get("quantity") or 0) <= 0:
        order["status"] = "closed"
        order["closed_at"] = _now()
        order.setdefault("events", []).append({"type": "position_closed", "at": _now()})
        return order

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

    stop_loss_price = float(order["stop_loss_price"])
    if order.get("stop_loss_enabled") and current_price <= stop_loss_price:
        _cancel_take_profit(order, env_dv)
        order_no, org_no, ok = _submit_exit_order(
            env_dv=env_dv,
            stock_code=str(order["stock_code"]),
            stock_name=str(order["stock_name"]),
            quantity=int(order["quantity"]),
            order_type="market" if market != "us" else "limit",
            price=current_price if market == "us" else None,
            market=market,
            exchange=order.get("exchange"),
            reason=(
                ("보호주문 손절 지정가 " if market == "us" else "보호주문 손절 시장가 ") +
                f"{order['stop_loss_pct']}%: 현재가 {current_price} <= {stop_loss_price}"
            ),
        )
        if ok:
            order["status"] = "exit_submitted"
            order["exit_order_no"] = order_no
            order["exit_org_no"] = org_no
            order["exit_reason"] = "stop_loss"
            order["closed_at"] = _now()
            order.setdefault("events", []).append({
                "type": "stop_loss_submitted",
                "at": _now(),
                "order_no": order_no,
                "current_price": current_price,
            })
        else:
            order["last_error"] = "stop-loss sell submit failed"

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
        "take_profit_enabled": take_profit_enabled,
        "take_profit_pct": take_profit_pct,
        "take_profit_price": _round_price(market, entry_price * (1 + (take_profit_pct or 0) / 100), "up"),
        "take_profit_status": "not_submitted",
        "take_profit_order_no": None,
        "take_profit_org_no": None,
        "stop_loss_enabled": stop_loss_enabled,
        "stop_loss_pct": stop_loss_pct,
        "stop_loss_price": _round_price(market, entry_price * (1 - (stop_loss_pct or 0) / 100), "down"),
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

    updated = await asyncio.to_thread(_check_order_sync, protection, env_dv)
    state = await _load_state()
    for index, order in enumerate(state.get("orders", [])):
        if order.get("id") == updated["id"]:
            state["orders"][index] = updated
            break
    await _save_state(state)
    return updated


async def run_monitor_cycle() -> None:
    state = await _load_state()
    changed = False
    updated_orders = []

    for order in state.get("orders", []):
        if order.get("status") != "active":
            updated_orders.append(order)
            continue

        try:
            updated = await asyncio.to_thread(_check_order_sync, order, str(order.get("env_dv") or "vps"))
            updated_orders.append(updated)
            changed = changed or updated != order
        except Exception as exc:
            order["last_error"] = str(exc)
            order["last_checked_at"] = _now()
            updated_orders.append(order)
            changed = True
            logger.exception("protective order check failed")

    if changed:
        state["orders"] = updated_orders
        await _save_state(state)


async def _monitor_loop() -> None:
    while True:
        try:
            await run_monitor_cycle()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("protective order monitor cycle failed")
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)


async def start_monitor() -> None:
    global _monitor_task
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


async def list_protective_orders() -> dict[str, Any]:
    return await _load_state()
