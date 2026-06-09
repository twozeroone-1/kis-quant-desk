from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pandas as pd

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


class _FakeEquityFetcher:
    def get_deposit(self, env_dv):
        return {
            "deposit": 0,
            "total_eval": 0,
            "available_amount": 10_000,
        }

    def get_holdings(self, env_dv):
        class EmptyFrame:
            empty = True

        return EmptyFrame()

    def get_buyable_amount(self, *args, **kwargs):
        return {"amount": 10_000}

    def get_pending_orders(self, *args, **kwargs):
        return pd.DataFrame(), True


class _FakeAnomalousEquityFetcher(_FakeEquityFetcher):
    def get_deposit(self, env_dv):
        return {
            "deposit": 11_708_281,
            "total_eval": 159_411_600,
            "available_amount": 159_411_600,
        }


class _FakeSignalFetcher:
    def __init__(self, price):
        self.price = price

    def get_daily_prices(self, *args, **kwargs):
        return pd.DataFrame({"close": [100.0] * 60})

    def get_current_price(self, *args, **kwargs):
        return {"price": self.price}


class _FakeIndicators:
    roc = 10.0

    @staticmethod
    def calc_ema(df, period):
        return pd.Series([110.0 if period == 20 else 100.0])

    @classmethod
    def calc_roc(cls, df, period):
        return pd.Series([cls.roc])

    @staticmethod
    def calc_rsi(df, period):
        return pd.Series([60.0])


