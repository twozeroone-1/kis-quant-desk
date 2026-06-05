"""Overseas stock API router."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from backend import get_current_mode, is_authenticated
from backend.routers.orders import OrderRequest, execute_order
from core import overseas_data_fetcher

logger = logging.getLogger(__name__)
router = APIRouter()

_ORDERABLE_BALANCE_CACHE_TTL = 30
_orderable_balance_cache_lock = threading.Lock()
_orderable_balance_cache: dict[str, object] = {
    "env_dv": None,
    "timestamp": 0.0,
    "data": None,
}


class OverseasOrderRequest(OrderRequest):
    market: str = "us"


class OverseasCancelRequest(BaseModel):
    order_no: str
    stock_code: str
    qty: int
    exchange: Optional[str] = None


def _require_auth() -> None:
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="인증이 필요합니다")


def _get_cached_default_orderable(env_dv: str) -> dict[str, object]:
    mode = overseas_data_fetcher.normalize_env(env_dv)
    now = time.monotonic()
    with _orderable_balance_cache_lock:
        cached = _orderable_balance_cache.get("data")
        if (
            cached is not None
            and _orderable_balance_cache.get("env_dv") == mode
            and now - float(_orderable_balance_cache.get("timestamp") or 0) < _ORDERABLE_BALANCE_CACHE_TTL
        ):
            return dict(cached)

    buyable = overseas_data_fetcher.get_buyable_amount("NVDA", 100, env_dv, "NASD")
    data = {
        "orderable_amount": buyable.get("amount", 0),
        "orderable_reference_symbol": "NVDA",
    }
    with _orderable_balance_cache_lock:
        _orderable_balance_cache.update({"env_dv": mode, "timestamp": time.monotonic(), "data": data})
    return data


@router.get("/search/{symbol}")
async def search_overseas_symbol(symbol: str, exchange: str | None = None):
    _require_auth()
    resolution = overseas_data_fetcher.resolve_exchange(symbol, exchange)
    return {
        "status": "success",
        "data": resolution.as_dict(),
    }


@router.get("/price/{symbol}")
async def get_overseas_price(
    symbol: str,
    exchange: str | None = None,
    env_dv: str = Query("demo", description="환경 구분 (real/demo/prod/vps)"),
):
    _require_auth()
    data = overseas_data_fetcher.get_current_price(symbol, env_dv, exchange)
    if not data:
        return {
            "status": "error",
            "message": f"해외 현재가 조회 실패: {symbol}",
        }
    return {
        "status": "success",
        "data": data,
    }


@router.get("/holdings")
async def get_overseas_holdings():
    _require_auth()
    env_dv = get_current_mode()
    df = overseas_data_fetcher.get_holdings(env_dv)
    return {
        "status": "success",
        "data": [] if df.empty else df.to_dict("records"),
        "logs": [{
            "type": "success",
            "message": "해외 보유종목 조회 완료",
            "timestamp": datetime.now().strftime("%H:%M:%S"),
        }],
    }


@router.get("/balance")
async def get_overseas_balance():
    _require_auth()
    env_dv = get_current_mode()
    data = await asyncio.to_thread(overseas_data_fetcher.get_deposit, env_dv)
    if not data:
        return {
            "status": "error",
            "message": "해외 예수금 정보를 가져올 수 없습니다",
        }
    if (
        float(data.get("total_eval") or 0) <= 0
        and float(data.get("deposit") or 0) <= 0
        and float(data.get("available_amount") or 0) <= 0
    ):
        data.update(await asyncio.to_thread(_get_cached_default_orderable, env_dv))
    else:
        data["orderable_amount"] = data.get("available_amount", 0)
    return {
        "status": "success",
        "data": {
            **data,
            "deposit_formatted": f"${data.get('deposit', 0):,.2f}",
            "total_eval_formatted": f"${data.get('total_eval', 0):,.2f}",
            "orderable_formatted": f"${data.get('orderable_amount', 0):,.2f}",
            "profit_loss_formatted": f"${data.get('profit_loss', 0):+,.2f}",
        },
    }


@router.get("/buyable/{symbol}")
async def get_overseas_buyable(
    symbol: str,
    price: float = 0,
    exchange: str | None = None,
):
    _require_auth()
    env_dv = get_current_mode()
    if price <= 0:
        current = overseas_data_fetcher.get_current_price(symbol, env_dv, exchange)
        price = float(current.get("price") or 0)
    if price <= 0:
        return {
            "status": "error",
            "message": "해외 현재가를 조회할 수 없습니다",
        }
    buyable = overseas_data_fetcher.get_buyable_amount(symbol, price, env_dv, exchange)
    return {
        "status": "success",
        "data": {
            "stock_code": symbol.upper(),
            "price": price,
            "amount": buyable.get("amount", 0),
            "quantity": buyable.get("quantity", 0),
            "amount_formatted": f"${buyable.get('amount', 0):,.2f}",
            "currency": "USD",
        },
    }


@router.get("/pending")
async def get_overseas_pending_orders(exchange: str = "NASD"):
    _require_auth()
    env_dv = get_current_mode()
    df, ok = overseas_data_fetcher.get_pending_orders(env_dv, exchange)
    if not ok:
        return {
            "status": "error",
            "orders": [],
            "total_count": 0,
            "message": "해외 미체결 주문 조회 실패",
        }
    orders = [] if df.empty else df.to_dict("records")
    return {
        "status": "success",
        "orders": orders,
        "total_count": len(orders),
    }


@router.post("/order")
async def execute_overseas_order(request: OverseasOrderRequest, http_request: Request):
    request.market = "us"
    return await execute_order(request, http_request)


@router.post("/cancel")
async def cancel_overseas_order(request: OverseasCancelRequest):
    _require_auth()
    env_dv = get_current_mode()
    result = overseas_data_fetcher.cancel_order(
        order_no=request.order_no,
        symbol=request.stock_code,
        qty=request.qty,
        env_dv=env_dv,
        exchange=request.exchange,
    )
    return {
        "status": "success" if result.get("success") else "error",
        "success": bool(result.get("success")),
        "order_no": request.order_no,
        "message": result.get("message") or "",
    }
