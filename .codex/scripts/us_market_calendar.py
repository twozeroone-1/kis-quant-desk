#!/usr/bin/env python3
"""XNYS calendar helpers for scheduled US paper-trading automation."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd


ET = ZoneInfo("America/New_York")
KST = ZoneInfo("Asia/Seoul")
UTC = ZoneInfo("UTC")
CALENDAR = xcals.get_calendar("XNYS")


def _as_datetime(value: Any) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.to_pydatetime()


def _session_label(session_date: str) -> pd.Timestamp:
    return pd.Timestamp(datetime.strptime(session_date, "%Y%m%d").date())


def _checked_at() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def scheduled_runs(session_date: str) -> list[dict[str, Any]]:
    label = _session_label(session_date)
    if not CALENDAR.is_session(label):
        return []

    opened = _as_datetime(CALENDAR.session_open(label))
    closed = _as_datetime(CALENDAR.session_close(label))
    current = opened + timedelta(minutes=15)
    last = closed - timedelta(minutes=15)
    runs: list[dict[str, Any]] = []
    while current <= last:
        current_et = current.astimezone(ET)
        hhmm = current_et.strftime("%H%M")
        runs.append({
            "run_id": f"{session_date}_{hhmm}_ET",
            "session_date": session_date,
            "hhmm_et": hhmm,
            "scheduled_at_et": current_et.isoformat(timespec="seconds"),
            "scheduled_at_kst": current.astimezone(KST).isoformat(timespec="seconds"),
            "scheduled_at_utc": current.astimezone(UTC).isoformat(timespec="seconds"),
        })
        current += timedelta(hours=1)
    return runs


def market_status(session_date: str) -> dict[str, Any]:
    label = _session_label(session_date)
    if not CALENDAR.is_session(label):
        day = label.date()
        reason = "weekend" if day.weekday() >= 5 else "exchange_holiday"
        return {
            "status": "success",
            "date": session_date,
            "is_open": False,
            "source": "exchange_calendars:XNYS",
            "record": {"reason": reason},
            "scheduled_runs": [],
            "checked_at": _checked_at(),
        }

    opened = _as_datetime(CALENDAR.session_open(label))
    closed = _as_datetime(CALENDAR.session_close(label))
    regular_close = opened + timedelta(hours=6, minutes=30)
    return {
        "status": "success",
        "date": session_date,
        "is_open": True,
        "source": "exchange_calendars:XNYS",
        "record": {
            "reason": "early_close" if closed < regular_close else "regular_session",
            "open_et": opened.astimezone(ET).isoformat(timespec="seconds"),
            "close_et": closed.astimezone(ET).isoformat(timespec="seconds"),
            "open_kst": opened.astimezone(KST).isoformat(timespec="seconds"),
            "close_kst": closed.astimezone(KST).isoformat(timespec="seconds"),
        },
        "scheduled_runs": scheduled_runs(session_date),
        "checked_at": _checked_at(),
    }


def resolve_scheduled_run(now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.now(KST)
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    current = current.astimezone(KST)
    current_et = current.astimezone(ET)
    session_date = current_et.strftime("%Y%m%d")
    status = market_status(session_date)

    if not status["is_open"]:
        first_slot = current_et.hour == 9 and current_et.minute == 45
        return {
            **status,
            "resolution": "market_closed" if first_slot else "not_scheduled",
            "first_closed_slot": first_slot,
            "now_et": current_et.isoformat(timespec="seconds"),
            "now_kst": current.isoformat(timespec="seconds"),
        }

    rounded = current.replace(second=0, microsecond=0)
    for index, run in enumerate(status["scheduled_runs"]):
        scheduled = datetime.fromisoformat(run["scheduled_at_kst"]).astimezone(KST)
        if scheduled == rounded:
            return {
                **status,
                **run,
                "resolution": "scheduled",
                "is_first_run": index == 0,
                "is_last_run": index == len(status["scheduled_runs"]) - 1,
                "now_et": current_et.isoformat(timespec="seconds"),
                "now_kst": current.isoformat(timespec="seconds"),
            }
    return {
        **status,
        "resolution": "not_scheduled",
        "now_et": current_et.isoformat(timespec="seconds"),
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
    parser.add_argument("--date", help="US session date in YYYYMMDD")
    parser.add_argument("--check-open", action="store_true", help="Exit 0 if open, 2 if closed.")
    parser.add_argument("--scheduled-runs", action="store_true", help="Print scheduled hourly runs.")
    parser.add_argument("--resolve-now", action="store_true", help="Resolve the current cron invocation.")
    parser.add_argument("--now", help="ISO timestamp used with --resolve-now.")
    args = parser.parse_args()

    if args.resolve_now:
        payload = resolve_scheduled_run(parse_now(args.now))
    elif args.scheduled_runs:
        if not args.date:
            parser.error("--date is required with --scheduled-runs")
        payload = {"date": args.date, "runs": scheduled_runs(args.date)}
    else:
        if not args.date:
            parser.error("--date is required")
        payload = market_status(args.date)

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.check_open and not payload.get("is_open"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
