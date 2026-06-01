#!/usr/bin/env python3
"""US regular-session trading-day guard for scheduled KIS automation.

The scheduler keys US sessions by the US market date, not the Korean local
date. This module intentionally avoids a new package dependency and implements
the core NYSE full-day holiday rules used by the daily paper-trading guard.
Early closes are still considered open days.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo


def nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    current = date(year, month, 1)
    while current.weekday() != weekday:
        current += timedelta(days=1)
    return current + timedelta(days=7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int) -> date:
    current = date(year, month + 1, 1) - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def observed_fixed(year: int, month: int, day: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def easter_date(year: int) -> date:
    """Return Gregorian Easter Sunday for the given year."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def nyse_full_holidays(year: int) -> dict[date, str]:
    holidays = {
        observed_fixed(year, 1, 1): "New Year's Day",
        nth_weekday(year, 1, 0, 3): "Martin Luther King Jr. Day",
        nth_weekday(year, 2, 0, 3): "Washington's Birthday",
        easter_date(year) - timedelta(days=2): "Good Friday",
        last_weekday(year, 5, 0): "Memorial Day",
        observed_fixed(year, 6, 19): "Juneteenth National Independence Day",
        observed_fixed(year, 7, 4): "Independence Day",
        nth_weekday(year, 9, 0, 1): "Labor Day",
        nth_weekday(year, 11, 3, 4): "Thanksgiving Day",
        observed_fixed(year, 12, 25): "Christmas Day",
    }
    return holidays


def market_status(session_date: str) -> dict[str, Any]:
    dt = datetime.strptime(session_date, "%Y%m%d").date()
    if dt.weekday() >= 5:
        return {
            "status": "success",
            "date": session_date,
            "is_open": False,
            "source": "nyse_rule_calendar",
            "record": {"reason": "weekend"},
            "checked_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
        }

    holidays = nyse_full_holidays(dt.year)
    if dt in holidays:
        return {
            "status": "success",
            "date": session_date,
            "is_open": False,
            "source": "nyse_rule_calendar",
            "record": {"reason": holidays[dt]},
            "checked_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
        }

    return {
        "status": "success",
        "date": session_date,
        "is_open": True,
        "source": "nyse_rule_calendar",
        "record": {"reason": "regular_or_early_close_trading_day"},
        "checked_at": datetime.now(ZoneInfo("Asia/Seoul")).isoformat(timespec="seconds"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True, help="US session date in YYYYMMDD")
    parser.add_argument("--check-open", action="store_true", help="Exit 0 if open, 2 if closed.")
    args = parser.parse_args()

    payload = market_status(args.date)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if args.check_open and not payload.get("is_open"):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
