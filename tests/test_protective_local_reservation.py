from __future__ import annotations

import asyncio
import copy
import os
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("KIS_CONFIG_ROOT", str(PROJECT_ROOT / "tests" / "fixtures" / "kis_config"))
os.environ.setdefault("KIS_TOKEN_ROOT", "/tmp/open-trading-api-test-kis-tokens")
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
    def test_legacy_broker_reservation_migrates_to_app_retry(self):
        order = {
            "status": "active",
            "env_dv": "vps",
            "market": "us",
            "app_exit_reservation_status": "broker_submitted",
            "app_exit_reservation": {
                "status": "broker_submitted",
                "reservation_order_no": "258",
            },
        }
        retry_at = datetime(2026, 6, 8, 22, 30)

        with patch.object(
            protective_orders,
            "_next_us_regular_session_retry_at",
            return_value=retry_at,
        ):
            normalized = protective_orders._normalize_runtime_order(order)

        self.assertEqual(normalized["app_exit_reservation_status"], "waiting_retry")
        self.assertEqual(normalized["app_exit_reservation"]["status"], "waiting_retry")
        self.assertNotIn("reservation_order_no", normalized["app_exit_reservation"])
        self.assertEqual(normalized["next_retry_at"], retry_at.isoformat(timespec="seconds"))

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
        ), patch.object(protective_orders, "_is_us_regular_session_now", return_value=True):
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
        ) as submit_mock, patch.object(protective_orders, "_is_us_regular_session_now", return_value=True):
            updated = protective_orders._check_realtime_trigger_sync(order, "vps", 180.0)

        submit_mock.assert_called_once()
        self.assertEqual(updated["status"], "exit_submitted")
        self.assertEqual(updated["exit_order_no"], "12345")
        self.assertEqual(updated["app_exit_reservation"]["status"], "submitted_unconfirmed")

    def test_us_paper_submit_stays_unconfirmed_until_holding_disappears(self):
        order = {
            "id": "order-1",
            "status": "active",
            "env_dv": "vps",
            "market": "us",
            "exchange": "NASD",
            "stock_code": "AMZN",
            "stock_name": "Amazon.com",
            "quantity": 1,
            "stop_loss_order_type": "limit",
            "events": [],
        }

        with patch.object(
            protective_orders,
            "_submit_exit_order",
            return_value=("258", "broker", True, None),
        ), patch.object(protective_orders, "_is_us_regular_session_now", return_value=True):
            updated = protective_orders._submit_triggered_exit(
                order,
                "vps",
                reason="test stop",
                exit_reason="stop_loss",
                order_type="limit",
                price=265.54,
                current_price=261.26,
            )

        self.assertEqual(updated["status"], "exit_submitted")
        self.assertEqual(updated["app_exit_reservation_status"], "submitted_unconfirmed")
        self.assertEqual(updated["app_exit_reservation"]["submitted_order_no"], "258")
        self.assertEqual(updated["events"][-1]["type"], "stop_loss_submitted")

    def test_us_stop_loss_uses_marketable_limit_below_current_price(self):
        price = protective_orders._us_stop_loss_order_price(265.54, 261.26)
        self.assertEqual(price, 256.03)

    def test_us_take_profit_uses_configured_marketable_offset(self):
        price = protective_orders._us_triggered_exit_order_price(
            115.0,
            112.0,
            "take_profit",
            {"us_take_profit_limit_offset_pct": 0.5},
        )

        self.assertEqual(price, 111.44)

    def test_us_exit_reprice_offset_expands_by_retry_count(self):
        price = protective_orders._us_triggered_exit_order_price(
            110.0,
            100.0,
            "stop_loss",
            {
                "us_stop_loss_limit_offset_pct": 2.0,
                "us_exit_reprice_step_pct": 0.75,
                "us_exit_max_offset_pct": 5.0,
            },
            reprice_count=2,
        )

        self.assertEqual(price, 96.5)

    def test_domestic_triggered_exit_uses_marketable_limit_and_tick(self):
        price = protective_orders._triggered_exit_order_price(
            "domestic",
            160800.0,
            160000.0,
            "stop_loss",
            {"domestic_stop_loss_limit_offset_pct": 2.0},
        )

        self.assertEqual(price, 156800.0)

    def test_domestic_take_profit_reprice_offset_expands_by_retry_count(self):
        price = protective_orders._triggered_exit_order_price(
            "domestic",
            174500.0,
            170000.0,
            "take_profit",
            {
                "domestic_take_profit_limit_offset_pct": 0.3,
                "domestic_exit_reprice_step_pct": 0.75,
                "domestic_exit_max_offset_pct": 5.0,
            },
            reprice_count=2,
        )

        self.assertEqual(price, 166900.0)

    def test_update_monitor_settings_saves_domestic_offsets(self):
        state = {"orders": [], "settings": {}, "health": {}}

        async def run_update():
            with patch.object(protective_orders, "_load_state", return_value=copy.deepcopy(state)), patch.object(
                protective_orders,
                "_save_state",
            ) as save_mock, patch.object(protective_orders, "_sync_realtime_subscriptions") as sync_mock:
                settings = await protective_orders.update_monitor_settings(
                    monitor_interval_seconds=15,
                    domestic_stop_loss_limit_offset_pct=1.5,
                    domestic_take_profit_limit_offset_pct=0.4,
                    domestic_exit_reprice_step_pct=0.5,
                    domestic_exit_max_offset_pct=3.0,
                )
                return settings, save_mock, sync_mock

        settings, save_mock, sync_mock = asyncio.run(run_update())

        self.assertEqual(settings["domestic_stop_loss_limit_offset_pct"], 1.5)
        self.assertEqual(settings["domestic_take_profit_limit_offset_pct"], 0.4)
        self.assertEqual(settings["domestic_exit_reprice_step_pct"], 0.5)
        self.assertEqual(settings["domestic_exit_max_offset_pct"], 3.0)
        save_mock.assert_awaited_once()
        sync_mock.assert_awaited_once()

    def test_monitor_snapshot_batches_holdings_and_pending_queries(self):
        orders = [
            {
                "env_dv": "vps",
                "market": "us",
                "exchange": "NASD",
                "stock_code": symbol,
            }
            for symbol in ("AMZN", "MSFT")
        ]

        with patch.object(
            protective_orders,
            "PROTECTIVE_KIS_CALL_INTERVAL_SECONDS",
            0,
        ), patch.object(
            protective_orders,
            "_get_holding_map",
            return_value={},
        ) as holdings_mock, patch.object(
            protective_orders,
            "_get_pending_order_state",
            return_value=({}, True),
        ) as pending_mock, patch.object(
            protective_orders.overseas_data_fetcher,
            "get_current_price",
            return_value={"price": 100.0},
        ) as price_mock, patch.object(
            protective_orders,
            "_is_order_market_open",
            return_value=True,
        ):
            snapshot = protective_orders._build_monitor_snapshot_sync(orders)

        self.assertEqual(holdings_mock.call_count, 1)
        self.assertEqual(pending_mock.call_count, 1)
        self.assertEqual(price_mock.call_count, 2)
        self.assertEqual(snapshot["api_calls"], 4)

    def test_monitor_snapshot_skips_price_queries_outside_regular_session(self):
        orders = [
            {
                "status": "active",
                "env_dv": "vps",
                "market": "us",
                "exchange": "NASD",
                "stock_code": "AMZN",
            }
        ]

        with patch.object(
            protective_orders,
            "PROTECTIVE_KIS_CALL_INTERVAL_SECONDS",
            0,
        ), patch.object(
            protective_orders,
            "_get_holding_map",
            return_value={},
        ), patch.object(
            protective_orders,
            "_get_pending_order_state",
            return_value=({}, True),
        ), patch.object(
            protective_orders,
            "_is_order_market_open",
            return_value=False,
        ), patch.object(
            protective_orders.overseas_data_fetcher,
            "get_current_price",
        ) as price_mock:
            snapshot = protective_orders._build_monitor_snapshot_sync(orders)

        price_mock.assert_not_called()
        self.assertEqual(snapshot["api_calls"], 2)

    def test_monitor_snapshot_reports_holdings_query_failure(self):
        orders = [
            {
                "status": "active",
                "env_dv": "vps",
                "market": "domestic",
                "stock_code": "005930",
            }
        ]

        with patch.object(
            protective_orders,
            "PROTECTIVE_KIS_CALL_INTERVAL_SECONDS",
            0,
        ), patch.object(
            protective_orders,
            "_get_holding_map",
            return_value=None,
        ), patch.object(
            protective_orders,
            "_get_pending_order_state",
            return_value=({}, True),
        ), patch.object(
            protective_orders,
            "_is_order_market_open",
            return_value=False,
        ):
            snapshot = protective_orders._build_monitor_snapshot_sync(orders)

        key = protective_orders._snapshot_key("vps", "domestic")
        self.assertIsNone(snapshot["holdings"][key])
        self.assertIn("holdings vps/domestic: query failed", snapshot["errors"])

    def test_us_paper_waiting_retry_is_due_after_cooldown(self):
        old = (datetime.now() - timedelta(seconds=61)).isoformat(timespec="seconds")
        waiting_retry = {
            "market": "us",
            "env_dv": "vps",
            "exit_submit_failed_at": old,
            "app_exit_reservation": {"status": "waiting_retry"},
        }

        with patch.object(protective_orders, "_is_us_regular_session_now", return_value=False):
            self.assertTrue(protective_orders._exit_submit_retry_due(waiting_retry))

    def test_us_paper_submit_does_not_fall_back_to_broker_reservation(self):
        result = type("Result", (), {
            "success": False,
            "dataframe": type("Frame", (), {"empty": True})(),
            "display_error": lambda self: "90000000 unsupported",
        })()

        with patch.object(protective_orders.overseas_data_fetcher, "submit_order", return_value=result):
            order_no, org_no, ok, error = protective_orders._submit_exit_order(
                env_dv="vps",
                stock_code="AMZN",
                stock_name="Amazon",
                quantity=1,
                reason="stop",
                order_type="limit",
                price=100.0,
                market="us",
                exchange="NASD",
            )

        self.assertFalse(ok)
        self.assertIsNone(order_no)
        self.assertIsNone(org_no)
        self.assertEqual(error, "90000000 unsupported")

    def test_us_paper_exit_waits_outside_regular_session_without_submitting(self):
        order = {
            "id": "order-1",
            "status": "active",
            "env_dv": "vps",
            "market": "us",
            "exchange": "NASD",
            "stock_code": "AMZN",
            "stock_name": "Amazon",
            "quantity": 1,
            "events": [],
        }

        with patch.object(
            protective_orders,
            "_is_us_regular_session_now",
            return_value=False,
        ), patch.object(protective_orders, "_submit_exit_order") as submit_mock:
            updated = protective_orders._submit_triggered_exit(
                order,
                "vps",
                reason="stop",
                exit_reason="stop_loss",
                order_type="limit",
                price=100.0,
                current_price=99.0,
            )

        submit_mock.assert_not_called()
        self.assertEqual(updated["app_exit_reservation_status"], "waiting_retry")
        self.assertTrue(updated["next_retry_at"])

    def test_us_regular_session_helper_respects_exchange_holiday(self):
        holiday = datetime(2026, 7, 3, 10, 0, tzinfo=ZoneInfo("America/New_York"))

        self.assertFalse(protective_orders._is_us_regular_session_now(holiday))
        self.assertEqual(
            protective_orders._next_us_regular_session_retry_at(holiday),
            datetime(2026, 7, 6, 13, 30),
        )

    def test_us_regular_session_retry_time_matches_runtime_utc_clock(self):
        pre_open_kst = datetime(2026, 6, 9, 22, 0, tzinfo=ZoneInfo("Asia/Seoul"))

        self.assertEqual(
            protective_orders._next_us_regular_session_retry_at(pre_open_kst),
            datetime(2026, 6, 9, 13, 30),
        )

    def test_legacy_kst_us_retry_time_is_due_on_runtime_utc_clock(self):
        order = {
            "status": "active",
            "env_dv": "vps",
            "market": "us",
            "next_retry_at": (datetime.now() + timedelta(hours=8)).isoformat(timespec="seconds"),
            "app_exit_reservation": {"status": "waiting_retry"},
        }

        self.assertTrue(protective_orders._exit_submit_retry_due(order))
        self.assertLess(datetime.fromisoformat(order["next_retry_at"]), datetime.now())

    def test_us_paper_error_policy_tracks_code_cooldown_and_unsupported_path(self):
        order = {
            "id": "order-1",
            "status": "active",
            "env_dv": "vps",
            "market": "us",
            "stock_code": "AMZN",
        }
        error = (
            "regular sell failed: 90000000 모의투자에서는 해당업무가 제공되지 않습니다.; "
            "reservation sell failed: 40490000 모의투자 예약주문시간을 확인해 주세요."
        )

        protective_orders._apply_us_paper_submit_error_policy(order, error)

        self.assertEqual(order["last_error_code"], "40490000")
        self.assertIn("us_paper_direct_sell", order["unsupported_paths"])
        self.assertEqual(order["retry_count"], 1)
        self.assertIsNotNone(order["next_retry_at"])
        self.assertGreater(datetime.fromisoformat(order["next_retry_at"]), datetime.now())

    def test_us_paper_rate_limit_uses_exponential_next_retry(self):
        order = {
            "id": "order-1",
            "status": "active",
            "env_dv": "vps",
            "market": "us",
            "stock_code": "AMZN",
        }

        protective_orders._apply_us_paper_submit_error_policy(order, "EGW00201 초당 거래건수 초과")
        first_retry = datetime.fromisoformat(order["next_retry_at"])
        protective_orders._apply_us_paper_submit_error_policy(order, "EGW00201 초당 거래건수 초과")
        second_retry = datetime.fromisoformat(order["next_retry_at"])

        self.assertEqual(order["last_error_code"], "EGW00201")
        self.assertEqual(order["retry_count"], 2)
        self.assertGreater(second_retry, first_retry)

    def test_exit_submitted_realtime_tick_does_not_submit_duplicate(self):
        order = {
            "id": "order-1",
            "status": "exit_submitted",
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
            "app_exit_reservation_status": "submitted_unconfirmed",
            "app_exit_reservation": {
                "status": "submitted_unconfirmed",
                "submitted_order_no": "258",
                "exit_reason": "stop_loss",
            },
        }

        with patch.object(protective_orders, "_submit_exit_order") as submit_mock:
            updated = protective_orders._check_realtime_trigger_sync(order, "vps", 150.0)

        submit_mock.assert_not_called()
        self.assertEqual(updated["status"], "exit_submitted")
        self.assertEqual(updated["app_exit_reservation_status"], "submitted_unconfirmed")

    def test_exit_submitted_retries_when_holding_still_present_without_pending(self):
        order = {
            "id": "order-1",
            "status": "exit_submitted",
            "env_dv": "prod",
            "market": "us",
            "exchange": "NASD",
            "stock_code": "GOOGL",
            "stock_name": "Alphabet A",
            "quantity": 1,
            "entry_price": 390.86,
            "stop_loss_order_type": "limit",
            "stop_loss_price": 379.13,
            "stop_loss_limit_price": 379.13,
            "exit_reason": "stop_loss",
            "exit_order_type": "limit",
            "exit_order_no": "254",
            "exit_org_no": "",
            "events": [],
        }

        with patch.object(
            protective_orders,
            "_get_holding_map",
            return_value={"GOOGL": {"quantity": 1, "current_price": 366.56}},
        ), patch.object(
            protective_orders,
            "_get_pending_order_state",
            return_value=({}, True),
        ), patch.object(
            protective_orders,
            "_current_order_price",
            side_effect=lambda target, env_dv, holding: float(holding["current_price"]),
        ), patch.object(
            protective_orders,
            "_submit_exit_order",
            return_value=("255", "broker", True, None),
        ), patch.object(
            protective_orders,
            "_is_order_market_open",
            return_value=True,
        ):
            updated = protective_orders._reconcile_exit_submitted_sync(order, "prod")

        self.assertEqual(updated["status"], "exit_submitted")
        self.assertEqual(updated["exit_order_no"], "255")
        self.assertEqual(updated["events"][-2]["type"], "exit_retry_position_still_held")
        self.assertEqual(updated["events"][-1]["type"], "stop_loss_submitted")

    def test_exit_reconciliation_does_not_reprice_outside_regular_session(self):
        order = {
            "id": "order-1",
            "status": "exit_submitted",
            "env_dv": "vps",
            "market": "us",
            "exchange": "NASD",
            "stock_code": "AMZN",
            "quantity": 1,
            "exit_reason": "stop_loss",
            "exit_order_no": "258",
            "events": [],
        }

        with patch.object(
            protective_orders,
            "_get_holding_map",
            return_value={"AMZN": {"quantity": 1, "current_price": 95.0}},
        ), patch.object(
            protective_orders,
            "_get_pending_order_state",
            return_value=({}, True),
        ), patch.object(
            protective_orders,
            "_is_order_market_open",
            return_value=False,
        ), patch.object(
            protective_orders,
            "_current_order_price",
        ) as price_mock, patch.object(
            protective_orders,
            "_submit_exit_order",
        ) as submit_mock:
            updated = protective_orders._reconcile_exit_submitted_sync(order, "vps")

        price_mock.assert_not_called()
        submit_mock.assert_not_called()
        self.assertIn("waiting for regular market session", updated["last_error"])

    def test_exit_reconciliation_defers_when_pending_query_fails(self):
        order = {
            "id": "order-1",
            "status": "exit_submitted",
            "env_dv": "vps",
            "market": "us",
            "exchange": "NASD",
            "stock_code": "AMZN",
            "stock_name": "Amazon",
            "quantity": 1,
            "exit_reason": "stop_loss",
            "exit_order_type": "limit",
            "exit_order_no": "258",
            "events": [],
        }

        with patch.object(
            protective_orders,
            "_get_holding_map",
            return_value={"AMZN": {"quantity": 1, "current_price": 100.0}},
        ), patch.object(
            protective_orders,
            "_get_pending_order_state",
            return_value=({}, False),
        ), patch.object(protective_orders, "_submit_exit_order") as submit_mock:
            updated = protective_orders._reconcile_exit_submitted_sync(order, "vps")

        submit_mock.assert_not_called()
        self.assertIn("pending order query failed", updated["last_error"])

    def test_partial_fill_reprice_cancels_only_unfilled_quantity(self):
        order = {
            "id": "order-1",
            "status": "exit_submitted",
            "env_dv": "vps",
            "market": "us",
            "exchange": "NASD",
            "stock_code": "AMZN",
            "stock_name": "Amazon",
            "quantity": 5,
            "exit_reason": "stop_loss",
            "exit_order_type": "limit",
            "exit_order_no": "258",
            "exit_submitted_at": (datetime.now() - timedelta(minutes=5)).isoformat(timespec="seconds"),
            "stop_loss_order_type": "limit",
            "stop_loss_limit_price": 100.0,
            "events": [],
        }
        pending = {
            "258": {
                "order_no": "258",
                "order_qty": 5,
                "filled_qty": 3,
                "unfilled_qty": 2,
            }
        }

        with patch.object(
            protective_orders,
            "_get_holding_map",
            return_value={"AMZN": {"quantity": 3, "current_price": 95.0}},
        ), patch.object(
            protective_orders,
            "_get_pending_order_state",
            return_value=(pending, True),
        ), patch.object(
            protective_orders,
            "_current_order_price",
            return_value=95.0,
        ), patch.object(
            protective_orders,
            "_settings_sync",
            return_value={"exit_reprice_interval_seconds": 5},
        ), patch.object(
            protective_orders.overseas_data_fetcher,
            "cancel_order",
            return_value={"success": True},
        ) as cancel_mock, patch.object(
            protective_orders,
            "_is_us_regular_session_now",
            return_value=True,
        ), patch.object(
            protective_orders,
            "_submit_exit_order",
            return_value=("259", "broker", True, None),
        ):
            updated = protective_orders._reconcile_exit_submitted_sync(order, "vps")

        self.assertEqual(cancel_mock.call_args.kwargs["qty"], 2)
        self.assertEqual(updated["quantity"], 3)
        self.assertEqual(updated["exit_reprice_count"], 1)
        self.assertEqual(updated["exit_order_no"], "259")

    def test_exit_submitted_closes_after_empty_holdings_confirmations(self):
        order = {
            "id": "order-1",
            "status": "exit_submitted",
            "env_dv": "prod",
            "market": "domestic",
            "stock_code": "005930",
            "stock_name": "삼성전자",
            "quantity": 1,
            "exit_reason": "stop_loss",
            "exit_order_type": "market",
            "exit_order_no": "123",
            "app_exit_reservation_status": "submitted_unconfirmed",
            "app_exit_reservation": {
                "status": "submitted_unconfirmed",
                "submitted_order_no": "123",
            },
            "events": [{"type": "stop_loss_submitted", "at": "2026-06-01T09:00:00"}],
        }

        with patch.object(protective_orders, "_get_holding_map", return_value={}):
            first = copy.deepcopy(protective_orders._reconcile_exit_submitted_sync(order, "prod"))
            second = copy.deepcopy(protective_orders._reconcile_exit_submitted_sync(order, "prod"))
            third = copy.deepcopy(protective_orders._reconcile_exit_submitted_sync(order, "prod"))

        self.assertEqual(first["status"], "exit_submitted")
        self.assertEqual(second["status"], "exit_submitted")
        self.assertEqual(third["status"], "closed")
        self.assertEqual(third["events"][-1]["type"], "position_closed_after_exit_submit")
        self.assertEqual(third["app_exit_reservation_status"], "filled")

    def test_active_order_closes_after_confirmed_empty_holdings(self):
        order = {
            "id": "order-1",
            "status": "active",
            "env_dv": "vps",
            "market": "domestic",
            "stock_code": "000270",
            "stock_name": "기아",
            "quantity": 1,
            "take_profit_enabled": True,
            "take_profit_submit_mode": "on_trigger",
            "stop_loss_enabled": True,
            "stop_loss_price": 155000.0,
            "events": [],
        }
        snapshot = {
            "holdings": {
                protective_orders._snapshot_key("vps", "domestic"): {},
            },
            "pending": {
                protective_orders._snapshot_key("vps", "domestic", None): {"orders": {}, "ok": True},
            },
            "prices": {},
        }

        first = copy.deepcopy(protective_orders._check_order_sync(order, "vps", snapshot))
        second = copy.deepcopy(protective_orders._check_order_sync(copy.deepcopy(first), "vps", snapshot))
        third = copy.deepcopy(protective_orders._check_order_sync(copy.deepcopy(second), "vps", snapshot))

        self.assertEqual(first["status"], "active")
        self.assertEqual(second["status"], "active")
        self.assertEqual(third["status"], "closed")
        self.assertEqual(third["events"][-1]["type"], "position_closed")

    def test_active_order_preserves_state_when_holdings_query_failed(self):
        order = {
            "id": "order-1",
            "status": "active",
            "env_dv": "vps",
            "market": "domestic",
            "stock_code": "000270",
            "stock_name": "기아",
            "quantity": 1,
            "take_profit_enabled": True,
            "take_profit_submit_mode": "on_trigger",
            "stop_loss_enabled": True,
            "stop_loss_price": 155000.0,
            "events": [],
        }
        snapshot = {
            "holdings": {
                protective_orders._snapshot_key("vps", "domestic"): None,
            },
            "pending": {
                protective_orders._snapshot_key("vps", "domestic", None): {"orders": {}, "ok": True},
            },
            "prices": {},
        }

        updated = protective_orders._check_order_sync(order, "vps", snapshot)

        self.assertEqual(updated["status"], "active")
        self.assertNotIn("position_missing_count", updated)
        self.assertIn("holdings query failed", updated["last_error"])

    def test_submitted_exit_preserves_state_when_holdings_query_failed(self):
        order = {
            "id": "order-1",
            "status": "exit_submitted",
            "env_dv": "vps",
            "market": "domestic",
            "stock_code": "005930",
            "quantity": 1,
            "position_missing_count": 1,
            "events": [],
        }
        snapshot = {
            "holdings": {
                protective_orders._snapshot_key("vps", "domestic"): None,
            },
        }

        updated = protective_orders._reconcile_exit_submitted_sync(order, "vps", snapshot)

        self.assertEqual(updated["status"], "exit_submitted")
        self.assertEqual(updated["position_missing_count"], 1)
        self.assertIn("reconciliation deferred", updated["last_error"])

    def test_domestic_holding_map_returns_none_when_balance_query_failed(self):
        with patch.object(
            protective_orders,
            "get_holdings",
            return_value=protective_orders.pd.DataFrame(),
        ), patch.object(
            protective_orders.data_fetcher,
            "get_balance_cache_error",
            return_value="timeout",
        ):
            holdings = protective_orders._get_holding_map("vps", "domestic")

        self.assertIsNone(holdings)

    def test_closed_market_position_reconciliation_closes_missing_holding(self):
        order = {
            "id": "order-1",
            "status": "active",
            "env_dv": "vps",
            "market": "domestic",
            "stock_code": "000270",
            "stock_name": "기아",
            "quantity": 1,
            "position_missing_count": 2,
            "last_error": "position not found in holdings (2/3)",
            "events": [],
        }
        snapshot = {
            "holdings": {
                protective_orders._snapshot_key("vps", "domestic"): {},
            },
        }

        updated = protective_orders._reconcile_active_position_only_sync(order, "vps", snapshot)

        self.assertEqual(updated["status"], "closed")
        self.assertEqual(updated["events"][-1]["type"], "position_closed")

    def test_domestic_empty_sell_result_preserves_error_detail(self):
        class EmptyOrderExecutor:
            def __init__(self, env_dv):
                self.env_dv = env_dv

            def execute_signal(self, signal):
                return protective_orders.pd.DataFrame()

        with patch.object(protective_orders, "OrderExecutor", EmptyOrderExecutor):
            order_no, org_no, ok, error = protective_orders._submit_exit_order(
                env_dv="vps",
                stock_code="000270",
                stock_name="기아",
                quantity=1,
                reason="test",
                order_type="market",
                market="domestic",
            )

        self.assertIsNone(order_no)
        self.assertIsNone(org_no)
        self.assertFalse(ok)
        self.assertEqual(error, "domestic sell returned empty order result")

    def test_legacy_submit_failed_events_get_error_detail(self):
        order = {
            "status": "closed",
            "market": "domestic",
            "env_dv": "vps",
            "last_error": "position not found in holdings (3/3)",
            "events": [
                {"type": "created", "at": "2026-06-10T00:00:00"},
                {"type": "stop_loss_submit_failed", "at": "2026-06-10T00:01:00", "error": None},
            ],
        }

        protective_orders._normalize_runtime_order(order)

        self.assertEqual(
            order["events"][-1]["error"],
            protective_orders.LEGACY_SUBMIT_FAILURE_ERROR,
        )

    def test_triggered_exit_without_error_detail_records_fallback(self):
        order = {
            "id": "order-1",
            "status": "active",
            "env_dv": "vps",
            "market": "domestic",
            "stock_code": "000270",
            "stock_name": "기아",
            "quantity": 1,
            "events": [],
        }

        with patch.object(protective_orders, "_submit_exit_order", return_value=(None, None, False, None)):
            updated = protective_orders._submit_triggered_exit(
                order,
                "vps",
                reason="test",
                exit_reason="stop_loss",
                order_type="market",
                price=None,
                current_price=1000.0,
            )

        self.assertIn(protective_orders.MISSING_SUBMIT_FAILURE_ERROR, updated["last_error"])
        self.assertEqual(
            updated["events"][-1]["error"],
            protective_orders.MISSING_SUBMIT_FAILURE_ERROR,
        )

    def test_protective_app_reservation_is_visible_in_reservation_list(self):
        state = {
            "orders": [{
                "id": "order-1",
                "status": "exit_submitted",
                "env_dv": "vps",
                "market": "us",
                "exchange": "NASD",
                "stock_code": "AMZN",
                "stock_name": "Amazon",
                "quantity": 1,
                "app_exit_reservation": {
                    "status": "submitted_unconfirmed",
                    "exit_reason": "stop_loss",
                    "order_type": "limit",
                    "limit_price": 98.0,
                    "reserved_at": datetime.now().isoformat(timespec="seconds"),
                    "submitted_order_no": "258",
                },
            }],
            "settings": {},
        }

        with patch.object(protective_orders, "_load_state", return_value=state):
            rows = asyncio.run(protective_orders.list_protective_app_reservations())

        self.assertEqual(rows[0]["reservation_kind"], "protective_exit")
        self.assertEqual(rows[0]["status"], "submitted_unconfirmed")
        self.assertFalse(rows[0]["cancellable"])

    def test_closed_protective_app_reservation_can_be_hidden_from_current_list(self):
        state = {
            "orders": [{
                "id": "order-closed",
                "status": "closed",
                "env_dv": "vps",
                "market": "us",
                "exchange": "NASD",
                "stock_code": "AMZN",
                "stock_name": "Amazon",
                "quantity": 1,
                "app_exit_reservation": {
                    "status": "closed",
                    "exit_reason": "stop_loss",
                    "order_type": "limit",
                    "limit_price": 98.0,
                    "reserved_at": datetime.now().isoformat(timespec="seconds"),
                    "last_error": "reservation sell failed: 40490000",
                },
            }],
            "settings": {},
        }

        with patch.object(protective_orders, "_load_state", return_value=state):
            visible = asyncio.run(protective_orders.list_protective_app_reservations())
            hidden = asyncio.run(protective_orders.list_protective_app_reservations(include_closed=False))

        self.assertEqual(visible[0]["stock_code"], "AMZN")
        self.assertEqual(visible[0]["status"], "closed")
        self.assertEqual(hidden, [])

    def test_monitor_health_does_not_alert_for_closed_market_retry(self):
        order = {
            "status": "active",
            "env_dv": "vps",
            "market": "us",
            "stock_code": "AMZN",
            "app_exit_reserved_at": (datetime.now() - timedelta(hours=2)).isoformat(),
            "next_retry_at": (datetime.now() + timedelta(days=2)).isoformat(),
            "app_exit_reservation": {"status": "waiting_retry"},
        }

        with patch.object(protective_orders, "_is_order_market_open", return_value=False):
            health = protective_orders._monitor_health(
                orders=[order],
                snapshot={"api_calls": 0, "errors": []},
                started_at=datetime.now(),
                processing_errors=[],
            )

        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["overdue_exit_count"], 0)

    def test_monitor_health_gives_future_retry_a_grace_period(self):
        order = {
            "status": "active",
            "env_dv": "vps",
            "market": "us",
            "stock_code": "AMZN",
            "app_exit_reserved_at": (datetime.now() - timedelta(hours=2)).isoformat(),
            "next_retry_at": (datetime.now() + timedelta(minutes=5)).isoformat(),
            "app_exit_reservation": {"status": "waiting_retry"},
        }

        with patch.object(protective_orders, "_is_order_market_open", return_value=True):
            health = protective_orders._monitor_health(
                orders=[order],
                snapshot={"api_calls": 0, "errors": []},
                started_at=datetime.now(),
                processing_errors=[],
            )

        self.assertEqual(health["status"], "healthy")
        self.assertEqual(health["overdue_exit_count"], 0)


if __name__ == "__main__":
    unittest.main()
