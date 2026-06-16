"""Liquidity-first dynamic candidate selection for market auto-runs."""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

KR_DYNAMIC_MIN_COUNT = 5
US_DYNAMIC_MIN_COUNT = 5
US_CORE_ETFS = {
    "SPY": ("SPY", "NYSE"),
    "QQQ": ("QQQ", "NASD"),
    "DIA": ("DIA", "NYSE"),
    "IWM": ("IWM", "NYSE"),
}
US_LEVERAGED_OR_INVERSE_ETFS = {
    "BOIL", "DRIP", "FAS", "FAZ", "LABD", "LABU", "NUGT", "SDS", "SOXL",
    "SOXS", "SPXU", "SSO", "SQQQ", "TECL", "TECS", "TNA", "TQQQ", "TZA",
    "UDOW", "UPRO", "UVXY", "VIXY", "YANG", "YINN",
}
KR_LEVERAGED_OR_INVERSE_NAME_TOKENS = (
    "인버스",
    "레버리지",
    "곱버스",
    "2X",
    "2배",
    "ETN",
    "ETC",
    "선물",
    "원유",
    "천연가스",
    "VIX",
)

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
    return datetime.now(UTC).isoformat(timespec="seconds")


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


def _dedupe_in_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    selected = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        selected.append(item)
    return selected


def _custom_mode_from_env(mode: str | None, env_name: str, mode_env_name: str) -> str:
    if mode is not None:
        return mode.strip().lower()
    if os.environ.get(env_name, "").strip():
        return "custom"
    return os.environ.get(mode_env_name, "dynamic").strip().lower()


def _parse_kr_custom_symbols(raw: str) -> tuple[list[str], list[str]]:
    warnings = []
    symbols = []
    for raw_token in raw.split(","):
        token = raw_token.strip()
        if not token:
            continue
        if not re.fullmatch(r"\d{6}", token):
            warnings.append(f"invalid KR custom symbol ignored: {token}")
            continue
        symbols.append(token)
    return _dedupe_in_order(symbols), warnings


def _parse_us_custom_symbols(raw: str) -> tuple[list[str], list[str]]:
    warnings = []
    symbols = []
    for raw_token in raw.split(","):
        token = raw_token.strip()
        if not token:
            continue
        symbol = token.upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9.]{0,9}", symbol):
            warnings.append(f"invalid US custom symbol ignored: {token}")
            continue
        symbols.append(symbol)
    return _dedupe_in_order(symbols), warnings


def _custom_kr_candidates(raw: str, limit: int) -> tuple[list[dict[str, Any]], list[str]]:
    symbols, warnings = _parse_kr_custom_symbols(raw)
    selected = []
    for index, code in enumerate(symbols[:limit]):
        selected.append({
            "code": code,
            "symbol": code,
            "name": code,
            "market": "KR",
            "exchange": "KRX",
            "category": "custom",
            "score": round(max(1.0, 100.0 - index), 2),
            "sources": ["custom"],
            "reasons": ["custom env candidate"],
            "metrics": {"rank": index + 1},
        })
    return selected, warnings


def _resolved_us_custom_profile(ranking_fetcher: Any, symbol: str) -> tuple[str, str]:
    resolver = getattr(ranking_fetcher, "resolve_exchange", None)
    if not callable(resolver):
        raise AttributeError("resolve_exchange unavailable")
    result = resolver(symbol)
    if result in (None, ""):
        raise ValueError("empty resolve_exchange result")
    if hasattr(result, "success") and not getattr(result, "success", False):
        message = result.display_error() if hasattr(result, "display_error") else "resolve_exchange failed"
        raise ValueError(str(message))
    if isinstance(result, dict):
        exchange = normalize_us_exchange(_first_value(result, ("exchange", "excd", "ovrs_excg_cd"), ""))
        name = str(_first_value(result, ("name", "ename", "prdt_name", "stock_name"), symbol)).strip() or symbol
        if not exchange:
            raise ValueError("resolve_exchange returned invalid exchange")
        return exchange, name
    if isinstance(result, (tuple, list)) and result:
        exchange = normalize_us_exchange(result[0])
        name = str(result[1]).strip() if len(result) > 1 else symbol
        if not exchange:
            raise ValueError("resolve_exchange returned invalid exchange")
        return exchange, name or symbol
    exchange = normalize_us_exchange(result)
    if not exchange:
        raise ValueError("resolve_exchange returned invalid exchange")
    return exchange, symbol


def _custom_us_candidates(raw: str, limit: int, ranking_fetcher: Any) -> tuple[list[dict[str, Any]], list[str]]:
    symbols, warnings = _parse_us_custom_symbols(raw)
    selected = []
    for index, symbol in enumerate(symbols[:limit]):
        try:
            exchange, name = _resolved_us_custom_profile(ranking_fetcher, symbol)
        except Exception as exc:
            exchange, name = "NASD", symbol
            warnings.append(f"{symbol}: resolve_exchange failed; using NASD fallback ({str(exc)[:200]})")
        selected.append({
            "code": symbol,
            "symbol": symbol,
            "name": name,
            "market": "US",
            "exchange": exchange,
            "category": "custom",
            "score": round(max(1.0, 100.0 - index), 2),
            "sources": ["custom"],
            "reasons": ["custom env candidate"],
            "metrics": {"rank": index + 1},
        })
    return selected, warnings


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
    disallowed_new_buy = is_disallowed_kr_new_buy_name(name)
    if disallowed_new_buy and source != "holding":
        return None
    category = "excluded_etp" if disallowed_new_buy else (
        "ETF" if any(token in name.upper() for token in ("ETF", "KODEX", "TIGER", "SOL ", "ACE ")) else "대형주"
    )
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
        "new_buy_disallowed": disallowed_new_buy,
        "metrics": metrics,
    }


