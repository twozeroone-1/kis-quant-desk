from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd
from starlette.requests import Request

PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("KIS_CONFIG_ROOT", str(PROJECT_ROOT / "tests" / "fixtures" / "kis_config"))
os.environ.setdefault("KIS_TOKEN_ROOT", "/tmp/open-trading-api-test-kis-tokens")
sys.path.insert(0, str(PROJECT_ROOT / "strategy_builder"))

try:
    from backend.routers import orders
except ModuleNotFoundError as exc:
    orders = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(orders is None, f"strategy_builder dependencies unavailable: {IMPORT_ERROR}")
class OrdersApiHelpersTest(unittest.TestCase):
    def test_compact_protective_order_limits_events_without_mutating_source(self):
        source_events = [{"type": f"event-{index}"} for index in range(25)]
        source = {
            "id": "order-1",
            "status": "active",
            "events": source_events,
        }

        compacted = orders._compact_protective_order_for_api(source)

        self.assertEqual(compacted["events_count"], 25)
        self.assertTrue(compacted["events_truncated"])
        self.assertEqual(len(compacted["events"]), orders.PROTECTIVE_API_EVENT_LIMIT)
        self.assertEqual(compacted["events"][0]["type"], "event-15")
        self.assertEqual(len(source["events"]), 25)

    def test_domestic_sell_holding_quantity_retries_transient_failure(self):
        failed = pd.DataFrame()
        holding = pd.DataFrame([{"stock_code": "005930", "quantity": 3}])
        errors = iter(["timeout", None])

        with patch.object(orders, "get_holdings", side_effect=[failed, holding]), patch.object(
            orders.data_fetcher,
            "get_balance_cache_error",
            side_effect=lambda env_dv: next(errors),
        ), patch.object(orders, "clear_balance_cache") as clear_mock:
            quantity, error = orders._domestic_sell_holding_quantity(
                "vps",
                "005930",
                retry_delay_seconds=0,
            )

        self.assertEqual(quantity, 3)
        self.assertIsNone(error)
        clear_mock.assert_called_once()

    def test_domestic_sell_holding_quantity_defers_repeated_failure(self):
        with patch.object(
            orders,
            "get_holdings",
            return_value=pd.DataFrame(),
        ), patch.object(
            orders.data_fetcher,
            "get_balance_cache_error",
            return_value="timeout",
        ), patch.object(orders, "clear_balance_cache"):
            quantity, error = orders._domestic_sell_holding_quantity(
                "vps",
                "005930",
                retry_delay_seconds=0,
            )

        self.assertIsNone(quantity)
        self.assertEqual(error, "timeout")

    def test_execute_order_returns_deferred_when_holdings_are_unavailable(self):
        order_request = orders.OrderRequest(
            stock_code="005930",
            stock_name="삼성전자",
            action="SELL",
            order_type="market",
            price=0,
            quantity=1,
            signal_reason="test",
        )
        http_request = Request({"type": "http", "headers": []})

        with patch.object(orders, "is_authenticated", return_value=True), patch.object(
            orders,
            "get_current_mode",
            return_value="vps",
        ), patch.object(
            orders,
            "_domestic_sell_holding_quantity",
            return_value=(None, "timeout"),
        ), patch.object(orders, "write_order_audit"):
            response = asyncio.run(orders.execute_order(order_request, http_request))

        self.assertEqual(response.status, "deferred")
        self.assertEqual(response.data["reason_code"], "holdings_unavailable")
        self.assertTrue(response.data["retryable"])

    def test_balance_cache_records_no_data_as_query_failure(self):
        orders.data_fetcher.clear_balance_cache()
        with patch.object(orders.data_fetcher, "_fetch_balance_raw", return_value=None):
            result = orders.data_fetcher._get_balance_cached("vps")

        self.assertIsNone(result)
        self.assertEqual(
            orders.data_fetcher.get_balance_cache_error("vps"),
            "KIS balance query returned no data",
        )


if __name__ == "__main__":
    unittest.main()
