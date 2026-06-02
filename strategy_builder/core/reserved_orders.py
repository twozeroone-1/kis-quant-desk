"""KIS reserved order helpers for domestic and US stocks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import pandas as pd

import kis_auth as ka
from core import overseas_data_fetcher

logger = logging.getLogger(__name__)

DOMESTIC_RESERVE_API = "/uapi/domestic-stock/v1/trading/order-resv"
DOMESTIC_RESERVE_CANCEL_API = "/uapi/domestic-stock/v1/trading/order-resv-rvsecncl"
DOMESTIC_RESERVE_LIST_API = "/uapi/domestic-stock/v1/trading/order-resv-ccnl"
OVERSEAS_RESERVE_API = "/uapi/overseas-stock/v1/trading/order-resv"
OVERSEAS_RESERVE_CANCEL_API = "/uapi/overseas-stock/v1/trading/order-resv-ccnl"
OVERSEAS_RESERVE_LIST_API = "/uapi/overseas-stock/v1/trading/order-resv-list"


@dataclass(frozen=True)
class ReservedOrderResult:
    dataframe: pd.DataFrame
    success: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    api_url: Optional[str] = None
    tr_id: Optional[str] = None

    def display_error(self) -> str:
        parts = [part for part in (self.error_code, self.error_message) if part]
        return " ".join(parts) if parts else "예약주문 처리 실패"

    def records(self) -> list[dict[str, Any]]:
        return [] if self.dataframe.empty else self.dataframe.to_dict("records")


def _assert_trenv_ready(context: str = "") -> bool:
    trenv = ka.getTREnv()
    if not hasattr(trenv, "my_url") or not trenv.my_url:
        logger.error("KIS API 미인증%s: 재인증이 필요합니다.", f" ({context})" if context else "")
        return False
    return True


def _body_get(body: Any, key: str, default: Any = None) -> Any:
    if isinstance(body, dict):
        return body.get(key, default)
    return getattr(body, key, default)


def _frame_from_output(value: Any) -> pd.DataFrame:
    if not value:
        return pd.DataFrame()
    if isinstance(value, list):
        return pd.DataFrame(value)
    return pd.DataFrame([value])


def _response_error(res: Any, fallback: str) -> tuple[Optional[str], str]:
    code = None
    message = fallback
    try:
        code = str(res.getErrorCode() or "") or None
    except Exception:
        code = None
    try:
        message = str(res.getErrorMessage() or "") or fallback
    except Exception:
        message = fallback
    return code, message


def _normalize_env(env_dv: str = "real") -> str:
    return "real" if env_dv in ("real", "prod") else "demo"


def _fetch(
    *,
    api_url: str,
    tr_id: str,
    params: dict[str, Any],
    output_key: str = "output",
    post: bool = False,
    tr_cont: str = "",
) -> ReservedOrderResult:
    try:
        res = ka._url_fetch(api_url, tr_id, tr_cont, params, postFlag=post)
        if not res.isOK():
            code, message = _response_error(res, "예약주문 처리 실패")
            logger.error("예약주문 API 실패 (%s): %s %s", api_url, code, message)
            res.printError(api_url)
            return ReservedOrderResult(
                dataframe=pd.DataFrame(),
                success=False,
                error_code=code,
                error_message=message,
                api_url=api_url,
                tr_id=tr_id,
            )
        return ReservedOrderResult(
            dataframe=_frame_from_output(_body_get(res.getBody(), output_key)),
            success=True,
            api_url=api_url,
            tr_id=tr_id,
        )
    except Exception as exc:
        logger.error("예약주문 API 에러 (%s): %s", api_url, exc)
        return ReservedOrderResult(
            dataframe=pd.DataFrame(),
            success=False,
            error_message=str(exc),
            api_url=api_url,
            tr_id=tr_id,
        )


def _domestic_order_division(order_type: str) -> tuple[str, str]:
    if order_type == "market":
        return "01", "0"
    if order_type == "preopen":
        return "05", "0"
    return "00", "limit"


def _overseas_order_division(action: str, order_type: str) -> str:
    if order_type == "moo" and action.upper() == "SELL":
        return "31"
    return "00"


def _normalized_order_no(row: dict[str, Any]) -> str:
    return str(
        row.get("RSVN_ORD_SEQ")
        or row.get("rsvn_ord_seq")
        or row.get("OVRS_RSVN_ODNO")
        or row.get("ovrs_rsvn_odno")
        or row.get("ODNO")
        or row.get("odno")
        or ""
    )


def _normalized_order_date(row: dict[str, Any]) -> str:
    return str(
        row.get("RSVN_ORD_ORD_DT")
        or row.get("rsvn_ord_ord_dt")
        or row.get("RSVN_ORD_RCIT_DT")
        or row.get("rsvn_ord_rcit_dt")
        or datetime.now().strftime("%Y%m%d")
    )


def _normalized_org_no(row: dict[str, Any]) -> str:
    return str(row.get("RSVN_ORD_ORGNO") or row.get("rsvn_ord_orgno") or "")


def first_normalized_record(result: ReservedOrderResult) -> dict[str, Any]:
    if result.dataframe.empty:
        return {}
    row = result.dataframe.iloc[0].to_dict()
    return {
        "reservation_order_no": _normalized_order_no(row),
        "reservation_order_date": _normalized_order_date(row),
        "reservation_order_org_no": _normalized_org_no(row),
        "raw": row,
    }


def submit_domestic_reservation(
    *,
    stock_code: str,
    action: str,
    quantity: int,
    price: float,
    order_type: str,
    env_dv: str = "real",
    end_date: Optional[str] = None,
) -> ReservedOrderResult:
    if not _assert_trenv_ready(f"국내 예약주문 {stock_code}"):
        return ReservedOrderResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")

    trenv = ka.getTREnv()
    division, normalized_price = _domestic_order_division(order_type)
    order_price = "0" if normalized_price == "0" else str(int(price))
    params: dict[str, Any] = {
        "CANO": trenv.my_acct,
        "ACNT_PRDT_CD": trenv.my_prod,
        "PDNO": stock_code,
        "ORD_QTY": str(quantity),
        "ORD_UNPR": order_price,
        "SLL_BUY_DVSN_CD": "02" if action.upper() == "BUY" else "01",
        "ORD_DVSN_CD": division,
        "ORD_OBJT_CBLC_DVSN_CD": "10",
    }
    if end_date:
        params["RSVN_ORD_END_DT"] = end_date
    return _fetch(api_url=DOMESTIC_RESERVE_API, tr_id="CTSC0008U", params=params, post=True)


def list_domestic_reservations(
    *,
    start_date: str,
    end_date: str,
    stock_code: str = "",
    action: str = "",
    include_cancelled: bool = True,
    max_depth: int = 10,
) -> ReservedOrderResult:
    if not _assert_trenv_ready("국내 예약주문 조회"):
        return ReservedOrderResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")

    trenv = ka.getTREnv()
    sll_buy = "02" if action == "BUY" else "01" if action == "SELL" else ""
    frames: list[pd.DataFrame] = []
    tr_cont = ""
    fk200 = ""
    nk200 = ""
    for depth in range(max_depth + 1):
        params = {
            "RSVN_ORD_ORD_DT": start_date,
            "RSVN_ORD_END_DT": end_date,
            "TMNL_MDIA_KIND_CD": "00",
            "CANO": trenv.my_acct,
            "ACNT_PRDT_CD": trenv.my_prod,
            "PRCS_DVSN_CD": "0",
            "CNCL_YN": "Y" if include_cancelled else "N",
            "RSVN_ORD_SEQ": "",
            "PDNO": stock_code,
            "SLL_BUY_DVSN_CD": sll_buy,
            "CTX_AREA_FK200": fk200,
            "CTX_AREA_NK200": nk200,
        }
        result = _fetch(
            api_url=DOMESTIC_RESERVE_LIST_API,
            tr_id="CTSC0004R",
            params=params,
            tr_cont=tr_cont,
        )
        if not result.success:
            return result
        if not result.dataframe.empty:
            frames.append(result.dataframe)
        header = getattr(ka, "last_response_header", None)
        # kis_auth does not expose the last response globally in all versions; stop after one page
        # when continuation metadata is unavailable.
        if header is None or depth >= max_depth:
            break
        break
    dataframe = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return ReservedOrderResult(dataframe=dataframe, success=True, api_url=DOMESTIC_RESERVE_LIST_API, tr_id="CTSC0004R")


def cancel_domestic_reservation(
    *,
    reservation_order_no: str,
    reservation_order_org_no: str,
    reservation_order_date: str,
) -> ReservedOrderResult:
    if not _assert_trenv_ready(f"국내 예약주문 취소 {reservation_order_no}"):
        return ReservedOrderResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")

    trenv = ka.getTREnv()
    params = {
        "CANO": trenv.my_acct,
        "ACNT_PRDT_CD": trenv.my_prod,
        "RSVN_ORD_SEQ": reservation_order_no,
        "RSVN_ORD_ORGNO": reservation_order_org_no,
        "RSVN_ORD_ORD_DT": reservation_order_date,
    }
    return _fetch(api_url=DOMESTIC_RESERVE_CANCEL_API, tr_id="CTSC0009U", params=params, post=True)


def modify_domestic_reservation(
    *,
    reservation_order_no: str,
    reservation_order_org_no: str,
    reservation_order_date: str,
    stock_code: str,
    action: str,
    quantity: int,
    price: float,
    order_type: str,
    end_date: Optional[str] = None,
) -> ReservedOrderResult:
    if not _assert_trenv_ready(f"국내 예약주문 정정 {reservation_order_no}"):
        return ReservedOrderResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")

    trenv = ka.getTREnv()
    division, normalized_price = _domestic_order_division(order_type)
    params: dict[str, Any] = {
        "CANO": trenv.my_acct,
        "ACNT_PRDT_CD": trenv.my_prod,
        "RSVN_ORD_SEQ": reservation_order_no,
        "RSVN_ORD_ORGNO": reservation_order_org_no,
        "RSVN_ORD_ORD_DT": reservation_order_date,
        "PDNO": stock_code,
        "ORD_QTY": str(quantity),
        "ORD_UNPR": "0" if normalized_price == "0" else str(int(price)),
        "SLL_BUY_DVSN_CD": "02" if action.upper() == "BUY" else "01",
        "ORD_DVSN_CD": division,
        "ORD_OBJT_CBLC_DVSN_CD": "10",
    }
    if end_date:
        params["RSVN_ORD_END_DT"] = end_date
    return _fetch(api_url=DOMESTIC_RESERVE_CANCEL_API, tr_id="CTSC0013U", params=params, post=True)


def submit_us_reservation(
    *,
    symbol: str,
    action: str,
    quantity: int,
    price: float,
    order_type: str,
    env_dv: str = "real",
    exchange: Optional[str] = None,
) -> ReservedOrderResult:
    if not _assert_trenv_ready(f"미국 예약주문 {symbol}"):
        return ReservedOrderResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")

    mode = _normalize_env(env_dv)
    resolution = overseas_data_fetcher.resolve_exchange(symbol, exchange)
    is_buy = action.upper() == "BUY"
    if mode == "real":
        tr_id = "TTTT3014U" if is_buy else "TTTT3016U"
    else:
        tr_id = "VTTT3014U" if is_buy else "VTTT3016U"

    trenv = ka.getTREnv()
    params = {
        "CANO": trenv.my_acct,
        "ACNT_PRDT_CD": trenv.my_prod,
        "PDNO": resolution.symbol,
        "OVRS_EXCG_CD": resolution.exchange,
        "FT_ORD_QTY": str(quantity),
        "FT_ORD_UNPR3": "0" if order_type == "moo" else str(round(float(price), 2)),
        "ORD_DVSN": _overseas_order_division(action, order_type),
    }
    return _fetch(api_url=OVERSEAS_RESERVE_API, tr_id=tr_id, params=params, post=True)


def list_us_reservations(
    *,
    start_date: str,
    end_date: str,
    exchange: str = "NASD",
    inquiry_division: str = "00",
    env_dv: str = "real",
) -> ReservedOrderResult:
    if not _assert_trenv_ready("미국 예약주문 조회"):
        return ReservedOrderResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")

    trenv = ka.getTREnv()
    mode = _normalize_env(env_dv)
    params = {
        "CANO": trenv.my_acct,
        "ACNT_PRDT_CD": trenv.my_prod,
        "INQR_STRT_DT": start_date,
        "INQR_END_DT": end_date,
        "INQR_DVSN_CD": inquiry_division,
        "OVRS_EXCG_CD": overseas_data_fetcher.resolve_exchange("AAPL", exchange).exchange,
        "PRDT_TYPE_CD": "",
        "CTX_AREA_FK200": "",
        "CTX_AREA_NK200": "",
    }
    tr_id = "TTTT3039R" if mode == "real" else "VTTT3039R"
    return _fetch(api_url=OVERSEAS_RESERVE_LIST_API, tr_id=tr_id, params=params)


def cancel_us_reservation(
    *,
    reservation_order_date: str,
    reservation_order_no: str,
    env_dv: str = "real",
) -> ReservedOrderResult:
    if not _assert_trenv_ready(f"미국 예약주문 취소 {reservation_order_no}"):
        return ReservedOrderResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")

    mode = _normalize_env(env_dv)
    tr_id = "TTTT3017U" if mode == "real" else "VTTT3017U"
    trenv = ka.getTREnv()
    params = {
        "CANO": trenv.my_acct,
        "ACNT_PRDT_CD": trenv.my_prod,
        "RSVN_ORD_RCIT_DT": reservation_order_date,
        "OVRS_RSVN_ODNO": reservation_order_no,
    }
    return _fetch(api_url=OVERSEAS_RESERVE_CANCEL_API, tr_id=tr_id, params=params, post=True)
