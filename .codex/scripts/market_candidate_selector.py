"""Liquidity-first dynamic candidate selection for market auto-runs."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Callable


KR_DYNAMIC_MIN_COUNT = 5
US_DYNAMIC_MIN_COUNT = 5
US_CORE_ETFS = {
    "SPY": ("SPY", "NYSE"),
    "QQQ": ("QQQ", "NASD"),
    "DIA": ("DIA", "NYSE"),
    "IWM": ("IWM", "NYSE"),
}

KR_SOURCE_WEIGHTS = {
    "volume_rank": 40.0,
    "volume_power_rank": 25.0,
    "market_cap_rank": 20.0,
    "foreign_institution": 15.0,
}

US_SOURCE_WEIGHTS = {
    "trade_value_rank": 40.0,
    "volume_power_rank": 25.0,
    "market_cap_rank": 20.0,
    "volume_surge_rank": 15.0,
}

US_EXCHANGE_ALIASES = {
    "NAS": "NASD",
    "NASD": "NASD",
    "NASDAQ": "NASD",
    "NYS": "NYSE",
    "NYSE": "NYSE",
    "AMS": "AMEX",
    "AMEX": "AMEX",
}

US_PRICE_EXCHANGE_BY_TRADING = {
    "NASD": "NAS",
    "NYSE": "NYS",
    "AMEX": "AMS",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _first_value(row: dict[str, Any], keys: tuple[str, ...], default: Any = "") -> Any:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return default


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(str(value).replace(",", "").replace("%", ""))
    except (TypeError, ValueError):
        return default


def _rank_score(row: dict[str, Any], weight: float, index: int) -> float:
    rank = _to_float(_first_value(row, ("rank", "data_rank", "hts_rank", "순위"), index + 1), index + 1)
    rank = max(rank, 1.0)
    return max(weight * 0.1, weight * (1.0 - min(rank - 1.0, 49.0) / 50.0))


def _static_kr_candidates(static_candidates: list[tuple[str, str, str]], limit: int) -> list[dict[str, Any]]:
    selected = []
    for index, (code, name, category) in enumerate(static_candidates[:limit]):
        selected.append({
            "code": code,
            "symbol": code,
            "name": name,
            "market": "KR",
            "exchange": "KRX",
            "category": category,
            "score": round(max(1.0, 100.0 - index), 2),
            "sources": ["static"],
            "reasons": ["static fallback/core seed"],
            "metrics": {"rank": index + 1},
        })
    return selected


def _static_us_candidates(static_candidates: list[tuple[str, str]], limit: int) -> list[dict[str, Any]]:
    selected = []
    for index, (symbol, exchange) in enumerate(static_candidates[:limit]):
        normalized_exchange = normalize_us_exchange(exchange) or "NASD"
        selected.append({
            "code": symbol.upper(),
            "symbol": symbol.upper(),
            "name": symbol.upper(),
            "market": "US",
            "exchange": normalized_exchange,
            "category": "core_etf" if symbol.upper() in US_CORE_ETFS else "large_cap",
            "score": round(max(1.0, 100.0 - index), 2),
            "sources": ["static"],
            "reasons": ["static fallback/core seed"],
            "metrics": {"rank": index + 1},
        })
    return selected


def normalize_kr_candidate_row(row: dict[str, Any], source: str = "") -> dict[str, Any] | None:
    code = str(_first_value(row, (
        "mksc_shrn_iscd",
        "stck_shrn_iscd",
        "pdno",
        "isu_cd",
        "code",
        "stock_code",
        "종목코드",
    ))).strip()
    code = re.sub(r"\D", "", code)
    if not re.fullmatch(r"\d{6}", code):
        return None

    name = str(_first_value(row, (
        "hts_kor_isnm",
        "stck_prdt_name",
        "prdt_name",
        "isu_abbrv",
        "name",
        "stock_name",
        "종목명",
    ), code)).strip() or code
    category = "ETF" if any(token in name.upper() for token in ("ETF", "KODEX", "TIGER", "SOL ", "ACE ")) else "대형주"
    metrics = {
        "rank": _to_float(_first_value(row, ("rank", "data_rank", "hts_rank", "순위"), 0)),
        "volume": _to_float(_first_value(row, ("acml_vol", "vol", "volume", "cntg_vol", "stck_vol", "거래량"), 0)),
        "trade_value": _to_float(_first_value(row, ("acml_tr_pbmn", "tr_pbmn", "trade_amt", "tamt", "거래대금"), 0)),
        "market_cap": _to_float(_first_value(row, ("stotprice", "hts_avls", "avls", "mkt_cap", "시가총액"), 0)),
        "volume_power": _to_float(_first_value(row, ("tday_rltv", "cntg_csnu", "volume_power", "체결강도"), 0)),
        "foreign_institution": _to_float(_first_value(row, ("frgn_ntby_qty", "orgn_ntby_qty", "ntby_qty", "순매수수량"), 0)),
    }
    return {
        "code": code,
        "symbol": code,
        "name": name,
        "market": "KR",
        "exchange": "KRX",
        "category": category,
        "source": source,
        "metrics": metrics,
    }


def normalize_us_exchange(exchange: Any) -> str | None:
    if exchange in (None, ""):
        return None
    return US_EXCHANGE_ALIASES.get(str(exchange).strip().upper())


def us_price_exchange(exchange: Any) -> str | None:
    trading_exchange = normalize_us_exchange(exchange)
    if not trading_exchange:
        return None
    return US_PRICE_EXCHANGE_BY_TRADING[trading_exchange]


def normalize_us_candidate_row(row: dict[str, Any], source: str = "", fallback_exchange: str | None = None) -> dict[str, Any] | None:
    symbol = str(_first_value(row, ("symb", "symbol", "pdno", "ovrs_pdno", "code", "stock_code", "종목코드"))).strip().upper()
    symbol = symbol.replace(" ", "")
    if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", symbol):
        return None
    exchange = normalize_us_exchange(_first_value(row, ("excd", "exchange", "ovrs_excg_cd", "거래소코드"), fallback_exchange or ""))
    if exchange not in {"NASD", "NYSE", "AMEX"}:
        return None
    name = str(_first_value(row, ("name", "ename", "enam", "knam", "prdt_name", "stock_name", "종목명"), symbol)).strip() or symbol
    metrics = {
        "rank": _to_float(_first_value(row, ("rank", "data_rank", "순위"), 0)),
        "volume": _to_float(_first_value(row, ("tvol", "volume", "거래량"), 0)),
        "trade_value": _to_float(_first_value(row, ("tamt", "a_tamt", "trade_value", "거래대금"), 0)),
        "market_cap": _to_float(_first_value(row, ("mcap", "tomv", "market_cap", "시가총액"), 0)),
        "volume_power": _to_float(_first_value(row, ("tpow", "powx", "strn", "volume_power", "체결강도"), 0)),
        "volume_surge": _to_float(_first_value(row, ("n_rate", "trat", "volume_surge", "증가율"), 0)),
    }
    return {
        "code": symbol,
        "symbol": symbol,
        "name": name,
        "market": "US",
        "exchange": exchange,
        "category": "core_etf" if symbol in US_CORE_ETFS else "large_cap",
        "source": source,
        "metrics": metrics,
    }


def _merge_candidate(accumulator: dict[str, dict[str, Any]], normalized: dict[str, Any], source: str, weight: float, row: dict[str, Any], index: int) -> None:
    key = normalized["symbol"]
    entry = accumulator.setdefault(key, {
        "code": normalized["code"],
        "symbol": normalized["symbol"],
        "name": normalized["name"],
        "market": normalized["market"],
        "exchange": normalized["exchange"],
        "category": normalized["category"],
        "score": 0.0,
        "sources": [],
        "reasons": [],
        "metrics": {},
    })
    entry["score"] += _rank_score(row, weight, index)
    if source not in entry["sources"]:
        entry["sources"].append(source)
    reason = source.replace("_", " ")
    if reason not in entry["reasons"]:
        entry["reasons"].append(reason)
    entry["metrics"].update({key: value for key, value in normalized.get("metrics", {}).items() if value not in (None, "", 0)})


def _finalize_selection(accumulator: dict[str, dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    selected = sorted(accumulator.values(), key=lambda item: (-float(item.get("score") or 0), item.get("symbol", "")))[:limit]
    for item in selected:
        item["score"] = round(float(item.get("score") or 0), 2)
    return selected


def _append_required_candidates(selected: list[dict[str, Any]], required: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = [dict(item) for item in selected]
    existing = {str(item.get("symbol") or item.get("code")) for item in merged}
    for item in required:
        key = str(item.get("symbol") or item.get("code"))
        if not key or key in existing:
            continue
        entry = {
            "code": item["code"],
            "symbol": item["symbol"],
            "name": item["name"],
            "market": item["market"],
            "exchange": item["exchange"],
            "category": item["category"],
            "score": 100.0,
            "sources": [item.get("source") or "required"],
            "reasons": ["required candidate included after limit cut"],
            "metrics": item.get("metrics", {}),
        }
        merged.append(entry)
        existing.add(key)
    return merged


def _holding_kr_candidates(account: dict[str, Any] | None) -> list[dict[str, Any]]:
    holdings = (account or {}).get("holdings", [])
    rows = []
    for row in holdings:
        normalized = normalize_kr_candidate_row(row, source="holding")
        if normalized:
            rows.append(normalized)
    return rows


def _holding_us_candidates(holdings: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    rows = []
    for row in holdings or []:
        normalized = normalize_us_candidate_row(row, source="holding", fallback_exchange=row.get("exchange") or "NASD")
        if normalized:
            rows.append(normalized)
    return rows


def _candidate_report(mode: str, selected: list[dict[str, Any]], fallback_used: bool, errors: list[str]) -> dict[str, Any]:
    return {
        "mode": mode,
        "selected": selected,
        "fallback_used": fallback_used,
        "errors": errors,
        "generated_at": _now_iso(),
    }


def _append_kr_holdings_to_static(static_selected: list[dict[str, Any]], account: dict[str, Any] | None) -> list[dict[str, Any]]:
    selected = [dict(item) for item in static_selected]
    existing = {str(item.get("code")) for item in selected}
    for holding in _holding_kr_candidates(account):
        if holding["code"] in existing:
            continue
        selected.append({
            "code": holding["code"],
            "symbol": holding["symbol"],
            "name": holding["name"],
            "market": "KR",
            "exchange": "KRX",
            "category": holding["category"],
            "score": 100.0,
            "sources": ["holding"],
            "reasons": ["current holding included for SELL signal check"],
            "metrics": holding.get("metrics", {}),
        })
        existing.add(holding["code"])
    return selected


def _append_us_holdings_to_static(static_selected: list[dict[str, Any]], holdings: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    selected = [dict(item) for item in static_selected]
    existing = {str(item.get("symbol")) for item in selected}
    for holding in _holding_us_candidates(holdings):
        if holding["symbol"] in existing:
            continue
        selected.append({
            "code": holding["code"],
            "symbol": holding["symbol"],
            "name": holding["name"],
            "market": "US",
            "exchange": holding["exchange"],
            "category": holding["category"],
            "score": 100.0,
            "sources": ["holding"],
            "reasons": ["current holding included for SELL signal check"],
            "metrics": holding.get("metrics", {}),
        })
        existing.add(holding["symbol"])
    return selected


def select_kr_candidates(
    *,
    api_get: Callable[[str], dict[str, Any]],
    account: dict[str, Any] | None,
    static_candidates: list[tuple[str, str, str]],
    mode: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    mode = (mode or os.environ.get("KR_MARKET_CANDIDATE_MODE", "dynamic")).strip().lower()
    limit = int(limit or os.environ.get("KR_MARKET_CANDIDATE_LIMIT", "20"))
    static_selected = _static_kr_candidates(static_candidates, limit)
    if mode == "static":
        return _candidate_report("static", static_selected, False, [])

    errors: list[str] = []
    accumulator: dict[str, dict[str, Any]] = {}
    source_successes = 0
    ranked_symbols: set[str] = set()
    sources = [
        ("volume_rank", "/api/screening/rankings/volume?market_div=J&input_iscd=0000&volume_min=0&max_depth=1"),
        ("volume_power_rank", "/api/screening/rankings/volume-power?market_div=J&input_iscd=0000&max_depth=1"),
        ("market_cap_rank", "/api/screening/rankings/market-cap?market_div=J&input_iscd=0000&max_depth=1"),
        ("foreign_institution", "/api/screening/investors/foreign-institution?market_div=V&input_iscd=0000&rank_sort=0"),
    ]
    for source, path in sources:
        try:
            response = api_get(path)
            if response.get("status") != "success":
                errors.append(f"{source}: {response.get('message') or response.get('status')}")
                continue
            source_successes += 1
            for index, row in enumerate(response.get("items") or []):
                normalized = normalize_kr_candidate_row(row, source=source)
                if not normalized:
                    continue
                ranked_symbols.add(normalized["symbol"])
                _merge_candidate(accumulator, normalized, source, KR_SOURCE_WEIGHTS[source], row, index)
        except Exception as exc:
            errors.append(f"{source}: {str(exc)[:300]}")

    holding_candidates = _holding_kr_candidates(account)
    for holding in holding_candidates:
        _merge_candidate(accumulator, holding, "holding", 100.0, {"rank": 1}, 0)

    selected = _finalize_selection(accumulator, limit)
    if source_successes == 0 or len(ranked_symbols) < KR_DYNAMIC_MIN_COUNT or len(selected) < KR_DYNAMIC_MIN_COUNT:
        fallback_selected = _append_kr_holdings_to_static(static_selected, account)
        return _candidate_report("dynamic", fallback_selected, True, errors + [f"dynamic ranked candidate count below minimum: {len(ranked_symbols)}"])
    selected = _append_required_candidates(selected, holding_candidates)
    return _candidate_report("dynamic", selected, False, errors)


def select_us_candidates(
    *,
    ranking_fetcher: Any,
    holdings: list[dict[str, Any]] | None,
    static_candidates: list[tuple[str, str]],
    mode: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    mode = (mode or os.environ.get("US_MARKET_CANDIDATE_MODE", "dynamic")).strip().lower()
    limit = int(limit or os.environ.get("US_MARKET_CANDIDATE_LIMIT", "18"))
    static_selected = _static_us_candidates(static_candidates, limit)
    if mode == "static":
        return _candidate_report("static", static_selected, False, [])

    errors: list[str] = []
    accumulator: dict[str, dict[str, Any]] = {}
    source_successes = 0
    ranked_symbols: set[str] = set()
    calls = [
        ("trade_value_rank", "get_overseas_trade_value_rank", {"nday": "0", "vol_rang": "2"}),
        ("volume_power_rank", "get_overseas_volume_power_rank", {"nday": "0", "vol_rang": "2"}),
        ("market_cap_rank", "get_overseas_market_cap_rank", {"vol_rang": "2"}),
        ("volume_surge_rank", "get_overseas_volume_surge_rank", {"minx": "3", "vol_rang": "2"}),
    ]
    for trading_exchange in ("NASD", "NYSE", "AMEX"):
        price_exchange = us_price_exchange(trading_exchange)
        for source, method_name, kwargs in calls:
            try:
                result = getattr(ranking_fetcher, method_name)(exchange=price_exchange, max_depth=1, **kwargs)
                if not getattr(result, "success", False):
                    display_error = result.display_error() if hasattr(result, "display_error") else "ranking failed"
                    errors.append(f"{source}/{trading_exchange}: {display_error}")
                    continue
                source_successes += 1
                for index, row in enumerate(result.records()):
                    normalized = normalize_us_candidate_row(row, source=source, fallback_exchange=trading_exchange)
                    if not normalized:
                        continue
                    ranked_symbols.add(normalized["symbol"])
                    _merge_candidate(accumulator, normalized, source, US_SOURCE_WEIGHTS[source], row, index)
            except Exception as exc:
                errors.append(f"{source}/{trading_exchange}: {str(exc)[:300]}")

    core_candidates = []
    for symbol, exchange in US_CORE_ETFS.values():
        normalized = normalize_us_candidate_row({"symbol": symbol, "exchange": exchange, "name": symbol}, source="core_etf")
        if normalized:
            core_candidates.append(normalized)
            _merge_candidate(accumulator, normalized, "core_etf", 100.0, {"rank": 1}, 0)
    holding_candidates = _holding_us_candidates(holdings)
    for holding in holding_candidates:
        _merge_candidate(accumulator, holding, "holding", 100.0, {"rank": 1}, 0)

    selected = _finalize_selection(accumulator, limit)
    if source_successes == 0 or len(ranked_symbols) < US_DYNAMIC_MIN_COUNT or len(selected) < US_DYNAMIC_MIN_COUNT:
        fallback_selected = _append_us_holdings_to_static(static_selected, holdings)
        return _candidate_report("dynamic", fallback_selected, True, errors + [f"dynamic ranked candidate count below minimum: {len(ranked_symbols)}"])
    selected = _append_required_candidates(selected, core_candidates + holding_candidates)
    return _candidate_report("dynamic", selected, False, errors)
