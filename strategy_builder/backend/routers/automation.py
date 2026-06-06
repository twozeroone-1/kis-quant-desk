"""Read-only US paper-trading automation reports."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse


router = APIRouter()
REPORT_DIR = Path(os.environ.get("US_MARKET_REPORT_DIR", "/app/us-market-reports"))
SESSION_RE = re.compile(r"^\d{8}$")
RUN_RE = re.compile(r"^\d{8}_(?:\d{4}_ET|closed|\d{6}_(?:open|mid|close|manual))$")


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


@router.get("/us/sessions")
async def list_us_sessions() -> dict[str, Any]:
    _require_vps()
    sessions = []
    if REPORT_DIR.exists():
        for path in sorted(REPORT_DIR.glob("*_summary.json"), reverse=True):
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
    return {"status": "success", "data": _read_json(REPORT_DIR / f"{session_date}_summary.json")}


@router.get("/us/runs/{run_id}")
async def get_us_run(run_id: str) -> dict[str, Any]:
    _require_vps()
    if not RUN_RE.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    payload = _read_json(REPORT_DIR / f"{run_id}.json")
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
    return {"status": "success", "data": payload}


@router.get("/us/runs/{run_id}/download")
async def download_us_run(
    run_id: str,
    format: str = Query("md", pattern="^(md|json)$"),
) -> FileResponse:
    _require_vps()
    if not RUN_RE.fullmatch(run_id):
        raise HTTPException(status_code=400, detail="Invalid run ID")
    path = REPORT_DIR / f"{run_id}.{format}"
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Report not found")
    media_type = "text/markdown" if format == "md" else "application/json"
    return FileResponse(path, media_type=media_type, filename=path.name)
