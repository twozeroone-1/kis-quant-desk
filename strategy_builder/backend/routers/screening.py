"""
Domestic candidate screening and intraday data APIs.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query

from backend import get_current_mode, is_authenticated
from core import domestic_screening

logger = logging.getLogger(__name__)

router = APIRouter()


def _require_authentication() -> None:
    if not is_authenticated():
        raise HTTPException(status_code=401, detail="KIS API 인증이 필요합니다.")


def _single_response(result: domestic_screening.ScreeningResult) -> dict[str, Any]:
    metadata = {
        "api_url": result.api_url,
        "tr_id": result.tr_id,
        "error_code": result.error_code,
    }
    if not result.success:
        return {
            "status": "error",
            "message": result.display_error(),
            "items": [],
            "total_count": 0,
            "data": metadata,
        }

    records = result.records()
    return {
        "status": "success",
        "items": records,
        "total_count": len(records),
        "data": metadata,
    }


def _multi_response(result: domestic_screening.MultiOutputResult) -> dict[str, Any]:
    metadata = {
        "api_url": result.api_url,
        "tr_id": result.tr_id,
        "error_code": result.error_code,
    }
    if not result.success:
        return {
            "status": "error",
            "message": result.display_error(),
            "outputs": {},
            "counts": {},
            "data": metadata,
        }

    outputs = result.records()
    return {
        "status": "success",
        "outputs": outputs,
        "counts": {key: len(value) for key, value in outputs.items()},
        "data": metadata,
    }


@router.get("/condition-searches")
def get_condition_searches(user_id: Optional[str] = Query(None, description="HTS ID. 생략 시 인증 환경의 HTS ID 사용")):
    """조건검색 목록을 조회합니다."""
    _require_authentication()
    result = domestic_screening.condition_search_titles(user_id=user_id)
    return _single_response(result)


@router.get("/condition-searches/{seq}/results")
def get_condition_search_results(
    seq: str,
    user_id: Optional[str] = Query(None, description="HTS ID. 생략 시 인증 환경의 HTS ID 사용"),
):
    """조건검색 결과를 조회합니다."""
    _require_authentication()
    result = domestic_screening.condition_search_results(seq=seq, user_id=user_id)
    return _single_response(result)


@router.get("/rankings/market-cap")
def get_market_cap_rank(
    market_div: str = Query("J", description="시장 구분. J=주식"),
    input_iscd: str = Query("0000", description="시장/업종 코드"),
    div_cls: str = Query("0", description="분류 코드"),
    price_min: str = Query("", description="최저가 필터"),
    price_max: str = Query("", description="최고가 필터"),
    volume_min: str = Query("", description="최소 거래량"),
    max_depth: int = Query(1, ge=1, le=20, description="연속조회 최대 횟수"),
):
    """시가총액 순위를 조회합니다."""
    _require_authentication()
    result = domestic_screening.market_cap_rank(
        market_div=market_div,
        input_iscd=input_iscd,
        div_cls=div_cls,
        price_min=price_min,
        price_max=price_max,
        volume_min=volume_min,
        max_depth=max_depth,
    )
    return _single_response(result)


@router.get("/rankings/fluctuation")
def get_fluctuation_rank(
    market_div: str = Query("J", description="시장 구분. J=주식"),
    input_iscd: str = Query("0000", description="시장/업종 코드"),
    rank_sort: str = Query("0000", description="정렬 구분"),
    count: str = Query("0", description="조회 개수 필터"),
    price_cls: str = Query("0", description="가격 구분"),
    price_min: str = Query("0", description="최저가 필터"),
    price_max: str = Query("1000000", description="최고가 필터"),
    volume_min: str = Query("0", description="최소 거래량"),
    target_cls: str = Query("0", description="대상 종목 구분"),
    target_exclude: str = Query("0", description="제외 종목 구분"),
    div_cls: str = Query("0", description="분류 코드"),
    rate_min: str = Query("0", description="최소 등락률"),
    rate_max: str = Query("999", description="최대 등락률"),
    max_depth: int = Query(1, ge=1, le=20, description="연속조회 최대 횟수"),
):
    """등락률 순위를 조회합니다."""
    _require_authentication()
    result = domestic_screening.fluctuation_rank(
        market_div=market_div,
        input_iscd=input_iscd,
        rank_sort=rank_sort,
        count=count,
        price_cls=price_cls,
        price_min=price_min,
        price_max=price_max,
        volume_min=volume_min,
        target_cls=target_cls,
        target_exclude=target_exclude,
        div_cls=div_cls,
        rate_min=rate_min,
        rate_max=rate_max,
        max_depth=max_depth,
    )
    return _single_response(result)


@router.get("/rankings/volume")
def get_volume_rank(
    market_div: str = Query("J", description="시장 구분. J=주식"),
    input_iscd: str = Query("0000", description="시장/업종 코드"),
    div_cls: str = Query("0", description="분류 코드"),
    blng_cls: str = Query("0", description="소속 구분"),
    target_cls: str = Query("111111111", description="대상 종목 구분"),
    target_exclude: str = Query("0000000000", description="제외 종목 구분"),
    price_min: str = Query("0", description="최저가 필터"),
    price_max: str = Query("1000000", description="최고가 필터"),
    volume_min: str = Query("0", description="최소 거래량"),
    input_date: str = Query("", description="기준일 YYYYMMDD"),
    max_depth: int = Query(1, ge=1, le=20, description="연속조회 최대 횟수"),
):
    """거래량 순위를 조회합니다."""
    _require_authentication()
    result = domestic_screening.volume_rank(
        market_div=market_div,
        input_iscd=input_iscd,
        div_cls=div_cls,
        blng_cls=blng_cls,
        target_cls=target_cls,
        target_exclude=target_exclude,
        price_min=price_min,
        price_max=price_max,
        volume_min=volume_min,
        input_date=input_date,
        max_depth=max_depth,
    )
    return _single_response(result)


@router.get("/rankings/volume-power")
def get_volume_power_rank(
    market_div: str = Query("J", description="시장 구분. J=주식"),
    input_iscd: str = Query("0000", description="시장/업종 코드"),
    div_cls: str = Query("0", description="분류 코드"),
    price_min: str = Query("", description="최저가 필터"),
    price_max: str = Query("", description="최고가 필터"),
    volume_min: str = Query("", description="최소 거래량"),
    target_cls: str = Query("0", description="대상 종목 구분"),
    target_exclude: str = Query("0", description="제외 종목 구분"),
    max_depth: int = Query(1, ge=1, le=20, description="연속조회 최대 횟수"),
):
    """체결강도 순위를 조회합니다."""
    _require_authentication()
    result = domestic_screening.volume_power_rank(
        market_div=market_div,
        input_iscd=input_iscd,
        div_cls=div_cls,
        price_min=price_min,
        price_max=price_max,
        volume_min=volume_min,
        target_cls=target_cls,
        target_exclude=target_exclude,
        max_depth=max_depth,
    )
    return _single_response(result)


@router.get("/investors/trend/{stock_code}")
def get_investor_trend_estimate(stock_code: str):
    """종목별 외국인/기관 추정 매매동향을 조회합니다."""
    _require_authentication()
    result = domestic_screening.investor_trend_estimate(stock_code=stock_code.strip())
    return _single_response(result)


@router.get("/investors/foreign-institution")
def get_foreign_institution_total(
    market_div: str = Query("V", description="시장 구분"),
    input_iscd: str = Query("0000", description="시장/업종 코드"),
    div_cls: str = Query("0", description="분류 코드"),
    rank_sort: str = Query("0", description="정렬 구분"),
    etc_cls: str = Query("0", description="기타 구분"),
):
    """외국인/기관 매매종목 가집계를 조회합니다."""
    _require_authentication()
    result = domestic_screening.foreign_institution_total(
        market_div=market_div,
        input_iscd=input_iscd,
        div_cls=div_cls,
        rank_sort=rank_sort,
        etc_cls=etc_cls,
    )
    return _single_response(result)


@router.get("/investors/daily/{stock_code}")
def get_investor_trade_by_stock_daily(
    stock_code: str,
    date: Optional[str] = Query(None, description="기준일 YYYYMMDD 또는 YYYY-MM-DD. 생략 시 오늘"),
    market_div: str = Query("J", description="시장 구분. J=주식"),
    max_depth: int = Query(1, ge=1, le=20, description="연속조회 최대 횟수"),
):
    """종목별 일별 투자자 매매동향을 조회합니다."""
    _require_authentication()
    result = domestic_screening.investor_trade_by_stock_daily(
        stock_code=stock_code.strip(),
        date=date,
        market_div=market_div,
        max_depth=max_depth,
    )
    return _multi_response(result)


@router.get("/minute-chart/{stock_code}")
def get_minute_chart(
    stock_code: str,
    market_div: str = Query("J", description="시장 구분. J=주식"),
    input_time: Optional[str] = Query(None, description="조회 기준 시각 HHMMSS. 생략 시 현재 시각"),
    include_past: str = Query("Y", description="과거 데이터 포함 여부 Y/N"),
):
    """국내 주식 분봉 데이터를 조회합니다."""
    _require_authentication()
    result = domestic_screening.minute_chart(
        stock_code=stock_code.strip(),
        env_dv=get_current_mode(),
        market_div=market_div,
        input_time=input_time,
        include_past=include_past,
    )
    return _multi_response(result)
