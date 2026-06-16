from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from strategy_builder.backend.routers import automation


class AutomationReportsApiTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.report_dir = Path(self.tempdir.name)
        self.kr_report_dir = self.report_dir / "kr"
        self.kr_report_dir.mkdir()
        self.us_report_patch = patch.object(automation, "US_REPORT_DIR", self.report_dir)
        self.kr_report_patch = patch.object(automation, "KR_REPORT_DIR", self.kr_report_dir)
        self.mode_patch = patch.dict(os.environ, {"KIS_LOCK_MODE": "vps"})
        self.us_report_patch.start()
        self.kr_report_patch.start()
        self.mode_patch.start()

    def tearDown(self):
        self.mode_patch.stop()
        self.kr_report_patch.stop()
        self.us_report_patch.stop()
        self.tempdir.cleanup()

    def test_lists_session_and_reads_run(self):
        summary = {"session_date": "20260605", "runs": [], "run_count": 0}
        detail = {"run_id": "20260605_0945_ET", "status": "completed"}
        (self.report_dir / "20260605_summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )
        (self.report_dir / "20260605_0945_ET.json").write_text(json.dumps(detail), encoding="utf-8")

        sessions = asyncio.run(automation.list_us_sessions())
        run = asyncio.run(automation.get_us_run("20260605_0945_ET"))

        self.assertEqual(sessions["total_count"], 1)
        self.assertEqual(run["data"]["status"], "completed")

    def test_reads_custom_report_run_ids(self):
        us_detail = {"run_id": "20260615_1845_ET_custom_report", "status": "report_only"}
        kr_detail = {"run_id": "20260616_0745_KST_custom_report", "status": "report_only"}
        (self.report_dir / "20260615_1845_ET_custom_report.json").write_text(
            json.dumps(us_detail), encoding="utf-8"
        )
        (self.kr_report_dir / "20260616_0745_KST_custom_report.json").write_text(
            json.dumps(kr_detail), encoding="utf-8"
        )

        us_run = asyncio.run(automation.get_us_run("20260615_1845_ET_custom_report"))
        kr_run = asyncio.run(automation.get_kr_run("20260616_0745_KST_custom_report"))

        self.assertEqual(us_run["data"]["status"], "report_only")
        self.assertEqual(kr_run["data"]["status"], "report_only")

    def test_adds_daily_record_to_us_session(self):
        summary = {
            "session_date": "20260605",
            "mode": "vps",
            "updated_at": "2026-06-05T18:00:00+09:00",
            "run_count": 2,
            "runs": [
                {
                    "run_id": "20260605_0945_ET",
                    "slot": "hourly",
                    "started_at": "2026-06-05T22:45:00+09:00",
                    "finished_at": "2026-06-05T22:46:00+09:00",
                    "status": "completed",
                    "report_only": False,
                    "signal_counts": {"BUY": 1, "SELL": 0, "HOLD": 2, "ERROR": 0},
                    "order_counts": {"submitted": 1, "filled": 1, "failed": 0, "skipped": 0},
                    "buy_notional": 100.0,
                    "sell_notional": 0.0,
                    "account_before": {"equity": 1000.0, "cash": 1000.0, "holdings_value": 0.0},
                    "account_after": {"equity": 995.0, "cash": 900.0, "holdings_value": 95.0},
                    "pending_count": 0,
                    "app_reservation_count": 0,
                    "protective_count": 1,
                    "errors": [],
                },
                {
                    "run_id": "20260605_1045_ET",
                    "slot": "hourly",
                    "started_at": "2026-06-05T23:45:00+09:00",
                    "finished_at": "2026-06-05T23:46:00+09:00",
                    "status": "completed",
                    "report_only": False,
                    "signal_counts": {"BUY": 0, "SELL": 1, "HOLD": 2, "ERROR": 0},
                    "order_counts": {"submitted": 1, "filled": 1, "failed": 0, "skipped": 0},
                    "buy_notional": 0.0,
                    "sell_notional": 50.0,
                    "account_before": {"equity": 995.0, "cash": 900.0, "holdings_value": 95.0},
                    "account_after": {"equity": 1020.0, "cash": 950.0, "holdings_value": 70.0},
                    "pending_count": 0,
                    "app_reservation_count": 0,
                    "protective_count": 0,
                    "errors": [],
                },
            ],
            "cumulative_buy_notional": 100.0,
            "cumulative_sell_notional": 50.0,
            "totals": {"submitted": 2, "filled": 2, "failed": 0, "errors": 0},
        }
        (self.report_dir / "20260605_summary.json").write_text(
            json.dumps(summary), encoding="utf-8"
        )

        session = asyncio.run(automation.get_us_session("20260605"))["data"]

        record = session["daily_record"]
        self.assertEqual(record["source"], "automation_report")
        self.assertTrue(record["estimate"])
        self.assertTrue(record["valid"])
        self.assertEqual(record["anomalies"], [])
        self.assertEqual(record["start_equity"], 1000)
        self.assertEqual(record["end_equity"], 1020)
        self.assertEqual(record["pnl"], 20)
        self.assertEqual(record["pnl_pct"], 2)
        self.assertEqual(record["cash_delta"], -50)
        self.assertEqual(record["holdings_value_delta"], 70)
        self.assertEqual(record["buy_notional"], 100)
        self.assertEqual(record["sell_notional"], 50)
        self.assertEqual(record["net_trade_cashflow"], -50)
        self.assertEqual(len(record["points"]), 2)

    def test_builds_monthly_record_from_daily_records(self):
        summaries = [
            {
                "session_date": "20260602",
                "run_count": 1,
                "runs": [
                    {
                        "run_id": "20260602_0945_ET",
                        "started_at": "2026-06-02T22:45:00+09:00",
                        "finished_at": "2026-06-02T22:46:00+09:00",
                        "buy_notional": 0,
                        "sell_notional": 50,
                        "account_before": {
                            "equity": 1020,
                            "cash": 900,
                            "holdings_value": 120,
                        },
                        "account_after": {
                            "equity": 1010,
                            "cash": 950,
                            "holdings_value": 60,
                        },
                        "order_counts": {"submitted": 1, "filled": 1, "failed": 0},
                        "errors": ["sample"],
                    }
                ],
                "cumulative_sell_notional": 50,
                "totals": {"submitted": 1, "filled": 1, "failed": 0, "errors": 1},
            },
            {
                "session_date": "20260601",
                "run_count": 1,
                "runs": [
                    {
                        "run_id": "20260601_0945_ET",
                        "started_at": "2026-06-01T22:45:00+09:00",
                        "finished_at": "2026-06-01T22:46:00+09:00",
                        "buy_notional": 100,
                        "sell_notional": 0,
                        "account_before": {
                            "equity": 1000,
                            "cash": 1000,
                            "holdings_value": 0,
                        },
                        "account_after": {
                            "equity": 1020,
                            "cash": 900,
                            "holdings_value": 120,
                        },
                        "order_counts": {"submitted": 1, "filled": 1, "failed": 0},
                        "errors": [],
                    }
                ],
                "cumulative_sell_notional": 0,
                "totals": {"submitted": 1, "filled": 1, "failed": 0, "errors": 0},
            },
            {
                "session_date": "20260529",
                "run_count": 0,
                "runs": [],
                "cumulative_sell_notional": 0,
                "totals": {"submitted": 0, "filled": 0, "failed": 0, "errors": 0},
            },
        ]
        for summary in summaries:
            path = self.report_dir / f"{summary['session_date']}_summary.json"
            path.write_text(json.dumps(summary), encoding="utf-8")

        monthly = asyncio.run(automation.get_us_monthly_record("2026-06"))["data"]

        self.assertEqual(monthly["market"], "us")
        self.assertEqual(monthly["month"], "2026-06")
        self.assertEqual(
            [day["session_date"] for day in monthly["days"]],
            ["20260601", "20260602"],
        )
        self.assertEqual(monthly["summary"]["day_count"], 2)
        self.assertEqual(monthly["summary"]["trading_days"], 2)
        self.assertEqual(monthly["summary"]["anomaly_days"], 0)
        self.assertEqual(monthly["summary"]["win_days"], 1)
        self.assertEqual(monthly["summary"]["loss_days"], 1)
        self.assertEqual(monthly["summary"]["pnl"], 10)
        self.assertEqual(monthly["summary"]["account_pnl"], 10)
        self.assertEqual(monthly["summary"]["start_equity"], 1000)
        self.assertEqual(monthly["summary"]["end_equity"], 1010)
        self.assertEqual(monthly["summary"]["buy_notional"], 100)
        self.assertEqual(monthly["summary"]["sell_notional"], 50)
        self.assertEqual(monthly["summary"]["error_count"], 1)

    def test_excludes_anomalous_daily_record_from_monthly_totals(self):
        normal = {
            "session_date": "20260601",
            "run_count": 1,
            "runs": [
                {
                    "run_id": "20260601_0945_ET",
                    "started_at": "2026-06-01T22:45:00+09:00",
                    "finished_at": "2026-06-01T22:46:00+09:00",
                    "buy_notional": 10,
                    "sell_notional": 0,
                    "account_before": {"equity": 1000, "cash": 1000, "holdings_value": 0},
                    "account_after": {"equity": 1010, "cash": 990, "holdings_value": 20},
                    "order_counts": {"submitted": 1, "filled": 1, "failed": 0},
                    "errors": [],
                }
            ],
            "totals": {"submitted": 1, "filled": 1, "failed": 0, "errors": 0},
        }
        anomalous = {
            "session_date": "20260605",
            "run_count": 1,
            "runs": [
                {
                    "run_id": "20260605_0945_ET",
                    "started_at": "2026-06-05T22:45:00+09:00",
                    "finished_at": "2026-06-05T22:46:00+09:00",
                    "buy_notional": 100,
                    "sell_notional": 0,
                    "account_before": {"equity": 1000, "cash": 1000, "holdings_value": 0},
                    "account_after": {"equity": 1000000, "cash": 999900, "holdings_value": 100},
                    "order_counts": {"submitted": 1, "filled": 1, "failed": 0},
                    "errors": [],
                }
            ],
            "totals": {"submitted": 1, "filled": 1, "failed": 0, "errors": 0},
        }
        for summary in (normal, anomalous):
            (self.report_dir / f"{summary['session_date']}_summary.json").write_text(
                json.dumps(summary), encoding="utf-8"
            )

        day = asyncio.run(automation.get_us_session("20260605"))["data"]["daily_record"]
        monthly = asyncio.run(automation.get_us_monthly_record("2026-06"))["data"]

        self.assertFalse(day["valid"])
        self.assertEqual(day["pnl"], 0)
        self.assertEqual(len(day["anomalies"]), 1)
        self.assertEqual(monthly["summary"]["day_count"], 2)
        self.assertEqual(monthly["summary"]["trading_days"], 1)
        self.assertEqual(monthly["summary"]["anomaly_days"], 1)
        self.assertEqual(monthly["summary"]["pnl"], 10)
        self.assertEqual(monthly["days"][1]["session_date"], "20260605")
        self.assertFalse(monthly["days"][1]["valid"])

    def test_rejects_invalid_month(self):
        with self.assertRaises(HTTPException) as raised:
            asyncio.run(automation.get_us_monthly_record("2026/06"))
        self.assertEqual(raised.exception.status_code, 400)

    def test_legacy_run_is_normalized_for_the_timeline(self):
        legacy = {"slot": "open", "started_at": "2026-06-04T23:45:00+09:00"}
        (self.report_dir / "20260604_234500_open.json").write_text(
            json.dumps(legacy), encoding="utf-8"
        )

        run = asyncio.run(automation.get_us_run("20260604_234500_open"))

        self.assertEqual(run["data"]["run_id"], "20260604_234500_open")
        self.assertEqual(run["data"]["status"], "legacy")
        self.assertEqual(run["data"]["duration_seconds"], 0.0)

    def test_rejects_invalid_paths_and_prod_mode(self):
        with self.assertRaises(HTTPException):
            asyncio.run(automation.get_us_run("../secret"))
        with self.assertRaises(HTTPException):
            asyncio.run(automation.get_kr_run("../secret"))

        with (
            patch.dict(os.environ, {"KIS_LOCK_MODE": "prod"}),
            self.assertRaises(HTTPException) as raised,
        ):
            asyncio.run(automation.list_us_sessions())
        self.assertEqual(raised.exception.status_code, 404)

    def test_synthesizes_kr_session_from_state_and_detail_report(self):
        state = {
            "runs": [
                {
                    "run_id": "20260609_0910_KST",
                    "slot": "hourly",
                    "started_at": "2026-06-09T09:10:00+09:00",
                    "scheduled_at_kst": "2026-06-09T09:10:00+09:00",
                    "status": "completed",
                    "report": str(self.kr_report_dir / "20260609_0910_KST.md"),
                }
            ],
            "orders": [],
        }
        detail = {
            "run_id": "20260609_0910_KST",
            "slot": "hourly",
            "started_at": "2026-06-09T09:10:00+09:00",
            "scheduled_at_kst": "2026-06-09T09:10:00+09:00",
            "signals": [
                {"code": "005930", "action": "BUY"},
                {"code": "035420", "action": "SELL"},
                {"code": "000660", "action": "HOLD"},
            ],
            "submitted_buys": [
                {
                    "code": "005930",
                    "action": "BUY",
                    "quantity": 2,
                    "amount": 140000,
                    "order_status": "success",
                }
            ],
            "submitted_sells": [
                {
                    "code": "035420",
                    "action": "SELL",
                    "target_price": 180000,
                    "order_status": "success",
                    "order_result": {
                        "logs": [{"message": "주문 실행 중: SELL 1주 @ 시장가 (시장가)"}],
                    },
                }
            ],
            "account_before": {
                "account": {"deposit": {"total_eval": 10_000_000, "deposit": 5_000_000}}
            },
            "account_after": {
                "account": {"deposit": {"total_eval": 10_050_000, "deposit": 4_910_000}},
                "pending": {"total_count": 0},
                "reservations": {"total_count": 0},
                "protective": {"orders": [{"stock_code": "005930"}]},
            },
        }
        (self.kr_report_dir / "20260609.json").write_text(json.dumps(state), encoding="utf-8")
        (self.kr_report_dir / "20260609_0910_KST.json").write_text(
            json.dumps(detail), encoding="utf-8"
        )
        (self.kr_report_dir / "20260609_0910_KST.md").write_text("# report\n", encoding="utf-8")

        sessions = asyncio.run(automation.list_kr_sessions())
        run = asyncio.run(automation.get_kr_run("20260609_0910_KST"))

        self.assertEqual(sessions["total_count"], 1)
        session = sessions["sessions"][0]
        self.assertEqual(session["session_date"], "20260609")
        self.assertEqual(session["run_count"], 1)
        self.assertEqual(session["runs"][0]["signal_counts"]["BUY"], 1)
        self.assertEqual(session["runs"][0]["signal_counts"]["SELL"], 1)
        self.assertEqual(session["runs"][0]["buy_notional"], 140000)
        self.assertEqual(session["runs"][0]["sell_notional"], 180000)
        self.assertEqual(session["runs"][0]["order_counts"]["filled"], 2)
        self.assertEqual(session["cumulative_sell_notional"], 180000)
        self.assertEqual(session["remaining_buy_budget"], 865000)
        self.assertEqual(session["daily_record"]["pnl"], 50000)
        self.assertEqual(session["daily_record"]["cash_delta"], -90000)
        self.assertEqual(session["daily_record"]["net_trade_cashflow"], 40000)
        self.assertEqual(session["daily_record"]["holdings_value_delta"], 140000)
        self.assertEqual(run["data"]["orders"][0]["code"], "005930")
        self.assertEqual(run["data"]["scheduled_at_kst"], "2026-06-09T09:10:00+09:00")