@unittest.skipIf(us_market_auto_run is None, f"us_market_auto_run unavailable: {IMPORT_ERROR}")
class UsMarketAutoRunTest(unittest.IsolatedAsyncioTestCase):
    def test_strategy_api_base_prefers_vps_endpoint(self):
        with patch.dict(
            "os.environ",
            {
                "KIS_STRATEGY_API": "http://127.0.0.1:8083",
                "KIS_VPS_STRATEGY_API": "http://127.0.0.1:8081/",
            },
            clear=False,
        ):
            self.assertEqual(us_market_auto_run.strategy_api_base(), "http://127.0.0.1:8081")

    def test_collect_payload_errors_includes_strategy_run_errors(self):
        payload = {
            "strategy_run": {"errors": ["momentum: rate limited"]},
            "signals": [],
            "submitted_sells": [],
            "orders": [],
            "account_after": {},
        }

        self.assertIn("strategy_run: momentum: rate limited", us_market_auto_run.collect_payload_errors(payload))

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

    def test_strategy_sell_skips_existing_pending_sell(self):
        odf = _FakeOverseasDataFetcher()
        signals = [{
            "symbol": "AMZN",
            "exchange": "NASD",
            "action": "SELL",
            "strength": 0.65,
            "price": 100.0,
        }]
        holdings = [{"stock_code": "AMZN", "quantity": 2, "current_price": 100.0}]

        submitted = us_market_auto_run.place_sells(
            signals,
            holdings,
            odf,
            pending_sell_symbols={"AMZN"},
        )

        self.assertFalse(odf.submitted)
        self.assertEqual(submitted[0]["order_status"], "skipped_pending_sell")

    def test_build_orders_excludes_held_pending_and_submitted_symbols(self):
        signals = [
            {"symbol": "AMZN", "action": "BUY", "strength": 0.9, "price": 100.0},
            {"symbol": "MSFT", "action": "BUY", "strength": 0.8, "price": 100.0},
        ]

        orders = us_market_auto_run.build_orders(
            signals,
            equity=10_000,
            cash=10_000,
            state={"orders": []},
            excluded_symbols={"AMZN"},
        )

        self.assertEqual([order["symbol"] for order in orders], ["MSFT"])

    def test_build_orders_blocks_new_buys_in_risk_control(self):
        signals = [{"symbol": "MSFT", "action": "BUY", "strength": 0.9, "price": 100.0}]

        orders = us_market_auto_run.build_orders(
            signals,
            equity=10_000,
            cash=10_000,
            state={"orders": []},
            market_regime="risk_control",
        )

        self.assertEqual(orders, [])

    def test_build_orders_caps_symbols_and_notional(self):
        signals = [
            {"symbol": symbol, "action": "BUY", "strength": strength, "price": 10.0}
            for symbol, strength in (("AAPL", 0.95), ("MSFT", 0.9), ("NVDA", 0.85))
        ]

        orders = us_market_auto_run.build_orders(
            signals,
            equity=100_000,
            cash=100_000,
            state={"orders": []},
        )

        self.assertEqual(len(orders), 2)
        self.assertTrue(all(order["notional"] <= 1_000 for order in orders))
        self.assertAlmostEqual(sum(order["weight"] for order in orders), 1.0, places=3)

    def test_build_orders_blocks_sector_when_existing_exposure_is_at_cap(self):
        signals = [{
            "symbol": "NVDA",
            "sector": "technology",
            "action": "BUY",
            "strength": 0.9,
            "price": 100.0,
        }]
        holdings = [{
            "stock_code": "MSFT",
            "quantity": 25,
            "current_price": 100.0,
        }]

        orders = us_market_auto_run.build_orders(
            signals,
            equity=10_000,
            cash=10_000,
            state={"orders": []},
            holdings=holdings,
        )

        self.assertEqual(orders, [])

    def test_market_risk_blocks_broad_benchmark_selloff(self):
        signals = [
            {"symbol": "SPY", "action": "SELL", "intraday_change_pct": -2.5},
            {"symbol": "QQQ", "action": "SELL", "intraday_change_pct": -3.2},
            {"symbol": "NVDA", "action": "HOLD", "intraday_change_pct": -4.0},
            {"symbol": "JPM", "action": "HOLD", "intraday_change_pct": -2.1},
        ]

        risk = us_market_auto_run.evaluate_market_risk(
            signals,
            {"regime": "broad_momentum"},
        )

        self.assertFalse(risk["risk_gate_open"])
        self.assertEqual(risk["regime"], "risk_control")
        self.assertTrue(risk["reasons"])

    def test_market_risk_does_not_block_on_unconfirmed_headlines_alone(self):
        signals = [
            {"symbol": "SPY", "action": "HOLD", "intraday_change_pct": 0.2},
            {"symbol": "QQQ", "action": "HOLD", "intraday_change_pct": 0.1},
            {"symbol": "NVDA", "action": "BUY", "intraday_change_pct": 0.4},
            {"symbol": "JPM", "action": "HOLD", "intraday_change_pct": -0.1},
        ]

        risk = us_market_auto_run.evaluate_market_risk(
            signals,
            {"regime": "risk_control"},
        )

        self.assertTrue(risk["risk_gate_open"])
        self.assertEqual(risk["regime"], "headline_caution")
        self.assertTrue(risk["warnings"])

    def test_candidate_quality_rejects_volume_surge_only_name(self):
        selection = {
            "selected": [
                {"symbol": "HOT", "sources": ["volume_surge_rank"], "category": "large_cap"},
                {"symbol": "NVDA", "sources": ["trade_value_rank"], "category": "large_cap"},
                {"symbol": "SPY", "sources": ["core_etf"], "category": "core_etf"},
            ]
        }

        filtered = us_market_auto_run.apply_candidate_quality_gate(selection)

        self.assertEqual(
            [item["symbol"] for item in filtered["selected"]],
            ["NVDA", "SPY"],
        )
        self.assertEqual(filtered["rejected"][0]["symbol"], "HOT")

    def test_last_completed_close_skips_current_session_daily_bar(self):
        market_date = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d")
        df = pd.DataFrame({
            "date": ["20260604", market_date],
            "close": [100.0, 90.0],
        })

        close = us_market_auto_run._last_completed_close(df, {"price": 89.0})

        self.assertEqual(close, 100.0)

    async def test_account_status_marks_broker_reservations_not_applicable_in_vps(self):
        async def protective():
            return {"orders": [], "settings": {}, "health": {"status": "healthy"}}

        async def app_reservations(**kwargs):
            return []

        with patch.object(us_market_auto_run.time, "sleep", return_value=None):
            status = await us_market_auto_run.account_status(
                _FakeEquityFetcher(),
                protective,
                app_reservations,
            )

        self.assertEqual(status["reservations"]["status"], "not_applicable")
        self.assertEqual(status["reservations"]["policy"], "vps_app_reservations_only")

    async def test_account_status_refreshes_stale_protective_monitor_once(self):
        calls = {"protective": 0, "refresh": 0}

        async def protective():
            calls["protective"] += 1
            if calls["protective"] == 1:
                return {"orders": [], "settings": {}, "health": {"status": "stale", "stale": True}}
            return {"orders": [], "settings": {}, "health": {"status": "healthy", "stale": False}}

        async def app_reservations(**kwargs):
            return []

        async def refresh():
            calls["refresh"] += 1

        with patch.object(us_market_auto_run.time, "sleep", return_value=None):
            status = await us_market_auto_run.account_status(
                _FakeEquityFetcher(),
                protective,
                app_reservations,
                refresh,
            )

        self.assertEqual(calls, {"protective": 2, "refresh": 1})
        self.assertEqual(status["protective"]["health"]["status"], "healthy")

    async def test_outside_hours_buy_uses_app_reservation(self):
        class BuyableFetcher:
            def get_buyable_amount(self, *args, **kwargs):
                return {"quantity": 10, "amount": 10_000}

        created = []

        async def create_app_reservation(**kwargs):
            created.append(kwargs)
            return {"id": "app-1", "scheduled_at": kwargs["scheduled_at"]}

        orders = [{
            "symbol": "MSFT",
            "exchange": "NASD",
            "quantity": 1,
            "limit_price": 100.0,
            "notional": 100.0,
        }]

        with patch.object(us_market_auto_run.time, "sleep", return_value=None):
            submitted = await us_market_auto_run.place_orders(
                orders,
                BuyableFetcher(),
                create_app_reservation,
                None,
                use_reservations=True,
                reservation_scheduled_at="2026-06-08T22:30:00+09:00",
            )

        self.assertEqual(submitted[0]["order_status"], "reservation_submitted")
        self.assertEqual(submitted[0]["reservation_result"]["reservation_source"], "app")
        self.assertEqual(created[0]["scheduled_at"], "2026-06-08T22:30:00+09:00")

    def test_signal_blocks_overextended_momentum_entry(self):
        _FakeIndicators.roc = 40.0
        with patch.object(us_market_auto_run.time, "sleep", return_value=None):
            signal = us_market_auto_run.signal_for(
                "AMZN",
                "NASD",
                _FakeSignalFetcher(price=112.0),
                _FakeIndicators,
            )

        self.assertEqual(signal["action"], "HOLD")
        self.assertIn("ROC20>25%", signal["reason"])

    def test_signal_turns_five_percent_intraday_drop_into_exit(self):
        _FakeIndicators.roc = 10.0
        with patch.object(us_market_auto_run.time, "sleep", return_value=None):
            signal = us_market_auto_run.signal_for(
                "AMZN",
                "NASD",
                _FakeSignalFetcher(price=94.0),
                _FakeIndicators,
            )

        self.assertEqual(signal["action"], "SELL")
        self.assertEqual(signal["intraday_change_pct"], -6.0)

    def test_active_run_reservation_blocks_crash_retry(self):
        state = {
            "runs": [],
            "active_runs": {"20260605_0945_ET": {"status": "running"}},
        }

        self.assertTrue(us_market_auto_run.run_already_recorded(state, "20260605_0945_ET"))

    def test_protective_report_payload_limits_event_history(self):
        payload = {
            "orders": [{"id": "p1", "events": [{"n": index} for index in range(30)]}],
            "settings": {"enabled": True},
        }

        compacted = us_market_auto_run.compact_protective_payload(payload, event_limit=5)

        self.assertEqual(compacted["orders"][0]["event_count"], 30)
        self.assertEqual([event["n"] for event in compacted["orders"][0]["events"]], [25, 26, 27, 28, 29])
        self.assertEqual(len(payload["orders"][0]["events"]), 30)

    def test_risk_equity_uses_usd_orderable_amount_when_balance_is_zero(self):
        with patch.object(us_market_auto_run.time, "sleep", return_value=None):
            snapshot = us_market_auto_run.account_equity_snapshot(_FakeEquityFetcher())

        self.assertEqual(snapshot["cash"], 10_000)
        self.assertEqual(snapshot["risk_equity"], 10_000)
        self.assertTrue(snapshot["risk_equity_trusted"])
        self.assertIn("get_buyable_amount:NVDA", snapshot["risk_equity_sources"])

    def test_risk_equity_ignores_anomalous_raw_balance(self):
        with patch.object(us_market_auto_run.time, "sleep", return_value=None):
            snapshot = us_market_auto_run.account_equity_snapshot(_FakeAnomalousEquityFetcher())

        self.assertEqual(snapshot["risk_equity"], 10_000)
        self.assertTrue(snapshot["balance_anomaly"])

    def test_session_risk_baseline_blocks_large_equity_jump(self):
        state = {"validated_risk_equity": 10_000}
        snapshot = {
            "risk_equity": 100_000,
            "risk_equity_trusted": True,
        }

        result = us_market_auto_run.apply_session_risk_baseline(snapshot, state)

        self.assertFalse(result["risk_gate_open"])
        self.assertEqual(result["validated_risk_equity"], 10_000)

    def test_state_save_is_atomic_and_summary_is_persistent(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.object(
            us_market_auto_run,
            "RUNTIME_DIR",
            Path(tempdir),
        ):
            state_path = Path(tempdir) / "20260605.json"
            state = {
                "runs": [{
                    "run_id": "20260605_0945_ET",
                    "slot": "hourly",
                    "status": "completed",
                    "duration_seconds": 1.2,
                    "signal_counts": {"BUY": 1, "SELL": 0, "HOLD": 2, "ERROR": 0},
                    "order_counts": {"submitted": 1, "filled": 0, "failed": 0, "skipped": 0},
                    "buy_notional": 100.0,
                    "account_after": {"risk_equity": 10_000},
                    "errors": [],
                }],
                "orders": [],
                "events": [],
            }

            us_market_auto_run.save_today_state(state_path, state)
            summary = us_market_auto_run.write_session_summary("20260605", state)

            self.assertEqual(us_market_auto_run.load_today_state(state_path)["runs"][0]["run_id"], "20260605_0945_ET")
            self.assertEqual(summary["remaining_buy_budget"], 900.0)
            self.assertTrue((Path(tempdir) / "20260605_summary.json").is_file())
            self.assertTrue((Path(tempdir) / "20260605_summary.md").is_file())

    def test_telegram_message_uses_tailnet_link_and_kst_time(self):
        payload = {
            "run_id": "20260605_0945_ET",
            "slot": "hourly",
            "date": "20260605",
            "started_at": "2026-06-05T22:45:02+09:00",
            "status": "completed",
            "signals": [
                {"symbol": "AAPL", "action": "BUY"},
                {"symbol": "IBM", "action": "HOLD"},
            ],
            "orders": [{"symbol": "AAPL", "notional": 100.0, "order_status": "submitted"}],
            "submitted_sells": [],
            "account_before": {},
            "account_after": {},
        }

        with patch.dict("os.environ", {"US_MARKET_REPORT_URL": "http://127.0.0.1:8081"}, clear=False):
            message = us_market_auto_run.telegram_message(payload)

        self.assertIn("2026-06-05 22:45 UTC+09:00", message)
        self.assertIn("http://ww.tailea9a3f.ts.net:8081/automation", message)
        self.assertNotIn("0945_ET", message)

    def test_session_telegram_message_uses_kst_update_time(self):
        summary = {
            "session_date": "20260608",
            "updated_at": "2026-06-09T04:46:12+09:00",
            "run_count": 7,
            "cumulative_buy_notional": 1788.9,
            "remaining_loss_budget": 481.65,
            "totals": {"submitted": 2, "filled": 2, "failed": 0, "errors": 28},
        }

        with patch.dict("os.environ", {}, clear=True):
            message = us_market_auto_run.session_telegram_message(summary)

        self.assertIn("Updated 2026-06-09 04:46 UTC+09:00", message)
        self.assertIn("http://ww.tailea9a3f.ts.net:8081/automation", message)

    def test_cleanup_removes_only_old_detail_reports(self):
        with tempfile.TemporaryDirectory() as tempdir, patch.object(
            us_market_auto_run,
            "RUNTIME_DIR",
            Path(tempdir),
        ):
            detail = Path(tempdir) / "20260401_0945_ET.json"
            summary = Path(tempdir) / "20260401_summary.json"
            detail.write_text("{}", encoding="utf-8")
            summary.write_text("{}", encoding="utf-8")
            old = time.time() - 40 * 24 * 60 * 60
            detail.touch()
            summary.touch()
            import os
            os.utime(detail, (old, old))
            os.utime(summary, (old, old))

            removed = us_market_auto_run.cleanup_old_detail_reports(
                datetime(2026, 6, 5, tzinfo=ZoneInfo("Asia/Seoul"))
            )

            self.assertEqual(removed, ["20260401_0945_ET.json"])
            self.assertFalse(detail.exists())
            self.assertTrue(summary.exists())

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
