"""
전략 관련 API Router

strategy_core 모듈을 사용하여 전략 조회/실행/빌드를 처리합니다.
"""

import time
import logging
import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from strategy_core import StrategyRegistry
import strategy_core.preset  # 10개 전략 자동 등록
from strategy_core.name_utils import sanitize_strategy_name
from strategy_core.executor import (
    execute_with_class,
    execute_from_builder_state,
    execute_custom_file,
)
from backend import authenticate, is_authenticated, get_current_mode
import kis_auth as ka
from strategy_core.dsl.codegen import StrategyCodeGenerator, generate_strategy_file
from strategy_core.dsl.parser import parse_strategy, StrategyDSLParser
from strategy_core.dsl.converter import builder_state_to_dsl
from core import data_fetcher, overseas_data_fetcher

router = APIRouter()
logger = logging.getLogger(__name__)
PRICE_FIELDS = {"close", "open", "high", "low"}


def _api_sleep():
    """모드에 따른 API 호출 간격 sleep (kis_auth rate limiter 보완)"""
    interval = 0.2 if ka.isPaperTrading() else 0.05
    time.sleep(interval)




def _custom_yaml_dirs() -> List[Path]:
    """Return custom strategy directories for local and Docker layouts."""
    strategy_builder_root = Path(__file__).resolve().parents[2]
    project_root = strategy_builder_root.parent
    candidates = [
        strategy_builder_root / "strategies" / "custom",
        project_root / "strategies" / "custom",
    ]

    seen: set[Path] = set()
    result: List[Path] = []
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved not in seen and resolved.is_dir():
            seen.add(resolved)
            result.append(resolved)
    return result


def _operand_from_indicator(name: str, output: str = "value") -> Dict[str, Any]:
    if name in PRICE_FIELDS:
        return {"type": "price", "priceField": name}
    return {"type": "indicator", "indicatorAlias": name, "indicatorOutput": output}


def _right_operand(condition: Dict[str, Any], indicator_aliases: set[str]) -> Dict[str, Any]:
    if "value" in condition:
        return {"type": "value", "value": condition.get("value")}

    compare_to = condition.get("compare_to")
    if compare_to is None:
        return {"type": "value", "value": 0}
    if isinstance(compare_to, (int, float)):
        return {"type": "value", "value": compare_to}
    if isinstance(compare_to, str):
        if compare_to in PRICE_FIELDS:
            return {"type": "price", "priceField": compare_to}
        if compare_to in indicator_aliases:
            return {
                "type": "indicator",
                "indicatorAlias": compare_to,
                "indicatorOutput": condition.get("compare_output", "value"),
            }
        try:
            return {"type": "value", "value": float(compare_to)}
        except ValueError:
            return {
                "type": "indicator",
                "indicatorAlias": compare_to,
                "indicatorOutput": condition.get("compare_output", "value"),
            }

    return {"type": "value", "value": 0}


def _condition_group_from_yaml(
    group: Dict[str, Any],
    prefix: str,
    indicator_aliases: set[str],
) -> Dict[str, Any]:
    conditions = []
    for index, condition in enumerate(group.get("conditions", []), start=1):
        conditions.append({
            "id": f"{prefix}_{index}",
            "left": _operand_from_indicator(
                condition.get("indicator", ""),
                condition.get("output", "value"),
            ),
            "operator": condition.get("operator", "greater_than"),
            "right": _right_operand(condition, indicator_aliases),
        })

    return {
        "logic": group.get("logic", "AND"),
        "conditions": conditions,
    }


