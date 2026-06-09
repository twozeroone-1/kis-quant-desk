from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / ".codex" / "scripts"))

import kr_market_calendar


KST = ZoneInfo("Asia/Seoul")


class KrMarketCalendarTest(unittest.TestCase):
    def test_regular_session_has_seven_hourly_runs(self):
        runs = kr_market_calendar.scheduled_runs("20260609")

        self.assertEqual(len(runs), 7)
        self.assertEqual(runs[0]["run_id"], "20260609_0910_KST")
        self.assertEqual(runs[0]["scheduled_at_kst"], "2026-06-09T09:10:00+09:00")
        self.assertEqual(runs[-1]["run_id"], "20260609_1510_KST")

    def test_resolve_scheduled_run_matches_hourly_slot(self):
        original = kr_market_calendar.market_status
        try:
            kr_market_calendar.market_status = lambda date, refresh=False: {
                "status": "success",
                "date": date,
                "is_open": True,
                "source": "test",
                "record": {},
                "scheduled_runs": kr_market_calendar.scheduled_runs(date),
            }

            resolved = kr_market_calendar.resolve_scheduled_run(
                datetime(2026, 6, 9, 10, 10, tzinfo=KST)
            )
        finally:
            kr_market_calendar.market_status = original

        self.assertEqual(resolved["resolution"], "scheduled")
        self.assertEqual(resolved["date"], "20260609")
        self.assertEqual(resolved["run_id"], "20260609_1010_KST")

    def test_closed_day_only_marks_first_slot(self):
        original = kr_market_calendar.market_status
        try:
            kr_market_calendar.market_status = lambda date, refresh=False: {
                "status": "success",
                "date": date,
                "is_open": False,
                "source": "test",
                "record": {"reason": "closed"},
                "scheduled_runs": [],
            }

            first = kr_market_calendar.resolve_scheduled_run(
                datetime(2026, 6, 6, 9, 10, tzinfo=KST)
            )
            later = kr_market_calendar.resolve_scheduled_run(
                datetime(2026, 6, 6, 10, 10, tzinfo=KST)
            )
        finally:
            kr_market_calendar.market_status = original

        self.assertEqual(first["resolution"], "market_closed")
        self.assertTrue(first["first_closed_slot"])
        self.assertEqual(later["resolution"], "not_scheduled")
        self.assertFalse(later["first_closed_slot"])


if __name__ == "__main__":
    unittest.main()
