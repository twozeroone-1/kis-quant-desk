from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / ".codex" / "scripts"))

import us_market_calendar


KST = ZoneInfo("Asia/Seoul")


class UsMarketCalendarTest(unittest.TestCase):
    def test_dst_regular_session_has_seven_hourly_runs(self):
        runs = us_market_calendar.scheduled_runs("20260605")

        self.assertEqual(len(runs), 7)
        self.assertEqual(runs[0]["run_id"], "20260605_0945_ET")
        self.assertEqual(runs[0]["scheduled_at_kst"], "2026-06-05T22:45:00+09:00")
        self.assertEqual(runs[-1]["run_id"], "20260605_1545_ET")

    def test_standard_time_regular_session_has_seven_hourly_runs(self):
        runs = us_market_calendar.scheduled_runs("20260105")

        self.assertEqual(len(runs), 7)
        self.assertEqual(runs[0]["scheduled_at_kst"], "2026-01-05T23:45:00+09:00")
        self.assertEqual(runs[-1]["scheduled_at_kst"], "2026-01-06T05:45:00+09:00")

    def test_early_close_has_four_hourly_runs(self):
        runs = us_market_calendar.scheduled_runs("20261127")
        status = us_market_calendar.market_status("20261127")

        self.assertEqual(len(runs), 4)
        self.assertEqual(runs[-1]["run_id"], "20261127_1245_ET")
        self.assertEqual(status["record"]["reason"], "early_close")

    def test_exchange_holiday_has_no_runs(self):
        status = us_market_calendar.market_status("20260703")

        self.assertFalse(status["is_open"])
        self.assertEqual(status["scheduled_runs"], [])

    def test_midnight_kst_resolves_to_previous_us_session(self):
        resolved = us_market_calendar.resolve_scheduled_run(
            datetime(2026, 6, 6, 0, 45, tzinfo=KST)
        )

        self.assertEqual(resolved["resolution"], "scheduled")
        self.assertEqual(resolved["date"], "20260605")
        self.assertEqual(resolved["run_id"], "20260605_1145_ET")

    def test_closed_day_only_marks_first_slot(self):
        first = us_market_calendar.resolve_scheduled_run(
            datetime(2026, 7, 3, 22, 45, tzinfo=KST)
        )
        later = us_market_calendar.resolve_scheduled_run(
            datetime(2026, 7, 3, 23, 45, tzinfo=KST)
        )

        self.assertEqual(first["resolution"], "market_closed")
        self.assertTrue(first["first_closed_slot"])
        self.assertEqual(later["resolution"], "not_scheduled")
        self.assertFalse(later["first_closed_slot"])
