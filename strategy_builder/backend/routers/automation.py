"""Read-only paper-trading automation reports."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

router = APIRouter()
US_REPORT_DIR = Path(os.environ.get("US_MARKET_REPORT_DIR", "/app/us-market-reports"))
KR_REPORT_DIR = Path(os.environ.get("KR_MARKET_REPORT_DIR", "/app/kr-market-reports"))
SESSION_RE = re.compile(r"^\d{8}$")
MONTH_RE = re.compile(r"^\d{4}-\d{2}$")
US_RUN_RE = re.compile(r"^\d{8}_(?:\d{4}_ET|closed|\d{6}_(?:open|mid|close|manual))$")
KR_RUN_RE = re.compile(r"^\d{8}_(?:\d{4}_KST|closed|\d{6}_(?:hourly|open|mid|close|manual))$")
SUCCESS_ORDER_STATUSES = {"success", "submitted", "reservation_submitted"}
FAILED_ORDER_STATUSES = {"failed", "error", "reservation_failed"}
MAX_DAILY_EQUITY_DEVIATION_PCT = 0.5


def _number(value: Any) -> float:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if number == number and abs(number) != float("inf") else 0.0


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


def _sell_notional(payload: dict[str, Any]) -> float:
    return round(
        sum(
            _order_notional(order)
            for order in payload.get("submitted_sells", [])
            if order.get("order_status") in SUCCESS_ORDER_STATUSES
        ),
        2,
    )


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
) -> dict[str, Any]:
    runs = []
    changed = False
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
        )
        if needs_detail and run_pattern.fullmatch(run_id):
            try:
                detail = _read_json(report_dir / f"{run_id}.json")
            except HTTPException:
                detail = {}
        if "sell_notional" not in normalized_run:
            normalized_run["sell_notional"] = 0.0
            normalized_run["sell_notional"] = _sell_notional(detail) if detail else 0.0
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
            "errors": sum(len(run.get("errors") or []) for run in runs if isinstance(run, dict)),
        }
    return _with_daily_record(normalized)


def _today_session_date() -> str:
    return datetime.now().strftime("%Y%m%d")


def _current_month() -> str:
    return datetime.now().strftime("%Y-%m")


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


def _kr_run_summary(payload: dict[str, Any], run_id: str) -> dict[str, Any]:
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
    payload.setdefault("run_id", run_id)
    payload.setdefault("status", "completed")
    payload.setdefault("report_only", False)
    payload.setdefault("duration_seconds", 0.0)
    payload.setdefault("signals", [])
    payload.setdefault("submitted_buys", [])
    payload.setdefault("orders", payload.get("submitted_buys", []))
    payload.setdefault("submitted_sells", [])
    payload.setdefault("errors", _kr_errors(payload))
    payload.setdefault("account_before", {})
    payload.setdefault("account_after", {})
    return payload


def _read_kr_detail(run_id: str) -> dict[str, Any] | None:
    path = KR_REPORT_DIR / f"{run_id}.json"
    if not path.is_file():
        return None
    return _read_json(path)


def _synthesize_kr_session(session_date: str) -> dict[str, Any]:
    state = _read_json(KR_REPORT_DIR / f"{session_date}.json")
    runs = []
    for run in state.get("runs", []):
        if not isinstance(run, dict):
            continue
        run_id = _kr_run_id_from_state_run(run)
        if run_id:
            detail = _read_kr_detail(run_id)
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
                "order_counts": {"submitted": 0, "filled": 0, "failed": 0, "skipped": 0},
                "buy_notional": 0.0,
                "sell_notional": 0.0,
                "account_before": {},
                "account_after": {},
                "pending_count": 0,
                "app_reservation_count": 0,
                "protective_count": 0,
                "errors": [],
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
    return _with_daily_record(
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
                "errors": sum(len(run.get("errors") or []) for run in runs),
            },
        }
    )


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
            ),
        }
    return {"status": "success", "data": _synthesize_kr_session(session_date)}


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
