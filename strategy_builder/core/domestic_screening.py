"""Read-only domestic stock screening helpers backed by KIS examples."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

import pandas as pd

import kis_auth as ka

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScreeningResult:
    dataframe: pd.DataFrame
    success: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    api_url: Optional[str] = None
    tr_id: Optional[str] = None
    extra: Optional[dict[str, Any]] = None

    def display_error(self) -> str:
        parts = [part for part in (self.error_code, self.error_message) if part]
        return " ".join(parts) if parts else "KIS 조회 실패"

    def records(self) -> list[dict[str, Any]]:
        return [] if self.dataframe.empty else self.dataframe.to_dict("records")


@dataclass(frozen=True)
class MultiOutputResult:
    outputs: dict[str, pd.DataFrame]
    success: bool
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    api_url: Optional[str] = None
    tr_id: Optional[str] = None

    def display_error(self) -> str:
        parts = [part for part in (self.error_code, self.error_message) if part]
        return " ".join(parts) if parts else "KIS 조회 실패"

    def records(self) -> dict[str, list[dict[str, Any]]]:
        return {
            key: [] if dataframe.empty else dataframe.to_dict("records")
            for key, dataframe in self.outputs.items()
        }


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


def _default_hts_id() -> str:
    trenv = ka.getTREnv()
    return str(getattr(trenv, "my_htsid", "") or "")


def _normalize_env(env_dv: str = "real") -> str:
    return "real" if env_dv in ("real", "prod") else "demo"


def _fetch(
    *,
    api_url: str,
    tr_id: str,
    params: dict[str, Any],
    output_key: str = "output",
    tr_cont: str = "",
) -> ScreeningResult:
    try:
        res = ka._url_fetch(api_url, tr_id, tr_cont, params)
        if not res.isOK():
            code, message = _response_error(res, "KIS 조회 실패")
            logger.warning("KIS screening API failed (%s): %s %s", api_url, code, message)
            res.printError(api_url)
            return ScreeningResult(
                dataframe=pd.DataFrame(),
                success=False,
                error_code=code,
                error_message=message,
                api_url=api_url,
                tr_id=tr_id,
            )
        return ScreeningResult(
            dataframe=_frame_from_output(_body_get(res.getBody(), output_key)),
            success=True,
            api_url=api_url,
            tr_id=tr_id,
        )
    except Exception as exc:
        logger.exception("KIS screening API error (%s)", api_url)
        return ScreeningResult(
            dataframe=pd.DataFrame(),
            success=False,
            error_message=str(exc),
            api_url=api_url,
            tr_id=tr_id,
        )


def _fetch_paged(
    *,
    api_url: str,
    tr_id: str,
    params: dict[str, Any],
    output_key: str = "output",
    max_depth: int = 3,
) -> ScreeningResult:
    if max_depth < 1:
        max_depth = 1
    frames: list[pd.DataFrame] = []
    tr_cont = ""
    for _ in range(max_depth):
        try:
            res = ka._url_fetch(api_url, tr_id, tr_cont, params)
            if not res.isOK():
                code, message = _response_error(res, "KIS 조회 실패")
                logger.warning("KIS screening API failed (%s): %s %s", api_url, code, message)
                res.printError(api_url)
                return ScreeningResult(
                    dataframe=pd.DataFrame(),
                    success=False,
                    error_code=code,
                    error_message=message,
                    api_url=api_url,
                    tr_id=tr_id,
                )
            frame = _frame_from_output(_body_get(res.getBody(), output_key))
            if not frame.empty:
                frames.append(frame)

            header_cont = str(getattr(res.getHeader(), "tr_cont", "") or "")
            if header_cont not in {"M", "F"}:
                break
            tr_cont = "N"
            ka.smart_sleep()
        except Exception as exc:
            logger.exception("KIS screening paged API error (%s)", api_url)
            return ScreeningResult(
                dataframe=pd.DataFrame(),
                success=False,
                error_message=str(exc),
                api_url=api_url,
                tr_id=tr_id,
            )

    dataframe = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return ScreeningResult(dataframe=dataframe, success=True, api_url=api_url, tr_id=tr_id)


def condition_search_titles(user_id: Optional[str] = None) -> ScreeningResult:
    if not _assert_trenv_ready("조건검색 목록"):
        return ScreeningResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")
    user_id = (user_id or _default_hts_id()).strip()
    if not user_id:
        return ScreeningResult(pd.DataFrame(), False, error_message="HTS ID가 필요합니다")
    return _fetch(
        api_url="/uapi/domestic-stock/v1/quotations/psearch-title",
        tr_id="HHKST03900300",
        params={"user_id": user_id},
        output_key="output2",
    )


def condition_search_results(*, seq: str, user_id: Optional[str] = None) -> ScreeningResult:
    if not _assert_trenv_ready(f"조건검색 결과 {seq}"):
        return ScreeningResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")
    user_id = (user_id or _default_hts_id()).strip()
    if not user_id:
        return ScreeningResult(pd.DataFrame(), False, error_message="HTS ID가 필요합니다")
    if not seq:
        return ScreeningResult(pd.DataFrame(), False, error_message="조건검색 seq가 필요합니다")
    return _fetch(
        api_url="/uapi/domestic-stock/v1/quotations/psearch-result",
        tr_id="HHKST03900400",
        params={"user_id": user_id, "seq": seq},
        output_key="output2",
    )


def market_cap_rank(
    *,
    market_div: str = "J",
    input_iscd: str = "0000",
    div_cls: str = "0",
    price_min: str = "",
    price_max: str = "",
    volume_min: str = "",
    max_depth: int = 1,
) -> ScreeningResult:
    if not _assert_trenv_ready("시가총액 순위"):
        return ScreeningResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")
    params = {
        "fid_input_price_2": price_max,
        "fid_cond_mrkt_div_code": market_div,
        "fid_cond_scr_div_code": "20174",
        "fid_div_cls_code": div_cls,
        "fid_input_iscd": input_iscd,
        "fid_trgt_cls_code": "0",
        "fid_trgt_exls_cls_code": "0",
        "fid_input_price_1": price_min,
        "fid_vol_cnt": volume_min,
    }
    return _fetch_paged(
        api_url="/uapi/domestic-stock/v1/ranking/market-cap",
        tr_id="FHPST01740000",
        params=params,
        max_depth=max_depth,
    )


def fluctuation_rank(
    *,
    market_div: str = "J",
    input_iscd: str = "0000",
    rank_sort: str = "0000",
    count: str = "0",
    price_cls: str = "0",
    price_min: str = "0",
    price_max: str = "1000000",
    volume_min: str = "0",
    target_cls: str = "0",
    target_exclude: str = "0",
    div_cls: str = "0",
    rate_min: str = "0",
    rate_max: str = "999",
    max_depth: int = 1,
) -> ScreeningResult:
    if not _assert_trenv_ready("등락률 순위"):
        return ScreeningResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")
    params = {
        "fid_rsfl_rate2": rate_max,
        "fid_cond_mrkt_div_code": market_div,
        "fid_cond_scr_div_code": "20170",
        "fid_input_iscd": input_iscd,
        "fid_rank_sort_cls_code": rank_sort,
        "fid_input_cnt_1": count,
        "fid_prc_cls_code": price_cls,
        "fid_input_price_1": price_min,
        "fid_input_price_2": price_max,
        "fid_vol_cnt": volume_min,
        "fid_trgt_cls_code": target_cls,
        "fid_trgt_exls_cls_code": target_exclude,
        "fid_div_cls_code": div_cls,
        "fid_rsfl_rate1": rate_min,
    }
    return _fetch_paged(
        api_url="/uapi/domestic-stock/v1/ranking/fluctuation",
        tr_id="FHPST01700000",
        params=params,
        max_depth=max_depth,
    )


def volume_rank(
    *,
    market_div: str = "J",
    input_iscd: str = "0000",
    div_cls: str = "0",
    blng_cls: str = "0",
    target_cls: str = "111111111",
    target_exclude: str = "0000000000",
    price_min: str = "0",
    price_max: str = "1000000",
    volume_min: str = "0",
    input_date: str = "",
    max_depth: int = 1,
) -> ScreeningResult:
    if not _assert_trenv_ready("거래량 순위"):
        return ScreeningResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")
    params = {
        "FID_COND_MRKT_DIV_CODE": market_div,
        "FID_COND_SCR_DIV_CODE": "20171",
        "FID_INPUT_ISCD": input_iscd,
        "FID_DIV_CLS_CODE": div_cls,
        "FID_BLNG_CLS_CODE": blng_cls,
        "FID_TRGT_CLS_CODE": target_cls,
        "FID_TRGT_EXLS_CLS_CODE": target_exclude,
        "FID_INPUT_PRICE_1": price_min,
        "FID_INPUT_PRICE_2": price_max,
        "FID_VOL_CNT": volume_min,
        "FID_INPUT_DATE_1": input_date,
    }
    return _fetch_paged(
        api_url="/uapi/domestic-stock/v1/quotations/volume-rank",
        tr_id="FHPST01710000",
        params=params,
        max_depth=max_depth,
    )


def volume_power_rank(
    *,
    market_div: str = "J",
    input_iscd: str = "0000",
    div_cls: str = "0",
    price_min: str = "",
    price_max: str = "",
    volume_min: str = "",
    target_cls: str = "0",
    target_exclude: str = "0",
    max_depth: int = 1,
) -> ScreeningResult:
    if not _assert_trenv_ready("체결강도 순위"):
        return ScreeningResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")
    params = {
        "fid_trgt_exls_cls_code": target_exclude,
        "fid_cond_mrkt_div_code": market_div,
        "fid_cond_scr_div_code": "20168",
        "fid_input_iscd": input_iscd,
        "fid_div_cls_code": div_cls,
        "fid_input_price_1": price_min,
        "fid_input_price_2": price_max,
        "fid_vol_cnt": volume_min,
        "fid_trgt_cls_code": target_cls,
    }
    return _fetch_paged(
        api_url="/uapi/domestic-stock/v1/ranking/volume-power",
        tr_id="FHPST01680000",
        params=params,
        max_depth=max_depth,
    )


def investor_trend_estimate(stock_code: str) -> ScreeningResult:
    if not _assert_trenv_ready(f"외인기관 추정 {stock_code}"):
        return ScreeningResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")
    if not stock_code:
        return ScreeningResult(pd.DataFrame(), False, error_message="종목코드가 필요합니다")
    return _fetch(
        api_url="/uapi/domestic-stock/v1/quotations/investor-trend-estimate",
        tr_id="HHPTJ04160200",
        params={"MKSC_SHRN_ISCD": stock_code},
        output_key="output2",
    )


def foreign_institution_total(
    *,
    market_div: str = "V",
    input_iscd: str = "0000",
    div_cls: str = "0",
    rank_sort: str = "0",
    etc_cls: str = "0",
) -> ScreeningResult:
    if not _assert_trenv_ready("외국인기관 매매종목 가집계"):
        return ScreeningResult(pd.DataFrame(), False, error_message="KIS API 인증이 필요합니다")
    params = {
        "FID_COND_MRKT_DIV_CODE": market_div,
        "FID_COND_SCR_DIV_CODE": "16449",
        "FID_INPUT_ISCD": input_iscd,
        "FID_DIV_CLS_CODE": div_cls,
        "FID_RANK_SORT_CLS_CODE": rank_sort,
        "FID_ETC_CLS_CODE": etc_cls,
    }
    return _fetch(
        api_url="/uapi/domestic-stock/v1/quotations/foreign-institution-total",
        tr_id="FHPTJ04400000",
        params=params,
    )


def investor_trade_by_stock_daily(
    *,
    stock_code: str,
    date: Optional[str] = None,
    market_div: str = "J",
    max_depth: int = 1,
) -> MultiOutputResult:
    if not _assert_trenv_ready(f"종목별 투자자매매동향 {stock_code}"):
        return MultiOutputResult({}, False, error_message="KIS API 인증이 필요합니다")
    if not stock_code:
        return MultiOutputResult({}, False, error_message="종목코드가 필요합니다")
    date = (date or datetime.now().strftime("%Y%m%d")).replace("-", "")
    params = {
        "FID_COND_MRKT_DIV_CODE": market_div,
        "FID_INPUT_ISCD": stock_code,
        "FID_INPUT_DATE_1": date,
        "FID_ORG_ADJ_PRC": "",
        "FID_ETC_CLS_CODE": "",
    }
    frames1: list[pd.DataFrame] = []
    frames2: list[pd.DataFrame] = []
    tr_cont = ""
    try:
        for _ in range(max(1, max_depth)):
            res = ka._url_fetch(
                "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily",
                "FHPTJ04160001",
                tr_cont,
                params,
            )
            if not res.isOK():
                code, message = _response_error(res, "종목별 투자자매매동향 조회 실패")
                res.printError("/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily")
                return MultiOutputResult({}, False, code, message, "/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily", "FHPTJ04160001")
            body = res.getBody()
            output1 = _frame_from_output(_body_get(body, "output1"))
            output2 = _frame_from_output(_body_get(body, "output2"))
            if not output1.empty:
                frames1.append(output1)
            if not output2.empty:
                frames2.append(output2)
            header_cont = str(getattr(res.getHeader(), "tr_cont", "") or "")
            if header_cont not in {"M", "F"}:
                break
            tr_cont = "N"
            ka.smart_sleep()
        return MultiOutputResult(
            outputs={
                "summary": pd.concat(frames1, ignore_index=True) if frames1 else pd.DataFrame(),
                "daily": pd.concat(frames2, ignore_index=True) if frames2 else pd.DataFrame(),
            },
            success=True,
            api_url="/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily",
            tr_id="FHPTJ04160001",
        )
    except Exception as exc:
        logger.exception("investor_trade_by_stock_daily error")
        return MultiOutputResult({}, False, error_message=str(exc), api_url="/uapi/domestic-stock/v1/quotations/investor-trade-by-stock-daily", tr_id="FHPTJ04160001")


def minute_chart(
    *,
    stock_code: str,
    env_dv: str = "real",
    market_div: str = "J",
    input_time: Optional[str] = None,
    include_past: str = "Y",
) -> MultiOutputResult:
    if not _assert_trenv_ready(f"국내 분봉 {stock_code}"):
        return MultiOutputResult({}, False, error_message="KIS API 인증이 필요합니다")
    if not stock_code:
        return MultiOutputResult({}, False, error_message="종목코드가 필요합니다")
    input_time = input_time or datetime.now().strftime("%H%M%S")
    mode = _normalize_env(env_dv)
    if mode not in {"real", "demo"}:
        return MultiOutputResult({}, False, error_message="env_dv는 real/demo/prod/vps만 지원합니다")
    params = {
        "FID_COND_MRKT_DIV_CODE": market_div,
        "FID_INPUT_ISCD": stock_code,
        "FID_INPUT_HOUR_1": input_time,
        "FID_PW_DATA_INCU_YN": include_past,
        "FID_ETC_CLS_CODE": "",
    }
    try:
        res = ka._url_fetch(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            "FHKST03010200",
            "",
            params,
        )
        if not res.isOK():
            code, message = _response_error(res, "국내 분봉 조회 실패")
            res.printError("/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice")
            return MultiOutputResult({}, False, code, message, "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice", "FHKST03010200")
        body = res.getBody()
        return MultiOutputResult(
            outputs={
                "quote": _frame_from_output(_body_get(body, "output1")),
                "bars": _frame_from_output(_body_get(body, "output2")),
            },
            success=True,
            api_url="/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            tr_id="FHKST03010200",
        )
    except Exception as exc:
        logger.exception("minute_chart error")
        return MultiOutputResult({}, False, error_message=str(exc), api_url="/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice", tr_id="FHKST03010200")
