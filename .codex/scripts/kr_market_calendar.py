#!/usr/bin/env python3
"""Korean-market trading-day guard for scheduled automation.

Uses KIS domestic holiday API (`/uapi/domestic-stock/v1/quotations/chk-holiday`)
and caches each date because KIS recommends calling this endpoint sparingly.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[2]
STRATEGY_BUILDER = PROJECT_ROOT / "strategy_builder"
RUNTIME_DIR = PROJECT_ROOT / ".codex" / "runtime" / "kr_market_auto" / "calendar"
KST = ZoneInfo("Asia/Seoul")
KR_HOURLY_START = (9, 10)
KR_HOURLY_END = (15, 10)


def cache_path(date: str) -> Path:
    return RUNTIME_DIR / f"{date}.json"


def load_cache(date: str) -> dict[str, Any] | None:
    path = cache_path(date)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_cache(date: str, payload: dict[str, Any]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    cache_path(date).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _checked_at() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def scheduled_runs(date: str) -> list[dict[str, Any]]:
    current = datetime.strptime(date, "%Y%m%d").replace(
        hour=KR_HOURLY_START[0],
        minute=KR_HOURLY_START[1],
        tzinfo=KST,
    )
    last = datetime.strptime(date, "%Y%m%d").replace(
        hour=KR_HOURLY_END[0],
        minute=KR_HOURLY_END[1],
        tzinfo=KST,
    )
    runs: list[dict[str, Any]] = []
    while current <= last:
        hhmm = current.strftime("%H%M")
        runs.append({
            "run_id": f"{date}_{hhmm}_KST",
            "session_date": date,
            "hhmm_kst": hhmm,
            "scheduled_at_kst": current.isoformat(timespec="seconds"),
        })
        current += timedelta(hours=1)
    return runs


def _with_schedule(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("is_open"):
        return {**payload, "scheduled_runs": scheduled_runs(str(payload.get("date")))}
    return {**payload, "scheduled_runs": []}


def normalize_flag(value: Any) -> str:
    return str(value or "").strip().upper()


def record_for_date(records: list[dict[str, Any]], date: str) -> dict[str, Any] | None:
    for record in records:
        record_date = (
            record.get("bass_dt")
            or record.get("BASS_DT")
            or record.get("bas_dt")
            or record.get("date")
        )
        if str(record_date or "").strip() == date:
            return record
    return records[0] if records else None


def is_open_from_record(record: dict[str, Any]) -> bool:
    opnd_yn = normalize_flag(record.get("opnd_yn") or record.get("OPND_YN"))
    if opnd_yn:
        return opnd_yn == "Y"
    tr_day_yn = normalize_flag(record.get("tr_day_yn") or record.get("TR_DAY_YN"))
    if tr_day_yn:
        return tr_day_yn == "Y"
    bzdy_yn = normalize_flag(record.get("bzdy_yn") or record.get("BZDY_YN"))
    if bzdy_yn:
        return bzdy_yn == "Y"
    return False


def query_kis_calendar(date: str) -> dict[str, Any]:
    sys.path.insert(0, str(STRATEGY_BUILDER))
    import kis_auth as ka

    ka.auth(svr="vps")
    params = {
        "BASS_DT": date,
        "CTX_AREA_FK": "",
        "CTX_AREA_NK": "",
    }
    result = ka._url_fetch(
        "/uapi/domestic-stock/v1/quotations/chk-holiday",
        "CTCA0903R",
        "",
        params,
    )
    if not result.isOK():
        return {
            "status": "error",
            "date": date,
            "is_open": False,
            "source": "kis_chk_holiday",
            "error_code": getattr(result, "getErrorCode", lambda: "")(),
            "error_message": getattr(result, "getErrorMessage", lambda: "")(),
            "error": "KIS chk-holiday API returned an error",
        }

    body = result.getBody()
    output = getattr(body, "output", [])
    if isinstance(output, dict):
        records = [output]
    else:
        records = list(output or [])

    record = record_for_date(records, date) or {}
    is_open = is_open_from_record(record)
    payload = {
        "status": "success",
        "date": date,
        "is_open": is_open,
        "source": "kis_chk_holiday",
        "record": record,
        "checked_at": _checked_at(),
    }
    payload = _with_schedule(payload)
    save_cache(date, payload)
    return payload


def query_price_probe(date: str, calendar_error: dict[str, Any]) -> dict[str, Any]:
    """Fallback when chk-holiday is unavailable.

    A successful KRX quote during scheduled regular-session slots is strong
    evidence that domestic trading is open. If this also fails, stay closed.
    """
    sys.path.insert(0, str(STRATEGY_BUILDER))
    import kis_auth as ka

    ka.auth(svr="vps")
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": "069500",
    }
    result = ka._url_fetch(
        "/uapi/domestic-stock/v1/quotations/inquire-price",
        "FHKST01010100",
        "",
        params,
    )
    if result.isOK():
        output = getattr(result.getBody(), "output", {}) or {}
        price = int(output.get("stck_prpr") or 0)
        if price > 0:
            payload = {
                "status": "success",
                "date": date,
                "is_open": True,
                "source": "price_probe_after_calendar_error",
                "record": {
                    "probe_symbol": "069500",
                    "price": price,
                    "calendar_error": calendar_error,
                },
                "checked_at": _checked_at(),
            }
            payload = _with_schedule(payload)
            save_cache(date, payload)
            return payload

    return {
        "status": "error",
        "date": date,
        "is_open": False,
        "source": "calendar_and_price_probe_failed",
        "error_code": getattr(result, "getErrorCode", lambda: "")(),
        "error_message": getattr(result, "getErrorMessage", lambda: "")(),
        "calendar_error": calendar_error,
        "error": "Both KIS chk-holiday and quote probe failed; fail closed.",
    }


def market_status(date: str, refresh: bool = False) -> dict[str, Any]:
    if not refresh:
        cached = load_cache(date)
        if cached:
            return {**_with_schedule(cached), "cached": True}

    # Weekend fallback before KIS call avoids pointless auth/API calls.
    dt = datetime.strptime(date, "%Y%m%d")
    if dt.weekday() >= 5:
        payload = {
            "status": "success",
            "date": date,
            "is_open": False,
            "source": "weekend",
            "record": {"reason": "weekend"},
            "checked_at": _checked_at(),
        }
        payload = _with_schedule(payload)
        save_cache(date, payload)
        return payload

    payload = query_kis_calendar(date)
    if payload.get("status") == "success":
        return _with_schedule(payload)
    return _with_schedule(query_price_probe(date, payload))


def resolve_scheduled_run(now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    current = current.astimezone(KST)
    session_date = current.strftime("%Y%m%d")
    status = market_status(session_date)
    rounded = current.replace(second=0, microsecond=0)

    if not status.get("is_open"):
        first_slot = (
            rounded.hour == KR_HOURLY_START[0]
            and rounded.minute == KR_HOURLY_START[1]
        )
        return {
            **status,
            "resolution": "market_closed" if first_slot else "not_scheduled",
            "first_closed_slot": first_slot,
            "now_kst": current.isoformat(timespec="seconds"),
        }

    runs = status.get("scheduled_runs") or []
    for index, run in enumerate(runs):
        scheduled = datetime.fromisoformat(run["scheduled_at_kst"]).astimezone(KST)
        if scheduled == rounded:
            return {
                **status,
                **run,
                "resolution": "scheduled",
                "is_first_run": index == 0,
                "is_last_run": index == len(runs) - 1,
                "now_kst": current.isoformat(timespec="seconds"),
            }
    return {
        **status,
        "resolution": "not_scheduled",
        "now_kst": current.isoformat(timespec="seconds"),
    }


def parse_now(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=KST)
    return parsed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now(KST).strftime("%Y%m%d"))
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--check-open", action="store_true", help="Exit 0 if open, 2 if closed/unknown.")
    parser.add_argument("--scheduled-runs", action="store_true", help="Print scheduled hourly runs.")
    parser.add_argument("--resolve-now", action="store_true", help="Resolve the current cron invocation.")
    parser.add_argument("--now", help="ISO timestamp used with --resolve-now.")
    args = parser.parse_args()

    try:
        if args.resolve_now:
            payload = resolve_scheduled_run(parse_now(args.now))
        elif args.scheduled_runs:
            payload = {"date": args.date, "runs": scheduled_runs(args.date)}
        else:
            payload = market_status(args.date, refresh=args.refresh)
    except Exception as exc:
        payload = {
            "status": "error",
            "date": args.date,
            "is_open": False,
            "source": "exception",
            "error": str(exc)[:1000],
        }

    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    if args.check_open and not payload.get("is_open"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
