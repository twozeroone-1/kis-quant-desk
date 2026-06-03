from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / ".codex" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

try:
    import kr_market_auto_run
except ModuleNotFoundError as exc:
    kr_market_auto_run = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


@unittest.skipIf(kr_market_auto_run is None, f"kr_market_auto_run unavailable: {IMPORT_ERROR}")
class KrMarketAutoRunTest(unittest.TestCase):
    def test_llm_modes_never_gate_deterministic_orders(self):
        planned = [{"code": "005930", "quantity": 3, "amount": 210000}]

        for mode in ("off", "shadow", "live-vps", "live-prod"):
            with self.subTest(mode=mode):
                executable = kr_market_auto_run.apply_llm_decision(
                    planned,
                    {"status": "error", "decision": {"should_trade": False}},
                    mode,
                )
                self.assertEqual(executable, planned)

    def test_live_llm_modes_normalize_to_shadow_with_warning(self):
        effective, warnings = kr_market_auto_run.normalize_llm_mode("live-prod")

        self.assertEqual(effective, "shadow")
        self.assertTrue(warnings)

    def test_prod_order_gate_uses_only_prod_confirmation(self):
        self.assertFalse(kr_market_auto_run.order_execution_enabled("prod", False))
        self.assertTrue(kr_market_auto_run.order_execution_enabled("prod", True))
        self.assertTrue(kr_market_auto_run.prod_llm_orders_enabled("prod", "off"))


if __name__ == "__main__":
    unittest.main()
