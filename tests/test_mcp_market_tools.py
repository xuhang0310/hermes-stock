from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.app.mcp_gateway import market_tools
from scripts.mcp_access import validate_url


class McpMarketToolsTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch("pymysql.connections.Connection.connect", side_effect=AssertionError("unexpected DB")))

    def test_quote_preserves_source_date_and_stale_flags(self):
        quote = {"symbol": "600036.SH", "trade_date": "20261029", "expected_trade_date": "20261030",
                 "stale": True, "intraday_unavailable": True, "source": "cache"}
        with patch.object(market_tools.stock_tools, "get_latest_price", return_value=quote) as latest:
            result = market_tools.get_stock_quote("600036")
        latest.assert_called_once_with("600036.SH")
        self.assertEqual(result, quote)

    def test_ambiguous_names_and_invalid_codes_are_not_used_as_symbols(self):
        with patch.object(market_tools.stock_tools, "get_latest_price") as latest:
            for symbol in ("银行", "900001.SH", "x", "123456.SZ"):
                with self.subTest(symbol=symbol), self.assertRaises(ValueError):
                    market_tools.get_stock_quote(symbol)
            latest.assert_not_called()

    def test_stock_evidence_preserves_partial_failures_and_freshness(self):
        quote = {"price": 38, "trade_date": "20261029", "stale": True}
        with patch.object(market_tools.stock_tools, "get_latest_price", return_value=quote), \
             patch.object(market_tools.stock_tools, "analyze_daily_bars", return_value={"error": "日线不足"}), \
             patch.object(market_tools.stock_tools, "get_stock_info", return_value={"financial": {}}):
            result = market_tools.analyze_stock("600036.SH")
        self.assertEqual(result["quote"], quote)
        self.assertEqual(result["data_gaps"], [{"dataset": "technical", "error": "日线不足"}])
        self.assertNotIn("buy", result)

    def test_market_summary_is_bounded_and_retains_quality_warnings(self):
        snapshot = {"overview": {
            "as_of": "2026-10-29", "expected_date": "2026-10-30", "coverage_status": "partial",
            "warnings": ["部分数据缺失"], "coverage": {"bar_ratio": 0.7}, "snapshot_id": "snapshot",
            "industries": [{"industry": "A", "avg_pct": 3}, {"industry": "B", "avg_pct": -2},
                           {"industry": "C", "avg_pct": None}, {"industry": "D", "avg_pct": 1}],
        }, "members": [{"symbol": "600036.SH"}]}
        with patch.object(market_tools.industry_service, "load_snapshot", return_value=snapshot):
            result = market_tools.analyze_market(1)
        self.assertEqual(result["leading_industries"][0]["industry"], "A")
        self.assertEqual(result["lagging_industries"][0]["industry"], "B")
        self.assertEqual(result["warnings"], snapshot["overview"]["warnings"])
        self.assertEqual(result["industry_count"], 4)
        self.assertNotIn("members", result)
        self.assertEqual(len(snapshot["overview"]["industries"]), 4)

    def test_access_helper_requires_secure_url(self):
        self.assertEqual(validate_url("https://shijingxuan.com/"), "https://shijingxuan.com")
        self.assertEqual(validate_url("http://127.0.0.1:8000"), "http://127.0.0.1:8000")
        for url in ("http://example.com", "https://user:pass@example.com", "https://example.com/?token=secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)


if __name__ == "__main__":
    unittest.main()
