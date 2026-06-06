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
        self.report_patch = patch.object(automation, "REPORT_DIR", self.report_dir)
        self.mode_patch = patch.dict(os.environ, {"KIS_LOCK_MODE": "vps"})
        self.report_patch.start()
        self.mode_patch.start()

    def tearDown(self):
        self.mode_patch.stop()
        self.report_patch.stop()
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

        with patch.dict(os.environ, {"KIS_LOCK_MODE": "prod"}):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(automation.list_us_sessions())
        self.assertEqual(raised.exception.status_code, 404)
