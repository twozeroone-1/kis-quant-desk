"""Regime-aware strategy orchestration for paper-trading automation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

TARGET_STRATEGY_COUNTS = {
    "kr": {"min": 8, "max": 12},
    "us": {"min": 3, "max": 5},
}

KR_STRATEGY_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "custom:today_krx_macro_rebound",
        "name": "KRX macro rebound",
        "family": "macro_rebound",
        "base_weight": 1.15,
        "regimes": {"large_cap_rebound", "semiconductor_momentum", "auto_momentum", "headline_caution"},
        "reason": "existing intraday macro rebound baseline",
    },
    {
        "id": "trend_filter",
        "name": "Trend filter",
        "family": "trend",
        "base_weight": 1.0,
        "regimes": {"large_cap_rebound", "semiconductor_momentum", "auto_momentum", "broad_momentum"},
        "reason": "keeps long entries aligned with the medium trend",
    },
    {
        "id": "momentum",
        "name": "Momentum",
        "family": "momentum",
        "base_weight": 0.95,
        "regimes": {"semiconductor_momentum", "auto_momentum", "broad_momentum"},
        "reason": "captures persistent relative strength",
    },
    {
        "id": "golden_cross",
        "name": "Golden cross",
        "family": "trend",
        "base_weight": 0.9,
        "regimes": {"large_cap_rebound", "broad_momentum"},
        "reason": "simple low-overfit trend confirmation",
    },
    {
        "id": "week52_high",
        "name": "52-week high breakout",
        "family": "breakout",
        "base_weight": 0.85,
        "regimes": {"semiconductor_momentum", "auto_momentum", "broad_momentum"},
        "reason": "participates only when leadership is strong",
    },
    {
        "id": "strong_close",
        "name": "Strong close",
        "family": "momentum",
        "base_weight": 0.8,
        "regimes": {"semiconductor_momentum", "auto_momentum", "broad_momentum"},
        "reason": "short-horizon confirmation of demand into the close",
    },
    {
        "id": "volatility",
        "name": "Volatility expansion",
        "family": "volatility",
        "base_weight": 0.75,
        "regimes": {"broad_momentum", "headline_caution"},
        "reason": "tests volatility contraction followed by expansion",
    },
    {
        "id": "mean_reversion",
        "name": "Mean reversion",
        "family": "mean_reversion",
        "base_weight": 0.85,
        "regimes": {"large_cap_rebound", "headline_caution", "risk_control"},
        "reason": "keeps a counter-trend sleeve for oversold rebounds",
    },
    {
        "id": "disparity",
        "name": "MA disparity",
        "family": "mean_reversion",
        "base_weight": 0.8,
        "regimes": {"large_cap_rebound", "headline_caution", "risk_control"},
        "reason": "checks whether price has stretched too far from its mean",
    },
    {
        "id": "breakout_fail",
        "name": "False breakout guard",
        "family": "defensive",
        "base_weight": 0.7,
        "regimes": {"headline_caution", "risk_control", "broad_momentum"},
        "reason": "adds failure/exit pressure when breakouts reverse",
    },
    {
        "id": "bollinger_rsi_mean_reversion",
        "name": "Bollinger RSI mean reversion",
        "family": "mean_reversion",
        "base_weight": 0.85,
        "regimes": {"large_cap_rebound", "headline_caution", "risk_control"},
        "reason": "custom YAML oversold band-reversion signal",
    },
    {
        "id": "macd_mfi_confirmation",
        "name": "MACD MFI confirmation",
        "family": "composite",
        "base_weight": 0.9,
        "regimes": {"semiconductor_momentum", "auto_momentum", "broad_momentum"},
        "reason": "requires trend turn plus money-flow confirmation",
    },
)

US_STRATEGY_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "today_us_news_trend_filter",
        "name": "US news trend filter",
        "family": "trend",
        "base_weight": 1.0,
        "regimes": {"ai_tech_momentum", "broad_momentum", "headline_caution"},
        "reason": "Strategy Builder mirror of the existing news-aware US trend anchor",
    },
    {
        "id": "trend_filter",
        "name": "Trend filter",
        "family": "trend",
        "base_weight": 0.75,
        "regimes": {"ai_tech_momentum", "broad_momentum"},
        "reason": "confirms that price remains aligned with the medium trend",
    },
    {
        "id": "momentum",
        "name": "Momentum",
        "family": "momentum",
        "base_weight": 0.7,
        "regimes": {"ai_tech_momentum", "broad_momentum", "energy_momentum"},
        "reason": "checks persistent relative strength without replacing the anchor",
    },
    {
        "id": "strong_close",
        "name": "Strong close",
        "family": "momentum",
        "base_weight": 0.6,
        "regimes": {"ai_tech_momentum", "broad_momentum"},
        "reason": "adds short-horizon demand confirmation",
    },
    {
        "id": "macd_mfi_confirmation",
        "name": "MACD MFI confirmation",
        "family": "composite",
        "base_weight": 0.65,
        "regimes": {"ai_tech_momentum", "broad_momentum", "headline_caution"},
        "reason": "requires trend turn plus money-flow confirmation",
    },
)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def select_strategy_candidates(
    *,
    market: str,
    regime: str,
    risk_gate_open: bool,
    catalog: tuple[dict[str, Any], ...] | None = None,
) -> dict[str, Any]:
    """Select a regime-sized strategy pool for the current market."""
    if market not in TARGET_STRATEGY_COUNTS:
        raise ValueError(f"unsupported organic strategy market: {market}")
    source = catalog or (KR_STRATEGY_CATALOG if market == "kr" else US_STRATEGY_CATALOG)
    target = TARGET_STRATEGY_COUNTS[market]
    normalized_regime = regime or "unknown"

    enabled: list[dict[str, Any]] = []
    disabled: list[dict[str, Any]] = []
    for item in source:
        regimes = set(item.get("regimes") or [])
        preferred = normalized_regime in regimes
        defensive = normalized_regime == "risk_control" and item.get("family") in {"defensive", "mean_reversion"}
        weight = float(item.get("base_weight") or 1.0)
        if preferred or defensive:
            adjusted = weight * (1.15 if preferred else 1.0)
            status_reason = item.get("reason", "regime match")
        else:
            adjusted = weight * 0.55
            status_reason = f"diversifier outside primary {normalized_regime} regime"
        enabled.append({
            "id": item["id"],
            "name": item.get("name", item["id"]),
            "family": item.get("family", "unknown"),
            "weight": round(adjusted, 4),
            "primary_regime_match": preferred,
            "reason": status_reason,
        })

    enabled.sort(key=lambda row: (not row["primary_regime_match"], -float(row["weight"]), row["id"]))
    enabled = enabled[:target["max"]]
    enabled_ids = {row["id"] for row in enabled}
    for item in source:
        if item["id"] in enabled_ids:
            continue
        disabled.append({
            "id": item["id"],
            "name": item.get("name", item["id"]),
            "family": item.get("family", "unknown"),
            "reason": f"outside top {target['max']} for {normalized_regime}",
        })

    warnings = []
    if len(enabled) < target["min"]:
        warnings.append(f"enabled strategy count below target: {len(enabled)}")
    if len(enabled) > target["max"]:
        warnings.append(f"enabled strategy count above target: {len(enabled)}")
    if not risk_gate_open:
        warnings.append("risk gate is closed; strategy signals are diagnostic and new buys remain blocked")

    return {
        "market": market,
        "regime": normalized_regime,
        "risk_gate_open": risk_gate_open,
        "target_strategy_count": target,
        "enabled": enabled,
        "disabled": disabled,
        "enabled_count": len(enabled),
        "warnings": warnings,
        "generated_at": utc_now_iso(),
    }


def execute_strategy_pool(
    api_call: Callable[..., dict[str, Any]],
    stock_codes: list[str],
    orchestration: dict[str, Any],
    *,
    market: str,
    symbol_meta: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run selected Strategy Builder strategies and merge their signals."""
    enabled = orchestration.get("enabled") or []
    strategy_weights = {str(item["id"]): float(item.get("weight") or 1.0) for item in enabled}
    runs: list[dict[str, Any]] = []
    errors: list[str] = []

    for strategy in enabled:
        strategy_id = str(strategy["id"])
        try:
            response = api_call("POST", "/api/strategies/execute", json={
                "strategy_id": strategy_id,
                "stocks": stock_codes,
                "params": {},
                "market": market,
                "symbol_meta": symbol_meta or {},
            })
        except Exception as exc:
            message = f"{strategy_id}: {type(exc).__name__}: {exc}"
            errors.append(message)
            runs.append({
                "strategy_id": strategy_id,
                "status": "error",
                "reason": message,
                "results": [],
            })
            continue

        status = str(response.get("status") or "unknown")
        results = response.get("results") or []
        if status != "success":
            message = f"{strategy_id}: {response.get('message') or status}"
            errors.append(message)
        runs.append({
            "strategy_id": strategy_id,
            "status": status,
            "message": response.get("message"),
            "result_count": len(results),
            "logs": [serialize_log_entry(item) for item in response.get("logs", [])],
            "results": results,
        })

    merged = merge_strategy_signals(runs, strategy_weights)
    return {
        "orchestration": orchestration,
        "runs": compact_strategy_runs(runs),
        "raw_result_count": sum(len(run.get("results") or []) for run in runs),
        "successful_strategy_count": sum(1 for run in runs if run.get("status") == "success"),
        "failed_strategy_count": sum(1 for run in runs if run.get("status") != "success"),
        "errors": errors,
        "merged_signals": merged,
    }


