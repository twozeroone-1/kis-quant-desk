from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "strategy_builder"))

try:
    from backend.services import protective_orders
except ModuleNotFoundError as exc:
    protective_orders = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(protective_orders is None, f"strategy_builder dependencies unavailable: {IMPORT_ERROR}")
class ProtectiveLocalReservationTest(unittest.TestCase):
    def test_us_paper_kis_limit_error_creates_local_reservation(self):
        error = (
            "regular sell failed: 90000000 모의투자에서는 해당업무가 제공되지 않습니다.; "
            "reservation sell failed: 40490000 모의투자 예약주문시간을 확인해 주세요."
        )

        self.assertTrue(
            protective_orders._should_create_local_us_paper_reservation("vps", "us", error)
        )
        self.assertFalse(
            protective_orders._should_create_local_us_paper_reservation("prod", "us", error)
        )

    def test_triggered_exit_marks_local_reservation_instead_of_last_error(self):
        order = {
            "id": "order-1",
            "status": "active",
            "env_dv": "vps",
            "market": "us",
            "exchange": "NASD",
            "stock_code": "AMZN",
            "stock_name": "Amazon.com",
            "quantity": 1,
            "events": [],
        }
        error = (
            "regular sell failed: 90000000 모의투자에서는 해당업무가 제공되지 않습니다.; "
            "reservation sell failed: 40490000 모의투자 예약주문시간을 확인해 주세요."
        )

        with patch.object(
            protective_orders,
            "_submit_exit_order",
            return_value=(None, None, False, error),
        ):
            updated = protective_orders._submit_triggered_exit(
                order,
                "vps",
                reason="test stop",
                exit_reason="stop_loss",
                order_type="limit",
                price=170.0,
                current_price=169.5,
            )

        self.assertEqual(updated["status"], "active")
        self.assertNotIn("last_error", updated)
        self.assertEqual(updated["app_exit_reservation_status"], "waiting_retry")
        self.assertEqual(updated["app_exit_reservation"]["stock_code"], "AMZN")
        self.assertEqual(updated["app_exit_reservation"]["exit_reason"], "stop_loss")
        self.assertEqual(updated["app_exit_reservation"]["last_error"], error)
        self.assertEqual(updated["events"][-1]["type"], "stop_loss_app_reserved")

    def test_local_reservation_retries_even_after_price_recovers(self):
        order = {
            "id": "order-1",
            "status": "active",
            "env_dv": "vps",
            "market": "us",
            "exchange": "NASD",
            "stock_code": "AMZN",
            "stock_name": "Amazon.com",
            "quantity": 1,
            "stop_loss_enabled": True,
            "stop_loss_price": 160.0,
            "stop_loss_order_type": "limit",
            "events": [],
            "app_exit_reservation": {
                "status": "waiting_retry",
                "exit_reason": "stop_loss",
                "order_type": "limit",
                "limit_price": 160.0,
            },
        }

        with patch.object(
            protective_orders,
            "_submit_exit_order",
            return_value=("12345", "broker", True, None),
        ) as submit_mock:
            updated = protective_orders._check_realtime_trigger_sync(order, "vps", 180.0)

        submit_mock.assert_called_once()
        self.assertEqual(updated["status"], "exit_submitted")
        self.assertEqual(updated["exit_order_no"], "12345")
        self.assertNotIn("app_exit_reservation", updated)


if __name__ == "__main__":
    unittest.main()