def _builder_state_from_kis_yaml(data: Dict[str, Any]) -> Dict[str, Any]:
    strategy = data.get("strategy", {})
    metadata = data.get("metadata", {})
    indicators = []
    indicator_aliases: set[str] = set()

    for index, indicator in enumerate(strategy.get("indicators", []), start=1):
        indicator_id = indicator.get("id", "")
        alias = indicator.get("alias") or f"{indicator_id}_{index}"
        indicator_aliases.add(alias)
        indicators.append({
            "id": f"{indicator_id}_{index}",
            "indicatorId": indicator_id,
            "alias": alias,
            "params": indicator.get("params") or {},
            "output": indicator.get("output", "value"),
        })

    risk = data.get("risk") or {}

    return {
        "metadata": {
            "id": strategy.get("id", ""),
            "name": metadata.get("name") or strategy.get("id", "custom_strategy"),
            "description": metadata.get("description", ""),
            "category": strategy.get("category", "custom"),
            "tags": metadata.get("tags") or [],
            "author": metadata.get("author", "user"),
        },
        "indicators": indicators,
        "entry": _condition_group_from_yaml(
            strategy.get("entry") or {},
            "entry",
            indicator_aliases,
        ),
        "exit": _condition_group_from_yaml(
            strategy.get("exit") or {},
            "exit",
            indicator_aliases,
        ),
        "risk": {
            "stopLoss": {
                "enabled": risk.get("stop_loss", {}).get("enabled", False),
                "percent": risk.get("stop_loss", {}).get("percent", 5),
            },
            "takeProfit": {
                "enabled": risk.get("take_profit", {}).get("enabled", False),
                "percent": risk.get("take_profit", {}).get("percent", 10),
            },
            "trailingStop": {
                "enabled": risk.get("trailing_stop", {}).get("enabled", False),
                "percent": risk.get("trailing_stop", {}).get("percent", 3),
            },
        },
    }


def _load_custom_yaml_strategies() -> List[Dict[str, Any]]:
    strategies = []
    for directory in _custom_yaml_dirs():
        for path in sorted(directory.glob("*.kis.yaml")):
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                builder_state = _builder_state_from_kis_yaml(data)
                strategy_id = builder_state["metadata"]["id"] or path.stem
                strategies.append({
                    "id": strategy_id,
                    "name": builder_state["metadata"]["name"],
                    "description": builder_state["metadata"]["description"],
                    "category": builder_state["metadata"]["category"],
                    "params": [],
                    "builder_state": builder_state,
                    "source": "custom_yaml",
                    "filename": path.name,
                })
            except Exception as exc:
                logger.warning("Failed to load custom strategy YAML %s: %s", path, exc)
    return strategies


def _get_custom_yaml_strategy(strategy_id: str) -> Optional[Dict[str, Any]]:
    for strategy in _load_custom_yaml_strategies():
        if strategy["id"] == strategy_id:
            return strategy
    return None


# ============================================
# 종목명 조회 (마스터파일 기반)
# ============================================

def get_stock_name(code: str) -> str:
    """종목코드로 종목명 조회

    symbols 모듈의 마스터파일 캐시를 사용하여 종목명을 반환합니다.
    캐시에 없는 경우 종목코드를 그대로 반환합니다.
    """
    context = data_fetcher.get_market_context()
    if context.get("market") == "us":
        meta = (context.get("symbol_meta") or {}).get(code) or (context.get("symbol_meta") or {}).get(code.upper()) or {}
        exchange = meta.get("exchange") if isinstance(meta, dict) else None
        return overseas_data_fetcher.resolve_exchange(code, exchange).name

    from backend.routers.symbols import _get_all_symbols, FALLBACK_STOCKS

    all_symbols = _get_all_symbols() or FALLBACK_STOCKS

    for stock in all_symbols:
        if stock["code"] == code:
            return stock["name"]

    return code


# ============================================
# Request/Response Models
# ============================================

class ExecuteRequest(BaseModel):
    strategy_id: str
    stocks: List[str]
    params: Dict[str, Any] = {}
    builder_state: Optional[Dict[str, Any]] = None
    market: str = "domestic"
    symbol_meta: Dict[str, Dict[str, Any]] = {}


class BuildRequest(BaseModel):
    name: str
    buy_condition: str
    sell_condition: Optional[str] = None


class SignalResult(BaseModel):
    code: str
    name: str
    action: str
    strength: float
    reason: str
    target_price: Optional[float] = None
    exchange: Optional[str] = None
    warning: Optional[str] = None


class LogEntry(BaseModel):
    type: str
    message: str
    timestamp: Optional[str] = None