def compact_strategy_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "strategy_id": run.get("strategy_id"),
            "status": run.get("status"),
            "message": run.get("message") or run.get("reason"),
            "result_count": len(run.get("results") or []),
        }
        for run in runs
    ]


def serialize_log_entry(item: Any) -> Any:
    if hasattr(item, "dict"):
        return item.dict()
    return item


def merge_strategy_signals(runs: list[dict[str, Any]], strategy_weights: dict[str, float]) -> list[dict[str, Any]]:
    by_code: dict[str, dict[str, Any]] = {}
    for run in runs:
        if run.get("status") != "success":
            continue
        strategy_id = str(run.get("strategy_id"))
        weight = float(strategy_weights.get(strategy_id, 1.0))
        for result in run.get("results") or []:
            code = str(result.get("code") or result.get("symbol") or "").strip()
            if not code:
                continue
            action = str(result.get("action") or "HOLD").upper()
            strength = float(result.get("strength") or 0)
            score = round(weight * strength, 6)
            bucket = by_code.setdefault(code, {
                "code": code,
                "name": result.get("name") or result.get("stock_name") or code,
                "target_price": 0.0,
                "votes": [],
                "scores": {"BUY": 0.0, "SELL": 0.0, "HOLD": 0.0, "ERROR": 0.0},
            })
            price = float(result.get("target_price") or result.get("price") or 0)
            if price > 0 and (action in {"BUY", "SELL"} or not bucket["target_price"]):
                bucket["target_price"] = price
            if action not in bucket["scores"]:
                action = "ERROR"
            bucket["scores"][action] += score
            bucket["votes"].append({
                "strategy_id": strategy_id,
                "action": action,
                "strength": round(strength, 4),
                "weighted_score": round(score, 4),
                "reason": str(result.get("reason") or "")[:240],
            })

    merged = []
    for code, bucket in sorted(by_code.items()):
        scores = bucket["scores"]
        buy_score = float(scores["BUY"])
        sell_score = float(scores["SELL"])
        hold_score = float(scores["HOLD"])
        if sell_score >= 0.5 and sell_score >= buy_score:
            action = "SELL"
            strength = min(0.99, sell_score)
        elif buy_score >= 0.5 and buy_score > sell_score:
            action = "BUY"
            strength = min(0.99, buy_score)
        elif scores["ERROR"] > 0 and not (buy_score or sell_score or hold_score):
            action = "ERROR"
            strength = 0.0
        else:
            action = "HOLD"
            strength = min(0.49, max(hold_score, buy_score, sell_score))
        merged.append({
            "code": code,
            "name": bucket["name"],
            "action": action,
            "strength": round(strength, 4),
            "target_price": round(float(bucket["target_price"] or 0), 4),
            "reason": summarize_votes(bucket["votes"], action),
            "strategy_votes": sorted(
                bucket["votes"],
                key=lambda row: (-float(row["weighted_score"]), row["strategy_id"]),
            ),
            "strategy_scores": {key: round(float(value), 4) for key, value in scores.items()},
        })
    return merged


