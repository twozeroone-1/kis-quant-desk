"""Overseas stock data and trading helpers for KIS Strategy Builder."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd

import kis_auth as ka

logger = logging.getLogger(__name__)

PRICE_EXCHANGE_BY_TRADING = {
    "NASD": "NAS",
    "NYSE": "NYS",
    "AMEX": "AMS",
}
TRADING_EXCHANGE_BY_PRICE = {
    "NAS": "NASD",
    "NYS": "NYSE",
    "AMS": "AMEX",
}
SEARCH_SEQUENCE = (
    ("512", "NASD", "NAS"),
    ("513", "NYSE", "NYS"),
    ("529", "AMEX", "AMS"),
)

_exchange_cache: dict[str, dict[str, Any]] = {}
_exchange_cache_lock = threading.Lock()
_balance_cache_lock = threading.Lock()
_balance_cache: dict[str, Any] = {"data": None, "timestamp": 0.0, "env_dv": None}
_present_balance_cache_lock = threading.Lock()
_present_balance_cache: dict[str, Any] = {"data": None, "timestamp": 0.0, "env_dv": None}
_BALANCE_CACHE_TTL = 10
US_DAYTIME_START = (10, 0)
US_DAYTIME_END = (18, 0)
US_EXCHANGES = {"NASD", "NYSE", "AMEX"}


@dataclass(frozen=True)
class ExchangeResolution:
    symbol: str
    exchange: str
    price_exchange: str
    name: str
    warning: Optional[str] = None

    def as_dict(self) -> dict[str, Any]:
        data = {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "price_exchange": self.price_exchange,
            "name": self.name,
        }
        if self.warning:
            data["warning"] = self.warning
        return data


@dataclass(frozen=True)
class OverseasOrderResult:
    dataframe: pd.DataFrame
    success: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    api_url: Optional[str] = None
    tr_id: Optional[str] = None

    def display_error(self) -> str:
        parts = [part for part in (self.error_code, self.error_message) if part]
        return " ".join(parts) if parts else "해외 주문 실행 실패"


@dataclass(frozen=True)
class OverseasRankingResult:
    dataframe: pd.DataFrame
    success: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    api_url: Optional[str] = None
    tr_id: Optional[str] = None

    def display_error(self) -> str:
        parts = [part for part in (self.error_code, self.error_message) if part]
        return " ".join(parts) if parts else "해외 랭킹 조회 실패"

    def records(self) -> list[dict[str, Any]]:
        return [] if self.dataframe.empty else self.dataframe.to_dict("records")


def normalize_env(env_dv: str = "real") -> str:
    """Map app modes to KIS overseas real/demo modes."""
    return "real" if env_dv in ("real", "prod") else "demo"


def is_us_daytime_session(now: Optional[datetime] = None) -> bool:
    """Return True during KIS US daytime trading hours in Korea time."""
    korea_now = now.astimezone(ZoneInfo("Asia/Seoul")) if now else datetime.now(ZoneInfo("Asia/Seoul"))
    if korea_now.weekday() >= 5:
        return False
    start_hour, start_minute = US_DAYTIME_START
    end_hour, end_minute = US_DAYTIME_END
    start = korea_now.replace(hour=start_hour, minute=start_minute, second=0, microsecond=0)
    end = korea_now.replace(hour=end_hour, minute=end_minute, second=0, microsecond=0)
    return start <= korea_now < end


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


def _first_value(data: dict[str, Any], keys: tuple[str, ...], default: Any = 0) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            return value
    return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    return int(_to_float(value, float(default)))


def _first_number(data: dict[str, Any], keys: tuple[str, ...], default: float = 0.0) -> float:
    fallback = default
    for key in keys:
        if key not in data:
            continue
        value = data.get(key)
        if value in (None, ""):
            continue
        number = _to_float(value, default)
        if number != 0:
            return number
        fallback = number
    return fallback


def _currency_row(df: pd.DataFrame, currency: str = "USD") -> dict[str, Any]:
    if df.empty:
        return {}
    desired = currency.upper()
    for _, item in df.iterrows():
        row = item.to_dict()
        currency_code = str(_first_value(row, ("crcy_cd", "buy_crcy_cd", "tr_crcy_cd", "crcy_cd_name"), "")).upper()
        if currency_code == desired or desired in currency_code:
            return row
    return df.iloc[0].to_dict()


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


def _trading_exchange(exchange: Optional[str]) -> Optional[str]:
    if not exchange:
        return None
    code = exchange.upper()
    return TRADING_EXCHANGE_BY_PRICE.get(code, code)


def _price_exchange(exchange: Optional[str]) -> Optional[str]:
    if not exchange:
        return None
    code = exchange.upper()
    return PRICE_EXCHANGE_BY_TRADING.get(code, code)


def resolve_exchange(symbol: str, exchange: Optional[str] = None) -> ExchangeResolution:
    """Resolve a US symbol to KIS trading and quotation exchange codes."""
    normalized = symbol.strip().upper()
    explicit_exchange = _trading_exchange(exchange)
    if explicit_exchange in PRICE_EXCHANGE_BY_TRADING:
        return ExchangeResolution(
            symbol=normalized,
            exchange=explicit_exchange,
            price_exchange=PRICE_EXCHANGE_BY_TRADING[explicit_exchange],
            name=normalized,
        )

    with _exchange_cache_lock:
        cached = _exchange_cache.get(normalized)
    if cached:
        return ExchangeResolution(**cached)

    if _assert_trenv_ready(f"해외 거래소 조회 {normalized}"):
        for product_type, trading_exchange, price_exchange in SEARCH_SEQUENCE:
            params = {
                "PRDT_TYPE_CD": product_type,
                "PDNO": normalized,
            }
            try:
                res = ka._url_fetch(
                    "/uapi/overseas-price/v1/quotations/search-info",
                    "CTPF1702R",
                    "",
                    params,
                )
                if not res.isOK():
                    continue
                df = _frame_from_output(_body_get(res.getBody(), "output"))
                if df.empty:
                    continue
                row = df.iloc[0].to_dict()
                name = str(_first_value(
                    row,
                    ("prdt_name", "prdt_eng_name", "ovrs_item_name", "hts_kor_isnm", "pdno"),
                    normalized,
                ))
                resolution = ExchangeResolution(
                    symbol=normalized,
                    exchange=trading_exchange,
                    price_exchange=price_exchange,
                    name=name or normalized,
                )
                with _exchange_cache_lock:
                    _exchange_cache[normalized] = resolution.as_dict()
                return resolution
            except Exception as exc:
                logger.debug("해외 거래소 조회 실패 %s/%s: %s", normalized, product_type, exc)

    warning = f"{normalized} 거래소 자동 추정 실패: NASD/NAS로 처리합니다"
    logger.warning(warning)
    resolution = ExchangeResolution(
        symbol=normalized,
        exchange="NASD",
        price_exchange="NAS",
        name=normalized,
        warning=warning,
    )
    with _exchange_cache_lock:
        _exchange_cache[normalized] = resolution.as_dict()
    return resolution


def get_current_price(symbol: str, env_dv: str = "real", exchange: Optional[str] = None) -> dict[str, Any]:
    if not _assert_trenv_ready(f"해외 현재가 조회 {symbol}"):
        return {}
    resolution = resolve_exchange(symbol, exchange)
    try:
        params = {
            "AUTH": "",
            "EXCD": resolution.price_exchange,
            "SYMB": resolution.symbol,
        }
        res = ka._url_fetch(
            "/uapi/overseas-price/v1/quotations/price",
            "HHDFS00000300",
            "",
            params,
        )
        if not res.isOK():
            logger.warning("해외 현재가 조회 실패: %s", resolution.symbol)
            return {}

        df = _frame_from_output(_body_get(res.getBody(), "output"))
        if df.empty:
            return {}
        row = df.iloc[0].to_dict()
        price = _to_float(_first_value(row, ("last", "stck_prpr", "ovrs_now_pric1", "clos", "base")))
        prev = _to_float(_first_value(row, ("base", "prev", "stck_sdpr"), price))
        change = _to_float(_first_value(row, ("diff", "prdy_vrss"), price - prev))
        change_rate = _to_float(_first_value(row, ("rate", "prdy_ctrt"), (change / prev * 100) if prev else 0))
        data = {
            "price": price,
            "change": change,
            "change_rate": change_rate,
            "high": _to_float(_first_value(row, ("high", "stck_hgpr"), 0)),
            "low": _to_float(_first_value(row, ("low", "stck_lwpr"), 0)),
            "volume": _to_int(_first_value(row, ("tvol", "acml_vol", "evol"), 0)),
            "w52_high": _to_float(_first_value(row, ("h52p", "w52_hgpr"), 0)),
            "w52_low": _to_float(_first_value(row, ("l52p", "w52_lwpr"), 0)),
            "exchange": resolution.exchange,
            "currency": "USD",
        }
        if resolution.warning:
            data["warning"] = resolution.warning
        return data
    except Exception as exc:
        logger.error("해외 현재가 조회 에러 (%s): %s", symbol, exc)
        return {}


def get_daily_prices(
    symbol: str,
    days: int = 100,
    env_dv: str = "real",
    exchange: Optional[str] = None,
) -> pd.DataFrame:
    if not _assert_trenv_ready(f"해외 일봉 조회 {symbol}"):
        return pd.DataFrame()
    resolution = resolve_exchange(symbol, exchange)
    try:
        params = {
            "AUTH": "",
            "EXCD": resolution.price_exchange,
            "SYMB": resolution.symbol,
            "GUBN": "0",
            "BYMD": datetime.now().strftime("%Y%m%d"),
            "MODP": "1",
        }
        res = ka._url_fetch(
            "/uapi/overseas-price/v1/quotations/dailyprice",
            "HHDFS76240000",
            "",
            params,
        )
        if not res.isOK():
            logger.warning("해외 일봉 조회 실패: %s", resolution.symbol)
            return pd.DataFrame()

        df = _frame_from_output(_body_get(res.getBody(), "output2"))
        if df.empty:
            return pd.DataFrame()

        df = df.rename(columns={
            "xymd": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "clos": "close",
            "last": "close",
            "tvol": "volume",
        })
        required = ["date", "open", "high", "low", "close", "volume"]
        for col in required:
            if col not in df.columns:
                df[col] = 0
        df = df[required]
        for col in ["open", "high", "low", "close", "volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        return df.tail(days).reset_index(drop=True)
    except Exception as exc:
        logger.error("해외 일봉 조회 에러 (%s): %s", symbol, exc)
        return pd.DataFrame()


def _fetch_overseas_ranking(
    *,
    api_url: str,
    tr_id: str,
    exchange: str,
    params: dict[str, Any],
    max_depth: int = 1,
) -> OverseasRankingResult:
    if not _assert_trenv_ready(f"해외 랭킹 조회 {exchange}"):
        return OverseasRankingResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")
    frames: list[pd.DataFrame] = []
    keyb = str(params.get("KEYB") or "")
    tr_cont = ""
    try:
        for _ in range(max(1, max_depth)):
            call_params = {**params, "EXCD": _price_exchange(exchange) or exchange, "KEYB": keyb}
            res = ka._url_fetch(api_url, tr_id, tr_cont, call_params)
            if not res.isOK():
                code, message = _response_error(res, "해외 랭킹 조회 실패")
                logger.warning("해외 랭킹 조회 실패 (%s/%s): %s %s", api_url, exchange, code, message)
                res.printError(api_url)
                return OverseasRankingResult(
                    pd.DataFrame(),
                    False,
                    error_code=code,
                    error_message=message,
                    api_url=api_url,
                    tr_id=tr_id,
                )
            body = res.getBody()
            frame = _frame_from_output(_body_get(body, "output2"))
            if not frame.empty:
                frames.append(frame)
            header_cont = str(getattr(res.getHeader(), "tr_cont", "") or "")
            if header_cont not in {"M", "F"}:
                break
            keyb = str(_body_get(body, "keyb", keyb) or keyb)
            tr_cont = "N"
            ka.smart_sleep()
        dataframe = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        return OverseasRankingResult(dataframe, True, api_url=api_url, tr_id=tr_id)
    except Exception as exc:
        logger.error("해외 랭킹 조회 에러 (%s/%s): %s", api_url, exchange, exc)
        return OverseasRankingResult(pd.DataFrame(), False, error_message=str(exc), api_url=api_url, tr_id=tr_id)


def get_overseas_trade_value_rank(
    *,
    exchange: str = "NAS",
    nday: str = "0",
    vol_rang: str = "2",
    max_depth: int = 1,
) -> OverseasRankingResult:
    return _fetch_overseas_ranking(
        api_url="/uapi/overseas-stock/v1/ranking/trade-pbmn",
        tr_id="HHDFS76320010",
        exchange=exchange,
        params={"NDAY": nday, "VOL_RANG": vol_rang, "AUTH": "", "PRC1": "", "PRC2": ""},
        max_depth=max_depth,
    )


def get_overseas_market_cap_rank(
    *,
    exchange: str = "NAS",
    vol_rang: str = "2",
    max_depth: int = 1,
) -> OverseasRankingResult:
    return _fetch_overseas_ranking(
        api_url="/uapi/overseas-stock/v1/ranking/market-cap",
        tr_id="HHDFS76350100",
        exchange=exchange,
        params={"VOL_RANG": vol_rang, "AUTH": ""},
        max_depth=max_depth,
    )


def get_overseas_volume_power_rank(
    *,
    exchange: str = "NAS",
    nday: str = "0",
    vol_rang: str = "2",
    max_depth: int = 1,
) -> OverseasRankingResult:
    return _fetch_overseas_ranking(
        api_url="/uapi/overseas-stock/v1/ranking/volume-power",
        tr_id="HHDFS76280000",
        exchange=exchange,
        params={"NDAY": nday, "VOL_RANG": vol_rang, "AUTH": ""},
        max_depth=max_depth,
    )


def get_overseas_volume_surge_rank(
    *,
    exchange: str = "NAS",
    minx: str = "3",
    vol_rang: str = "2",
    max_depth: int = 1,
) -> OverseasRankingResult:
    return _fetch_overseas_ranking(
        api_url="/uapi/overseas-stock/v1/ranking/volume-surge",
        tr_id="HHDFS76270000",
        exchange=exchange,
        params={"MINX": minx, "VOL_RANG": vol_rang, "AUTH": ""},
        max_depth=max_depth,
    )


def _fetch_balance_raw(env_dv: str = "real") -> Optional[dict[str, Any]]:
    if not _assert_trenv_ready("해외 잔고 조회"):
        return None
    trenv = ka.getTREnv()
    mode = normalize_env(env_dv)
    tr_id = "TTTS3012R" if mode == "real" else "VTTS3012R"
    params = {
        "CANO": trenv.my_acct,
        "ACNT_PRDT_CD": trenv.my_prod,
        "OVRS_EXCG_CD": "NASD",
        "TR_CRCY_CD": "USD",
        "CTX_AREA_FK200": "",
        "CTX_AREA_NK200": "",
    }
    res = ka._url_fetch(
        "/uapi/overseas-stock/v1/trading/inquire-balance",
        tr_id,
        "",
        params,
    )
    if not res.isOK():
        return None
    body = res.getBody()
    return {
        "output1": _body_get(body, "output1", []),
        "output2": _body_get(body, "output2", []),
    }


def _fetch_present_balance_raw(env_dv: str = "real", currency_division: str = "02") -> Optional[dict[str, Any]]:
    if not _assert_trenv_ready("해외 체결기준현재잔고 조회"):
        return None
    trenv = ka.getTREnv()
    mode = normalize_env(env_dv)
    tr_id = "CTRP6504R" if mode == "real" else "VTRP6504R"
    params = {
        "CANO": trenv.my_acct,
        "ACNT_PRDT_CD": trenv.my_prod,
        "WCRC_FRCR_DVSN_CD": currency_division,
        "NATN_CD": "000",
        "TR_MKET_CD": "00",
        "INQR_DVSN_CD": "00",
    }
    res = ka._url_fetch(
        "/uapi/overseas-stock/v1/trading/inquire-present-balance",
        tr_id,
        "",
        params,
    )
    if not res.isOK():
        code, message = _response_error(res, "해외 체결기준현재잔고 조회 실패")
        logger.warning("해외 체결기준현재잔고 조회 실패: %s %s", code, message)
        return None
    body = res.getBody()
    return {
        "output1": _body_get(body, "output1", []),
        "output2": _body_get(body, "output2", []),
        "output3": _body_get(body, "output3", []),
    }


def _get_balance_cached(env_dv: str = "real") -> Optional[dict[str, Any]]:
    global _balance_cache
    mode = normalize_env(env_dv)
    with _balance_cache_lock:
        now = time.monotonic()
        if (
            _balance_cache["data"] is not None
            and _balance_cache["env_dv"] == mode
            and (now - _balance_cache["timestamp"]) < _BALANCE_CACHE_TTL
        ):
            return _balance_cache["data"]
    data = _fetch_balance_raw(mode)
    with _balance_cache_lock:
        _balance_cache = {"data": data, "timestamp": time.monotonic(), "env_dv": mode}
    return data


def _get_present_balance_cached(env_dv: str = "real") -> Optional[dict[str, Any]]:
    global _present_balance_cache
    mode = normalize_env(env_dv)
    with _present_balance_cache_lock:
        now = time.monotonic()
        if (
            _present_balance_cache["data"] is not None
            and _present_balance_cache["env_dv"] == mode
            and (now - _present_balance_cache["timestamp"]) < _BALANCE_CACHE_TTL
        ):
            return _present_balance_cache["data"]
    data = _fetch_present_balance_raw(mode, "02")
    if data is not None and _frame_from_output(data.get("output3")).empty:
        data = _fetch_present_balance_raw(mode, "01") or data
    with _present_balance_cache_lock:
        _present_balance_cache = {"data": data, "timestamp": time.monotonic(), "env_dv": mode}
    return data


def clear_balance_cache() -> None:
    global _balance_cache, _present_balance_cache
    with _balance_cache_lock:
        _balance_cache = {"data": None, "timestamp": 0.0, "env_dv": None}
    with _present_balance_cache_lock:
        _present_balance_cache = {"data": None, "timestamp": 0.0, "env_dv": None}


def get_holdings(env_dv: str = "real") -> pd.DataFrame:
    try:
        raw = _get_balance_cached(env_dv)
        if raw is None:
            return pd.DataFrame()
        df = _frame_from_output(raw.get("output1"))
        if df.empty:
            return pd.DataFrame()
        rows = []
        for _, item in df.iterrows():
            row = item.to_dict()
            quantity = _to_float(_first_value(row, ("ovrs_cblc_qty", "cblc_qty13", "hldg_qty", "ord_psbl_qty"), 0))
            if quantity <= 0:
                continue
            stock_code = str(_first_value(row, ("ovrs_pdno", "pdno", "item_cd"), "")).upper()
            avg_price = _to_float(_first_value(row, ("pchs_avg_pric", "frcr_pchs_amt1", "avg_unpr3"), 0))
            current_price = _to_float(_first_value(row, ("now_pric2", "ovrs_now_pric1", "bass_exrt"), 0))
            eval_amount = _to_float(_first_value(row, ("ovrs_stck_evlu_amt", "frcr_evlu_amt2", "evlu_amt"), current_price * quantity))
            profit_loss = _to_float(_first_value(row, ("frcr_evlu_pfls_amt", "evlu_pfls_amt", "ovrs_evlu_pfls_amt"), 0))
            rows.append({
                "stock_code": stock_code,
                "stock_name": str(_first_value(row, ("ovrs_item_name", "prdt_name", "prdt_eng_name"), stock_code)),
                "quantity": quantity,
                "avg_price": avg_price,
                "current_price": current_price,
                "eval_amount": eval_amount,
                "profit_loss": profit_loss,
                "profit_rate": _to_float(_first_value(row, ("evlu_pfls_rt", "profit_rate"), 0)),
                "exchange": str(_first_value(row, ("ovrs_excg_cd", "tr_mket_name"), "")),
                "currency": "USD",
            })
        return pd.DataFrame(rows)
    except Exception as exc:
        logger.error("해외 잔고 조회 에러: %s", exc)
        return pd.DataFrame()


def get_deposit(env_dv: str = "real") -> dict[str, Any]:
    try:
        raw = _get_balance_cached(env_dv)
        present_raw = _get_present_balance_cached(env_dv)
        if raw is None and present_raw is None:
            return {}
        balance_df = _frame_from_output((raw or {}).get("output2"))
        balance_summary = balance_df.iloc[0].to_dict() if not balance_df.empty else {}
        holding_df = _frame_from_output((raw or {}).get("output1"))
        present_holding_df = _frame_from_output((present_raw or {}).get("output1"))
        present_currency_df = _frame_from_output((present_raw or {}).get("output2"))
        present_currency = _currency_row(present_currency_df, "USD")
        present_df = _frame_from_output((present_raw or {}).get("output3"))
        present_summary = present_df.iloc[0].to_dict() if not present_df.empty else {}

        deposit = _first_number(balance_summary, ("frcr_buy_amt_smtl1", "frcr_buy_amt_smtl2", "buy_psbl_amt", "ovrs_ord_psbl_amt"))
        if deposit == 0:
            deposit = _first_number(balance_summary, ("frcr_drwg_psbl_amt_1", "frcr_dncl_amt_2"))
        if deposit == 0:
            deposit = _first_number(
                present_currency,
                ("frcr_dncl_amt_2", "frcr_use_psbl_amt", "frcr_drwg_psbl_amt_1", "nxdy_frcr_drwg_psbl_amt"),
            )
        available_amount = _first_number(
            present_currency,
            ("frcr_use_psbl_amt", "frcr_drwg_psbl_amt_1", "frcr_dncl_amt_2", "nxdy_frcr_drwg_psbl_amt"),
        )
        stock_eval_amount = 0.0
        stock_purchase_amount = 0.0
        stock_profit_loss = 0.0
        holding_sources = [df for df in (holding_df, present_holding_df) if not df.empty]
        if holding_sources:
            for _, item in holding_sources[0].iterrows():
                row = item.to_dict()
                quantity = _first_number(row, ("ovrs_cblc_qty", "cblc_qty13", "hldg_qty", "ord_psbl_qty"))
                if quantity <= 0:
                    continue
                stock_eval_amount += _first_number(row, ("ovrs_stck_evlu_amt", "frcr_evlu_amt2", "evlu_amt"))
                stock_purchase_amount += _first_number(row, ("frcr_pchs_amt1", "frcr_pchs_amt", "pchs_amt", "pchs_amt_smtl"))
                stock_profit_loss += _first_number(row, ("frcr_evlu_pfls_amt", "evlu_pfls_amt", "evlu_pfls_amt2", "ovrs_evlu_pfls_amt"))
        purchase_amount = stock_purchase_amount or _first_number(balance_summary, ("pchs_amt_smtl_amt", "pchs_amt_smtl", "frcr_pchs_amt1"))
        eval_amount = stock_eval_amount or _first_number(balance_summary, ("ovrs_stck_evlu_amt", "frcr_evlu_amt2", "evlu_amt_smtl_amt", "evlu_amt_smtl", "tot_evlu_amt"))
        profit_loss = stock_profit_loss or _first_number(balance_summary, ("evlu_pfls_smtl_amt", "frcr_evlu_pfls_amt", "tot_evlu_pfls_amt", "ovrs_tot_pfls"))
        total_eval = deposit + eval_amount
        total_asset_krw = _first_number(
            present_summary,
            ("tot_asst_amt", "frcr_evlu_tota", "evlu_amt_smtl_amt", "evlu_amt_smtl", "tot_dncl_amt", "wdrw_psbl_tot_amt"),
        )
        return {
            "deposit": deposit,
            "total_eval": total_eval,
            "purchase_amount": purchase_amount,
            "eval_amount": eval_amount,
            "profit_loss": profit_loss,
            "total_asset_krw": total_asset_krw,
            "available_amount": available_amount,
            "currency": "USD",
        }
    except Exception as exc:
        logger.error("해외 예수금 조회 에러: %s", exc)
        return {}


def get_buyable_amount(
    symbol: str,
    price: float,
    env_dv: str = "real",
    exchange: Optional[str] = None,
) -> dict[str, Any]:
    if not _assert_trenv_ready(f"해외 매수가능 조회 {symbol}"):
        return {"amount": 0, "quantity": 0}
    resolution = resolve_exchange(symbol, exchange)
    try:
        trenv = ka.getTREnv()
        mode = normalize_env(env_dv)
        tr_id = "TTTS3007R" if mode == "real" else "VTTS3007R"
        params = {
            "CANO": trenv.my_acct,
            "ACNT_PRDT_CD": trenv.my_prod,
            "OVRS_EXCG_CD": resolution.exchange,
            "OVRS_ORD_UNPR": str(price),
            "ITEM_CD": resolution.symbol,
        }
        res = ka._url_fetch(
            "/uapi/overseas-stock/v1/trading/inquire-psamount",
            tr_id,
            "",
            params,
        )
        if not res.isOK():
            return {"amount": 0, "quantity": 0}
        df = _frame_from_output(_body_get(res.getBody(), "output"))
        row = df.iloc[0].to_dict() if not df.empty else {}
        amount = _to_float(_first_value(row, ("ovrs_ord_psbl_amt", "max_ord_psbl_amt", "frcr_ord_psbl_amt1"), 0))
        quantity = _to_int(_first_value(row, ("max_ord_psbl_qty", "ord_psbl_qty", "ovrs_ord_psbl_qty"), 0))
        if quantity <= 0 and price > 0 and amount > 0:
            quantity = int(amount // price)
        return {"amount": amount, "quantity": quantity, "currency": "USD"}
    except Exception as exc:
        logger.error("해외 매수가능 조회 에러 (%s): %s", symbol, exc)
        return {"amount": 0, "quantity": 0}


def submit_order(
    *,
    symbol: str,
    action: str,
    quantity: int,
    price: float,
    env_dv: str = "real",
    exchange: Optional[str] = None,
) -> OverseasOrderResult:
    if not _assert_trenv_ready(f"해외 주문 {symbol}"):
        return OverseasOrderResult(
            dataframe=pd.DataFrame(),
            success=False,
            error_message="KIS API 인증이 필요합니다",
        )
    resolution = resolve_exchange(symbol, exchange)
    mode = normalize_env(env_dv)
    trenv = ka.getTREnv()
    is_buy = action.upper() == "BUY"
    # KIS paper trading does not provide the US daytime-trading endpoint.
    # Keep the daytime route limited to live mode; vps/demo uses the standard
    # overseas order endpoint even during Korean daytime hours.
    use_daytime = mode == "real" and resolution.exchange in US_EXCHANGES and is_us_daytime_session()
    if use_daytime:
        api_url = "/uapi/overseas-stock/v1/trading/daytime-order"
        tr_id = "TTTS6036U" if is_buy else "TTTS6037U"
    else:
        api_url = "/uapi/overseas-stock/v1/trading/order"
        tr_id = "TTTT1002U" if is_buy else "TTTT1006U"
        if mode == "demo":
            tr_id = "V" + tr_id[1:]

    params = {
        "CANO": trenv.my_acct,
        "ACNT_PRDT_CD": trenv.my_prod,
        "OVRS_EXCG_CD": resolution.exchange,
        "PDNO": resolution.symbol,
        "ORD_QTY": str(quantity),
        "OVRS_ORD_UNPR": str(price),
        "CTAC_TLNO": "",
        "MGCO_APTM_ODNO": "",
        "ORD_SVR_DVSN_CD": "0",
        "ORD_DVSN": "00",
    }
    if not use_daytime:
        params["SLL_TYPE"] = "" if is_buy else "00"

    try:
        res = ka._url_fetch(
            api_url,
            tr_id,
            "",
            params,
            postFlag=True,
        )
        if not res.isOK():
            code, message = _response_error(res, "해외 주문 실행 실패")
            logger.error("해외 주문 실패 (%s %s): %s %s", action, resolution.symbol, code, message)
            res.printError(api_url)
            return OverseasOrderResult(
                dataframe=pd.DataFrame(),
                success=False,
                error_code=code,
                error_message=message,
                api_url=api_url,
                tr_id=tr_id,
            )
        dataframe = _frame_from_output(_body_get(res.getBody(), "output"))
        return OverseasOrderResult(
            dataframe=dataframe,
            success=not dataframe.empty,
            api_url=api_url,
            tr_id=tr_id,
        )
    except Exception as exc:
        logger.error("해외 주문 실행 에러 (%s): %s", symbol, exc)
        return OverseasOrderResult(
            dataframe=pd.DataFrame(),
            success=False,
            error_message=str(exc),
            api_url=api_url,
            tr_id=tr_id,
        )


def execute_order(
    *,
    symbol: str,
    action: str,
    quantity: int,
    price: float,
    env_dv: str = "real",
    exchange: Optional[str] = None,
) -> pd.DataFrame:
    return submit_order(
        symbol=symbol,
        action=action,
        quantity=quantity,
        price=price,
        env_dv=env_dv,
        exchange=exchange,
    ).dataframe


def get_pending_orders(env_dv: str = "real", exchange: str = "NASD") -> tuple[pd.DataFrame, bool]:
    if not _assert_trenv_ready("해외 미체결 조회"):
        return pd.DataFrame(), False
    try:
        trenv = ka.getTREnv()
        mode = normalize_env(env_dv)
        tr_id = "TTTS3018R" if mode == "real" else "VTTS3018R"
        params = {
            "CANO": trenv.my_acct,
            "ACNT_PRDT_CD": trenv.my_prod,
            "OVRS_EXCG_CD": _trading_exchange(exchange) or "NASD",
            "SORT_SQN": "DS",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        res = ka._url_fetch(
            "/uapi/overseas-stock/v1/trading/inquire-nccs",
            tr_id,
            "",
            params,
        )
        if not res.isOK():
            return pd.DataFrame(), False
        df = _frame_from_output(_body_get(res.getBody(), "output"))
        if df.empty:
            return pd.DataFrame(), True
        rows = []
        for _, item in df.iterrows():
            row = item.to_dict()
            symbol = str(_first_value(row, ("pdno", "ovrs_pdno"), "")).upper()
            order_no = str(_first_value(row, ("odno", "orgn_odno"), ""))
            order_qty = _to_int(_first_value(row, ("ft_ord_qty", "ord_qty"), 0))
            filled_qty = _to_int(_first_value(row, ("ft_ccld_qty", "tot_ccld_qty"), 0))
            unfilled_qty = _to_int(_first_value(row, ("nccs_qty", "rmn_qty"), max(order_qty - filled_qty, 0)))
            if unfilled_qty <= 0:
                continue
            rows.append({
                "order_no": order_no,
                "org_no": str(_first_value(row, ("ord_gno_brno", "org_no"), "")),
                "stock_code": symbol,
                "stock_name": str(_first_value(row, ("prdt_name", "ovrs_item_name"), symbol)),
                "order_type": str(_first_value(row, ("sll_buy_dvsn_cd_name", "sll_buy_dvsn_cd"), "")),
                "order_qty": order_qty,
                "order_price": _to_float(_first_value(row, ("ft_ord_unpr3", "ord_unpr", "ovrs_ord_unpr"), 0)),
                "filled_qty": filled_qty,
                "unfilled_qty": unfilled_qty,
                "order_time": str(_first_value(row, ("ord_tmd", "ord_dt"), "")),
                "exchange": str(_first_value(row, ("ovrs_excg_cd",), exchange)),
                "currency": "USD",
            })
        return pd.DataFrame(rows), True
    except Exception as exc:
        logger.error("해외 미체결 조회 에러: %s", exc)
        return pd.DataFrame(), False


def cancel_order(
    order_no: str,
    symbol: str,
    qty: int,
    env_dv: str = "real",
    exchange: Optional[str] = None,
) -> dict[str, Any]:
    if not _assert_trenv_ready(f"해외 주문 취소 {symbol}"):
        return {"success": False, "order_no": order_no, "message": "인증이 필요합니다"}
    resolution = resolve_exchange(symbol, exchange)
    mode = normalize_env(env_dv)
    trenv = ka.getTREnv()
    tr_id = "TTTT1004U" if mode == "real" else "VTTT1004U"
    params = {
        "CANO": trenv.my_acct,
        "ACNT_PRDT_CD": trenv.my_prod,
        "OVRS_EXCG_CD": resolution.exchange,
        "PDNO": resolution.symbol,
        "ORGN_ODNO": order_no,
        "RVSE_CNCL_DVSN_CD": "02",
        "ORD_QTY": str(qty),
        "OVRS_ORD_UNPR": "0",
        "MGCO_APTM_ODNO": "",
        "ORD_SVR_DVSN_CD": "0",
    }
    try:
        res = ka._url_fetch(
            "/uapi/overseas-stock/v1/trading/order-rvsecncl",
            tr_id,
            "",
            params,
            postFlag=True,
        )
        if not res.isOK():
            return {"success": False, "order_no": order_no, "message": res.getErrorMessage()}
        return {"success": True, "order_no": order_no, "message": "취소 주문이 접수되었습니다"}
    except Exception as exc:
        logger.error("해외 주문 취소 에러 (%s): %s", symbol, exc)
        return {"success": False, "order_no": order_no, "message": str(exc)}
