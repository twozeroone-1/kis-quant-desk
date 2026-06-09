from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "strategy_builder"))

from core.signal import Action, Signal  # noqa: E402


class SignalModelTest(unittest.TestCase):
    def test_accepts_domestic_code_and_us_symbol(self):
        domestic = Signal("005930", "Samsung", Action.BUY, 0.7, "domestic")
        us = Signal("BRK.B", "Berkshire Hathaway", Action.HOLD, 0.0, "us")

        self.assertEqual(domestic.stock_code, "005930")
        self.assertEqual(us.stock_code, "BRK.B")

    def test_rejects_invalid_symbol_text(self):
        with self.assertRaisesRegex(ValueError, "valid US symbol"):
            Signal("bad symbol", "Invalid", Action.HOLD, 0.0, "invalid")


if __name__ == "__main__":
    unittest.main()
