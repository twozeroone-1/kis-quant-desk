from __future__ import annotations

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = PROJECT_ROOT / ".codex" / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import organic_strategy_router as router  # noqa: E402


class OrganicStrategyRouterTest(unittest.TestCase):
    def test_selects_target_sized_kr_strategy_pool(self):
        orchestration = router.select_strategy_candidates(
            market="kr",
            regime="semiconductor_momentum",
            risk_gate_open=True,
        )

        self.assertEqual(orchestration["enabled_count"], 12)
        self.assertGreaterEqual(orchestration["enabled_count"], 8)
        self.assertLessEqual(orchestration["enabled_count"], 12)
        self.assertEqual(orchestration["enabled"][0]["id"], "custom:today_krx_macro_rebound")
        self.assertTrue(any(item["primary_regime_match"] for item in orchestration["enabled"]))

    def test_selects_compact_us_confirmation_pool(self):
        orchestration = router.select_strategy_candidates(
            market="us",
            regime="broad_momentum",
            risk_gate_open=True,
        )

        self.assertGreaterEqual(orchestration["enabled_count"], 3)
        self.assertLessEqual(orchestration["enabled_count"], 5)
        self.assertEqual(orchestration["target_strategy_count"], {"min": 3, "max": 5})
        self.assertIn(
            "today_us_news_trend_filter",
            {item["id"] for item in orchestration["enabled"]},
        )

    def test_risk_closed_keeps_diagnostic_strategies_and_warns(self):
        orchestration = router.select_strategy_candidates(
            market="kr",
            regime="risk_control",
            risk_gate_open=False,
        )

        self.assertEqual(orchestration["enabled_count"], 12)
        self.assertTrue(orchestration["warnings"])
        self.assertIn("diagnostic", orchestration["warnings"][0])

    def test_execute_pool_merges_strategy_votes(self):
        orchestration = {
            "enabled": [
                {"id": "trend_filter", "weight": 1.0},
                {"id": "mean_reversion", "weight": 0.8},
            ],
        }

        def fake_api(method, path, **kwargs):
            strategy_id = kwargs["json"]["strategy_id"]
            rows = {
                "trend_filter": [
                    {
                        "code": "005930",
                        "name": "Samsung",
                        "action": "BUY",
                        "strength": 0.8,
                        "target_price": 70000,
                        "reason": "trend",
                    }
                ],
                "mean_reversion": [
                    {
                        "code": "005930",
                        "name": "Samsung",
                        "action": "HOLD",
                        "strength": 0.4,
                        "target_price": 70000,
                        "reason": "not oversold",
                    }
                ],
            }
            return {"status": "success", "results": rows[strategy_id], "logs": []}

        result = router.execute_strategy_pool(fake_api, ["005930"], orchestration, market="domestic")

        self.assertEqual(result["successful_strategy_count"], 2)
        self.assertEqual(result["failed_strategy_count"], 0)
        self.assertEqual(result["merged_signals"][0]["action"], "BUY")
        self.assertGreaterEqual(result["merged_signals"][0]["strength"], 0.7)
        self.assertEqual(len(result["merged_signals"][0]["strategy_votes"]), 2)

    def test_us_anchor_merge_confirms_without_promoting_anchor_hold(self):
        anchor = [
            {"symbol": "AAPL", "action": "BUY", "strength": 0.72, "price": 100.0, "reason": "anchor buy"},
            {"symbol": "MSFT", "action": "HOLD", "strength": 0.2, "price": 200.0, "reason": "anchor hold"},
            {"symbol": "NVDA", "action": "SELL", "strength": 0.8, "price": 150.0, "reason": "anchor sell"},
        ]
        organic = [
            {"code": "AAPL", "action": "BUY", "strength": 0.9, "strategy_votes": [{"strategy_id": "momentum"}]},
            {"code": "MSFT", "action": "BUY", "strength": 0.95},
            {"code": "NVDA", "action": "BUY", "strength": 0.95},
        ]

        merged = router.merge_us_anchor_signals(anchor, organic)
        by_symbol = {row["symbol"]: row for row in merged}

        self.assertEqual(by_symbol["AAPL"]["action"], "BUY")
        self.assertGreater(by_symbol["AAPL"]["strength"], 0.72)
        self.assertEqual(by_symbol["MSFT"]["action"], "HOLD")
        self.assertLess(by_symbol["MSFT"]["strength"], 0.5)
        self.assertEqual(by_symbol["NVDA"]["action"], "SELL")

    def test_order_decisions_explain_risk_gate_blocks(self):
        decisions = router.explain_order_decisions(
            [{"code": "005930", "name": "Samsung", "action": "BUY", "strength": 0.9, "target_price": 70000}],
            [],
            min_buy_strength=0.7,
            risk_gate_open=False,
            risk_reasons=["risk_control news regime blocks new buys"],
            order_execution_enabled=True,
            order_block_reasons=[],
        )

        self.assertEqual(decisions[0]["status"], "blocked")
        self.assertIn("risk_control", decisions[0]["reasons"][0])

    def test_order_decisions_accept_us_symbol_and_price(self):
        decisions = router.explain_order_decisions(
            [{"symbol": "AAPL", "name": "Apple", "action": "BUY", "strength": 0.8, "price": 190.0}],
            [{"symbol": "AAPL", "quantity": 1}],
            min_buy_strength=0.7,
            risk_gate_open=True,
            risk_reasons=[],
            order_execution_enabled=True,
            order_block_reasons=[],
        )

        self.assertEqual(decisions[0]["status"], "planned")


if __name__ == "__main__":
    unittest.main()
