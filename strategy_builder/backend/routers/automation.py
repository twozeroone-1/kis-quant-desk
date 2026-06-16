"""Read-only paper-trading automation reports."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

router = APIRouter()
US_REPORT_DIR = Path(os.environ.get("US_MARKET_REPORT_DIR", "/app/us-market-reports"))
KR_REPORT_DIR = Path(os.environ.get("KR_MARKET_REPORT_DIR", "/app/kr-market-reports"))
SESSION_RE = re.compile(r"^\d{8}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
REPORT_TAG_RE = r"(?:_[a-z0-9]+(?:_[a-z0-9]+)*_report)?"
US_RUN_RE = re.compile(rf"^\d{{8}}_(?:\d{{4}}_ET{REPORT_TAG_RE}|closed|\d{{6}}_(?:open|mid|close|manual))$")
KR_RUN_RE = re.compile(rf"^\d{{8}}_(?:\d{{4}}_KST{REPORT_TAG_RE}|closed|\d{{6}}_(?:hourly|open|mid|close|manual))$")
SUCCESS_ORDER_STATUSES = {"success", "submitted", "reservation_submitted"}
FAILED_ORDER_STATUSES = {"failed", "error", "reservation_failed"}
DEFERRED_ORDER_STATUSES = {"deferred"}
MAX_DAILY_EQUITY_DEVIATION_PCT = 0.5
EXIT_REASON_LABELS = {
    "take_profit": "익절",
    "stop_loss": "손절",
    "strategy_sell": "전략 매도",
    "manual_or_external": "수동/외부 청산",
}
NONCRITICAL_US_ERROR_MARKERS = (
    "일봉 데이터 부족",
    "organic missing confirmation",
)


def _number(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _require_vps() -> None:
    if os.environ.get("KIS_LOCK_MODE") != "vps":
        raise HTTPException(status_code=404, detail="Not found")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Report not found") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=500, detail=f"Report read failed: {exc}") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=500, detail="Invalid report")
    return payload


def _order_quantity(order: dict[str, Any]) -> float:
    quantity = _number(order.get("quantity"))
    if quantity > 0:
        return quantity
    result = order.get("order_result") if isinstance(order.get("order_result"), dict) else {}
    for log in result.get("logs", []):
        message = str(log.get("message") or "") if isinstance(log, dict) else ""
        match = re.search(r"\bSELL\s+([0-9,]+)\s*주", message)
        if match:
            return float(match.group(1).replace(",", ""))
    return 0.0


def _is_filled_or_completed_order(order: dict[str, Any]) -> bool:
    if _number(order.get("filled_quantity")) > 0:
        return True
    status = str(order.get("order_status") or "")
    if status == "success":
        return True
    result = order.get("order_result") if isinstance(order.get("order_result"), dict) else {}
    return result.get("status") == "success"


def _order_notional(order: dict[str, Any]) -> float:
    explicit = _number(order.get("amount") or order.get("notional"))
    if explicit > 0:
        return explicit
    price = _number(order.get("limit_price") or order.get("target_price") or order.get("price"))
    return price * _order_quantity(order)


def _is_noncritical_us_error(message: Any) -> bool:
    text = str(message or "")
    return any(marker in text for marker in NONCRITICAL_US_ERROR_MARKERS)


def _order_symbol(order: dict[str, Any]) -> str:
    return str(order.get("stock_code") or order.get("code") or order.get("symbol") or "").upper()


def _order_name(order: dict[str, Any]) -> str:
    return str(order.get("stock_name") or order.get("name") or _order_symbol(order) or "-")


def _run_time(run: dict[str, Any]) -> str:
    return str(
        run.get("finished_at")
        or run.get("started_at")
        or run.get("scheduled_at_kst")
        or run.get("scheduled_at_et")
        or ""
    )


def _date_from_text(value: Any):
    if not value:
        return None
    text = str(value)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        match = re.search(r"(\d{4})-?(\d{2})-?(\d{2})", text)
        if not match:
            return None
        try:
            return datetime(
                int(match.group(1)),
                int(match.group(2)),
                int(match.group(3)),
            ).date()
        except ValueError:
            return None


def _held_days(opened_at: str, reference_at: str) -> int:
    opened = _date_from_text(opened_at)
    reference = _date_from_text(reference_at)
    if opened is None or reference is None:
        return 0
    return max(0, (reference - opened).days)


def _sell_notional(payload: dict[str, Any]) -> float:
    return round(
        sum(
            _order_notional(order)
            for order in _list(payload.get("submitted_sells"))
            if isinstance(order, dict)
            if order.get("order_status") in SUCCESS_ORDER_STATUSES
        ),
        2,
    )


def _protective_orders(payload: dict[str, Any]) -> list[dict[str, Any]]:
    protective = payload.get("account_after", {}).get("protective", {})
    orders = _list(protective.get("orders")) if isinstance(protective, dict) else []
    return [order for order in orders if isinstance(order, dict)]


def _match_protective_order(
    buy: dict[str, Any],
    protective_orders: list[dict[str, Any]],
) -> dict[str, Any] | None:
    protection_id = buy.get("protection_id") or buy.get("protective_order_id")
    if protection_id:
        for order in protective_orders:
            if order.get("id") == protection_id:
                return order

    symbol = _order_symbol(buy)
    quantity = _order_quantity(buy)
    matches = [order for order in protective_orders if _order_symbol(order) == symbol]
    if not matches:
        return None

    def score(order: dict[str, Any]) -> tuple[int, float, str]:
        active_score = 0 if order.get("status") == "active" else 1
        order_quantity = _number(order.get("quantity"))
        quantity_delta = abs(order_quantity - quantity) if quantity and order_quantity else 999999.0
        return (active_score, quantity_delta, str(order.get("created_at") or ""))

    return sorted(matches, key=score)[0]


def _position_journal(
    runs: list[dict[str, Any]],
    details_by_run: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    positions: list[dict[str, Any]] = []
    open_positions: dict[str, list[dict[str, Any]]] = {}
    latest_protective_by_id: dict[str, dict[str, Any]] = {}
    all_protective_orders: list[dict[str, Any]] = []
    latest_protective_ids: set[str] = set()
    latest_seen_at = ""

    for run in runs:
        run_id = str(run.get("run_id") or "")
        detail = details_by_run.get(run_id, {})
        run_time = _run_time(run) or _run_time(detail)
        latest_seen_at = run_time or latest_seen_at
        protective_orders = _protective_orders(detail)
        all_protective_orders.extend(protective_orders)
        if protective_orders:
            latest_protective_ids = {
                str(order.get("id") or "") for order in protective_orders if order.get("id")
            }
        for order in protective_orders:
            order_id = str(order.get("id") or "")
            if order_id:
                latest_protective_by_id[order_id] = order

        buy_orders = [
            order
            for order in [
                *_list(detail.get("submitted_buys")),
                *_list(detail.get("orders")),
            ]
            if isinstance(order, dict)
            and str(order.get("action") or "").upper() == "BUY"
            and _is_filled_or_completed_order(order)
        ]
        for order in buy_orders:
            symbol = _order_symbol(order)
            if not symbol:
                continue
            protective_order = _match_protective_order(order, protective_orders)
            protection_id = (
                order.get("protection_id")
                or order.get("protective_order_id")
                or (protective_order or {}).get("id")
            )
            entry_price = _number(
                order.get("filled_avg_price")
                or order.get("avg_price")
                or order.get("entry_price")
                or order.get("limit_price")
                or order.get("target_price")
                or order.get("price")
            )
            position = {
                "symbol": symbol,
                "name": _order_name(order),
                "market": (protective_order or {}).get("market"),
                "quantity": _order_quantity(order),
                "entry_price": round(entry_price, 4),
                "entry_notional": round(_order_notional(order), 2),
                "opened_at": run_time,
                "opened_run_id": run_id,
                "status": "active",
                "status_label": "보유중",
                "exit_reason": None,
                "exit_reason_label": None,
                "exit_at": None,
                "exit_run_id": None,
                "exit_price": None,
                "exit_notional": None,
                "exit_order_type": None,
                "protection_id": protection_id,
                "protection_status": (protective_order or {}).get("status"),
                "last_error": None,
                "held_days": 0,
                "held_over_2_days": False,
            }
            positions.append(position)
            open_positions.setdefault(symbol, []).append(position)

        sell_orders = [
            order
            for order in [
                *_list(detail.get("submitted_sells")),
                *_list(detail.get("orders")),
            ]
            if isinstance(order, dict)
            and str(order.get("action") or "").upper() == "SELL"
            and _is_filled_or_completed_order(order)
        ]
        for order in sell_orders:
            symbol = _order_symbol(order)
            candidates = [item for item in open_positions.get(symbol, []) if item["status"] == "active"]
            if not candidates:
                continue
            position = candidates[0]
            exit_price = _number(
                order.get("filled_avg_price")
                or order.get("limit_price")
                or order.get("target_price")
                or order.get("price")
            )
            position.update(
                {
                    "status": "closed",
                    "status_label": "청산",
                    "exit_reason": "strategy_sell",
                    "exit_reason_label": EXIT_REASON_LABELS["strategy_sell"],
                    "exit_at": run_time,
                    "exit_run_id": run_id,
                    "exit_price": round(exit_price, 4),
                    "exit_notional": round(_order_notional(order), 2),
                    "exit_order_type": order.get("order_type") or order.get("order_method"),
                    "held_days": _held_days(str(position.get("opened_at") or ""), run_time),
                }
            )
            position["held_over_2_days"] = int(position["held_days"]) >= 2

    for position in positions:
        protection_id = str(position.get("protection_id") or "")
        protective_order = latest_protective_by_id.get(protection_id)
        reference_at = latest_seen_at
        if protective_order:
            position["protection_status"] = protective_order.get("status")
            position["last_error"] = protective_order.get("last_error")
            exit_reason = protective_order.get("exit_reason") or protective_order.get(
                "app_exit_reason"
            )
            exit_at = protective_order.get("closed_at") or protective_order.get("exit_submitted_at")
            exit_price = _number(
                protective_order.get("exit_order_price") or protective_order.get("last_price")
            )
            if exit_reason or protective_order.get("status") in {"closed", "filled"}:
                position.update(
                    {
                        "status": "closed",
                        "status_label": "청산",
                        "exit_reason": exit_reason or "manual_or_external",
                        "exit_reason_label": EXIT_REASON_LABELS.get(
                            str(exit_reason or "manual_or_external"),
                            str(exit_reason or "수동/외부 청산"),
                        ),
                        "exit_at": exit_at,
                        "exit_price": round(exit_price, 4) if exit_price else None,
                        "exit_notional": round(exit_price * _number(position.get("quantity")), 2)
                        if exit_price
                        else position.get("exit_notional"),
                        "exit_order_type": protective_order.get("exit_order_type"),
                    }
                )
                reference_at = str(exit_at or reference_at)
            elif protective_order.get("status") in {"exit_submitted", "submitted_unconfirmed"}:
                position.update({"status": "exiting", "status_label": "매도 확인중"})
                reference_at = str(protective_order.get("exit_submitted_at") or reference_at)
            elif protection_id and protection_id not in latest_protective_ids:
                position.update(
                    {
                        "status": "unknown",
                        "status_label": "추적 끊김",
                        "protection_status": "missing_from_latest_snapshot",
                    }
                )

        position["held_days"] = _held_days(str(position.get("opened_at") or ""), reference_at)
        position["held_over_2_days"] = int(position["held_days"]) >= 2

    unresolved_by_symbol: dict[str, list[dict[str, Any]]] = {}
    for position in positions:
        if position.get("status") in {"active", "unknown"}:
            unresolved_by_symbol.setdefault(str(position.get("symbol") or ""), []).append(position)

    for symbol, unresolved in unresolved_by_symbol.items():
        if not symbol:
            continue
        unresolved_quantity = sum(_number(position.get("quantity")) for position in unresolved)
        aggregate_exits = [
            order
            for order in all_protective_orders
            if _order_symbol(order) == symbol
            and order.get("status") in {"closed", "filled"}
            and (order.get("exit_reason") or order.get("app_exit_reason"))
            and _number(order.get("quantity")) >= unresolved_quantity
        ]
        if not aggregate_exits:
            continue
        aggregate_exit = sorted(
            aggregate_exits,
            key=lambda order: str(order.get("closed_at") or order.get("exit_submitted_at") or ""),
        )[-1]
        exit_reason = aggregate_exit.get("exit_reason") or aggregate_exit.get("app_exit_reason")
        exit_at = aggregate_exit.get("closed_at") or aggregate_exit.get("exit_submitted_at")
        exit_price = _number(aggregate_exit.get("exit_order_price") or aggregate_exit.get("last_price"))
        for position in unresolved:
            position.update(
                {
                    "status": "closed",
                    "status_label": "청산",
                    "exit_reason": exit_reason,
                    "exit_reason_label": EXIT_REASON_LABELS.get(str(exit_reason), str(exit_reason)),
                    "exit_at": exit_at,
                    "exit_price": round(exit_price, 4) if exit_price else None,
                    "exit_notional": round(exit_price * _number(position.get("quantity")), 2)
                    if exit_price
                    else None,
                    "exit_order_type": aggregate_exit.get("exit_order_type"),
                    "protection_id": aggregate_exit.get("id"),
                    "protection_status": "closed_aggregate",
                    "last_error": aggregate_exit.get("last_error"),
                }
            )
            position["held_days"] = _held_days(str(position.get("opened_at") or ""), str(exit_at or ""))
            position["held_over_2_days"] = int(position["held_days"]) >= 2

    return positions


def _position_journal_summary(positions: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total": len(positions),
        "active": sum(1 for item in positions if item.get("status") == "active"),
        "closed": sum(1 for item in positions if item.get("status") == "closed"),
        "exiting": sum(1 for item in positions if item.get("status") == "exiting"),
        "unknown": sum(1 for item in positions if item.get("status") == "unknown"),
        "take_profit": sum(1 for item in positions if item.get("exit_reason") == "take_profit"),
        "stop_loss": sum(1 for item in positions if item.get("exit_reason") == "stop_loss"),
        "strategy_sell": sum(1 for item in positions if item.get("exit_reason") == "strategy_sell"),
        "held_over_2_days": sum(1 for item in positions if item.get("held_over_2_days")),
    }


def _with_position_journal(
    summary: dict[str, Any],
    details_by_run: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    normalized = dict(summary)
    positions = _position_journal(
        [run for run in normalized.get("runs", []) if isinstance(run, dict)],
        details_by_run,
    )
    normalized["position_journal"] = positions
    normalized["position_journal_summary"] = _position_journal_summary(positions)
    return normalized


def _account_snapshot(value: Any) -> dict[str, float] | None:
    if not isinstance(value, dict) or not any(
        key in value for key in ("equity", "cash", "holdings_value")
    ):
        return None
    equity = round(_number(value.get("equity") or value.get("risk_equity")), 2)
    cash = round(_number(value.get("cash")), 2)
    holdings_value = round(_number(value.get("holdings_value")), 2)
    if equity <= 0 and cash <= 0 and holdings_value <= 0:
        return None
    return {
        "equity": equity,
        "cash": cash,
        "holdings_value": holdings_value,
    }


def _daily_record(runs: list[Any]) -> dict[str, Any] | None:
    normalized_runs = [run for run in runs if isinstance(run, dict)]
    opening_run = next(
        (
            (run, snapshot)
            for run in normalized_runs
            if (snapshot := _account_snapshot(run.get("account_before"))) is not None
        ),
        None,
    )
    closing_run = next(
        (
            (run, snapshot)
            for run in reversed(normalized_runs)
            if (snapshot := _account_snapshot(run.get("account_after"))) is not None
        ),
        None,
    )
    if opening_run is None or closing_run is None:
        return None

    _, opening = opening_run
    buy_notional = round(sum(_number(run.get("buy_notional")) for run in normalized_runs), 2)
    sell_notional = round(sum(_number(run.get("sell_notional")) for run in normalized_runs), 2)
    baseline_equity = opening["equity"]
    anomalies = []
    points = []
    for run in normalized_runs:
        snapshot = _account_snapshot(run.get("account_after"))
        if snapshot is None:
            continue
        if baseline_equity and snapshot["equity"] > 0:
            deviation = abs(snapshot["equity"] - baseline_equity) / baseline_equity
            if deviation > MAX_DAILY_EQUITY_DEVIATION_PCT:
                anomalies.append(
                    f"{run.get('run_id') or 'unknown'}: equity changed "
                    f"{deviation * 100:.1f}% from opening snapshot"
                )
                continue
        points.append(
            {
                "run_id": str(run.get("run_id") or ""),
                "time": (
                    run.get("finished_at")
                    or run.get("started_at")
                    or run.get("scheduled_at_kst")
                    or run.get("scheduled_at_et")
                    or ""
                ),
                **snapshot,
                "buy_notional": round(_number(run.get("buy_notional")), 2),
                "sell_notional": round(_number(run.get("sell_notional")), 2),
                "net_trade_cashflow": round(
                    _number(run.get("sell_notional")) - _number(run.get("buy_notional")),
                    2,
                ),
            }
        )

    valid = bool(points) and not anomalies
    closing_for_record = points[-1] if valid else opening
    pnl = round(closing_for_record["equity"] - opening["equity"], 2) if valid else 0.0
    cash_delta = round(closing_for_record["cash"] - opening["cash"], 2) if valid else 0.0
    holdings_value_delta = (
        round(closing_for_record["holdings_value"] - opening["holdings_value"], 2) if valid else 0.0
    )

    return {
        "source": "automation_report",
        "estimate": True,
        "valid": valid,
        "anomalies": anomalies,
        "start_equity": opening["equity"],
        "end_equity": closing_for_record["equity"],
        "pnl": pnl,
        "pnl_pct": round((pnl / opening["equity"]) * 100, 4) if opening["equity"] else 0.0,
        "start_cash": opening["cash"],
        "end_cash": closing_for_record["cash"],
        "cash_delta": cash_delta,
        "start_holdings_value": opening["holdings_value"],
        "end_holdings_value": closing_for_record["holdings_value"],
        "holdings_value_delta": holdings_value_delta,
        "buy_notional": buy_notional,
        "sell_notional": sell_notional,
        "net_trade_cashflow": round(sell_notional - buy_notional, 2),
        "cash_reconciliation_delta": (
            round(cash_delta - (sell_notional - buy_notional), 2) if valid else 0.0
        ),
        "points": points,
    }


def _with_daily_record(summary: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(summary)
    normalized["daily_record"] = _daily_record(list(summary.get("runs") or []))
    return normalized


def _session_date_to_iso(session_date: str) -> str:
    return f"{session_date[:4]}-{session_date[4:6]}-{session_date[6:8]}"


def _monthly_day(summary: dict[str, Any]) -> dict[str, Any] | None:
    record = summary.get("daily_record")
    if not isinstance(record, dict):
        return None
    session_date = str(summary.get("session_date") or "")
    if not SESSION_RE.fullmatch(session_date):
        return None
    valid = bool(record.get("valid", True))
    return {
        "date": _session_date_to_iso(session_date),
        "session_date": session_date,
        "valid": valid,
        "anomalies": record.get("anomalies") if isinstance(record.get("anomalies"), list) else [],
        "run_count": int(summary.get("run_count") or len(summary.get("runs") or [])),
        "pnl": round(_number(record.get("pnl")), 2),
        "pnl_pct": round(_number(record.get("pnl_pct")), 4),
        "start_equity": round(_number(record.get("start_equity")), 2),
        "end_equity": round(_number(record.get("end_equity")), 2),
        "start_cash": round(_number(record.get("start_cash")), 2),
        "end_cash": round(_number(record.get("end_cash")), 2),
        "cash_delta": round(_number(record.get("cash_delta")), 2),
        "start_holdings_value": round(_number(record.get("start_holdings_value")), 2),
        "end_holdings_value": round(_number(record.get("end_holdings_value")), 2),
        "holdings_value_delta": round(_number(record.get("holdings_value_delta")), 2),
        "buy_notional": round(_number(record.get("buy_notional")), 2),
        "sell_notional": round(_number(record.get("sell_notional")), 2),
        "net_trade_cashflow": round(_number(record.get("net_trade_cashflow")), 2),
        "cash_reconciliation_delta": round(_number(record.get("cash_reconciliation_delta")), 2),
        "error_count": int((summary.get("totals") or {}).get("errors") or 0),
    }


def _monthly_record(
    *,
    market: str,
    month: str,
    sessions: list[dict[str, Any]],
) -> dict[str, Any]:
    days = [
        day
        for session in sorted(sessions, key=lambda item: str(item.get("session_date") or ""))
        if str(session.get("session_date") or "").startswith(month.replace("-", ""))
        if (day := _monthly_day(session)) is not None
    ]
    valid_days = [day for day in days if day.get("valid", True)]
    first_day = valid_days[0] if valid_days else {}
    last_day = valid_days[-1] if valid_days else {}
    start_equity = _number(first_day.get("start_equity"))
    end_equity = _number(last_day.get("end_equity"))
    account_pnl = round(end_equity - start_equity, 2) if valid_days else 0.0
    pnl = round(sum(_number(day.get("pnl")) for day in valid_days), 2)
    return {
        "market": market,
        "month": month,
        "source": "automation_report",
        "estimate": True,
        "summary": {
            "day_count": len(days),
            "trading_days": len(valid_days),
            "anomaly_days": len(days) - len(valid_days),
            "win_days": sum(1 for day in valid_days if _number(day.get("pnl")) > 0),
            "loss_days": sum(1 for day in valid_days if _number(day.get("pnl")) < 0),
            "flat_days": sum(1 for day in valid_days if _number(day.get("pnl")) == 0),
            "pnl": pnl,
            "pnl_pct": round(sum(_number(day.get("pnl_pct")) for day in valid_days), 4),
            "account_pnl": account_pnl,
            "account_pnl_pct": round((account_pnl / start_equity) * 100, 4)
            if start_equity
            else 0.0,
            "start_equity": round(start_equity, 2),
            "end_equity": round(end_equity, 2),
            "buy_notional": round(sum(_number(day.get("buy_notional")) for day in valid_days), 2),
            "sell_notional": round(sum(_number(day.get("sell_notional")) for day in valid_days), 2),
            "net_trade_cashflow": round(
                sum(_number(day.get("net_trade_cashflow")) for day in valid_days), 2
            ),
            "cash_delta": round(sum(_number(day.get("cash_delta")) for day in valid_days), 2),
            "cash_reconciliation_delta": round(
                sum(_number(day.get("cash_reconciliation_delta")) for day in valid_days), 2
            ),
            "error_count": sum(int(day.get("error_count") or 0) for day in valid_days),
        },
        "days": days,
    }


def _normalize_session_sell_notionals(
    summary: dict[str, Any],
    *,
    report_dir: Path,
    run_pattern: re.Pattern[str],
    include_position_journal: bool = False,
    downgrade_noncritical_us_errors: bool = False,
    normalize_kr_runtime: bool = False,
) -> dict[str, Any]:
    runs = []
    changed = False
    details_by_run: dict[str, dict[str, Any]] = {}

    def read_detail(run_id: str) -> dict[str, Any]:
        if not run_pattern.fullmatch(run_id):
            return {}
        if run_id not in details_by_run:
            try:
                details_by_run[run_id] = _read_json(report_dir / f"{run_id}.json")
            except HTTPException:
                details_by_run[run_id] = {}
        return details_by_run[run_id]

    for run in summary.get("runs", []):
        if not isinstance(run, dict):
            runs.append(run)
            continue
        normalized_run = dict(run)
        run_id = str(run.get("run_id") or "")
        detail = None
        needs_detail = (
            "sell_notional" not in normalized_run
            or int(normalized_run.get("order_counts", {}).get("filled") or 0) == 0
            or include_position_journal
            or normalize_kr_runtime
        )
        if needs_detail:
            detail = read_detail(run_id)
        if "sell_notional" not in normalized_run:
            normalized_run["sell_notional"] = 0.0
            normalized_run["sell_notional"] = _sell_notional(detail) if detail else 0.0
            changed = True
        if downgrade_noncritical_us_errors:
            raw_errors = _list(normalized_run.get("errors"))
            critical_errors = [
                item for item in raw_errors if not _is_noncritical_us_error(item)
            ]
            if len(critical_errors) != len(raw_errors):
                noncritical_errors = [
                    item for item in raw_errors if _is_noncritical_us_error(item)
                ]
                normalized_run["errors"] = critical_errors
                normalized_run["warnings"] = list(dict.fromkeys([
                    *_list(normalized_run.get("warnings")),
                    *noncritical_errors,
                ]))
                changed = True
        if normalize_kr_runtime and detail:
            normalized_detail = _normalize_kr_detail(detail, run_id)
            rebuilt = _kr_run_summary(normalized_detail, run_id)
            for key in ("order_counts", "errors", "warnings"):
                if normalized_run.get(key) != rebuilt.get(key):
                    normalized_run[key] = rebuilt.get(key)
                    changed = True
        order_counts = (
            normalized_run.get("order_counts")
            if isinstance(normalized_run.get("order_counts"), dict)
            else {}
        )
        if detail and int(order_counts.get("filled") or 0) == 0:
            detail_orders = [
                *detail.get("submitted_sells", []),
                *detail.get("submitted_buys", []),
                *detail.get("orders", []),
            ]
            filled = sum(1 for order in detail_orders if _is_filled_or_completed_order(order))
            if filled:
                normalized_run["order_counts"] = {**order_counts, "filled": filled}
                changed = True
        runs.append(normalized_run)
    normalized = dict(summary)
    normalized["runs"] = runs
    if changed or "cumulative_sell_notional" not in summary:
        normalized["cumulative_sell_notional"] = round(
            sum(_number(run.get("sell_notional")) for run in runs if isinstance(run, dict)), 2
        )
        normalized["totals"] = {
            "submitted": sum(
                int(run.get("order_counts", {}).get("submitted") or 0)
                for run in runs
                if isinstance(run, dict)
            ),
            "filled": sum(
                int(run.get("order_counts", {}).get("filled") or 0)
                for run in runs
                if isinstance(run, dict)
            ),
            "failed": sum(
                int(run.get("order_counts", {}).get("failed") or 0)
                for run in runs
                if isinstance(run, dict)
            ),
            "deferred": sum(
                int(run.get("order_counts", {}).get("deferred") or 0)
                for run in runs
                if isinstance(run, dict)
            ),
            "errors": sum(len(run.get("errors") or []) for run in runs if isinstance(run, dict)),
        }
    normalized = _with_daily_record(normalized)
    if include_position_journal:
        normalized = _with_position_journal(normalized, details_by_run)
    return normalized


def _today_session_date() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")


def _current_month() -> str:
    return datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m")


def _normalize_month(month: str | None) -> str:
    normalized = month or _current_month()
    if not MONTH_RE.fullmatch(normalized):
        raise HTTPException(status_code=400, detail="Invalid month")
    return normalized


def _normalize_us_run(payload: dict[str, Any], run_id: str) -> dict[str, Any]:
    payload.setdefault("run_id", run_id)
    payload.setdefault("status", "legacy")
    payload.setdefault("report_only", False)
    payload.setdefault("duration_seconds", 0.0)
    payload.setdefault("signals", [])
    payload.setdefault("orders", [])
    payload.setdefault("submitted_sells", [])
    payload.setdefault("errors", [])
    payload.setdefault("account_before", payload.get("account", {}))
    payload.setdefault("account_after", {})
    return payload


def _compact_kr_account(snapshot: dict[str, Any]) -> dict[str, Any]:
    account = snapshot.get("account", {}) if isinstance(snapshot, dict) else {}
    deposit = account.get("deposit", {}) if isinstance(account, dict) else {}
    holdings = account.get("holdings", []) if isinstance(account, dict) else []
    total_eval = _number(deposit.get("total_eval"))
    cash = _number(deposit.get("deposit") or deposit.get("available_amount"))
    explicit_holdings_value = deposit.get("eval_amount") or deposit.get("evlu_amt_smtl_amt")
    holdings_value = (
        _number(explicit_holdings_value)
        if explicit_holdings_value is not None
        else max(0.0, total_eval - cash)
    )
    return {
        "equity": round(total_eval, 2),
        "risk_equity": round(total_eval, 2),
        "cash": round(cash, 2),
        "holdings_value": round(holdings_value, 2),
        "holdings_count": len(holdings) if isinstance(holdings, list) else 0,
    }


def _kr_errors(payload: dict[str, Any]) -> list[str]:
    errors = [str(item) for item in payload.get("errors", []) if item]
    errors.extend(
        f"candidate_selection: {item}"
        for item in payload.get("candidate_selection", {}).get("errors", [])
        if item
    )
    for signal in payload.get("signals", []):
        if signal.get("action") == "ERROR" and signal.get("reason"):
            errors.append(f"{signal.get('code') or signal.get('symbol')}: {signal.get('reason')}")
    for order in [*payload.get("submitted_sells", []), *payload.get("submitted_buys", [])]:
        status = str(order.get("order_status") or "")
        if status in FAILED_ORDER_STATUSES and (order.get("last_error") or order.get("message")):
            errors.append(
                f"{order.get('code') or order.get('stock_code') or order.get('symbol')}: "
                f"{order.get('last_error') or order.get('message')}"
            )
        result = order.get("order_result") if isinstance(order.get("order_result"), dict) else {}
        if result.get("status") == "error" and result.get("message"):
            errors.append(
                f"{order.get('code') or order.get('stock_code')}: {result.get('message')}"
            )
    for item in payload.get("account_after", {}).get("reservations", {}).get("errors", []):
        errors.append(f"reservations: {item}")
    protective_health = payload.get("account_after", {}).get("protective", {}).get("health", {})
    if protective_health.get("status") in {"degraded", "stale"}:
        errors.append(f"protective monitor: {protective_health.get('status')}")
    return list(dict.fromkeys(errors))


def _holding_codes(snapshot: Any) -> set[str]:
    if not isinstance(snapshot, dict):
        return set()
    account = snapshot.get("account")
    if not isinstance(account, dict):
        return set()
    return {
        str(row.get("stock_code") or "")
        for row in _list(account.get("holdings"))
        if isinstance(row, dict) and _number(row.get("quantity")) > 0
    }


def _normalize_kr_runtime_detail(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    warnings = [str(item) for item in _list(normalized.get("warnings")) if item]
    raw_errors = [str(item) for item in _list(normalized.get("errors")) if item]

    candidate = normalized.get("candidate_selection")
    if isinstance(candidate, dict) and not candidate.get("fallback_used"):
        candidate = dict(candidate)
        warnings.extend(
            f"candidate_selection: {item}"
            for item in _list(candidate.get("warnings"))
            if item
        )
        candidate_errors = [str(item) for item in _list(candidate.get("errors")) if item]
        if candidate_errors:
            candidate["errors"] = []
            candidate["warnings"] = list(dict.fromkeys([
                *[str(item) for item in _list(candidate.get("warnings")) if item],
                *candidate_errors,
            ]))
            warnings.extend(f"candidate_selection: {item}" for item in candidate_errors)
            removable = {f"candidate_selection: {item}" for item in candidate_errors}
            raw_errors = [item for item in raw_errors if item not in removable]
        normalized["candidate_selection"] = candidate

    before_codes = _holding_codes(normalized.get("account_before"))
    after_codes = _holding_codes(normalized.get("account_after"))
    normalized_sells = []
    for order in _list(normalized.get("submitted_sells")):
        if not isinstance(order, dict):
            normalized_sells.append(order)
            continue
        normalized_order = dict(order)
        raw_result = order.get("order_result")
        result = dict(raw_result) if isinstance(raw_result, dict) else {}
        code = _order_symbol(order)
        message = str(result.get("message") or order.get("message") or "")
        transient_false_missing = (
            order.get("order_status") in FAILED_ORDER_STATUSES
            and message.startswith("미보유 종목입니다.")
            and code in before_codes
            and code in after_codes
        )
        if transient_false_missing:
            normalized_order["original_order_status"] = order.get("order_status")
            normalized_order["order_status"] = "deferred"
            normalized_order["deferred_reason"] = "holdings_unavailable"
            result.update({
                "status": "deferred",
                "message": "KIS 잔고 조회 지연으로 매도 주문을 보류했습니다",
                "data": {
                    "reason_code": "holdings_unavailable",
                    "retryable": True,
                    "original_message": message,
                },
            })
            normalized_order["order_result"] = result
            original_error = f"{code}: {message}"
            raw_errors = [item for item in raw_errors if item != original_error]
        normalized_sells.append(normalized_order)

    normalized["submitted_sells"] = normalized_sells
    normalized["errors"] = list(dict.fromkeys(raw_errors))
    normalized["warnings"] = list(dict.fromkeys(warnings))
    return normalized


def _kr_run_summary(payload: dict[str, Any], run_id: str) -> dict[str, Any]:
    payload = _normalize_kr_runtime_detail(payload)
    signals = payload.get("signals", [])
    orders = [*payload.get("submitted_sells", []), *payload.get("submitted_buys", [])]
    return {
        "run_id": payload.get("run_id") or run_id,
        "slot": payload.get("slot", "legacy"),
        "scheduled_at_kst": payload.get("scheduled_at_kst"),
        "started_at": payload.get("started_at"),
        "finished_at": payload.get("finished_at"),
        "duration_seconds": round(float(payload.get("duration_seconds") or 0), 2),
        "status": payload.get("status", "completed"),
        "report_only": bool(payload.get("report_only", False)),
        "signal_counts": {
            action: sum(1 for signal in signals if signal.get("action") == action)
            for action in ("BUY", "SELL", "HOLD", "ERROR")
        },
        "order_counts": {
            "submitted": sum(
                1 for order in orders if order.get("order_status") in SUCCESS_ORDER_STATUSES
            ),
            "filled": sum(1 for order in orders if _is_filled_or_completed_order(order)),
            "failed": sum(
                1 for order in orders if order.get("order_status") in FAILED_ORDER_STATUSES
            ),
            "skipped": sum(
                1 for order in orders if str(order.get("order_status") or "").startswith("skipped")
            ),
            "deferred": sum(
                1 for order in orders if order.get("order_status") in DEFERRED_ORDER_STATUSES
            ),
        },
        "buy_notional": round(
            sum(
                float(order.get("amount") or order.get("notional") or 0)
                for order in payload.get("submitted_buys", [])
                if order.get("order_status") in SUCCESS_ORDER_STATUSES
            ),
            2,
        ),
        "sell_notional": _sell_notional(payload),
        "account_before": _compact_kr_account(payload.get("account_before", {})),
        "account_after": _compact_kr_account(payload.get("account_after", {})),
        "pending_count": int(
            payload.get("account_after", {}).get("pending", {}).get("total_count") or 0
        ),
        "app_reservation_count": int(
            payload.get("account_after", {}).get("reservations", {}).get("total_count") or 0
        ),
        "protective_count": len(
            payload.get("account_after", {}).get("protective", {}).get("orders") or []
        ),
        "errors": _kr_errors(payload),
        "warnings": [str(item) for item in _list(payload.get("warnings")) if item],
        "json_report": f"{run_id}.json",
        "markdown_report": f"{run_id}.md",
    }


def _kr_run_id_from_state_run(run: dict[str, Any]) -> str | None:
    run_id = run.get("run_id")
    if isinstance(run_id, str) and KR_RUN_RE.fullmatch(run_id):
        return run_id
    report = str(run.get("report") or "")
    if not report:
        return None
    stem = Path(report).stem
    return stem if KR_RUN_RE.fullmatch(stem) else None


def _normalize_kr_detail(payload: dict[str, Any], run_id: str) -> dict[str, Any]:
    payload = _normalize_kr_runtime_detail(payload)
    payload.setdefault("run_id", run_id)
    payload.setdefault("status", "completed")
    payload.setdefault("report_only", False)
    payload.setdefault("duration_seconds", 0.0)
    payload.setdefault("signals", [])
    payload.setdefault("submitted_buys", [])
    payload.setdefault("orders", payload.get("submitted_buys", []))
    payload.setdefault("submitted_sells", [])
    payload.setdefault("errors", _kr_errors(payload))
    payload.setdefault("warnings", [])
    payload.setdefault("account_before", {})
    payload.setdefault("account_after", {})
    return payload


def _read_kr_detail(run_id: str) -> dict[str, Any] | None:
    path = KR_REPORT_DIR / f"{run_id}.json"
    if not path.is_file():
        return None
    return _read_json(path)


def _synthesize_kr_session(session_date: str, *, include_position_journal: bool = False) -> dict[str, Any]:
    state = _read_json(KR_REPORT_DIR / f"{session_date}.json")
    runs = []
    details_by_run: dict[str, dict[str, Any]] = {}
    for run in state.get("runs", []):
        if not isinstance(run, dict):
            continue
        run_id = _kr_run_id_from_state_run(run)
        if run_id:
            detail = _read_kr_detail(run_id)
            if detail:
                details_by_run[run_id] = detail
            summary = _kr_run_summary(detail, run_id) if detail else None
            if summary:
                summary.setdefault("status", run.get("status", summary.get("status")))
                runs.append(summary)
                continue
        runs.append(
            {
                "run_id": run_id or f"legacy_{run.get('started_at', 'unknown')}",
                "slot": run.get("slot", "legacy"),
                "scheduled_at_kst": run.get("scheduled_at_kst"),
                "started_at": run.get("started_at"),
                "finished_at": None,
                "duration_seconds": 0.0,
                "status": run.get("status", "legacy"),
                "report_only": False,
                "signal_counts": {"BUY": 0, "SELL": 0, "HOLD": 0, "ERROR": 0},
                "order_counts": {
                    "submitted": 0,
                    "filled": 0,
                    "failed": 0,
                    "skipped": 0,
                    "deferred": 0,
                },
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "account_before": {},
                "account_after": {},
                "pending_count": 0,
                "app_reservation_count": 0,
                "protective_count": 0,
                "errors": [],
                "warnings": [],
                "json_report": f"{run_id}.json" if run_id else None,
                "markdown_report": f"{run_id}.md" if run_id else None,
            }
        )
    cumulative_buy = round(sum(float(run.get("buy_notional") or 0) for run in runs), 2)
    cumulative_sell = round(sum(float(run.get("sell_notional") or 0) for run in runs), 2)
    latest_account = next(
        (run.get("account_after") for run in reversed(runs) if run.get("account_after")), {}
    )
    risk_equity = float((latest_account or {}).get("risk_equity") or 0)
    normalized = _with_daily_record(
        {
            "session_date": session_date,
            "mode": "vps",
            "updated_at": state.get("updated_at") or state.get("generated_at") or "",
            "run_count": len(runs),
            "runs": runs,
            "events": state.get("events", []),
            "cumulative_buy_notional": cumulative_buy,
            "cumulative_sell_notional": cumulative_sell,
            "session_buy_limit": round(risk_equity * 0.10, 2),
            "remaining_buy_budget": round(max(0.0, risk_equity * 0.10 - cumulative_buy), 2),
            "session_loss_limit": round(risk_equity * 0.005, 2),
            "remaining_loss_budget": round(
                max(0.0, risk_equity * 0.005 - cumulative_buy * 0.03), 2
            ),
            "latest_account": latest_account,
            "totals": {
                "submitted": sum(
                    int(run.get("order_counts", {}).get("submitted") or 0) for run in runs
                ),
                "filled": sum(int(run.get("order_counts", {}).get("filled") or 0) for run in runs),
                "failed": sum(int(run.get("order_counts", {}).get("failed") or 0) for run in runs),
                "deferred": sum(
                    int(run.get("order_counts", {}).get("deferred") or 0) for run in runs
                ),
                "errors": sum(len(run.get("errors") or []) for run in runs),
            },
        }
    )
    if include_position_journal:
        normalized = _with_position_journal(normalized, details_by_run)
    return normalized


def _list_us_session_summaries() -> list[dict[str, Any]]:
    sessions = []
    if not US_REPORT_DIR.exists():
        return sessions
    for path in sorted(US_REPORT_DIR.glob("*_summary.json"), reverse=True):
        session_date = path.name.removesuffix("_summary.json")
        if not SESSION_RE.fullmatch(session_date):
            continue
        try:
            sessions.append(
                _normalize_session_sell_notionals(
                    _read_json(path),
                    report_dir=US_REPORT_DIR,
                    run_pattern=US_RUN_RE,
                    downgrade_noncritical_us_errors=True,
                )
            )
        except HTTPException:
            continue
    return sessions


def _list_kr_session_summaries() -> list[dict[str, Any]]:
    sessions = []
    if not KR_REPORT_DIR.exists():
        return sessions
    summary_paths = {
        path.name.removesuffix("_summary.json"): path
        for path in KR_REPORT_DIR.glob("*_summary.json")
        if SESSION_RE.fullmatch(path.name.removesuffix("_summary.json"))
    }
    session_dates = {
        path.stem for path in KR_REPORT_DIR.glob("*.json") if SESSION_RE.fullmatch(path.stem)
    } | set(summary_paths)
    for session_date in sorted(
        (item for item in session_dates if item <= _today_session_date()), reverse=True
    ):
        try:
            if session_date in summary_paths:
                sessions.append(
                    _normalize_session_sell_notionals(
                        _read_json(summary_paths[session_date]),
                        report_dir=KR_REPORT_DIR,
                        run_pattern=KR_RUN_RE,
                        normalize_kr_runtime=True,
                    )
                )
            else:
                sessions.append(_synthesize_kr_session(session_date))
        except HTTPException:
            continue
    return sessions


@router.get("/us/sessions")
async def list_us_sessions() -> dict[str, Any]:
    _require_vps()
    sessions = _list_us_session_summaries()
    return {"status": "success", "sessions": sessions, "total_count": len(sessions)}


@router.get("/us/records/monthly")
async def get_us_monthly_record(
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
) -> dict[str, Any]:
    _require_vps()
    normalized_month = _normalize_month(month)
    return {
        "status": "success",
        "data": _monthly_record(
            market="us",
            month=normalized_month,
            sessions=_list_us_session_summaries(),
        ),
    }


@router.get("/us/sessions/{session_date}")
async def get_us_session(session_date: str) -> dict[str, Any]:
    _require_vps()
    if not SESSION_RE.fullmatch(session_date):
        raise HTTPException(status_code=400, detail="Invalid session date")
    return {
        "status": "success",
        "data": _normalize_session_sell_notionals(
            _read_json(US_REPORT_DIR / f"{session_date}_summary.json"),
            report_dir=US_REPORT_DIR,
            run_pattern=US_RUN_RE,
            include_position_journal=True,
            downgrade_noncritical_us_errors=True,
        ),
    }


@router.get("/us/runs/{run_id}")
async def get_us_run(run_id: str) -> dict[str, Any]:
    _require_vps()
    if not US_RUN_RE.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    payload = _normalize_us_run(_read_json(US_REPORT_DIR / f"{run_id}.json"), run_id)
    return {"status": "success", "data": payload}


@router.get("/us/runs/{run_id}/download")
async def download_us_run(
    run_id: str,
    format: str = Query("md", pattern="^(md|json)$"),
) -> FileResponse:
    _require_vps()
    if not US_RUN_RE.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    path = US_REPORT_DIR / f"{run_id}.{format}"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Report not found")
    media_type = "text/markdown" if format == "md" else "application/json"
    return FileResponse(path, media_type=media_type, filename=path.name)


@router.get("/kr/sessions")
async def list_kr_sessions() -> dict[str, Any]:
    _require_vps()
    sessions = _list_kr_session_summaries()
    return {"status": "success", "sessions": sessions, "total_count": len(sessions)}


@router.get("/kr/records/monthly")
async def get_kr_monthly_record(
    month: str | None = Query(None, pattern=r"^\d{4}-\d{2}$"),
) -> dict[str, Any]:
    _require_vps()
    normalized_month = _normalize_month(month)
    return {
        "status": "success",
        "data": _monthly_record(
            market="kr",
            month=normalized_month,
            sessions=_list_kr_session_summaries(),
        ),
    }


@router.get("/kr/sessions/{session_date}")
async def get_kr_session(session_date: str) -> dict[str, Any]:
    _require_vps()
    if not SESSION_RE.fullmatch(session_date):
        raise HTTPException(status_code=400, detail="Invalid session date")
    summary_path = KR_REPORT_DIR / f"{session_date}_summary.json"
    if summary_path.is_file():
        return {
            "status": "success",
            "data": _normalize_session_sell_notionals(
                _read_json(summary_path),
                report_dir=KR_REPORT_DIR,
                run_pattern=KR_RUN_RE,
                include_position_journal=True,
                normalize_kr_runtime=True,
            ),
        }
    return {
        "status": "success",
        "data": _synthesize_kr_session(session_date, include_position_journal=True),
    }


@router.get("/kr/runs/{run_id}")
async def get_kr_run(run_id: str) -> dict[str, Any]:
    _require_vps()
    if not KR_RUN_RE.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    payload = _normalize_kr_detail(_read_json(KR_REPORT_DIR / f"{run_id}.json"), run_id)
    return {"status": "success", "data": payload}


@router.get("/kr/runs/{run_id}/download")
async def download_kr_run(
    run_id: str,
    format: str = Query("md", pattern="^(md|json)$"),
) -> FileResponse:
    _require_vps()
    if not KR_RUN_RE.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    path = KR_REPORT_DIR / f"{run_id}.{format}"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Report not found")
    media_type = "text/markdown" if format == "md" else "application/json"
    return FileResponse(path, media_type=media_type, filename=path.name)
