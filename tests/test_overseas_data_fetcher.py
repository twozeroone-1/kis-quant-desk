from __future__ import annotations

import sys
import unittest
import os
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("KIS_CONFIG_ROOT", str(PROJECT_ROOT / "tests" / "fixtures" / "kis_config"))
os.environ.setdefault("KIS_TOKEN_ROOT", "/tmp/open-trading-api-test-kis-tokens")
sys.path.insert(0, str(PROJECT_ROOT / "strategy_builder"))

try:
    from core import overseas_data_fetcher
except ModuleNotFoundError as exc:
    overseas_data_fetcher = None
    IMPORT_ERROR = exc
else:
    IMPORT_ERROR = None


class _FakeTREnv:
    my_acct = "12345678"
    my_prod = "01"


class _FakeResponse:
    def isOK(self):
        return True

    def getBody(self):
        return {"output": []}


@unittest.skipIf(overseas_data_fetcher is None, f"strategy_builder dependencies unavailable: {IMPORT_ERROR}")
class OverseasDataFetcherTest(unittest.TestCase):
    def test_pending_orders_uses_demo_tr_id_for_vps(self):
        seen = {}

        def fake_fetch(api_url, tr_id, tr_cont, params):
            seen["api_url"] = api_url
            seen["tr_id"] = tr_id
            seen["params"] = params
            return _FakeResponse()

        with patch.object(overseas_data_fetcher, "_assert_trenv_ready", return_value=True), patch.object(
            overseas_data_fetcher.ka, "getTREnv", return_value=_FakeTREnv()
        ), patch.object(overseas_data_fetcher.ka, "_url_fetch", side_effect=fake_fetch):
            df, ok = overseas_data_fetcher.get_pending_orders("vps", exchange="NASD")

        self.assertTrue(ok)
        self.assertTrue(df.empty)
        self.assertEqual(seen["api_url"], "/uapi/overseas-stock/v1/trading/inquire-nccs")
        self.assertEqual(seen["tr_id"], "VTTS3018R")
        self.assertEqual(seen["params"]["OVRS_EXCG_CD"], "NASD")

    def test_pending_orders_uses_real_tr_id_for_prod(self):
        seen = {}

        def fake_fetch(api_url, tr_id, tr_cont, params):
            seen["tr_id"] = tr_id
            return _FakeResponse()

        with patch.object(overseas_data_fetcher, "_assert_trenv_ready", return_value=True), patch.object(
            overseas_data_fetcher.ka, "getTREnv", return_value=_FakeTREnv()
        ), patch.object(overseas_data_fetcher.ka, "_url_fetch", side_effect=fake_fetch):
            _df, ok = overseas_data_fetcher.get_pending_orders("prod", exchange="NYSE")

        self.assertTrue(ok)
        self.assertEqual(seen["tr_id"], "TTTS3018R")


if __name__ == "__main__":
    unittest.main()
