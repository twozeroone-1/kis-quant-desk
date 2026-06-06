from __future__ import annotations

import sys
import tempfile
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
        self.assertTrue(kr_market_auto_run.order_execution_enabled("prod", False, True))
        self.assertTrue(kr_market_auto_run.prod_llm_orders_enabled("prod", "off"))

    def test_build_buy_orders_balances_when_all_candidates_can_get_one_share(self):
        results = [
            {"code": "105560", "name": "KB금융", "action": "BUY", "strength": 0.95, "target_price": 400000},
            {"code": "396500", "name": "TIGER 반도체TOP10", "action": "BUY", "strength": 0.80, "target_price": 100000},
            {"code": "005930", "name": "삼성전자", "action": "BUY", "strength": 0.75, "target_price": 200000},
        ]
        account = {"deposit": {"total_eval": 10_000_000, "deposit": 10_000_000}}

        orders = kr_market_auto_run.build_buy_orders(results, account, {"orders": []})

        self.assertEqual([order["code"] for order in orders], ["105560", "396500", "005930"])
        self.assertEqual({order["code"]: order["quantity"] for order in orders}, {
            "105560": 1,
            "396500": 3,
            "005930": 1,
        })
        self.assertLessEqual(sum(order["amount"] for order in orders), 1_000_000)

    def test_build_buy_orders_uses_signal_order_when_only_one_share_fits(self):
        results = [
            {"code": "105560", "name": "KB금융", "action": "BUY", "strength": 0.95, "target_price": 90000},
            {"code": "396500", "name": "TIGER 반도체TOP10", "action": "BUY", "strength": 0.80, "target_price": 60000},
            {"code": "005930", "name": "삼성전자", "action": "BUY", "strength": 0.75, "target_price": 60000},
        ]
        account = {"deposit": {"total_eval": 1_000_000, "deposit": 1_000_000}}

        orders = kr_market_auto_run.build_buy_orders(results, account, {"orders": []})

        self.assertEqual([(order["code"], order["quantity"]) for order in orders], [("105560", 1)])

    def test_prod_telegram_approval_adds_confirm_prod_to_buy_payload(self):
        calls = []
        original_api = kr_market_auto_run.api
        original_request = kr_market_auto_run.prod_telegram_approval.request_approval
        try:
            def fake_request(payload, details, **kwargs):
                return {
                    "status": "approved",
                    "approval_id": "abc123",
                    "payload_hash": kr_market_auto_run.prod_telegram_approval.payload_hash(payload),
                }

            def fake_api(method, path, **kwargs):
                calls.append((method, path, kwargs["json"]))
                return {"status": "success", "order_id": "ord-1"}

            kr_market_auto_run.prod_telegram_approval.request_approval = fake_request
            kr_market_auto_run.api = fake_api

            submitted = kr_market_auto_run.place_buys(
                [{
                    "code": "005930",
                    "name": "삼성전자",
                    "target_price": 70000,
                    "quantity": 2,
                    "amount": 140000,
                    "strength": 0.75,
                    "reason": "test buy",
                    "take_profit": 74200,
                    "stop_loss": 67900,
                    "order_decision": "주문",
                }],
                "prod",
                False,
                True,
            )

            self.assertEqual(submitted[0]["order_status"], "success")
            self.assertEqual(len(calls), 1)
            self.assertTrue(calls[0][2]["confirm_prod"])
            self.assertEqual(calls[0][2]["stock_code"], "005930")
        finally:
            kr_market_auto_run.api = original_api
            kr_market_auto_run.prod_telegram_approval.request_approval = original_request

    def test_prod_telegram_rejected_buy_does_not_call_api(self):
        calls = []
        original_api = kr_market_auto_run.api
        original_request = kr_market_auto_run.prod_telegram_approval.request_approval
        try:
            kr_market_auto_run.api = lambda *args, **kwargs: calls.append((args, kwargs))
            kr_market_auto_run.prod_telegram_approval.request_approval = lambda *args, **kwargs: {
                "status": "rejected",
                "approval_id": "abc123",
            }

            submitted = kr_market_auto_run.place_buys(
                [{
                    "code": "005930",
                    "name": "삼성전자",
                    "target_price": 70000,
                    "quantity": 1,
                    "amount": 70000,
                    "strength": 0.75,
                    "reason": "test buy",
                    "take_profit": 74200,
                    "stop_loss": 67900,
                    "order_decision": "주문",
                }],
                "prod",
                False,
                True,
            )

            self.assertEqual(submitted[0]["order_status"], "telegram_rejected")
            self.assertFalse(calls)
        finally:
            kr_market_auto_run.api = original_api
            kr_market_auto_run.prod_telegram_approval.request_approval = original_request

    def test_prod_telegram_timeout_sell_does_not_call_api(self):
        calls = []
        original_api = kr_market_auto_run.api
        original_request = kr_market_auto_run.prod_telegram_approval.request_approval
        try:
            kr_market_auto_run.api = lambda *args, **kwargs: calls.append((args, kwargs))
            kr_market_auto_run.prod_telegram_approval.request_approval = lambda *args, **kwargs: {
                "status": "timeout",
                "approval_id": "abc123",
            }

            submitted = kr_market_auto_run.place_sells(
                [{"code": "005930", "name": "삼성전자", "action": "SELL", "strength": 0.8, "target_price": 70000}],
                {"005930": {"quantity": 3}},
                "prod",
                False,
                True,
            )

            self.assertEqual(submitted[0]["order_status"], "telegram_timeout")
            self.assertFalse(calls)
        finally:
            kr_market_auto_run.api = original_api
            kr_market_auto_run.prod_telegram_approval.request_approval = original_request

    def test_prod_telegram_hash_mismatch_does_not_call_api(self):
        calls = []
        original_api = kr_market_auto_run.api
        original_request = kr_market_auto_run.prod_telegram_approval.request_approval
        try:
            kr_market_auto_run.api = lambda *args, **kwargs: calls.append((args, kwargs))
            kr_market_auto_run.prod_telegram_approval.request_approval = lambda *args, **kwargs: {
                "status": "approved",
                "approval_id": "abc123",
                "payload_hash": "not-the-current-payload",
            }

            submitted = kr_market_auto_run.place_buys(
                [{
                    "code": "005930",
                    "name": "삼성전자",
                    "target_price": 70000,
                    "quantity": 1,
                    "amount": 70000,
                    "strength": 0.75,
                    "reason": "test buy",
                    "take_profit": 74200,
                    "stop_loss": 67900,
                    "order_decision": "주문",
                }],
                "prod",
                False,
                True,
            )

            self.assertEqual(submitted[0]["order_status"], "telegram_hash_mismatch")
            self.assertFalse(calls)
        finally:
            kr_market_auto_run.api = original_api
            kr_market_auto_run.prod_telegram_approval.request_approval = original_request

    def test_prod_file_approval_flow_still_submits_without_telegram(self):
        calls = []
        original_api = kr_market_auto_run.api
        try:
            def fake_api(method, path, **kwargs):
                calls.append(kwargs["json"])
                return {"status": "success"}

            kr_market_auto_run.api = fake_api

            submitted = kr_market_auto_run.place_sells(
                [{"code": "005930", "name": "삼성전자", "action": "SELL", "strength": 0.8, "target_price": 70000}],
                {"005930": {"quantity": 3}},
                "prod",
                True,
                False,
            )

            self.assertEqual(submitted[0]["order_status"], "success")
            self.assertTrue(calls[0]["confirm_prod"])
            self.assertNotIn("telegram_approval", submitted[0])
        finally:
            kr_market_auto_run.api = original_api

    def test_buy_approved_by_telegram_reuses_approval_for_protection(self):
        calls = []
        original_api = kr_market_auto_run.api
        try:
            def fake_api(method, path, **kwargs):
                calls.append((path, kwargs["json"]))
                return {"status": "success", "stock_code": kwargs["json"]["stock_code"]}

            kr_market_auto_run.api = fake_api

            protections = kr_market_auto_run.register_protection_for_holdings(
                set(),
                {"holdings": [{"stock_code": "005930", "stock_name": "삼성전자", "quantity": 2, "avg_price": 70000}]},
                "prod",
                False,
                {"005930"},
                True,
                {"005930"},
            )

            self.assertEqual(protections[0]["status"], "success")
            self.assertEqual(protections[0]["telegram_approval"]["status"], "approved_reused_buy_approval")
            self.assertTrue(calls[0][1]["confirm_prod"])
        finally:
            kr_market_auto_run.api = original_api

    def test_wrong_telegram_chat_id_is_ignored_until_timeout(self):
        class FakeTelegramClient:
            def __init__(self):
                self.approve_token = None
                self.answered = []
                self.edited = []

            def send_message(self, chat_id, text, reply_markup):
                self.approve_token = reply_markup["inline_keyboard"][0][0]["callback_data"]
                return {"result": {"message_id": 42}}

            def get_updates(self, offset, timeout_seconds):
                if offset is None:
                    return [{
                        "update_id": 1,
                        "callback_query": {
                            "id": "cb-1",
                            "data": self.approve_token,
                            "message": {"chat": {"id": "bad-chat"}},
                        },
                    }]
                return []

            def answer_callback_query(self, callback_query_id, text):
                self.answered.append((callback_query_id, text))

            def edit_message_reply_markup(self, chat_id, message_id, reply_markup):
                self.edited.append((chat_id, message_id, reply_markup))

        current_time = [0.0]

        def fake_time():
            return current_time[0]

        def fake_sleep(seconds):
            current_time[0] += seconds

        with tempfile.TemporaryDirectory() as tmpdir:
            result = kr_market_auto_run.prod_telegram_approval.request_approval(
                {"stock_code": "005930", "quantity": 1, "confirm_prod": False},
                {
                    "action": "BUY",
                    "stock_code": "005930",
                    "stock_name": "삼성전자",
                    "quantity": 1,
                    "order_type": "market",
                    "price": 70000,
                    "estimated_amount": 70000,
                    "signal_strength": "0.75",
                    "reason": "test",
                    "protection_summary": "test",
                },
                store_dir=Path(tmpdir),
                client=FakeTelegramClient(),
                allowed_chat_id="allowed-chat",
                timeout_seconds=1,
                poll_interval=1,
                time_fn=fake_time,
                sleep_fn=fake_sleep,
            )

        self.assertEqual(result["status"], "timeout")


if __name__ == "__main__":
    unittest.main()
