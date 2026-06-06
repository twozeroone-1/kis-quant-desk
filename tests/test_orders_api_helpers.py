from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
