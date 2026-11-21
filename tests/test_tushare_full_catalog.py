"""2026-11-21 TuShare 全量 281 接口目录、官方 SDK pro_bar 桥接及新增语义工具测试。"""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from backend.app.market import stock_tools
from backend.app.market.tushare_catalog import TUSHARE_API_SET, TUSHARE_CATALOG
from backend.app.skills.registry import SkillRegistry
from backend.app.topnews.collectors.capital_flow import CapitalFlowCollector
from backend.app.topnews.collectors.futures import FuturesCollector
from backend.app.topnews.collectors.us_stock import USStockCollector


OFFICIAL_20261121_APIS = frozenset({
    "stock_basic", "stk_premarket", "trade_cal", "stock_st", "st", "stock_hsgt",
    "namechange", "stock_company", "stk_managers", "stk_rewards", "bse_mapping",
    "new_share", "bak_basic", "daily", "rt_k", "stk_mins", "rt_min", "rt_min_daily",
    "weekly", "monthly", "stk_weekly_monthly", "stk_week_month_adj", "adj_factor",
    "daily_basic", "pro_bar", "stk_limit", "suspend_d", "hsgt_top10", "bak_daily",
    "income", "income_vip", "balancesheet", "balancesheet_vip", "cashflow",
    "cashflow_vip", "forecast", "forecast_vip", "express", "express_vip",
    "dividend", "fina_indicator", "fina_indicator_vip", "fina_audit", "fina_mainbz",
    "fina_mainbz_vip", "disclosure_date", "stk_shock", "stk_high_shock", "stk_alert",
    "top10_holders", "top10_floatholders", "pledge_stat", "pledge_detail",
    "repurchase", "share_float", "block_trade", "stk_account", "stk_account_old",
    "stk_holdernumber", "stk_holdertrade", "stk_seasoned", "report_rc", "cyq_perf",
    "cyq_chips", "stk_factor", "stk_factor_pro", "stk_auction_o", "stk_auction_c",
    "stk_nineturn", "stk_ah_comparison", "stk_surv", "broker_recommend", "margin",
    "margin_detail", "margin_secs", "slb_sec", "slb_len", "slb_sec_detail",
    "slb_len_mm", "moneyflow", "moneyflow_ths", "moneyflow_dc", "moneyflow_cnt_ths",
    "moneyflow_ind_ths", "moneyflow_ind_dc", "moneyflow_mkt_dc", "top_list",
    "top_inst", "limit_list_ths", "limit_list_d", "limit_step", "limit_cpt_list",
    "ths_index", "ths_daily", "ths_member", "dc_index", "dc_member", "dc_daily",
    "stk_auction", "hm_list", "hm_detail", "ths_hot", "dc_hot", "tdx_index",
    "tdx_member", "tdx_daily", "kpl_list", "kpl_concept_cons", "etf_basic",
    "etf_index", "rt_etf_min", "rt_etf_min_daily", "etf_mins", "rt_etf_k",
    "fund_daily", "etf_auction", "fund_adj", "etf_limit", "fund_factor_pro",
    "etf_share_size", "etf_sh_cons", "etf_sz_cons", "rt_etf_sz_iopv",
    "etf_bs_sh_detail", "etf_bs_sz_detail", "index_basic", "index_daily",
    "rt_idx_k", "rt_idx_min", "rt_idx_min_daily", "index_weekly", "idx_mins",
    "index_monthly", "index_weight", "index_dailybasic", "index_classify",
    "index_member_all", "sw_daily", "rt_sw_k", "sw_mins", "ci_index_member",
    "ci_daily", "ci_index_weight_d", "index_global", "idx_factor_pro",
    "daily_info", "sz_daily_info", "vix_index", "idx_anns", "fund_basic",
    "fund_company", "fund_manager", "mkt_idx_bmk", "fund_share", "fund_nav",
    "fund_div", "fund_portfolio", "csrc_fund_new", "fut_basic", "fut_trade_cal",
    "fut_daily", "fut_daily_adj", "fut_adjfactor", "fut_weekly_monthly",
    "rt_fut_min", "rt_fut_min_daily", "fut_wsr", "fut_settle", "ft_mins",
    "fut_holding", "fut_index_daily", "fut_inv_weekly", "fut_rcpt_mat",
    "fut_trade_param", "fut_trade_param_cfx", "fut_trade_param_gfe",
    "fut_trade_param_dce", "fut_mapping", "fut_weekly_detail", "ft_limit",
    "sge_basic", "sge_daily", "opt_basic", "opt_daily", "opt_mins", "cb_basic",
    "cb_issue", "cb_call", "cb_rate", "cb_daily", "top10_cb_holders",
    "cb_factor_pro", "cb_price_chg", "cb_share", "cb_rating", "repo_daily",
    "bc_otcqt", "bc_bestotcqt", "bond_blk", "bond_blk_detail", "yc_cb", "eco_cal",
    "fx_midrate", "fx_obasic", "fx_daily", "fx_reserves", "gold_reserves",
    "sdr_reserves", "hk_basic", "hk_tradecal", "hk_daily", "hk_daily_adj",
    "hk_adjfactor", "hk_mins", "rt_hk_k", "hk_income", "hk_balancesheet",
    "hk_cashflow", "hk_fina_indicator", "us_basic", "us_tradecal", "us_daily",
    "us_daily_adj", "us_adjfactor", "us_etf_basic", "us_etf_daily", "us_income",
    "us_balancesheet", "us_cashflow", "us_fina_indicator", "cn_schedule", "shibor",
    "shibor_quote", "shibor_lpr", "libor", "hibor", "wz_index", "gz_index",
    "cn_gdp", "cn_cpi", "cn_ppi", "cn_m", "sf_month", "cn_pmi", "us_tycr",
    "us_trycr", "us_tbr", "us_tltr", "us_trltr", "us_pmi", "us_unemployment",
    "us_rs", "us_hs", "us_bp", "us_job_offers", "us_pce", "us_ppi",
    "stk_trend_kofia", "stk_funding_kofia", "fund_aum_amc_kofia",
    "fund_aum_type_kofia", "fund_flow_kofia", "fund_mmf_kofia",
    "fund_overseas_kofia", "fund_sales_kofia", "npr", "research_report",
    "monetary_policy", "news", "major_news", "cctv_news", "anns_d", "irm_qa_sh",
    "irm_qa_sz", "fintech_award", "factor_list", "factor_value", "p_list",
    "p_get", "p_save", "p_delete",
})


