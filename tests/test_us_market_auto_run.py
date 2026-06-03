from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / ".codex" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import us_market_auto_run
except ModuleNotFoundError as exc:
    us_market_auto_run = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


class _FakeRow(dict):
    def to_dict(self):
        return dict(self)


class _FakeILoc:
    def __getitem__(self, index):
        return _FakeRow({"ODNO": "12345"})


class _FakeDataFrame:
    empty = False
    iloc = _FakeILoc()


class _FakeOrderResult:
    success = True
    dataframe = _FakeDataFrame()

    def display_error(self):
        return ""


class _FakeOverseasDataFetcher:
    def __init__(self):
        self.submitted = []

    def submit_order(self, **kwargs):
        self.submitted.append(kwargs)
        return _FakeOrderResult()


@unittest.skipIf(us_market_auto_run is None, f"us_market_auto_run unavailable: {IMPORT_ERROR}")
class UsMarketAutoRunTest(unittest.IsolatedAsyncioTestCase):
    def test_live_llm_mode_is_shadow_alias_not_order_gate(self):
        planned = [{"symbol": "AMZN", "quantity": 1, "notional": 100.0}]

        effective, warnings = us_market_auto_run.normalize_llm_mode("live-vps")
        executable = us_market_auto_run.apply_llm_decision(
            planned,
            {"status": "error", "decision": {"should_trade": False}},
            live_mode=True,
        )

        self.assertEqual(effective, "shadow")
        self.assertTrue(warnings)
        self.assertEqual(executable, planned)

    def test_strategy_sell_uses_marketable_limit(self):
        odf = _FakeOverseasDataFetcher()
        signals = [{
            "symbol": "AMZN",
            "exchange": "NASD",
            "action": "SELL",
            "strength": 0.65,
            "price": 100.0,
        }]
        holdings = [{"stock_code": "AMZN", "quantity": 2, "current_price": 100.0}]

        with patch.object(us_market_auto_run.time, "sleep", return_value=None):
            submitted = us_market_auto_run.place_sells(signals, holdings, odf)

        self.assertEqual(len(submitted), 1)
        self.assertEqual(odf.submitted[0]["price"], 98.0)
        self.assertEqual(submitted[0]["limit_price"], 98.0)
        self.assertEqual(submitted[0]["order_status"], "submitted")

    async def test_register_missing_protection_skips_existing_active_order(self):
        calls = []

        async def upsert(**kwargs):
            calls.append(kwargs)
            return {"id": f"protection-{kwargs['stock_code']}", "status": "active"}

        holdings = [
            {"stock_code": "AMZN", "stock_name": "Amazon", "quantity": 1, "avg_price": 100.0, "exchange": "NASD"},
            {"stock_code": "MSFT", "stock_name": "Microsoft", "quantity": 1, "avg_price": 100.0, "exchange": "NASD"},
            {"stock_code": "TSLA", "stock_name": "Tesla", "quantity": 1, "avg_price": 100.0, "exchange": "NASD"},
        ]
        protective = {
            "orders": [{
                "stock_code": "AMZN",
                "market": "us",
                "env_dv": "vps",
                "status": "active",
                "stop_loss_enabled": True,
            }]
        }

        with patch.object(us_market_auto_run.time, "sleep", return_value=None):
            protections = await us_market_auto_run.register_missing_protection_for_holdings(
                holdings,
                protective,
                upsert,
                eligible_symbols={"AMZN", "MSFT"},
            )

        self.assertEqual([call["stock_code"] for call in calls], ["MSFT"])
        self.assertEqual(calls[0]["take_profit_trigger_price"], 106.0)
        self.assertEqual(calls[0]["stop_loss_trigger_price"], 97.0)
        self.assertEqual(calls[0]["stop_loss_order_type"], "limit")
        self.assertEqual(protections[0]["stock_code"], "MSFT")
        self.assertEqual(protections[0]["status"], "success")


if __name__ == "__main__":
    unittest.main()
