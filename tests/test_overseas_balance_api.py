from __future__ import annotations

import asyncio
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("KIS_CONFIG_ROOT", str(PROJECT_ROOT / "tests" / "fixtures" / "kis_config"))
os.environ.setdefault("KIS_TOKEN_ROOT", "/tmp/open-trading-api-test-kis-tokens")
sys.path.insert(0, str(PROJECT_ROOT / "strategy_builder"))

try:
    from backend.routers import overseas
except ModuleNotFoundError as exc:
    overseas = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(overseas is None, f"strategy_builder dependencies unavailable: {IMPORT_ERROR}")
class OverseasBalanceApiTest(unittest.TestCase):
    def setUp(self) -> None:
        with overseas._orderable_balance_cache_lock:
            overseas._orderable_balance_cache.update({"env_dv": None, "timestamp": 0.0, "data": None})

    def test_balance_uses_psamount_orderable_when_available_cash_is_zero(self):
        deposit = {
            "deposit": 0.0,
            "total_eval": 3152.69,
            "purchase_amount": 3229.66,
            "eval_amount": 3152.69,
            "profit_loss": -76.97,
            "available_amount": 0.0,
            "currency": "USD",
        }

        with patch.object(overseas, "is_authenticated", return_value=True), patch.object(
            overseas, "get_current_mode", return_value="vps"
        ), patch.object(overseas.overseas_data_fetcher, "get_deposit", return_value=deposit), patch.object(
            overseas.overseas_data_fetcher,
            "get_buyable_amount",
            return_value={"amount": 103050.89, "quantity": 1020, "currency": "USD"},
        ) as buyable:
            response = asyncio.run(overseas.get_overseas_balance())

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["data"]["orderable_amount"], 103050.89)
        self.assertEqual(response["data"]["orderable_reference_symbol"], "NVDA")
        buyable.assert_called_once_with("NVDA", 100, "vps", "NASD")

    def test_balance_prefers_present_balance_available_amount_when_positive(self):
        deposit = {
            "deposit": 200.0,
            "total_eval": 1200.0,
            "purchase_amount": 900.0,
            "eval_amount": 1000.0,
            "profit_loss": 100.0,
            "available_amount": 180.0,
            "currency": "USD",
        }

        with patch.object(overseas, "is_authenticated", return_value=True), patch.object(
            overseas, "get_current_mode", return_value="vps"
        ), patch.object(overseas.overseas_data_fetcher, "get_deposit", return_value=deposit), patch.object(
            overseas.overseas_data_fetcher, "get_buyable_amount"
        ) as buyable:
            response = asyncio.run(overseas.get_overseas_balance())

        self.assertEqual(response["status"], "success")
        self.assertEqual(response["data"]["orderable_amount"], 180.0)
        buyable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
