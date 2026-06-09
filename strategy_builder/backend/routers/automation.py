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
US_RUN_RE = re.compile(r"^\d{8}_(?:\d{4}_ET|closed|\d{6}_(?:open|mid|close|manual))$")
KR_RUN_RE = re.compile(r"^\d{8}_(?:\d{4}_KST|closed|\d{6}_(?:hourly|open|mid|close|manual))$")
SUCCESS_ORDER_STATUSES = {"success", "submitted", "reservation_submitted"}
FAILED_ORDER_STATUSES = {"failed", "error", "reservation_failed"}


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


def _today_session_date() -> str:
    return datetime.now().strftime("%Y%m%d")


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
    total_eval = float(deposit.get("total_eval") or 0)
    cash = float(deposit.get("deposit") or deposit.get("available_amount") or 0)
    return {
        "equity": round(total_eval, 2),
        "risk_equity": round(total_eval, 2),
        "cash": round(cash, 2),
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
            errors.append(f"{order.get('code') or order.get('stock_code')}: {result.get('message')}")
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
            "submitted": sum(1 for order in orders if order.get("order_status") in SUCCESS_ORDER_STATUSES),
            "filled": sum(1 for order in orders if float(order.get("filled_quantity") or 0) > 0),
            "failed": sum(1 for order in orders if order.get("order_status") in FAILED_ORDER_STATUSES),
            "skipped": sum(1 for order in orders if str(order.get("order_status") or "").startswith("skipped")),
        },
        "buy_notional": round(sum(
            float(order.get("amount") or order.get("notional") or 0)
            for order in payload.get("submitted_buys", [])
            if order.get("order_status") in SUCCESS_ORDER_STATUSES
        ), 2),
        "account_before": _compact_kr_account(payload.get("account_before", {})),
        "account_after": _compact_kr_account(payload.get("account_after", {})),
        "pending_count": int(payload.get("account_after", {}).get("pending", {}).get("total_count") or 0),
        "app_reservation_count": int(payload.get("account_after", {}).get("reservations", {}).get("total_count") or 0),
        "protective_count": len(payload.get("account_after", {}).get("protective", {}).get("orders") or []),
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
        runs.append({
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
            "account_before": {},
            "account_after": {},
            "pending_count": 0,
            "app_reservation_count": 0,
            "protective_count": 0,
            "errors": [],
            "json_report": f"{run_id}.json" if run_id else None,
            "markdown_report": f"{run_id}.md" if run_id else None,
        })
    cumulative_buy = round(sum(float(run.get("buy_notional") or 0) for run in runs), 2)
    latest_account = next((run.get("account_after") for run in reversed(runs) if run.get("account_after")), {})
    risk_equity = float((latest_account or {}).get("risk_equity") or 0)
    return {
        "session_date": session_date,
        "mode": "vps",
        "updated_at": state.get("updated_at") or state.get("generated_at") or "",
        "run_count": len(runs),
        "runs": runs,
        "events": state.get("events", []),
        "cumulative_buy_notional": cumulative_buy,
        "session_buy_limit": round(risk_equity * 0.10, 2),
        "remaining_buy_budget": round(max(0.0, risk_equity * 0.10 - cumulative_buy), 2),
        "session_loss_limit": round(risk_equity * 0.005, 2),
        "remaining_loss_budget": round(max(0.0, risk_equity * 0.005 - cumulative_buy * 0.03), 2),
        "latest_account": latest_account,
        "totals": {
            "submitted": sum(int(run.get("order_counts", {}).get("submitted") or 0) for run in runs),
            "filled": sum(int(run.get("order_counts", {}).get("filled") or 0) for run in runs),
            "failed": sum(int(run.get("order_counts", {}).get("failed") or 0) for run in runs),
            "errors": sum(len(run.get("errors") or []) for run in runs),
        },
    }


@router.get("/us/sessions")
async def list_us_sessions() -> dict[str, Any]:
    _require_vps()
    sessions = []
    if US_REPORT_DIR.exists():
        for path in sorted(US_REPORT_DIR.glob("*_summary.json"), reverse=True):
            session_date = path.name.removesuffix("_summary.json")
            if not SESSION_RE.fullmatch(session_date):
                continue
            try:
                sessions.append(_read_json(path))
            except HTTPException:
                continue
    return {"status": "success", "sessions": sessions, "total_count": len(sessions)}


@router.get("/us/sessions/{session_date}")
async def get_us_session(session_date: str) -> dict[str, Any]:
    _require_vps()
    if not SESSION_RE.fullmatch(session_date):
        raise HTTPException(status_code=400, detail="Invalid session date")
    return {"status": "success", "data": _read_json(US_REPORT_DIR / f"{session_date}_summary.json")}


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
    sessions = []
    if KR_REPORT_DIR.exists():
        summary_paths = {
            path.name.removesuffix("_summary.json"): path
            for path in KR_REPORT_DIR.glob("*_summary.json")
            if SESSION_RE.fullmatch(path.name.removesuffix("_summary.json"))
        }
        session_dates = {
            path.stem
            for path in KR_REPORT_DIR.glob("*.json")
            if SESSION_RE.fullmatch(path.stem)
        } | set(summary_paths)
        for session_date in sorted((item for item in session_dates if item <= _today_session_date()), reverse=True):
            try:
                if session_date in summary_paths:
                    sessions.append(_read_json(summary_paths[session_date]))
                else:
                    sessions.append(_synthesize_kr_session(session_date))
            except HTTPException:
                continue
    return {"status": "success", "sessions": sessions, "total_count": len(sessions)}


@router.get("/kr/sessions/{session_date}")
async def get_kr_session(session_date: str) -> dict[str, Any]:
    _require_vps()
    if not SESSION_RE.fullmatch(session_date):
        raise HTTPException(status_code=400, detail="Invalid session date")
    summary_path = KR_REPORT_DIR / f"{session_date}_summary.json"
    if summary_path.is_file():
        return {"status": "success", "data": _read_json(summary_path)}
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
