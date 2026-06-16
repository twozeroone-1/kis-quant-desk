from __future__ import annotations

import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = PROJECT_ROOT / ".codex" / "scripts" / "market_candidate_selector.py"
SPEC = importlib.util.spec_from_file_location("market_candidate_selector", MODULE_PATH)
selector = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(selector)


KR_STATIC = [
    ("005930", "삼성전자", "대형주"),
    ("000660", "SK하이닉스", "대형주"),
    ("005380", "현대차", "대형주"),
    ("373220", "LG에너지솔루션", "대형주"),
    ("000270", "기아", "대형주"),
    ("105560", "KB금융", "대형주"),
]

US_STATIC = [
    ("SPY", "NYSE"),
    ("QQQ", "NASD"),
    ("DIA", "NYSE"),
    ("IWM", "NYSE"),
    ("NVDA", "NASD"),
    ("MSFT", "NASD"),
]


class FakeRankingResult:
    def __init__(self, rows, success=True, message="boom"):
        self._rows = rows
        self.success = success
        self.message = message

    def records(self):
        return self._rows

    def display_error(self):
        return self.message


class FakeUSFetcher:
    def __init__(self, rows_by_method):
        self.rows_by_method = rows_by_method

    def _result(self, method):
        rows = self.rows_by_method.get(method)
        if rows is None:
            return FakeRankingResult([], success=False)
        return FakeRankingResult(rows)

    def get_overseas_trade_value_rank(self, **kwargs):
        return self._result("trade")

    def get_overseas_volume_power_rank(self, **kwargs):
        return self._result("power")

    def get_overseas_market_cap_rank(self, **kwargs):
        return self._result("cap")

    def get_overseas_volume_surge_rank(self, **kwargs):
        return self._result("surge")


class FakeUSFetcherWithUnsupportedCap(FakeUSFetcher):
    def get_overseas_market_cap_rank(self, **kwargs):
        return FakeRankingResult(
            [],
            success=False,
            message="OPSQ2001 ERROR INPUT FIELD NOT FOUND [CURR_GB]",
        )


class FakeUSResolver:
    def __init__(self, profiles):
        self.profiles = profiles

    def resolve_exchange(self, symbol):
        if symbol not in self.profiles:
            raise RuntimeError(f"unknown symbol: {symbol}")
        return self.profiles[symbol]


