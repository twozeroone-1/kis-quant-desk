from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from strategy_builder.backend.routers import automation


class AutomationReportsApiTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.report_dir = Path(self.tempdir.name)
        self.kr_report_dir = self.report_dir / "kr"
        self.kr_report_dir.mkdir()
        self.us_report_patch = patch.object(automation, "US_REPORT_DIR", self.report_dir)
        self.kr_report_patch = patch.object(automation, "KR_REPORT_DIR", self.kr_report_dir)
        self.mode_patch = patch.dict(os.environ, {"KIS_LOCK_MODE": "vps"})
        self.us_report_patch.start()
        self.kr_report_patch.start()
        self.mode_patch.start()

    def tearDown(self):
        self.mode_patch.stop()
        self.kr_report_patch.stop()
        self.us_report_patch.stop()
        self.tempdir.cleanup()

    def test_lists_session_and_reads_run(self):
        summary = {"session_date": "20260605", "runs": [], "run_count": 0}
        detail = {"run_id": "20260605_0945_ET", "status": "completed"}
        (self.report_dir / "20260605_summary.json").write_text(json.dumps(summary), encoding="utf-8")
        (self.report_dir / "20260605_0945_ET.json").write_text(json.dumps(detail), encoding="utf-8")

        sessions = asyncio.run(automation.list_us_sessions())
        run = asyncio.run(automation.get_us_run("20260605_0945_ET"))

        self.assertEqual(sessions["total_count"], 1)
        self.assertEqual(run["data"]["status"], "completed")

    def test_legacy_run_is_normalized_for_the_timeline(self):
        legacy = {"slot": "open", "started_at": "2026-06-04T23:45:00+09:00"}
        (self.report_dir / "20260604_234500_open.json").write_text(json.dumps(legacy), encoding="utf-8")

        run = asyncio.run(automation.get_us_run("20260604_234500_open"))

        self.assertEqual(run["data"]["run_id"], "20260604_234500_open")
        self.assertEqual(run["data"]["status"], "legacy")
        self.assertEqual(run["data"]["duration_seconds"], 0.0)

    def test_rejects_invalid_paths_and_prod_mode(self):
        with self.assertRaises(HTTPException):
            asyncio.run(automation.get_us_run("../secret"))
        with self.assertRaises(HTTPException):
            asyncio.run(automation.get_kr_run("../secret"))

        with patch.dict(os.environ, {"KIS_LOCK_MODE": "prod"}):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(automation.list_us_sessions())
        self.assertEqual(raised.exception.status_code, 404)

    def test_synthesizes_kr_session_from_state_and_detail_report(self):
        state = {
            "runs": [{
                "run_id": "20260609_0910_KST",
                "slot": "hourly",
                "started_at": "2026-06-09T09:10:00+09:00",
                "scheduled_at_kst": "2026-06-09T09:10:00+09:00",
                "status": "completed",
                "report": str(self.kr_report_dir / "20260609_0910_KST.md"),
            }],
            "orders": [],
        }
        detail = {
            "run_id": "20260609_0910_KST",
            "slot": "hourly",
            "started_at": "2026-06-09T09:10:00+09:00",
            "scheduled_at_kst": "2026-06-09T09:10:00+09:00",
            "signals": [
                {"code": "005930", "action": "BUY"},
                {"code": "000660", "action": "HOLD"},
            ],
            "submitted_buys": [{
                "code": "005930",
                "action": "BUY",
                "quantity": 2,
                "amount": 140000,
                "order_status": "success",
            }],
            "submitted_sells": [],
            "account_before": {"account": {"deposit": {"total_eval": 10_000_000, "deposit": 5_000_000}}},
            "account_after": {
                "account": {"deposit": {"total_eval": 10_000_000, "deposit": 4_860_000}},
                "pending": {"total_count": 0},
                "reservations": {"total_count": 0},
                "protective": {"orders": [{"stock_code": "005930"}]},
            },
        }
        (self.kr_report_dir / "20260609.json").write_text(json.dumps(state), encoding="utf-8")
        (self.kr_report_dir / "20260609_0910_KST.json").write_text(json.dumps(detail), encoding="utf-8")
        (self.kr_report_dir / "20260609_0910_KST.md").write_text("# report\n", encoding="utf-8")

        sessions = asyncio.run(automation.list_kr_sessions())
        run = asyncio.run(automation.get_kr_run("20260609_0910_KST"))

        self.assertEqual(sessions["total_count"], 1)
        session = sessions["sessions"][0]
        self.assertEqual(session["session_date"], "20260609")
        self.assertEqual(session["run_count"], 1)
        self.assertEqual(session["runs"][0]["signal_counts"]["BUY"], 1)
        self.assertEqual(session["runs"][0]["buy_notional"], 140000)
        self.assertEqual(session["remaining_buy_budget"], 860000)
        self.assertEqual(run["data"]["orders"][0]["code"], "005930")
        self.assertEqual(run["data"]["scheduled_at_kst"], "2026-06-09T09:10:00+09:00")
