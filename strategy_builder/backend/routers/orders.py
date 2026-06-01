"""
주문 실행 API 라우터

Account Information and Pending Orders API:
- GET /account: 통합 계좌 정보 (예수금 + 보유종목)
- GET /pending: 미체결 주문 목록
- POST /cancel: 주문 취소
- POST /account/clear-cache: 캐시 삭제
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from core import data_fetcher, overseas_data_fetcher, reserved_orders
from core.order_executor import OrderExecutor
from core.signal import Action, Signal
from core.data_fetcher import get_deposit, get_holdings, get_pending_orders, cancel_order, clear_balance_cache
from backend import is_authenticated, get_current_mode
from backend.services.audit_log import write_order_audit
from backend.services.protective_orders import (
    list_protective_orders,
    register_after_buy,
    run_monitor_cycle,
    update_monitor_settings,
    upsert_existing_position_protection,
)
from backend.services.realtime_price_stream import get_realtime_price_stream

logging.basicConfig(level=logging.INFO)

router = APIRouter()

# ============================================
# 계좌 정보 캐싱 (30초)
# ============================================

_account_cache: Optional[dict] = None
_account_cache_time: Optional[datetime] = None
CACHE_TTL_SECONDS = 30

# 미체결 주문 캐시 (5초)
_pending_cache: Optional[list] = None
_pending_cache_time: Optional[datetime] = None
PENDING_CACHE_TTL = 5

# Optimistic Update 보호: 주문 직후 추가된 항목이 API 지연으로 사라지지 않도록
_optimistic_order_nos: set[str] = set()
_optimistic_added_at: Optional[datetime] = None
OPTIMISTIC_GRACE_SECONDS = 15


def _get_cached_account(force_refresh: bool = False) -> dict:
    """캐시된 계좌 정보 반환 (30초 TTL)"""
    global _account_cache, _account_cache_time
    
    now = datetime.now()
    
    # 캐시가 유효한지 확인
    if not force_refresh and _account_cache is not None and _account_cache_time is not None:
        elapsed = (now - _account_cache_time).total_seconds()
        if elapsed < CACHE_TTL_SECONDS:
            return _account_cache
    
    # 캐시 갱신
    env_dv = get_current_mode()
    
    try:
        deposit = get_deposit(env_dv)
        holdings_df = get_holdings(env_dv)
        
        holdings = []
        if not holdings_df.empty:
            holdings = holdings_df.to_dict("records")
        
        _account_cache = {
            "deposit": deposit,
            "holdings": holdings,
            "holdings_count": len(holdings),
            "cached_at": now.isoformat(),
        }
        _account_cache_time = now
        
        return _account_cache
        
    except Exception as e:
        logging.error(f"계좌 정보 조회 에러: {e}")
        # 캐시가 있으면 폴백으로 반환
        if _account_cache is not None:
            return {
                **_account_cache,
                "stale": True,
                "error": str(e)
            }
        return {
            "deposit": {},
            "holdings": [],
            "holdings_count": 0,
            "error": str(e)
        }


def _clear_account_cache():
    """계좌 정보 캐시 삭제 (data_fetcher 잔고 캐시도 함께 삭제)

    Note: 미체결 캐시(_pending_cache, _pending_cache_time)는 건드리지 않음.
    주문 직후 Optimistic Update의 TTL 보호를 유지하기 위함.
    """
    global _account_cache, _account_cache_time
    _account_cache = None
    _account_cache_time = None
    clear_balance_cache()


class ProtectiveOrderRequest(BaseModel):
    """매수 후 생성할 앱 레벨 OCO 보호주문"""
    enabled: bool = False
    take_profit_percent: float | None = None
    stop_loss_percent: float | None = None


class ProtectiveReviewRequest(BaseModel):
    """이미 보유 중인 종목에 붙이는 전략 검토 감시 설정"""
    stock_code: str
    stock_name: str
    quantity: int
    entry_price: float
    enabled: bool = True
    take_profit_enabled: bool = True
    take_profit_trigger_price: float | None = None
    take_profit_order_type: str = "limit"
    take_profit_limit_price: float | None = None
    stop_loss_enabled: bool = True
    stop_loss_trigger_price: float | None = None
    stop_loss_order_type: str = "market"
    stop_loss_limit_price: float | None = None
    market: str = "domestic"
    exchange: str | None = None
    currency: str = "KRW"
    confirm_prod: bool = False


class ProtectiveSettingsRequest(BaseModel):
    """전략 검토 감시 서비스 설정"""
    monitor_interval_seconds: int


class OrderRequest(BaseModel):
    """주문 요청"""
    stock_code: str
    stock_name: str
    action: str  # "BUY" or "SELL"
    order_type: str  # "limit" or "market"
    price: float
    quantity: int
    signal_reason: str
    protective_order: ProtectiveOrderRequest | None = None
    market: str = "domestic"
    exchange: str | None = None
    confirm_prod: bool = False


class LogEntry(BaseModel):
    """로그 항목"""
    type: str  # "info", "success", "error", "warning"
    message: str
    timestamp: str


class OrderResponse(BaseModel):
    """주문 응답"""
    status: str
    message: str = ""
    data: dict | None = None
    logs: list[LogEntry] = []


@router.post("/execute", response_model=OrderResponse)
async def execute_order(request: OrderRequest, http_request: Request):
    """
    주문 실행

    Args:
        request: 주문 요청 (OrderRequest)

    Returns:
        OrderResponse with:
        - status: "success" | "error"
        - message: 결과 메시지
        - data: 주문 결과 데이터 (선택)
        - logs: 실행 로그 목록
    """
    logs = []

    def add_log(log_type: str, message: str):
        """로그 추가"""
        logs.append({
            "type": log_type,
            "message": message,
            "timestamp": datetime.now().strftime("%H:%M:%S")
        })

    authenticated_user = http_request.headers.get("X-Authenticated-User", "unknown")

    try:
        # 1. 인증 확인
        if not is_authenticated():
            add_log("error", "인증이 필요합니다")
            response = OrderResponse(
                status="error",
                message="인증이 필요합니다",
                logs=logs
            )
            write_order_audit({
                "authenticated_user": authenticated_user,
                "mode": get_current_mode(),
                "action": "execute",
                "stock_code": request.stock_code,
                "quantity": request.quantity,
                "price": request.price,
                "order_type": request.order_type,
                "result": "error",
                "order_id": None,
                "error_message": response.message,
            })
            return response

        add_log("info", f"주문 검증 중: {request.stock_name}")

        # 2. 입력 검증
        if request.quantity <= 0:
            add_log("error", "주문 수량이 올바르지 않습니다")
            response = OrderResponse(
                status="error",
                message="주문 수량이 올바르지 않습니다",
                logs=logs
            )
            write_order_audit({
                "authenticated_user": authenticated_user,
                "mode": get_current_mode(),
                "action": "execute",
                "stock_code": request.stock_code,
                "quantity": request.quantity,
                "price": request.price,
                "order_type": request.order_type,
                "result": "error",
                "order_id": None,
                "error_message": response.message,
            })
            return response

        if request.action not in ["BUY", "SELL"]:
            add_log("error", "주문 구분이 올바르지 않습니다")
            response = OrderResponse(
                status="error",
                message="주문 구분이 올바르지 않습니다",
                logs=logs
            )
            write_order_audit({
                "authenticated_user": authenticated_user,
                "mode": get_current_mode(),
                "action": "execute",
                "stock_code": request.stock_code,
                "quantity": request.quantity,
                "price": request.price,
                "order_type": request.order_type,
                "result": "error",
                "order_id": None,
                "error_message": response.message,
            })
            return response

        if request.order_type not in ["limit", "market"]:
            add_log("error", "주문 유형이 올바르지 않습니다")
            response = OrderResponse(
                status="error",
                message="주문 유형이 올바르지 않습니다",
                logs=logs
            )
            write_order_audit({
                "authenticated_user": authenticated_user,
                "mode": get_current_mode(),
                "action": "execute",
                "stock_code": request.stock_code,
                "quantity": request.quantity,
                "price": request.price,
                "order_type": request.order_type,
                "result": "error",
                "order_id": None,
                "error_message": response.message,
            })
            return response

        add_log("success", "주문 검증 완료")

        env_dv = get_current_mode()

        if env_dv in ("prod", "real") and not request.confirm_prod:
            add_log("error", "실전 주문은 confirm_prod=true 확인이 필요합니다")
            write_order_audit({
                "authenticated_user": authenticated_user,
                "mode": env_dv,
                "action": "execute",
                "stock_code": request.stock_code,
                "quantity": request.quantity,
                "price": request.price,
                "order_type": request.order_type,
                "result": "error",
                "order_id": None,
                "error_message": "confirm_prod required",
            })
            raise HTTPException(
                status_code=400,
                detail="실전 주문은 confirm_prod=true 확인이 필요합니다",
            )

        if request.market == "us":
            if request.order_type != "limit":
                add_log("error", "미국 주식 주문은 지정가만 지원합니다")
                response = OrderResponse(
                    status="error",
                    message="미국 주식 주문은 지정가만 지원합니다",
                    logs=logs,
                )
                write_order_audit({
                    "authenticated_user": authenticated_user,
                    "mode": env_dv,
                    "action": "execute",
                    "stock_code": request.stock_code,
                    "quantity": request.quantity,
                    "price": request.price,
                    "order_type": request.order_type,
                    "result": "error",
                    "order_id": None,
                    "error_message": response.message,
                })
                return response

            if request.price <= 0:
                add_log("error", "미국 주식 지정가가 올바르지 않습니다")
                response = OrderResponse(
                    status="error",
                    message="미국 주식 지정가가 올바르지 않습니다",
                    logs=logs,
                )
                write_order_audit({
                    "authenticated_user": authenticated_user,
                    "mode": env_dv,
                    "action": "execute",
                    "stock_code": request.stock_code,
                    "quantity": request.quantity,
                    "price": request.price,
                    "order_type": request.order_type,
                    "result": "error",
                    "order_id": None,
                    "error_message": response.message,
                })
                return response

            if env_dv in ("prod", "real") and not request.confirm_prod:
                add_log("error", "실전 해외주식 주문은 confirm_prod=true 확인이 필요합니다")
                write_order_audit({
                    "authenticated_user": authenticated_user,
                    "mode": env_dv,
                    "action": "execute",
                    "stock_code": request.stock_code,
                    "quantity": request.quantity,
                    "price": request.price,
                    "order_type": request.order_type,
                    "result": "error",
                    "order_id": None,
                    "error_message": "confirm_prod required",
                })
                raise HTTPException(
                    status_code=400,
                    detail="실전 해외주식 주문은 confirm_prod=true 확인이 필요합니다",
                )

            symbol = request.stock_code.strip().upper()
            resolution = overseas_data_fetcher.resolve_exchange(symbol, request.exchange)
            add_log("info", f"해외주식 지정가 주문 실행 중: {request.action} {symbol} {request.quantity}주 @ ${request.price:g}")

            if request.action == "SELL":
                holdings_df = overseas_data_fetcher.get_holdings(env_dv)
                if holdings_df.empty or symbol not in set(holdings_df["stock_code"].astype(str)):
                    add_log("error", f"미보유 종목입니다. {request.stock_name}을(를) 보유하고 있지 않습니다.")
                    return OrderResponse(
                        status="error",
                        message=f"미보유 종목입니다. {request.stock_name}을(를) 보유하고 있지 않습니다.",
                        logs=logs,
                    )

            order_result = overseas_data_fetcher.submit_order(
                symbol=symbol,
                action=request.action,
                quantity=request.quantity,
                price=request.price,
                env_dv=env_dv,
                exchange=resolution.exchange,
            )

            if not order_result.success or order_result.dataframe.empty:
                error_message = f"해외주식 주문 실행 실패: {order_result.display_error()}"
                add_log("error", error_message)
                response = OrderResponse(
                    status="error",
                    message=error_message,
                    logs=logs,
                )
                write_order_audit({
                    "authenticated_user": authenticated_user,
                    "mode": env_dv,
                    "action": "execute",
                    "stock_code": symbol,
                    "quantity": request.quantity,
                    "price": request.price,
                    "order_type": request.order_type,
                    "result": "error",
                    "order_id": None,
                    "error_message": response.message,
                })
                return response

            result = order_result.dataframe
            order_id = str(result.iloc[0].get("ODNO", result.iloc[0].get("odno", "")))
            order_data = {
                "order_id": order_id,
                "status": "submitted",
                "message": "해외주식 주문이 접수되었습니다",
                "exchange": resolution.exchange,
                "currency": "USD",
            }

            if request.action == "BUY" and request.protective_order and request.protective_order.enabled:
                try:
                    protective = await register_after_buy(
                        env_dv=env_dv,
                        stock_code=symbol,
                        stock_name=request.stock_name,
                        quantity=request.quantity,
                        entry_price=request.price,
                        take_profit_pct=request.protective_order.take_profit_percent,
                        stop_loss_pct=request.protective_order.stop_loss_percent,
                        source_order_no=order_id,
                        market="us",
                        exchange=resolution.exchange,
                        currency="USD",
                    )
                    order_data["protective_order"] = protective
                    add_log("success", "해외주식 보호주문 생성 완료")
                except Exception as protective_error:
                    order_data["protective_order_error"] = str(protective_error)
                    add_log("warning", f"보호주문 생성 실패: {protective_error}")

            overseas_data_fetcher.clear_balance_cache()
            add_log("success", f"해외주식 주문 실행 성공 (주문번호: {order_id})")
            response = OrderResponse(
                status="success",
                message="해외주식 주문이 접수되었습니다",
                data=order_data,
                logs=logs,
            )
            write_order_audit({
                "authenticated_user": authenticated_user,
                "mode": env_dv,
                "action": "execute",
                "stock_code": symbol,
                "quantity": request.quantity,
                "price": request.price,
                "order_type": request.order_type,
                "result": "success",
                "order_id": order_id,
                "error_message": None,
            })
            return response

        # 3. Signal 객체 생성
        # 지정가 주문: strength < 0.8 → order_executor가 target_price 사용
        # 시장가 주문: strength >= 0.8 → order_executor가 시장가 사용
        is_limit_order = request.order_type == "limit"
        signal = Signal(
            stock_code=request.stock_code,
            stock_name=request.stock_name,
            action=Action.BUY if request.action == "BUY" else Action.SELL,
            strength=0.7 if is_limit_order else 1.0,  # 지정가면 0.7, 시장가면 1.0
            reason=request.signal_reason,
            target_price=request.price if is_limit_order else None,
            quantity=request.quantity
        )

        # 주문 유형 로깅
        order_type_display = "지정가" if is_limit_order else "시장가"
        price_display = f"{request.price:,}원" if is_limit_order else "시장가"
        add_log("info", f"주문 실행 중: {request.action} {request.quantity}주 @ {price_display} ({order_type_display})")
        logging.info(f"[주문] order_type={request.order_type}, price={request.price}, strength={signal.strength}, target_price={signal.target_price}")

        # 4. OrderExecutor 호출 (현재 트레이딩 모드 사용)
        executor = OrderExecutor(env_dv=env_dv)
        result = executor.execute_signal(signal)

        # 5. 결과 처리
        if result.empty:
            error_reason = "주문 실행 실패"

            if request.action == "SELL":
                quantity = executor.position_manager.get_holding_quantity(request.stock_code)
                if quantity <= 0:
                    error_reason = f"미보유 종목입니다. {request.stock_name}을(를) 보유하고 있지 않습니다."
            elif request.action == "BUY":
                error_reason = "주문이 거부되었습니다. 서버 로그를 확인하세요."

            add_log("error", error_reason)

            response = OrderResponse(
                status="error",
                message=error_reason,
                logs=logs
            )
            write_order_audit({
                "authenticated_user": authenticated_user,
                "mode": env_dv,
                "action": "execute",
                "stock_code": request.stock_code,
                "quantity": request.quantity,
                "price": request.price,
                "order_type": request.order_type,
                "result": "error",
                "order_id": None,
                "error_message": response.message,
            })
            return response

        # 주문 성공 (POST API 응답은 대문자 필드명: ODNO, ORD_TMD, KRX_FWDG_ORD_ORGNO)
        order_id = result.iloc[0].get("ODNO", "")
        order_time = result.iloc[0].get("ORD_TMD", datetime.now().strftime("%H%M%S"))
        order_org_no = result.iloc[0].get("KRX_FWDG_ORD_ORGNO", "")

        order_data = {
            "order_id": order_id,
            "status": "submitted",
            "message": "주문이 접수되었습니다"
        }

        add_log("success", f"주문 실행 성공 (주문번호: {order_id})")

        if request.action == "BUY" and request.protective_order and request.protective_order.enabled:
            entry_price = request.price
            if request.order_type == "market" or entry_price <= 0:
                try:
                    price_data = data_fetcher.get_current_price(request.stock_code, env_dv)
                    entry_price = int(price_data.get("price") or 0)
                except Exception:
                    entry_price = 0

            if entry_price > 0:
                try:
                    protective = await register_after_buy(
                        env_dv=env_dv,
                        stock_code=request.stock_code,
                        stock_name=request.stock_name,
                        quantity=request.quantity,
                        entry_price=entry_price,
                        take_profit_pct=request.protective_order.take_profit_percent,
                        stop_loss_pct=request.protective_order.stop_loss_percent,
                        source_order_no=order_id,
                    )
                    order_data["protective_order"] = protective
                    add_log("success", "보호주문 생성 완료")
                except Exception as protective_error:
                    order_data["protective_order_error"] = str(protective_error)
                    add_log("warning", f"보호주문 생성 실패: {protective_error}")
            else:
                order_data["protective_order_error"] = "진입 가격을 확인할 수 없습니다"
                add_log("warning", "보호주문 생성 실패: 진입 가격 확인 불가")

        # 미체결 캐시에 직접 추가 (Optimistic Update)
        global _pending_cache, _pending_cache_time, _optimistic_order_nos, _optimistic_added_at
        if _pending_cache is None:
            _pending_cache = []

        new_pending = PendingOrderItem(
            order_no=str(order_id),
            org_no=str(order_org_no),
            stock_code=request.stock_code,
            stock_name=request.stock_name,
            order_type="매수" if request.action == "BUY" else "매도",
            order_qty=request.quantity,
            order_price=request.price,
            filled_qty=0,
            unfilled_qty=request.quantity,
            order_time=order_time
        )
        _pending_cache.append(new_pending)
        _pending_cache_time = datetime.now()
        _optimistic_order_nos.add(str(order_id))
        _optimistic_added_at = datetime.now()
        logging.info(f"미체결 캐시에 추가: {request.stock_name} {request.action} {request.quantity}주 (TTL {PENDING_CACHE_TTL}초 + Grace {OPTIMISTIC_GRACE_SECONDS}초)")

        # 계좌 캐시 클리어 (잔고 갱신 위해, 미체결 캐시는 유지)
        _clear_account_cache()

        response = OrderResponse(
            status="success",
            message="주문이 접수되었습니다",
            data=order_data,
            logs=logs
        )
        write_order_audit({
            "authenticated_user": authenticated_user,
            "mode": env_dv,
            "action": "execute",
            "stock_code": request.stock_code,
            "quantity": request.quantity,
            "price": request.price,
            "order_type": request.order_type,
            "result": "success",
            "order_id": order_id,
            "error_message": None,
        })
        return response

    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"주문 실행 에러: {e}")
        add_log("error", f"주문 실행 에러: {str(e)}")
        response = OrderResponse(
            status="error",
            message=str(e),
            logs=logs
        )
        write_order_audit({
            "authenticated_user": authenticated_user,
            "mode": get_current_mode(),
            "action": "execute",
            "stock_code": request.stock_code,
            "quantity": request.quantity,
            "price": request.price,
            "order_type": request.order_type,
            "result": "error",
            "order_id": None,
            "error_message": response.message,
        })
        return response


# ============================================
# 계좌 정보 API
# ============================================


class AccountResponse(BaseModel):
    """계좌 정보 응답"""
    status: str
    deposit: dict
    holdings: list
    holdings_count: int
    cached_at: str | None = None
    stale: bool = False
    error: str | None = None


class PendingOrderItem(BaseModel):
    """미체결 주문 항목"""
    order_no: str
    org_no: str = ""
    stock_code: str
    stock_name: str
    order_type: str
    order_qty: int
    order_price: int
    filled_qty: int
    unfilled_qty: int
    order_time: str


class PendingOrdersResponse(BaseModel):
    """미체결 주문 응답"""
    status: str
    orders: list[PendingOrderItem]
    total_count: int


class CancelOrderRequest(BaseModel):
    """주문 취소 요청"""
    order_no: str
    org_no: str = ""
    stock_code: str
    qty: int


class CancelOrderResponse(BaseModel):
    """주문 취소 응답"""
    status: str
    success: bool
    order_no: str
    message: str


class ReservationSubmitRequest(BaseModel):
    """브로커 예약주문 접수 요청"""
    market: str = "domestic"
    stock_code: str
    stock_name: str = ""
    action: str
    quantity: int
    price: float = 0
    order_type: str = "limit"
    exchange: str | None = None
    end_date: str | None = None
    confirm_prod: bool = False


class ReservationCancelRequest(BaseModel):
    """브로커 예약주문 취소 요청"""
    market: str = "domestic"
    reservation_order_no: str
    reservation_order_date: str
    reservation_order_org_no: str = ""
    confirm_prod: bool = False


class ReservationModifyRequest(ReservationSubmitRequest):
    """국내 예약주문 정정 요청"""
    reservation_order_no: str
    reservation_order_date: str
    reservation_order_org_no: str = ""


def _require_prod_confirmation(confirm_prod: bool) -> None:
    env_dv = get_current_mode()
    if env_dv in ("prod", "real") and not confirm_prod:
        raise HTTPException(status_code=400, detail="실전 예약주문은 confirm_prod=true 확인이 필요합니다")


def _validate_reservation_submit(request: ReservationSubmitRequest) -> None:
    if request.market not in {"domestic", "us"}:
        raise HTTPException(status_code=400, detail="시장 구분이 올바르지 않습니다")
    if request.action not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="예약주문 방향이 올바르지 않습니다")
    if request.quantity <= 0:
        raise HTTPException(status_code=400, detail="예약주문 수량이 올바르지 않습니다")
    if request.market == "domestic":
        if request.order_type not in {"limit", "market", "preopen"}:
            raise HTTPException(status_code=400, detail="국내 예약주문 방식이 올바르지 않습니다")
        if request.order_type == "limit" and request.price <= 0:
            raise HTTPException(status_code=400, detail="국내 지정가 예약주문 가격이 필요합니다")
    else:
        if request.order_type not in {"limit", "moo"}:
            raise HTTPException(status_code=400, detail="미국 예약주문은 지정가 또는 매도 MOO만 지원합니다")
        if request.order_type == "moo" and request.action != "SELL":
            raise HTTPException(status_code=400, detail="MOO 예약주문은 미국 매도에만 사용할 수 있습니다")
        if request.order_type == "limit" and request.price <= 0:
            raise HTTPException(status_code=400, detail="미국 지정가 예약주문 가격이 필요합니다")


def _assert_sellable_for_reservation(request: ReservationSubmitRequest, env_dv: str) -> None:
    if request.action != "SELL":
        return

    if request.market == "us":
        holdings_df = overseas_data_fetcher.get_holdings(env_dv)
        symbol = request.stock_code.strip().upper()
        if holdings_df.empty:
            raise HTTPException(status_code=400, detail="미국 보유수량을 확인할 수 없어 예약매도를 차단했습니다")
        row = holdings_df[holdings_df["stock_code"].astype(str) == symbol]
    else:
        holdings_df = get_holdings(env_dv)
        if holdings_df.empty:
            raise HTTPException(status_code=400, detail="국내 보유수량을 확인할 수 없어 예약매도를 차단했습니다")
        row = holdings_df[holdings_df["stock_code"].astype(str) == request.stock_code.strip()]

    if row.empty:
        raise HTTPException(status_code=400, detail="미보유 종목은 예약매도할 수 없습니다")
    holding_qty = int(float(row.iloc[0].get("quantity") or 0))
    if holding_qty < request.quantity:
        raise HTTPException(status_code=400, detail=f"예약매도 수량이 보유수량({holding_qty})을 초과합니다")


def _reservation_error_response(result: reserved_orders.ReservedOrderResult) -> dict:
    return {
        "status": "error",
        "message": result.display_error(),
        "data": {
            "api_url": result.api_url,
            "tr_id": result.tr_id,
            "error_code": result.error_code,
        },
    }


def _normalize_reservation_submit_request(request: ReservationSubmitRequest) -> ReservationSubmitRequest:
    request.market = request.market.strip().lower()
    request.action = request.action.strip().upper()
    request.order_type = request.order_type.strip().lower()
    request.stock_code = request.stock_code.strip().upper() if request.market == "us" else request.stock_code.strip()
    request.stock_name = request.stock_name.strip()
    request.exchange = request.exchange.strip().upper() if request.exchange else None
    request.end_date = _compact_date(request.end_date, "") or None
    return request


def _compact_date(value: str | None, default: str) -> str:
    compact = (value or default).strip().replace("-", "")
    if compact and (len(compact) != 8 or not compact.isdigit()):
        raise HTTPException(status_code=400, detail="날짜는 YYYYMMDD 또는 YYYY-MM-DD 형식이어야 합니다")
    return compact


def _reservation_success_data(
    *,
    result: reserved_orders.ReservedOrderResult,
    request: ReservationSubmitRequest | ReservationCancelRequest,
    action_name: str,
) -> dict:
    data = reserved_orders.first_normalized_record(result)
    return {
        **data,
        "market": request.market,
        "action": getattr(request, "action", action_name),
        "stock_code": getattr(request, "stock_code", ""),
        "quantity": getattr(request, "quantity", None),
        "price": getattr(request, "price", None),
        "order_type": getattr(request, "order_type", None),
        "api_url": result.api_url,
        "tr_id": result.tr_id,
        "records": result.records(),
    }


@router.post("/reservations")
async def submit_reservation_order_api(request: ReservationSubmitRequest, http_request: Request):
    """브로커 예약주문 접수."""
    authenticated_user = http_request.headers.get("X-Authenticated-User", "unknown")
    request = _normalize_reservation_submit_request(request)

    if not is_authenticated():
        write_order_audit({
            "authenticated_user": authenticated_user,
            "mode": get_current_mode(),
            "action": "reserve_execute",
            "stock_code": request.stock_code,
            "quantity": request.quantity,
            "price": request.price,
            "order_type": request.order_type,
            "result": "error",
            "order_id": None,
            "error_message": "인증이 필요합니다",
        })
        raise HTTPException(status_code=401, detail="인증이 필요합니다")

    _validate_reservation_submit(request)
    _require_prod_confirmation(request.confirm_prod)

    env_dv = get_current_mode()
    _assert_sellable_for_reservation(request, env_dv)

    if request.market == "us":
        result = reserved_orders.submit_us_reservation(
            symbol=request.stock_code,
            action=request.action,
            quantity=request.quantity,
            price=request.price,
            order_type=request.order_type,
            env_dv=env_dv,
            exchange=request.exchange,
        )
    else:
        result = reserved_orders.submit_domestic_reservation(
            stock_code=request.stock_code,
            action=request.action,
            quantity=request.quantity,
            price=request.price,
            order_type=request.order_type,
            env_dv=env_dv,
            end_date=request.end_date,
        )

    if not result.success:
        write_order_audit({
            "authenticated_user": authenticated_user,
            "mode": env_dv,
            "action": "reserve_execute",
            "stock_code": request.stock_code,
            "quantity": request.quantity,
            "price": request.price,
            "order_type": request.order_type,
            "result": "error",
            "order_id": None,
            "error_message": result.display_error(),
        })
        return _reservation_error_response(result)

    data = _reservation_success_data(result=result, request=request, action_name="submit")
    write_order_audit({
        "authenticated_user": authenticated_user,
        "mode": env_dv,
        "action": "reserve_execute",
        "stock_code": request.stock_code,
        "quantity": request.quantity,
        "price": request.price,
        "order_type": request.order_type,
        "result": "success",
        "order_id": data.get("reservation_order_no"),
        "error_message": None,
    })
    return {
        "status": "success",
        "message": "예약주문이 접수되었습니다",
        "data": data,
    }


@router.get("/reservations")
async def get_reservation_orders_api(
    market: str = "domestic",
    start_date: str | None = None,
    end_date: str | None = None,
    stock_code: str = "",
    action: str = "",
    exchange: str = "NASD",
    include_cancelled: bool = True,
):
    """브로커 예약주문 목록 조회."""
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="인증이 필요합니다")

    normalized_market = market.strip().lower()
    if normalized_market not in {"domestic", "us"}:
        raise HTTPException(status_code=400, detail="시장 구분이 올바르지 않습니다")

    today = datetime.now().strftime("%Y%m%d")
    default_start = (datetime.now() - timedelta(days=30)).strftime("%Y%m%d")
    start = _compact_date(start_date, default_start)
    end = _compact_date(end_date, today)
    normalized_action = action.strip().upper()
    if normalized_action and normalized_action not in {"BUY", "SELL"}:
        raise HTTPException(status_code=400, detail="예약주문 방향이 올바르지 않습니다")

    if normalized_market == "us":
        result = reserved_orders.list_us_reservations(
            start_date=start,
            end_date=end,
            exchange=exchange.strip().upper() or "NASD",
        )
    else:
        result = reserved_orders.list_domestic_reservations(
            start_date=start,
            end_date=end,
            stock_code=stock_code.strip(),
            action=normalized_action,
            include_cancelled=include_cancelled,
        )

    if not result.success:
        return {
            **_reservation_error_response(result),
            "orders": [],
            "total_count": 0,
        }

    orders = result.records()
    return {
        "status": "success",
        "orders": orders,
        "total_count": len(orders),
        "market": normalized_market,
        "start_date": start,
        "end_date": end,
    }


@router.post("/reservations/cancel")
async def cancel_reservation_order_api(request: ReservationCancelRequest, http_request: Request):
    """브로커 예약주문 취소."""
    authenticated_user = http_request.headers.get("X-Authenticated-User", "unknown")
    request.market = request.market.strip().lower()
    request.reservation_order_no = request.reservation_order_no.strip()
    request.reservation_order_date = _compact_date(request.reservation_order_date, "")
    request.reservation_order_org_no = request.reservation_order_org_no.strip()

    if not is_authenticated():
        write_order_audit({
            "authenticated_user": authenticated_user,
            "mode": get_current_mode(),
            "action": "reserve_cancel",
            "stock_code": "",
            "quantity": None,
            "price": None,
            "order_type": None,
            "result": "error",
            "order_id": request.reservation_order_no,
            "error_message": "인증이 필요합니다",
        })
        raise HTTPException(status_code=401, detail="인증이 필요합니다")
    if request.market not in {"domestic", "us"}:
        raise HTTPException(status_code=400, detail="시장 구분이 올바르지 않습니다")
    if not request.reservation_order_no or not request.reservation_order_date:
        raise HTTPException(status_code=400, detail="예약주문번호와 주문일자가 필요합니다")

    _require_prod_confirmation(request.confirm_prod)
    env_dv = get_current_mode()

    if request.market == "us":
        result = reserved_orders.cancel_us_reservation(
            reservation_order_date=request.reservation_order_date,
            reservation_order_no=request.reservation_order_no,
            env_dv=env_dv,
        )
    else:
        if not request.reservation_order_org_no:
            raise HTTPException(status_code=400, detail="국내 예약주문 취소에는 예약주문조직번호가 필요합니다")
        result = reserved_orders.cancel_domestic_reservation(
            reservation_order_no=request.reservation_order_no,
            reservation_order_org_no=request.reservation_order_org_no,
            reservation_order_date=request.reservation_order_date,
        )

    if not result.success:
        write_order_audit({
            "authenticated_user": authenticated_user,
            "mode": env_dv,
            "action": "reserve_cancel",
            "stock_code": "",
            "quantity": None,
            "price": None,
            "order_type": None,
            "result": "error",
            "order_id": request.reservation_order_no,
            "error_message": result.display_error(),
        })
        return _reservation_error_response(result)

    data = _reservation_success_data(result=result, request=request, action_name="cancel")
    write_order_audit({
        "authenticated_user": authenticated_user,
        "mode": env_dv,
        "action": "reserve_cancel",
        "stock_code": "",
        "quantity": None,
        "price": None,
        "order_type": None,
        "result": "success",
        "order_id": request.reservation_order_no,
        "error_message": None,
    })
    return {
        "status": "success",
        "message": "예약주문 취소가 접수되었습니다",
        "data": data,
    }


@router.post("/reservations/modify")
async def modify_reservation_order_api(request: ReservationModifyRequest, http_request: Request):
    """국내 브로커 예약주문 정정."""
    authenticated_user = http_request.headers.get("X-Authenticated-User", "unknown")
    request = _normalize_reservation_submit_request(request)
    request.reservation_order_no = request.reservation_order_no.strip()
    request.reservation_order_date = _compact_date(request.reservation_order_date, "")
    request.reservation_order_org_no = request.reservation_order_org_no.strip()

    if not is_authenticated():
        write_order_audit({
            "authenticated_user": authenticated_user,
            "mode": get_current_mode(),
            "action": "reserve_modify",
            "stock_code": request.stock_code,
            "quantity": request.quantity,
            "price": request.price,
            "order_type": request.order_type,
            "result": "error",
            "order_id": request.reservation_order_no,
            "error_message": "인증이 필요합니다",
        })
        raise HTTPException(status_code=401, detail="인증이 필요합니다")

    _validate_reservation_submit(request)
    _require_prod_confirmation(request.confirm_prod)
    if request.market != "domestic":
        raise HTTPException(status_code=400, detail="미국 예약주문 정정은 KIS API에서 별도 지원되지 않습니다. 취소 후 재접수하세요")
    if not request.reservation_order_no or not request.reservation_order_org_no or not request.reservation_order_date:
        raise HTTPException(status_code=400, detail="예약주문번호, 조직번호, 주문일자가 필요합니다")

    env_dv = get_current_mode()
    _assert_sellable_for_reservation(request, env_dv)
    result = reserved_orders.modify_domestic_reservation(
        reservation_order_no=request.reservation_order_no,
        reservation_order_org_no=request.reservation_order_org_no,
        reservation_order_date=request.reservation_order_date,
        stock_code=request.stock_code,
        action=request.action,
        quantity=request.quantity,
        price=request.price,
        order_type=request.order_type,
        end_date=request.end_date,
    )

    if not result.success:
        write_order_audit({
            "authenticated_user": authenticated_user,
            "mode": env_dv,
            "action": "reserve_modify",
            "stock_code": request.stock_code,
            "quantity": request.quantity,
            "price": request.price,
            "order_type": request.order_type,
            "result": "error",
            "order_id": request.reservation_order_no,
            "error_message": result.display_error(),
        })
        return _reservation_error_response(result)

    data = _reservation_success_data(result=result, request=request, action_name="modify")
    write_order_audit({
        "authenticated_user": authenticated_user,
        "mode": env_dv,
        "action": "reserve_modify",
        "stock_code": request.stock_code,
        "quantity": request.quantity,
        "price": request.price,
        "order_type": request.order_type,
        "result": "success",
        "order_id": request.reservation_order_no,
        "error_message": None,
    })
    return {
        "status": "success",
        "message": "예약주문 정정이 접수되었습니다",
        "data": data,
    }


@router.get("/protective")
async def get_protective_orders_api():
    """앱 레벨 OCO 보호주문 상태 조회"""
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="인증이 필요합니다")
    state = await list_protective_orders()
    return {
        "status": "success",
        "orders": state.get("orders", []),
        "total_count": len(state.get("orders", [])),
        "settings": state.get("settings", {}),
        "realtime": get_realtime_price_stream().status(),
    }


@router.post("/protective/settings")
async def update_protective_settings_api(request: ProtectiveSettingsRequest):
    """보호주문 감시 서비스 설정 저장"""
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="인증이 필요합니다")
    settings = await update_monitor_settings(
        monitor_interval_seconds=request.monitor_interval_seconds,
    )
    return {
        "status": "success",
        "settings": settings,
    }


@router.post("/protective")
async def upsert_protective_order_api(request: ProtectiveReviewRequest):
    """보유 종목의 손익절 감시 설정을 저장합니다."""
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="인증이 필요합니다")
    if get_current_mode() in ("prod", "real") and not request.confirm_prod:
        raise HTTPException(status_code=400, detail="실전 보호주문은 confirm_prod=true 확인이 필요합니다")
    if request.quantity <= 0:
        raise HTTPException(status_code=400, detail="감시 수량이 올바르지 않습니다")
    if request.entry_price <= 0:
        raise HTTPException(status_code=400, detail="기준 단가가 올바르지 않습니다")
    if request.take_profit_order_type not in {"market", "limit"}:
        raise HTTPException(status_code=400, detail="익절 주문 방식이 올바르지 않습니다")
    if request.stop_loss_order_type not in {"market", "limit"}:
        raise HTTPException(status_code=400, detail="손절 주문 방식이 올바르지 않습니다")
    if request.market not in {"domestic", "us"}:
        raise HTTPException(status_code=400, detail="시장 구분이 올바르지 않습니다")

    try:
        protection = await upsert_existing_position_protection(
            env_dv=get_current_mode(),
            stock_code=request.stock_code.strip(),
            stock_name=request.stock_name.strip(),
            quantity=request.quantity,
            entry_price=request.entry_price,
            enabled=request.enabled,
            take_profit_enabled=request.take_profit_enabled,
            take_profit_trigger_price=request.take_profit_trigger_price,
            take_profit_order_type=request.take_profit_order_type,
            take_profit_limit_price=request.take_profit_limit_price,
            stop_loss_enabled=request.stop_loss_enabled,
            stop_loss_trigger_price=request.stop_loss_trigger_price,
            stop_loss_order_type=request.stop_loss_order_type,
            stop_loss_limit_price=request.stop_loss_limit_price,
            market=request.market,
            exchange=request.exchange,
            currency=request.currency,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "status": "success",
        "order": protection,
    }


@router.post("/protective/check")
async def check_protective_orders_api():
    """보호주문 감시 루프를 즉시 1회 실행"""
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="인증이 필요합니다")
    await run_monitor_cycle()
    state = await list_protective_orders()
    return {
        "status": "success",
        "orders": state.get("orders", []),
        "total_count": len(state.get("orders", [])),
        "settings": state.get("settings", {}),
        "realtime": get_realtime_price_stream().status(),
    }


@router.websocket("/protective/prices/ws")
async def protective_prices_ws(websocket: WebSocket):
    """전략 검토 화면용 실시간 체결가 스트림."""
    await websocket.accept()
    if not is_authenticated():
        await websocket.send_json({"type": "error", "message": "인증이 필요합니다"})
        await websocket.close(code=1008)
        return

    stream = get_realtime_price_stream()
    await stream.start()
    try:
        while True:
            raw = await websocket.receive_json()
            if raw.get("type") == "subscribe":
                await stream.add_client(websocket, raw.get("symbols") or [])
                await websocket.send_json({
                    "type": "status",
                    "realtime": stream.status(),
                })
    except WebSocketDisconnect:
        pass
    finally:
        await stream.remove_client(websocket)


@router.get("/account", response_model=AccountResponse)
async def get_account_info():
    """통합 계좌 정보 조회
    
    예수금과 보유종목 정보를 통합하여 반환합니다.
    30초 캐싱이 적용되어 있습니다.
    
    Returns:
        - deposit: 예수금 정보 (deposit, total_eval, purchase_amount, eval_amount, profit_loss)
        - holdings: 보유종목 목록
        - holdings_count: 보유종목 수
        - cached_at: 캐시 시간
        - stale: 캐시된 데이터 사용 여부 (API 실패 시 true)
    """
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="인증이 필요합니다")
    
    account = _get_cached_account()
    
    return AccountResponse(
        status="success",
        deposit=account.get("deposit", {}),
        holdings=account.get("holdings", []),
        holdings_count=account.get("holdings_count", 0),
        cached_at=account.get("cached_at"),
        stale=account.get("stale", False),
        error=account.get("error")
    )


@router.post("/account/clear-cache")
async def clear_account_cache():
    """계좌 정보 캐시 삭제
    
    캐시를 삭제하여 다음 조회 시 최신 데이터를 가져옵니다.
    """
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="인증이 필요합니다")
    
    _clear_account_cache()
    
    return {
        "status": "success",
        "message": "캐시가 삭제되었습니다"
    }


@router.get("/pending", response_model=PendingOrdersResponse)
async def get_pending_orders_api():
    """미체결 주문 목록 조회

    오늘 접수된 미체결 주문 목록을 반환합니다.
    5초 캐싱이 적용되어 있습니다.

    Returns:
        - orders: 미체결 주문 목록
        - total_count: 미체결 주문 수
    """
    global _pending_cache, _pending_cache_time, _optimistic_order_nos, _optimistic_added_at

    if not is_authenticated():
        raise HTTPException(status_code=401, detail="인증이 필요합니다")

    # 캐시 확인 (5초 TTL)
    now = datetime.now()
    if _pending_cache is not None and _pending_cache_time is not None:
        elapsed = (now - _pending_cache_time).total_seconds()
        if elapsed < PENDING_CACHE_TTL:
            return PendingOrdersResponse(
                status="success",
                orders=_pending_cache,
                total_count=len(_pending_cache)
            )

    env_dv = get_current_mode()

    try:
        df, api_success = get_pending_orders(env_dv)

        # API 실패 시 (rate limit 등) 이전 캐시 반환 + TTL 갱신
        if not api_success:
            logging.info("미체결 조회 API 실패 - 이전 캐시 반환")
            if _pending_cache is not None:
                _pending_cache_time = now
                return PendingOrdersResponse(
                    status="success",
                    orders=_pending_cache,
                    total_count=len(_pending_cache)
                )

        # API 결과 파싱
        api_orders: list[PendingOrderItem] = []
        api_order_nos: set[str] = set()
        if not df.empty:
            for _, row in df.iterrows():
                ono = str(row.get("order_no", ""))
                if not ono or ono in api_order_nos:
                    continue
                api_order_nos.add(ono)
                api_orders.append(PendingOrderItem(
                    order_no=ono,
                    org_no=str(row.get("org_no", "")),
                    stock_code=str(row.get("stock_code", "")),
                    stock_name=str(row.get("stock_name", "")),
                    order_type=str(row.get("order_type", "")),
                    order_qty=int(row.get("order_qty", 0)),
                    order_price=int(row.get("order_price", 0)),
                    filled_qty=int(row.get("filled_qty", 0)),
                    unfilled_qty=int(row.get("unfilled_qty", 0)),
                    order_time=str(row.get("order_time", ""))
                ))

        # Optimistic Grace Period: API에 아직 안 나타난 최근 주문 보존
        merged = list(api_orders)
        if _optimistic_added_at and _optimistic_order_nos:
            grace_elapsed = (now - _optimistic_added_at).total_seconds()
            if grace_elapsed < OPTIMISTIC_GRACE_SECONDS:
                missing = _optimistic_order_nos - api_order_nos
                if missing and _pending_cache:
                    for item in _pending_cache:
                        if item.order_no in missing:
                            merged.append(item)
                    logging.info(f"Optimistic Grace: {len(missing)}건 보존 ({grace_elapsed:.0f}s/{OPTIMISTIC_GRACE_SECONDS}s)")
            else:
                _optimistic_order_nos.clear()
                _optimistic_added_at = None
                logging.info("Optimistic Grace 만료 - API 결과만 사용")

        if merged:
            logging.info(f"미체결 캐시 업데이트: API {len(api_orders)}건 + Optimistic {len(merged) - len(api_orders)}건")
        _pending_cache = merged
        _pending_cache_time = now

        return PendingOrdersResponse(
            status="success",
            orders=merged,
            total_count=len(merged)
        )

    except Exception as e:
        logging.error(f"미체결 주문 조회 에러: {e}")
        if _pending_cache is not None:
            _pending_cache_time = now
            return PendingOrdersResponse(
                status="success",
                orders=_pending_cache,
                total_count=len(_pending_cache)
            )
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/cancel", response_model=CancelOrderResponse)
async def cancel_order_api(request: CancelOrderRequest, http_request: Request):
    """주문 취소
    
    미체결 주문을 취소합니다.
    
    Args:
        order_no: 취소할 주문번호
        stock_code: 종목코드
        qty: 취소수량
        
    Returns:
        - success: 취소 성공 여부
        - order_no: 취소된 주문번호
        - message: 결과 메시지
    """
    authenticated_user = http_request.headers.get("X-Authenticated-User", "unknown")

    if not is_authenticated():
        write_order_audit({
            "authenticated_user": authenticated_user,
            "mode": get_current_mode(),
            "action": "cancel",
            "stock_code": request.stock_code,
            "quantity": request.qty,
            "price": None,
            "order_type": None,
            "result": "error",
            "order_id": request.order_no,
            "error_message": "인증이 필요합니다",
        })
        raise HTTPException(status_code=401, detail="인증이 필요합니다")
    
    env_dv = get_current_mode()
    
    try:
        logging.info(f"취소 요청 수신: order_no={request.order_no}, org_no='{request.org_no}'")
        result = cancel_order(
            order_no=request.order_no,
            stock_code=request.stock_code,
            qty=request.qty,
            org_no=request.org_no,
            env_dv=env_dv
        )

        # 캐시 업데이트
        global _pending_cache, _pending_cache_time, _optimistic_order_nos
        if result.get("success"):
            _optimistic_order_nos.discard(request.order_no)
            if _pending_cache is not None:
                _pending_cache = [
                    p for p in _pending_cache
                    if p.order_no != request.order_no
                ]
                logging.info(f"미체결 캐시에서 제거: 주문번호 {request.order_no}")
            _clear_account_cache()
        else:
            # 취소 실패: 이미 체결되었거나 존재하지 않는 주문
            # Optimistic 캐시와 Grace에서도 제거 (체결된 주문이 UI에 남지 않도록)
            _optimistic_order_nos.discard(request.order_no)
            if _pending_cache is not None:
                _pending_cache = [
                    p for p in _pending_cache
                    if p.order_no != request.order_no
                ]
            _pending_cache_time = None
            logging.info(f"취소 실패(체결 완료 추정) - 캐시에서 제거: 주문번호 {request.order_no}")

        response = CancelOrderResponse(
            status="success" if result.get("success") else "error",
            success=result.get("success", False),
            order_no=result.get("order_no", request.order_no),
            message=result.get("message") or ""
        )
        write_order_audit({
            "authenticated_user": authenticated_user,
            "mode": env_dv,
            "action": "cancel",
            "stock_code": request.stock_code,
            "quantity": request.qty,
            "price": None,
            "order_type": None,
            "result": "success" if response.success else "error",
            "order_id": response.order_no,
            "error_message": None if response.success else response.message,
        })
        return response
        
    except Exception as e:
        logging.error(f"주문 취소 에러: {e}")
        write_order_audit({
            "authenticated_user": authenticated_user,
            "mode": env_dv,
            "action": "cancel",
            "stock_code": request.stock_code,
            "quantity": request.qty,
            "price": None,
            "order_type": None,
            "result": "error",
            "order_id": request.order_no,
            "error_message": str(e),
        })
        raise HTTPException(status_code=500, detail=str(e))