def merge_us_anchor_signals(
    anchor_signals: list[dict[str, Any]],
    organic_signals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Use Strategy Builder signals as US confirmation, not as a standalone buy engine."""
    organic_by_symbol = {
        str(row.get("code") or row.get("symbol") or "").upper(): row
        for row in organic_signals
    }
    merged: list[dict[str, Any]] = []
    for anchor in anchor_signals:
        symbol = str(anchor.get("symbol") or anchor.get("code") or "").upper()
        organic = organic_by_symbol.get(symbol)
        anchor_action = str(anchor.get("action") or "HOLD").upper()
        organic_action = str((organic or {}).get("action") or "MISSING").upper()
        anchor_strength = float(anchor.get("strength") or 0)
        organic_strength = float((organic or {}).get("strength") or 0)
        final = dict(anchor)
        final["anchor_signal"] = {
            "action": anchor_action,
            "strength": round(anchor_strength, 4),
            "reason": anchor.get("reason"),
        }
        if organic:
            final["organic_signal"] = organic
            final["strategy_votes"] = organic.get("strategy_votes") or []
            final["strategy_scores"] = organic.get("strategy_scores") or {}

        if anchor_action == "SELL":
            reason_suffix = "Strategy Builder confirmation skipped because anchor exit dominates"
        elif anchor_action == "BUY" and organic_action == "BUY":
            final["strength"] = round(min(0.99, anchor_strength + organic_strength * 0.12), 4)
            reason_suffix = f"organic confirmation BUY {organic_strength:.2f}"
        elif anchor_action == "BUY" and organic_action == "SELL":
            final["action"] = "HOLD"
            final["strength"] = round(min(0.49, anchor_strength), 4)
            reason_suffix = f"blocked by organic SELL conflict {organic_strength:.2f}"
        elif anchor_action == "BUY" and organic_action in {"HOLD", "MISSING"}:
            final["strength"] = round(max(0.0, anchor_strength - 0.04), 4)
            reason_suffix = "anchor BUY kept with limited organic confirmation"
        elif anchor_action == "HOLD" and organic_action == "BUY":
            final["strength"] = round(max(anchor_strength, min(0.49, organic_strength * 0.5)), 4)
            reason_suffix = f"organic BUY noted, anchor remains HOLD {organic_strength:.2f}"
        else:
            reason_suffix = f"organic {organic_action.lower()} confirmation"

        final["reason"] = f"{anchor.get('reason', '')}; {reason_suffix}".strip("; ")
        merged.append(final)
    return merged


def summarize_votes(votes: list[dict[str, Any]], action: str) -> str:
    matching = [vote for vote in votes if vote.get("action") == action]
    source = matching or votes
    top = sorted(source, key=lambda row: -float(row.get("weighted_score") or 0))[:3]
    parts = [
        f"{vote.get('strategy_id')} {vote.get('action')} {float(vote.get('strength') or 0):.2f}"
        for vote in top
    ]
    return "; ".join(parts)[:500] or "no strategy votes"


def explain_order_decisions(
    signals: list[dict[str, Any]],
    planned_buys: list[dict[str, Any]],
    *,
    min_buy_strength: float,
    risk_gate_open: bool,
    risk_reasons: list[str],
    order_execution_enabled: bool,
    order_block_reasons: list[str],
) -> list[dict[str, Any]]:
    planned_by_code = {
        str(order.get("code") or order.get("symbol") or ""): order
        for order in planned_buys
    }
    decisions = []
    for signal in signals:
        code = str(signal.get("code") or signal.get("symbol") or "")
        action = str(signal.get("action") or "HOLD").upper()
        reasons: list[str] = []
        status = "blocked"
        if action == "BUY" and code in planned_by_code:
            status = "planned"
            reasons.append("passed strategy, strength, price, and budget gates")
        elif action != "BUY":
            status = "not_buy"
            reasons.append(f"merged signal is {action}")
        else:
            if not risk_gate_open:
                reasons.extend(risk_reasons or ["risk gate closed"])
            if not order_execution_enabled:
                reasons.extend(order_block_reasons or ["order execution disabled"])
            if float(signal.get("strength") or 0) < min_buy_strength:
                reasons.append(f"strength below {min_buy_strength:.2f}")
            if float(signal.get("target_price") or signal.get("price") or 0) <= 0:
                reasons.append("target price missing")
            if not reasons:
                reasons.append("budget, max symbol count, or per-symbol cap blocked the order")
        decisions.append({
            "code": code,
            "name": signal.get("name") or code,
            "action": action,
            "strength": round(float(signal.get("strength") or 0), 4),
            "status": status,
            "reasons": reasons,
        })
    return decisions
