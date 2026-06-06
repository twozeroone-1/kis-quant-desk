from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from zoneinfo import ZoneInfo

import os
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("KIS_CONFIG_ROOT", str(PROJECT_ROOT / "tests" / "fixtures" / "kis_config"))
os.environ.setdefault("KIS_TOKEN_ROOT", "/tmp/open-trading-api-test-kis-tokens")
sys.path.insert(0, str(PROJECT_ROOT / "strategy_builder"))

try:
    from backend.services import app_reservations
except ModuleNotFoundError as exc:
    app_reservations = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


KST = ZoneInfo("Asia/Seoul")


def iso_at(offset_minutes: int) -> str:
    return (datetime.now(KST) + timedelta(minutes=offset_minutes)).isoformat(timespec="seconds")


@unittest.skipIf(app_reservations is None, f"strategy_builder dependencies unavailable: {IMPORT_ERROR}")
class AppReservationsTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.state_file = Path(self.tempdir.name) / "app_reservations.json"
        self.state_patch = patch.object(app_reservations, "STATE_FILE", self.state_file)
        self.audit_patch = patch.object(app_reservations, "write_order_audit")
        self.state_patch.start()
        self.audit_patch.start()

    def tearDown(self):
        self.audit_patch.stop()
        self.state_patch.stop()
        self.tempdir.cleanup()

    def test_create_list_and_cancel_app_reservation(self):
        with patch.object(app_reservations, "get_current_mode", return_value="vps"):
            order = asyncio.run(app_reservations.create_app_reservation(
                market="domestic",
                stock_code="005930",
                stock_name="삼성전자",
                action="BUY",
                quantity=1,
                price=70000,
                order_type="limit",
                exchange=None,
                scheduled_at=iso_at(5),
                expires_at=iso_at(35),
            ))

        self.assertEqual(order["status"], "scheduled")
        listed = asyncio.run(app_reservations.list_app_reservations(market="domestic"))
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["reservation_order_no"], order["id"])

        cancelled = asyncio.run(app_reservations.cancel_app_reservation(reservation_id=order["id"]))
        self.assertEqual(cancelled["status"], "cancelled")

    def test_create_rejects_non_vps_and_unsupported_order_types(self):
        with patch.object(app_reservations, "get_current_mode", return_value="prod"):
            with self.assertRaisesRegex(ValueError, "vps"):
                asyncio.run(app_reservations.create_app_reservation(
                    market="domestic",
                    stock_code="005930",
                    stock_name="삼성전자",
                    action="BUY",
                    quantity=1,
                    price=70000,
                    order_type="limit",
                    exchange=None,
                    scheduled_at=iso_at(5),
                ))

        with patch.object(app_reservations, "get_current_mode", return_value="vps"):
            with self.assertRaisesRegex(ValueError, "미국 지정가"):
                asyncio.run(app_reservations.create_app_reservation(
                    market="us",
                    stock_code="AAPL",
                    stock_name="Apple",
                    action="SELL",
                    quantity=1,
                    price=0,
                    order_type="moo",
                    exchange="NASD",
                    scheduled_at=iso_at(5),
                ))

    def test_due_reservation_submits_and_marks_submitted(self):
        with patch.object(app_reservations, "get_current_mode", return_value="vps"):
            order = asyncio.run(app_reservations.create_app_reservation(
                market="domestic",
                stock_code="005930",
                stock_name="삼성전자",
                action="BUY",
                quantity=1,
                price=70000,
                order_type="limit",
                exchange=None,
                scheduled_at=iso_at(-1),
                expires_at=iso_at(30),
            ))

        with patch.object(app_reservations, "_submit_order_sync", return_value=(True, "000001", None)):
            result = asyncio.run(app_reservations.run_due_reservations())

        self.assertEqual(result["submitted"], 1)
        listed = asyncio.run(app_reservations.list_app_reservations(market="domestic"))
        self.assertEqual(listed[0]["id"], order["id"])
        self.assertEqual(listed[0]["status"], "submitted")
        self.assertEqual(listed[0]["submitted_order_no"], "000001")

    def test_retryable_failure_schedules_limited_retry(self):
        with patch.object(app_reservations, "get_current_mode", return_value="vps"):
            asyncio.run(app_reservations.create_app_reservation(
                market="domestic",
                stock_code="005930",
                stock_name="삼성전자",
                action="BUY",
                quantity=1,
                price=70000,
                order_type="limit",
                exchange=None,
                scheduled_at=iso_at(-1),
                expires_at=iso_at(30),
            ))

        with patch.object(app_reservations, "_submit_order_sync", return_value=(False, None, "EGW00201 초당 거래건수 초과")):
            result = asyncio.run(app_reservations.run_due_reservations())

        self.assertEqual(result["failed"], 1)
        listed = asyncio.run(app_reservations.list_app_reservations(market="domestic"))
        self.assertEqual(listed[0]["status"], "scheduled")
        self.assertEqual(listed[0]["attempt_count"], 1)
        self.assertTrue(listed[0]["next_retry_at"])

    def test_due_reservation_expires_without_submit(self):
        state = {
            "reservations": [{
                "id": "expired-1",
                "env_dv": "vps",
                "market": "domestic",
                "stock_code": "005930",
                "stock_name": "삼성전자",
                "action": "BUY",
                "quantity": 1,
                "price": 70000,
                "order_type": "limit",
                "scheduled_at": iso_at(-60),
                "expires_at": iso_at(-1),
                "status": "scheduled",
                "events": [],
            }],
        }
        app_reservations._save_state_sync(state)

        result = asyncio.run(app_reservations.run_due_reservations())

        self.assertEqual(result["expired"], 1)
        listed = asyncio.run(app_reservations.list_app_reservations(market="domestic"))
        self.assertEqual(listed[0]["status"], "expired")


if __name__ == "__main__":
    unittest.main()