def normalize_us_exchange(exchange: Any) -> str | None:
    if exchange in (None, ""):
        return None
    return US_EXCHANGE_ALIASES.get(str(exchange).strip().upper())


def is_disallowed_us_new_buy_symbol(symbol: Any) -> bool:
    return str(symbol or "").strip().upper() in US_LEVERAGED_OR_INVERSE_ETFS


def is_disallowed_kr_new_buy_name(name: Any) -> bool:
    normalized = str(name or "").strip().upper()
    if not normalized:
        return False
    return any(token in normalized for token in KR_LEVERAGED_OR_INVERSE_NAME_TOKENS)


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


def _candidate_report(
    mode: str,
    selected: list[dict[str, Any]],
    fallback_used: bool,
    errors: list[str],
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "selected": selected,
        "fallback_used": fallback_used,
        "errors": errors,
        "warnings": warnings or [],
        "generated_at": _now_iso(),
    }


def _is_unsupported_us_market_cap_error(source: str, message: Any) -> bool:
    text = str(message or "")
    return source == "market_cap_rank" and "OPSQ2001" in text and "CURR_GB" in text


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
    mode = _custom_mode_from_env(mode, "KR_MARKET_CANDIDATE_SYMBOLS", "KR_MARKET_CANDIDATE_MODE")
    limit = int(limit or os.environ.get("KR_MARKET_CANDIDATE_LIMIT", "20"))
    static_selected = _static_kr_candidates(static_candidates, limit)
    if mode == "static":
        return _candidate_report("static", static_selected, False, [])
    if mode == "custom":
        selected, warnings = _custom_kr_candidates(os.environ.get("KR_MARKET_CANDIDATE_SYMBOLS", ""), limit)
        if not selected:
            return _candidate_report(
                "custom",
                [],
                False,
                ["custom candidate symbols did not include any valid KR 6-digit codes"],
                warnings,
            )
        selected = _append_kr_holdings_to_static(selected, account)
        return _candidate_report("custom", selected, False, [], warnings)

    errors: list[str] = []
    warnings: list[str] = []
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
        return _candidate_report(
            "dynamic",
            fallback_selected,
            True,
            [*errors, f"dynamic ranked candidate count below minimum: {len(ranked_symbols)}"],
            warnings,
        )
    selected = _append_required_candidates(selected, holding_candidates)
    return _candidate_report("dynamic", selected, False, [], [*warnings, *errors])


def select_us_candidates(
    *,
    ranking_fetcher: Any,
    holdings: list[dict[str, Any]] | None,
    static_candidates: list[tuple[str, str]],
    mode: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    mode = _custom_mode_from_env(mode, "US_MARKET_CANDIDATE_SYMBOLS", "US_MARKET_CANDIDATE_MODE")
    limit = int(limit or os.environ.get("US_MARKET_CANDIDATE_LIMIT", "18"))
    static_selected = _static_us_candidates(static_candidates, limit)
    if mode == "static":
        return _candidate_report("static", static_selected, False, [])
    if mode == "custom":
        selected, warnings = _custom_us_candidates(
            os.environ.get("US_MARKET_CANDIDATE_SYMBOLS", ""),
            limit,
            ranking_fetcher,
        )
        if not selected:
            return _candidate_report(
                "custom",
                [],
                False,
                ["custom candidate symbols did not include any valid US symbols"],
                warnings,
            )
        selected = _append_us_holdings_to_static(selected, holdings)
        return _candidate_report("custom", selected, False, [], warnings)

    errors: list[str] = []
    warnings: list[str] = []
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
                    item = f"{source}/{trading_exchange}: {display_error}"
                    if _is_unsupported_us_market_cap_error(source, display_error):
                        warnings.append(item)
                    else:
                        errors.append(item)
                    continue
                source_successes += 1
                for index, row in enumerate(result.records()):
                    normalized = normalize_us_candidate_row(row, source=source, fallback_exchange=trading_exchange)
                    if not normalized:
                        continue
                    if is_disallowed_us_new_buy_symbol(normalized["symbol"]):
                        continue
                    ranked_symbols.add(normalized["symbol"])
                    _merge_candidate(accumulator, normalized, source, US_SOURCE_WEIGHTS[source], row, index)
            except Exception as exc:
                item = f"{source}/{trading_exchange}: {str(exc)[:300]}"
                if _is_unsupported_us_market_cap_error(source, str(exc)):
                    warnings.append(item)
                else:
                    errors.append(item)

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
        return _candidate_report(
            "dynamic",
            fallback_selected,
            True,
            [*errors, f"dynamic ranked candidate count below minimum: {len(ranked_symbols)}"],
            warnings,
        )
    selected = _append_required_candidates(selected, core_candidates + holding_candidates)
    return _candidate_report("dynamic", selected, False, errors, warnings)