class ExecuteResponse(BaseModel):
    status: str
    results: List[SignalResult] = []
    logs: List[LogEntry] = []
    message: Optional[str] = None


def _decorate_results_for_market(
    results: list[dict[str, Any]],
    market: str,
    symbol_meta: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if market != "us":
        return results
    decorated = []
    for result in results:
        code = str(result.get("code") or "").upper()
        meta = symbol_meta.get(code) or {}
        resolution = overseas_data_fetcher.resolve_exchange(code, meta.get("exchange"))
        decorated.append({
            **result,
            "code": code,
            "name": resolution.name or result.get("name") or code,
            "exchange": resolution.exchange,
            **({"warning": resolution.warning} if resolution.warning else {}),
        })
    return decorated


# ============================================
# API Endpoints
# ============================================

@router.get("")
async def list_strategies():
    """전략 목록 조회 - builder_state 포함 (SSoT)"""
    strategies = StrategyRegistry.get_list()
    known_ids = {strategy["id"] for strategy in strategies}
    strategies.extend(
        strategy
        for strategy in _load_custom_yaml_strategies()
        if strategy["id"] not in known_ids
    )
    return {"strategies": strategies}


@router.get("/custom")
async def list_custom_strategies():
    """커스텀 전략 목록 조회"""
    import os
    import re

    custom_strategies = []
    strategy_dir = os.path.join(os.path.dirname(__file__), "..", "..", "strategy")

    default_files = {
        'strategy_01_golden_cross.py', 'strategy_02_momentum.py',
        'strategy_03_week52_high.py', 'strategy_04_consecutive.py',
        'strategy_05_disparity.py', 'strategy_06_breakout_fail.py',
        'strategy_07_strong_close.py', 'strategy_08_volatility.py',
        'strategy_09_mean_reversion.py', 'strategy_10_trend_filter.py',
        'base_strategy.py', '__init__.py'
    }

    for filename in os.listdir(strategy_dir):
        if filename.endswith('.py') and filename not in default_files:
            filepath = os.path.join(strategy_dir, filename)

            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read()

                name = filename.replace('strategy_', '').replace('.py', '')
                buy_condition = ""
                sell_condition = ""
                description = ""

                if '"""' in content:
                    doc_start = content.find('"""', content.find('class '))
                    if doc_start > 0:
                        doc_end = content.find('"""', doc_start + 3)
                        if doc_end > 0:
                            doc = content[doc_start+3:doc_end].strip()
                            for line in doc.split('\n'):
                                line = line.strip()
                                if line.startswith('매수 조건:'):
                                    buy_condition = line.replace('매수 조건:', '').strip()
                                elif line.startswith('매도 조건:'):
                                    sell_condition = line.replace('매도 조건:', '').strip()

                            if buy_condition:
                                description = f"매수: {buy_condition}"

                custom_strategies.append({
                    'id': f'custom:{name}',
                    'name': name.replace('_', ' ').title(),
                    'description': description or '사용자 정의 전략',
                    'category': '커스텀',
                    'filename': filename,
                    'params': [],
                    'buy_condition': buy_condition,
                    'sell_condition': sell_condition,
                })
            except Exception:
                pass

    return {"strategies": custom_strategies}


@router.get("/indicators")
async def list_indicators():
    """사용 가능한 지표 목록"""
    return {
        "indicators": [
            {"name": "ma", "label": "이동평균", "params": ["period"], "example": "ma(20)"},
            {"name": "ema", "label": "지수이동평균", "params": ["period"], "example": "ema(12)"},
            {"name": "rsi", "label": "RSI", "params": ["period"], "example": "rsi(14)"},
            {"name": "macd", "label": "MACD", "params": ["fast", "slow", "signal"], "example": "macd(12,26,9)"},
            {"name": "macd_signal", "label": "MACD 시그널", "params": ["fast", "slow", "signal"], "example": "macd_signal(12,26,9)"},
            {"name": "bb_upper", "label": "볼린저 상단", "params": ["period", "std"], "example": "bb_upper(20,2)"},
            {"name": "bb_lower", "label": "볼린저 하단", "params": ["period", "std"], "example": "bb_lower(20,2)"},
            {"name": "atr", "label": "ATR", "params": ["period"], "example": "atr(14)"},
            {"name": "adx", "label": "ADX", "params": ["period"], "example": "adx(14)"},
            {"name": "stoch_k", "label": "스토캐스틱 %K", "params": ["period"], "example": "stoch_k(14)"},
        ],
        "variables": ["close", "open", "high", "low", "volume", "change"],
        "operators": {
            "comparison": [">", "<", ">=", "<=", "=="],
            "crossover": ["crosses_above", "crosses_below"],
            "logical": ["AND", "OR"],
        },
    }


@router.post("/execute", response_model=ExecuteResponse)
async def execute_strategy(request: ExecuteRequest):
    """전략 실행"""
    strategy_id = request.strategy_id
    market = "us" if request.market == "us" else "domestic"
    stocks = [
        stock.strip().upper() if market == "us" else stock.strip()
        for stock in request.stocks
        if stock and stock.strip()
    ]
    params = request.params
    logs = []

    def log(msg_type: str, message: str):
        logs.append(LogEntry(
            type=msg_type,
            message=message,
            timestamp=datetime.datetime.now().strftime("%H:%M:%S"),
        ))

    # 인증 확인
    if not is_authenticated():
        log("error", "KIS API 인증 필요 - 설정에서 인증해주세요")
        return ExecuteResponse(status='error', logs=logs, message='인증이 필요합니다')

    current_mode = get_current_mode()
    mode_display = "모의투자" if current_mode == "vps" else "실전투자"
    market_display = "미국" if market == "us" else "한국"
    log("info", f"KIS API 인증 확인 ({mode_display}, {market_display})")

    token = data_fetcher.set_market_context(market, request.symbol_meta)

    try:
        # 1) 로컬 전략 (프론트엔드에서 builder_state 직접 전달)
        if strategy_id.startswith('local_'):
            if not request.builder_state:
                log("error", "로컬 전략 실행에는 builder_state가 필요합니다")
                return ExecuteResponse(status='error', logs=logs, message='builder_state 필요')

            strategy_name = request.builder_state.get('metadata', {}).get('name', 'Local Strategy')
            log("info", f"로컬 전략: {strategy_name}")
            log("info", f"종목: {', '.join(stocks)}")

            results = execute_from_builder_state(
                request.builder_state, strategy_name, stocks,
                log, get_stock_name, _api_sleep,
            )
            results = _decorate_results_for_market(results, market, request.symbol_meta)
            log("success", "로컬 전략 실행 완료")
            return ExecuteResponse(
                status='success',
                results=[SignalResult(**r) for r in results],
                logs=logs,
            )

        # 2) 커스텀 전략 (파일 기반)
        if strategy_id.startswith('custom:'):
            import os
            import re
            custom_name = strategy_id.removeprefix('custom:')
            if not re.fullmatch(r'[a-zA-Z0-9_]+', custom_name):
                raise HTTPException(400, "유효하지 않은 전략 ID")
            log("info", f"커스텀 전략: {custom_name}")
            log("info", f"종목: {', '.join(stocks)}")

            strategy_dir = os.path.join(os.path.dirname(__file__), "..", "..", "strategy")
            results = execute_custom_file(
                custom_name, strategy_dir, stocks,
                log, get_stock_name, _api_sleep,
            )
            results = _decorate_results_for_market(results, market, request.symbol_meta)
            log("success", "전략 실행 완료")
            return ExecuteResponse(
                status='success',
                results=[SignalResult(**r) for r in results],
                logs=logs,
            )

        # 3) 레지스트리 전략
        schema = StrategyRegistry.get(strategy_id)
        if not schema:
            custom_yaml = _get_custom_yaml_strategy(strategy_id)
            if custom_yaml:
                schema = {
                    **custom_yaml,
                    "strategy_class": None,
                    "param_map": {},
                }
        if not schema:
            log("error", f"알 수 없는 전략: {strategy_id}")
            return ExecuteResponse(status='error', logs=logs, message=f'알 수 없는 전략: {strategy_id}')

        # 3a) 빌더 전용 전략 (strategy_class=None)
        if schema.get('strategy_class') is None:
            builder_state = schema.get('builder_state', {})
            if not builder_state:
                log("error", f"전략 builder_state가 없습니다: {strategy_id}")
                return ExecuteResponse(status='error', logs=logs, message='builder_state 필요')

            strategy_name = schema.get('name', strategy_id)
            log("info", f"빌더 전략: {strategy_name}")
            log("info", f"종목: {', '.join(stocks)}")

            results = execute_from_builder_state(
                builder_state, strategy_name, stocks,
                log, get_stock_name, _api_sleep,
            )
            results = _decorate_results_for_market(results, market, request.symbol_meta)
            log("success", "빌더 전략 실행 완료")
            return ExecuteResponse(
                status='success',
                results=[SignalResult(**r) for r in results],
                logs=logs,
            )

        # 3b) 기본 전략 (strategy_class 사용)
        results = execute_with_class(
            schema['strategy_class'], schema['param_map'], params, stocks,
            strategy_id, log, get_stock_name, _api_sleep,
        )
        results = _decorate_results_for_market(results, market, request.symbol_meta)
        log("success", "전략 실행 완료")
        return ExecuteResponse(
            status='success',
            results=[SignalResult(**r) for r in results],
            logs=logs,
        )

    except Exception as e:
        log("error", f"전략 실행 오류: {str(e)}")
        return ExecuteResponse(status='error', logs=logs, message=str(e))
    finally:
        data_fetcher.reset_market_context(token)


@router.post("/build")
async def build_strategy(request: BuildRequest):
    """커스텀 전략 생성"""
    try:
        name_snake = sanitize_strategy_name(request.name)

        parser = StrategyDSLParser()
        parser.parse(request.buy_condition)
        if request.sell_condition:
            parser.parse(request.sell_condition)

        file_path = generate_strategy_file(
            name=name_snake,
            name_ko=request.name,
            buy_condition=request.buy_condition,
            sell_condition=request.sell_condition,
            output_dir="strategy",
        )

        return {
            "status": "success",
            "message": f"전략 생성 완료: {file_path}",
            "file_path": file_path,
            "strategy_name": request.name,
        }

    except Exception as e:
        return {
            "status": "error",
            "message": f"전략 생성 실패: {str(e)}",
        }


@router.post("/preview")
async def preview_strategy(request: BuildRequest):
    """커스텀 전략 미리보기 (코드 생성만)"""
    try:
        name_snake = sanitize_strategy_name(request.name)

        strategy = parse_strategy(
            name=name_snake,
            name_ko=request.name,
            buy_condition=request.buy_condition,
            sell_condition=request.sell_condition,
        )

        generator = StrategyCodeGenerator()
        code = generator.generate(strategy)

        return {
            "status": "success",
            "code": code,
            "required_days": strategy.get_required_days(),
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }


@router.post("/preview-code")
async def preview_code_from_state(request: dict):
    """BuilderState → Python 코드 미리보기"""
    try:
        builder_state = request.get("builder_state", {})
        strategy_name = builder_state.get("metadata", {}).get("name", "custom")

        buy_condition, sell_condition = builder_state_to_dsl(builder_state)

        if not buy_condition:
            return {"status": "error", "message": "매수 조건이 없습니다"}

        # 클래스명용 snake_case: metadata.id 우선 (preset은 영문 id 보유)
        metadata_id = builder_state.get("metadata", {}).get("id", "")
        if metadata_id and metadata_id != "custom_strategy":
            name_snake = sanitize_strategy_name(metadata_id)
        else:
            name_snake = sanitize_strategy_name(strategy_name)

        strategy_def = parse_strategy(
            name=name_snake,
            name_ko=strategy_name,
            buy_condition=buy_condition,
            sell_condition=sell_condition,
        )

        generator = StrategyCodeGenerator()
        code = generator.generate(strategy_def)

        return {
            "status": "success",
            "code": code,
            "buy_dsl": buy_condition,
            "sell_dsl": sell_condition,
        }

    except Exception as e:
        return {
            "status": "error",
            "message": str(e),
        }