class TushareFullCatalogAndSdkTests(unittest.TestCase):
    def test_all_281_official_apis_are_registered_without_duplicates(self) -> None:
        self.assertEqual(len(OFFICIAL_20261121_APIS), 281)
        self.assertEqual(len(TUSHARE_CATALOG), len(TUSHARE_API_SET))
        missing = OFFICIAL_20261121_APIS - TUSHARE_API_SET
        self.assertEqual(missing, set(), f"Missing official TuShare APIs: {sorted(missing)}")

    def test_every_catalog_semantic_tool_is_registered_in_skill_registry(self) -> None:
        reg = SkillRegistry()
        reg.discover()
        unresolved = [
            (e["api_name"], e["semantic_tool"])
            for e in TUSHARE_CATALOG
            if e.get("semantic_tool") and not reg.has_tool(e["semantic_tool"])
        ]
        self.assertEqual(unresolved, [])

    def test_sdk_pro_bar_qfq_ma_factors_and_tushare_query_passthrough(self) -> None:
        daily_rows = [
            {
                "ts_code": "600519.SH",
                "trade_date": f"202611{10 + i:02d}",
                "open": 1400.0 + i * 10.0,
                "high": 1415.0 + i * 10.0,
                "low": 1390.0 + i * 10.0,
                "close": 1405.0 + i * 10.0,
                "pre_close": 1395.0 + i * 10.0,
                "change": 10.0,
                "pct_chg": 0.71,
                "vol": 20000.0 + i * 500.0,
                "amount": 2800000.0,
            }
            for i in range(6)
        ]
        adj_rows = [
            {
                "ts_code": "600519.SH",
                "trade_date": f"202611{10 + i:02d}",
                "adj_factor": 10.0 if i < 3 else 12.0,
            }
            for i in range(6)
        ]
        basic_rows = [
            {
                "ts_code": "600519.SH",
                "trade_date": f"202611{10 + i:02d}",
                "turnover_rate": 0.35 + i * 0.02,
                "volume_ratio": 1.05,
            }
            for i in range(6)
        ]

        def fake_request(api_name: str, params: dict, fields: str) -> dict:
            if api_name == "daily":
                return {"rows": daily_rows, "ms": 5}
            if api_name == "adj_factor":
                return {"rows": adj_rows, "ms": 3}
            if api_name == "daily_basic":
                return {"rows": basic_rows, "ms": 3}
            return {"rows": [], "ms": 1}

        with patch.object(stock_tools, "_tushare_request", side_effect=fake_request):
            res = stock_tools.get_pro_bar(
                "600519",
                adj="qfq",
                ma="5",
                factors="tor,vr",
                start_date="20261110",
                end_date="20261115",
            )
            self.assertNotIn("error", res)
            self.assertEqual(res["symbol"], "600519.SH")
            self.assertEqual(res["adj"], "qfq")
            self.assertEqual(res["count"], 6)
            # 确保按时间升序排列，且包含均线 ma5、换手率 turnover_rate，且 JSON 无 NaN 报错
            self.assertEqual(res["bars"][0]["trade_date"], "20261110")
            self.assertEqual(res["bars"][-1]["trade_date"], "20261115")
            self.assertIsNotNone(res["bars"][-1]["ma5"])
            self.assertIn("turnover_rate", res["bars"][-1])
            json.dumps(res, allow_nan=False)

            # 同时验证通用透传 tushare_query("pro_bar", ...) 自动走 SDK
            q_res = stock_tools.tushare_query(
                "pro_bar",
                params={"ts_code": "510300", "adj": "hfq", "ma": [5]},
            )
            self.assertNotIn("error", q_res)
            self.assertEqual(q_res["source"], "tushare.pro_bar")
            self.assertEqual(q_res["count"], 6)
            json.dumps(q_res, allow_nan=False)

    def test_sdk_pro_bar_surfaces_upstream_error_cleanly(self) -> None:
        with patch.object(
            stock_tools,
            "_tushare_request",
            return_value={"error": "TuShare 返回错误: 抱歉，您每分钟最多访问该接口200次"},
        ):
            res = stock_tools.get_pro_bar("600519.SH")
            self.assertIn("error", res)
            self.assertIn("每分钟最多访问", res["error"])

    def test_new_semantic_tools_and_vip_passthrough_execute_cleanly(self) -> None:
        captured: list[tuple[str, dict]] = []

        def fake_req(api_name: str, params: dict, fields: str) -> dict:
            captured.append((api_name, dict(params)))
            return {"rows": [{"api": api_name, "ok": 1}], "ms": 2}

        with patch.object(stock_tools, "_tushare_request", side_effect=fake_req):
            self.assertEqual(stock_tools.get_stk_seasoned("600036")["count"], 1)
            self.assertEqual(stock_tools.get_fx_midrate(trade_date="20261120")["count"], 1)
            self.assertEqual(stock_tools.get_vix_index(trade_date="20261120")["count"], 1)
            self.assertEqual(stock_tools.get_us_macro("us_pce", start_m="202501", end_m="202610")["count"], 1)
            self.assertEqual(stock_tools.get_cn_gdp(q="2026Q3")["count"], 1)
            self.assertEqual(stock_tools.get_cn_ppi(start_m="202601", end_m="202610")["count"], 1)
            self.assertEqual(stock_tools.get_cn_pmi(start_m="202601", end_m="202610")["count"], 1)
            self.assertEqual(
                stock_tools.tushare_query("income_vip", params={"period": "20260930"})["count"],
                1,
            )
            self.assertEqual(
                stock_tools.tushare_query("stk_trend_kofia", params={"start_date": "20261101"})["count"],
                1,
            )

        called_apis = [a for a, _ in captured]
        for expected in (
            "stk_seasoned",
            "fx_midrate",
            "vix_index",
            "us_pce",
            "cn_gdp",
            "cn_ppi",
            "cn_pmi",
            "income_vip",
            "stk_trend_kofia",
        ):
            self.assertIn(expected, called_apis)

    def test_topnews_collectors_fetch_futures_vix_and_fx_midrate(self) -> None:
        def fake_req(api_name: str, params: dict, fields: str) -> dict:
            if api_name == "fut_daily" and params.get("ts_code") == "IF.CFX":
                return {
                    "rows": [{"ts_code": "IF.CFX", "trade_date": "20261120", "close": 3980.0, "pre_settle": 3950.0, "change1": 30.0}]
                }
            if api_name == "sge_daily":
                return {
                    "rows": [{"ts_code": "Au99.99", "trade_date": "20261120", "close": 615.5, "change": 4.2, "pct_change": 0.69}]
                }
            if api_name == "index_global":
                return {
                    "rows": [{"ts_code": "SPX", "trade_date": "20261120", "close": 5920.0, "change": 20.0, "pct_chg": 0.34}]
                }
            if api_name == "vix_index":
                return {
                    "rows": [{"trade_date": "20261120", "close": 15.82, "change": -0.65, "pct_chg": -3.95}]
                }
            if api_name == "moneyflow_mkt_dc":
                return {
                    "rows": [{"trade_date": "20261120", "net_amount": -12500000000.0, "net_amount_rate": -1.8}]
                }
            if api_name == "fx_midrate":
                return {
                    "rows": [{"trade_date": "20261120", "usd": 7.1856}]
                }
            return {"rows": []}

        async def _run() -> tuple:
            fut = FuturesCollector()
            us = USStockCollector()
            cap = CapitalFlowCollector()
            try:
                return await fut.collect(), await us.collect(), await cap.collect()
            finally:
                await fut.close()
                await us.close()
                await cap.close()

        with patch.object(stock_tools, "_tushare_request", side_effect=fake_req):
            fut_data, us_data, cap_data = asyncio.run(_run())

        self.assertEqual({it["code"] for it in fut_data.items}, {"IF.CFX", "Au99.99"})
        self.assertEqual({it["code"] for it in us_data.items}, {"SPX", "VIX"})
        self.assertEqual(
            {it["name"] for it in cap_data.items},
            {"两市主力净流向", "美元兑人民币中间价"},
        )


if __name__ == "__main__":
    unittest.main()