class MarketCandidateSelectorTest(unittest.TestCase):
    def test_kr_row_normalizes_multiple_kis_field_names(self):
        row = {
            "mksc_shrn_iscd": "005930",
            "hts_kor_isnm": "삼성전자",
            "data_rank": "2",
            "acml_vol": "1,234,567",
            "acml_tr_pbmn": "987654321",
        }

        normalized = selector.normalize_kr_candidate_row(row, "volume_rank")

        self.assertEqual(normalized["code"], "005930")
        self.assertEqual(normalized["name"], "삼성전자")
        self.assertEqual(normalized["metrics"]["rank"], 2)
        self.assertEqual(normalized["metrics"]["volume"], 1234567)

    def test_kr_selection_dedupes_and_sums_scores(self):
        responses = {
            "/api/screening/rankings/volume?market_div=J&input_iscd=0000&volume_min=0&max_depth=1": {
                "status": "success",
                "items": [{"mksc_shrn_iscd": "005930", "hts_kor_isnm": "삼성전자", "rank": "1"}],
            },
            "/api/screening/rankings/volume-power?market_div=J&input_iscd=0000&max_depth=1": {
                "status": "success",
                "items": [{"stck_shrn_iscd": "005930", "stck_prdt_name": "삼성전자", "rank": "1"}],
            },
            "/api/screening/rankings/market-cap?market_div=J&input_iscd=0000&max_depth=1": {
                "status": "success",
                "items": [
                    {"mksc_shrn_iscd": "000660", "hts_kor_isnm": "SK하이닉스", "rank": "1"},
                    {"mksc_shrn_iscd": "005380", "hts_kor_isnm": "현대차", "rank": "2"},
                    {"mksc_shrn_iscd": "000270", "hts_kor_isnm": "기아", "rank": "3"},
                    {"mksc_shrn_iscd": "105560", "hts_kor_isnm": "KB금융", "rank": "4"},
                ],
            },
            "/api/screening/investors/foreign-institution?market_div=V&input_iscd=0000&rank_sort=0": {
                "status": "success",
                "items": [],
            },
        }

        report = selector.select_kr_candidates(
            api_get=lambda path: responses[path],
            account={"holdings": []},
            static_candidates=KR_STATIC,
            limit=10,
        )

        symbols = [item["code"] for item in report["selected"]]
        self.assertEqual(symbols.count("005930"), 1)
        samsung = next(item for item in report["selected"] if item["code"] == "005930")
        self.assertIn("volume_rank", samsung["sources"])
        self.assertIn("volume_power_rank", samsung["sources"])
        self.assertGreater(samsung["score"], 60)
        self.assertFalse(report["fallback_used"])

    def test_kr_selection_includes_holdings(self):
        def api_get(path):
            return {
                "status": "success",
                "items": [
                    {"mksc_shrn_iscd": "005930", "hts_kor_isnm": "삼성전자", "rank": "1"},
                    {"mksc_shrn_iscd": "000660", "hts_kor_isnm": "SK하이닉스", "rank": "2"},
                    {"mksc_shrn_iscd": "005380", "hts_kor_isnm": "현대차", "rank": "3"},
                    {"mksc_shrn_iscd": "000270", "hts_kor_isnm": "기아", "rank": "4"},
                ],
            }

        report = selector.select_kr_candidates(
            api_get=api_get,
            account={"holdings": [{"stock_code": "068270", "stock_name": "셀트리온", "quantity": 1}]},
            static_candidates=KR_STATIC,
            limit=20,
        )

        self.assertIn("068270", [item["code"] for item in report["selected"]])
        holding = next(item for item in report["selected"] if item["code"] == "068270")
        self.assertIn("holding", holding["sources"])

    def test_kr_selection_excludes_leveraged_etp_from_ranked_candidates_but_keeps_holdings(self):
        rows = [
            {"mksc_shrn_iscd": "252670", "hts_kor_isnm": "KODEX 200선물인버스2X", "rank": "1"},
            {"mksc_shrn_iscd": "005930", "hts_kor_isnm": "삼성전자", "rank": "2"},
            {"mksc_shrn_iscd": "000660", "hts_kor_isnm": "SK하이닉스", "rank": "3"},
            {"mksc_shrn_iscd": "005380", "hts_kor_isnm": "현대차", "rank": "4"},
            {"mksc_shrn_iscd": "000270", "hts_kor_isnm": "기아", "rank": "5"},
            {"mksc_shrn_iscd": "105560", "hts_kor_isnm": "KB금융", "rank": "6"},
        ]

        report = selector.select_kr_candidates(
            api_get=lambda path: {"status": "success", "items": rows},
            account={"holdings": [{"stock_code": "252670", "stock_name": "KODEX 200선물인버스2X", "quantity": 1}]},
            static_candidates=KR_STATIC,
            limit=20,
        )

        inverse = next(item for item in report["selected"] if item["code"] == "252670")
        self.assertEqual(inverse["sources"], ["holding"])
        self.assertEqual(inverse["category"], "excluded_etp")

    def test_kr_holding_survives_limit_cut(self):
        rows = [
            {"mksc_shrn_iscd": f"{index:06d}", "hts_kor_isnm": f"종목{index}", "rank": str(index)}
            for index in range(1, 8)
        ]

        report = selector.select_kr_candidates(
            api_get=lambda path: {"status": "success", "items": rows},
            account={"holdings": [{"stock_code": "999999", "stock_name": "보유종목", "quantity": 1}]},
            static_candidates=KR_STATIC,
            limit=5,
        )

        self.assertFalse(report["fallback_used"])
        self.assertIn("999999", [item["code"] for item in report["selected"]])

    def test_kr_selection_falls_back_on_api_errors(self):
        report = selector.select_kr_candidates(
            api_get=lambda path: {"status": "error", "message": "rate limit"},
            account={"holdings": [{"stock_code": "068270", "stock_name": "셀트리온", "quantity": 1}]},
            static_candidates=KR_STATIC,
            limit=5,
        )

        self.assertTrue(report["fallback_used"])
        self.assertEqual([item["code"] for item in report["selected"][:5]], [item[0] for item in KR_STATIC[:5]])
        self.assertIn("068270", [item["code"] for item in report["selected"]])
        self.assertTrue(report["errors"])

    def test_kr_partial_source_errors_become_warnings_when_candidates_are_sufficient(self):
        rows = [
            {
                "mksc_shrn_iscd": f"{index:06d}",
                "hts_kor_isnm": f"종목{index}",
                "rank": str(index),
            }
            for index in range(1, 8)
        ]

        def api_get(path):
            if "volume-power" in path:
                raise TimeoutError("ranking timeout")
            return {"status": "success", "items": rows}

        report = selector.select_kr_candidates(
            api_get=api_get,
            account={"holdings": []},
            static_candidates=KR_STATIC,
            limit=10,
        )

        self.assertFalse(report["fallback_used"])
        self.assertEqual(report["errors"], [])
        self.assertIn("volume_power_rank: ranking timeout", report["warnings"])

    def test_kr_custom_env_uses_symbols_without_dynamic_api(self):
        with patch.dict(os.environ, {"KR_MARKET_CANDIDATE_SYMBOLS": "010170,005930,000660"}):
            report = selector.select_kr_candidates(
                api_get=lambda path: self.fail(f"unexpected dynamic API call: {path}"),
                account={"holdings": []},
                static_candidates=KR_STATIC,
                limit=20,
            )

        codes = [item["code"] for item in report["selected"]]
        self.assertEqual(report["mode"], "custom")
        self.assertFalse(report["fallback_used"])
        self.assertEqual(codes, ["010170", "005930", "000660"])
        self.assertEqual(
            next(item for item in report["selected"] if item["code"] == "010170")["sources"],
            ["custom"],
        )

    def test_kr_custom_env_dedupes_invalids_and_keeps_holdings(self):
        with patch.dict(os.environ, {"KR_MARKET_CANDIDATE_SYMBOLS": "010170,bad,010170,000660"}):
            report = selector.select_kr_candidates(
                api_get=lambda path: self.fail(f"unexpected dynamic API call: {path}"),
                account={"holdings": [{"stock_code": "068270", "stock_name": "셀트리온", "quantity": 1}]},
                static_candidates=KR_STATIC,
                limit=20,
            )

        self.assertEqual([item["code"] for item in report["selected"]], ["010170", "000660", "068270"])
        self.assertIn("invalid KR custom symbol ignored: bad", report["warnings"])
        holding = next(item for item in report["selected"] if item["code"] == "068270")
        self.assertEqual(holding["sources"], ["holding"])

    def test_kr_custom_env_empty_valid_list_does_not_fallback(self):
        with patch.dict(os.environ, {"KR_MARKET_CANDIDATE_SYMBOLS": "bad,12345"}):
            report = selector.select_kr_candidates(
                api_get=lambda path: self.fail(f"unexpected dynamic API call: {path}"),
                account={"holdings": [{"stock_code": "068270", "stock_name": "셀트리온", "quantity": 1}]},
                static_candidates=KR_STATIC,
                limit=20,
            )

        self.assertEqual(report["mode"], "custom")
        self.assertFalse(report["fallback_used"])
        self.assertEqual(report["selected"], [])
        self.assertTrue(report["errors"])

    def test_explicit_mode_overrides_custom_env(self):
        with patch.dict(os.environ, {"KR_MARKET_CANDIDATE_SYMBOLS": "010170"}):
            report = selector.select_kr_candidates(
                api_get=lambda path: self.fail(f"unexpected dynamic API call: {path}"),
                account={"holdings": []},
                static_candidates=KR_STATIC,
                mode="static",
                limit=2,
            )

        self.assertEqual(report["mode"], "static")
        self.assertEqual([item["code"] for item in report["selected"]], ["005930", "000660"])

    def test_us_exchange_normalization(self):
        self.assertEqual(selector.normalize_us_exchange("NAS"), "NASD")
        self.assertEqual(selector.normalize_us_exchange("NASD"), "NASD")
        self.assertEqual(selector.normalize_us_exchange("NYS"), "NYSE")
        self.assertEqual(selector.normalize_us_exchange("NYSE"), "NYSE")
        self.assertEqual(selector.normalize_us_exchange("AMS"), "AMEX")
        self.assertEqual(selector.normalize_us_exchange("AMEX"), "AMEX")

    def test_us_selection_includes_holdings_and_fallbacks(self):
        fallback = selector.select_us_candidates(
            ranking_fetcher=FakeUSFetcher({}),
            holdings=[{"stock_code": "AAPL", "exchange": "NASD", "stock_name": "Apple", "quantity": 2}],
            static_candidates=US_STATIC,
            limit=5,
        )
        self.assertTrue(fallback["fallback_used"])
        self.assertEqual([item["symbol"] for item in fallback["selected"][:5]], [item[0] for item in US_STATIC[:5]])
        self.assertIn("AAPL", [item["symbol"] for item in fallback["selected"]])

        dynamic = selector.select_us_candidates(
            ranking_fetcher=FakeUSFetcher({
                "trade": [
                    {"symb": "NVDA", "excd": "NAS", "rank": "1"},
                    {"symb": "MSFT", "excd": "NAS", "rank": "2"},
                    {"symb": "JPM", "excd": "NYS", "rank": "3"},
                    {"symb": "XOM", "excd": "NYS", "rank": "4"},
                    {"symb": "AMD", "excd": "NAS", "rank": "5"},
                ],
                "power": [],
                "cap": [],
                "surge": [],
            }),
            holdings=[{"stock_code": "AAPL", "exchange": "NASD", "stock_name": "Apple", "quantity": 2}],
            static_candidates=US_STATIC,
            limit=20,
        )
        self.assertFalse(dynamic["fallback_used"])
        self.assertIn("AAPL", [item["symbol"] for item in dynamic["selected"]])
        aapl = next(item for item in dynamic["selected"] if item["symbol"] == "AAPL")
        self.assertIn("holding", aapl["sources"])

    def test_us_core_etfs_and_holdings_survive_limit_cut(self):
        ranked = [
            {"symb": symbol, "excd": "NAS", "rank": str(index)}
            for index, symbol in enumerate(("NVDA", "MSFT", "AVGO", "AMD", "AMZN", "META"), start=1)
        ]
        report = selector.select_us_candidates(
            ranking_fetcher=FakeUSFetcher({"trade": ranked, "power": [], "cap": [], "surge": []}),
            holdings=[{"stock_code": "AAPL", "exchange": "NASD", "stock_name": "Apple", "quantity": 2}],
            static_candidates=US_STATIC,
            limit=5,
        )

        symbols = [item["symbol"] for item in report["selected"]]
        self.assertFalse(report["fallback_used"])
        for symbol in ("SPY", "QQQ", "DIA", "IWM", "AAPL"):
            self.assertIn(symbol, symbols)

    def test_us_selection_excludes_leveraged_etf_from_ranked_candidates(self):
        ranked = [
            {"symb": symbol, "excd": "NAS", "rank": str(index)}
            for index, symbol in enumerate(("SOXL", "NVDA", "MSFT", "AVGO", "AMD", "AMZN"), start=1)
        ]

        report = selector.select_us_candidates(
            ranking_fetcher=FakeUSFetcher({"trade": ranked, "power": [], "cap": [], "surge": []}),
            holdings=[],
            static_candidates=US_STATIC,
            limit=20,
        )

        self.assertNotIn("SOXL", [item["symbol"] for item in report["selected"]])

    def test_us_unsupported_market_cap_rank_is_warning_not_error(self):
        ranked = [
            {"symb": symbol, "excd": "NAS", "rank": str(index)}
            for index, symbol in enumerate(("NVDA", "MSFT", "AVGO", "AMD", "AMZN"), start=1)
        ]

        report = selector.select_us_candidates(
            ranking_fetcher=FakeUSFetcherWithUnsupportedCap({
                "trade": ranked,
                "power": [],
                "surge": [],
            }),
            holdings=[],
            static_candidates=US_STATIC,
            limit=20,
        )

        self.assertFalse(report["errors"])
        self.assertTrue(report["warnings"])
        self.assertTrue(all("market_cap_rank" in item for item in report["warnings"]))

    def test_us_custom_env_resolves_exchange_and_keeps_holdings(self):
        with patch.dict(os.environ, {"US_MARKET_CANDIDATE_SYMBOLS": "nvda,VRT,AVGO,VRT"}):
            report = selector.select_us_candidates(
                ranking_fetcher=FakeUSResolver({
                    "NVDA": {"exchange": "NASD", "name": "NVIDIA"},
                    "VRT": {"exchange": "NYSE", "name": "Vertiv"},
                }),
                holdings=[{"stock_code": "AAPL", "exchange": "NASD", "stock_name": "Apple", "quantity": 2}],
                static_candidates=US_STATIC,
                limit=30,
            )

        symbols = [item["symbol"] for item in report["selected"]]
        self.assertEqual(report["mode"], "custom")
        self.assertFalse(report["fallback_used"])
        self.assertEqual(symbols, ["NVDA", "VRT", "AVGO", "AAPL"])
        self.assertEqual(next(item for item in report["selected"] if item["symbol"] == "NVDA")["name"], "NVIDIA")
        self.assertEqual(next(item for item in report["selected"] if item["symbol"] == "VRT")["exchange"], "NYSE")
        self.assertEqual(next(item for item in report["selected"] if item["symbol"] == "AVGO")["exchange"], "NASD")
        self.assertTrue(any("AVGO: resolve_exchange failed" in item for item in report["warnings"]))
        self.assertNotIn("SPY", symbols)


if __name__ == "__main__":
    unittest.main()
