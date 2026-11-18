"""全站统一 L1–L7 分层量化选股架构端到端单元测试（SQLite 内存库，零外部网络依赖）。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth.auth import create_access_token
from backend.app.core.database import Base
from backend.app.core.models import (
    DailyBar,
    IndexDailyBar,
    Instrument,
    StockDailyBasic,
    StockModelSignal,
    TushareFactorValue,
    User,
)
from backend.app.market.event_signal_sync import (
    load_pit_event_signals,
    record_public_event_signals,
)
from backend.app.market.financial_pit_sync import (
    load_pit_financials,
    upsert_financial_pit_rows,
)
from backend.app.market.index_sync import upsert_index_bars
from backend.app.quant import expectation_gap, layered_selection, market_regime
from backend.app.quant.layered_selection import (
    PLAN_LONG,
    PLAN_SWING,
    VERDICT_ACTION,
    VERDICT_PASS,
    VERDICT_WATCH,
    build_daily_selection_snapshots,
    combine_layers_with_shrinkage,
    compute_execution_levels,
    compute_l7_position_sizing,
    inspect_single_stock_layered,
    query_layered_selection,
)
from backend.app.quant.layered_selection_api import router as selection_router
from backend.app.quant.order_engine import load_table as load_order_table, model_buy_price, plan_orders
from backend.app.quant.tech_signal import Bar
from backend.app.shadow import rules as shadow_rules
from backend.app.skills.registry import SkillRegistry


def _trading_days(count: int, end_compact: str = "20260904") -> list[str]:
    cur = datetime.strptime(end_compact, "%Y%m%d").date()
    days: list[str] = []
    while len(days) < count:
        if cur.weekday() < 5:
            days.append(cur.strftime("%Y%m%d"))
        cur -= timedelta(days=1)
    return list(reversed(days))


class LayeredSelectionArchitectureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        for target in (
            "backend.app.core.database.SessionLocal",
            "backend.app.quant.market_regime.SessionLocal",
            "backend.app.quant.layered_selection.SessionLocal",
            "backend.app.quant.layered_selection_api.SessionLocal",
            "backend.app.quant.expectation_gap.SessionLocal",
        ):
            p = patch(target, self.SessionFactory)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.engine.dispose)

    def test_pit_financial_strictly_prevents_lookahead_and_computes_acceleration(self) -> None:
        rows = [
            # Q3 2025 财报，2025-10-25 披露
            {
                "ts_code": "600001.SH",
                "end_date": "20250930",
                "ann_date": "20251025",
                "roe": 12.0,
                "q_sales_yoy": 10.0,
                "q_profit_yoy": 15.0,
            },
            # 2025 年报（end_date=20251231），真实披露日是 2026-04-20！
            {
                "ts_code": "600001.SH",
                "end_date": "20251231",
                "ann_date": "20260420",
                "roe": 18.5,
                "q_sales_yoy": 24.0,
                "q_profit_yoy": 35.0,
            },
            # 脏数据：ann_date 早于 end_date，必须拒收
            {
                "ts_code": "600002.SH",
                "end_date": "20251231",
                "ann_date": "20251101",
                "roe": 99.0,
            },
        ]
        with self.SessionFactory() as db:
            written = upsert_financial_pit_rows(db, rows)
            db.commit()
            self.assertEqual(written, 2)

            # 在 2026-03-01 查询：绝对不能偷看到 2026-04-20 才披露的 2025 年报！
            march_view = load_pit_financials(db, as_of_date="20260301")
            self.assertEqual(march_view["600001.SH"]["end_date"], "20250930")
            self.assertEqual(march_view["600001.SH"]["roe"], 12.0)
            self.assertNotIn("600002.SH", march_view)

            # 在 2026-04-21 查询：可以看到 2025 年报，且加速度 = 35.0 - 15.0 = +20.0
            april_view = load_pit_financials(db, as_of_date="20260421")
            self.assertEqual(april_view["600001.SH"]["end_date"], "20251231")
            self.assertAlmostEqual(april_view["600001.SH"]["profit_yoy_accel"], 20.0)
            self.assertAlmostEqual(april_view["600001.SH"]["tr_yoy_accel"], 14.0)

            # 验证付费 TuShare 因子库 PIT 截面融合
            db.add(TushareFactorValue(factor_name="roe_ttm", symbol="600003.SH", trade_date="20260420", factor_value=0.21))
            db.add(TushareFactorValue(factor_name="sa", symbol="600003.SH", trade_date="20260420", factor_value=0.65))
            db.commit()
            fused = load_pit_financials(db, as_of_date="20260421")
            self.assertAlmostEqual(fused["600003.SH"]["roe"], 21.0)
            self.assertAlmostEqual(fused["600003.SH"]["tr_yoy_accel"], 0.65)

    def test_public_event_signals_screen_non_held_stocks_with_pit_gate(self) -> None:
        anns = [
            {"ts_code": "600088.SH", "ann_date": "20260902", "title": "关于收到中国证监会立案告知书的公告"},
            {"ts_code": "600099.SH", "ann_date": "20260906", "title": "关于公司股票可能被实施退市风险警示的提示性公告"},
            {"ts_code": "600066.SH", "ann_date": "20260903", "title": "关于以集中竞价交易方式回购公司股份方案的公告"},
        ]
        with self.SessionFactory() as db:
            inserted = record_public_event_signals(db, anns)
            db.commit()
            self.assertEqual(inserted, 3)

            # 幂等重跑不重复插入
            self.assertEqual(record_public_event_signals(db, anns), 0)

            # 20260904 截面只能看到 <=20260904 的公告，看不到 20260906 的未来退市公告
            pit = load_pit_event_signals(db, as_of_date="20260904")
            self.assertIn("600088.SH", pit)
            self.assertEqual(pit["600088.SH"][0]["polarity"], "veto")
            self.assertIn("600066.SH", pit)
            self.assertEqual(pit["600066.SH"][0]["polarity"], "positive")
            self.assertNotIn("600099.SH", pit)

    def test_bayesian_shrinkage_blocks_low_coverage_garbage_and_veto_overrides_high_score(self) -> None:
        # 1) 冷门缺数据股：只有 L2 行业拿了 98 分，其余全缺 -> 覆盖率仅 0.15，收缩后决不能进 action！
        sparse = combine_layers_with_shrinkage(
            {"l2_industry": 98.0, "l3_fundamental": None, "l4_expectation": None, "l5_timing": None, "l6_event": None},
            plan_type=PLAN_SWING,
            veto_codes=[],
        )
        self.assertLess(sparse["coverage_ratio"], 0.20)
        self.assertLess(sparse["total_score"], 62.0)
        self.assertEqual(sparse["verdict"], VERDICT_WATCH)

        # 2) 全覆盖高分股：正常进入 action（高分）
        strong = combine_layers_with_shrinkage(
            {"l2_industry": 82.0, "l3_fundamental": 78.0, "l4_expectation": 80.0, "l5_timing": 90.0, "l6_event": 75.0},
            plan_type=PLAN_SWING,
            veto_codes=[],
        )
        self.assertGreaterEqual(strong["total_score"], 70.0)
        self.assertEqual(strong["verdict"], VERDICT_ACTION)

        # 3) 全部 95 分但触发立案调查一票否决 -> 强制打入 pass（低分或排除）
        vetoed = combine_layers_with_shrinkage(
            {"l2_industry": 95.0, "l3_fundamental": 95.0, "l4_expectation": 95.0, "l5_timing": 95.0, "l6_event": 0.0},
            plan_type=PLAN_SWING,
            veto_codes=["event_critical_veto"],
        )
        self.assertTrue(vetoed["vetoed"])
        self.assertEqual(vetoed["verdict"], VERDICT_PASS)

    def test_l7_uses_the_plan_sizing_rule_and_skips_held_or_unpriced_stocks(self) -> None:
        # 不指定账户按 8 只持股：风险预算 1%、单只上限 25%。20 万预算、收盘 20 元、ATR 1 元：
        # 风险股数 = 2000 ÷ (2 × 1) = 1000 股，上限 = 50000 ÷ 20 = 2500 股，排名 0.5 不调整
        normal = compute_l7_position_sizing(
            symbol="600036.SH", entry_price=19.8, close=20.0, atr14=1.0, model_rank=0.5, capital=200_000.0,
        )
        self.assertTrue(normal["eligible"])
        self.assertEqual(normal["advised_shares"], 1000)
        self.assertEqual(normal["advised_shares"], int(shadow_rules.target_shares(200_000.0, 20.0, 1.0) // 100) * 100)
        self.assertAlmostEqual(normal["max_loss_amount"], 2000.0)
        self.assertNotIn("l1_max_position_ratio", normal)

        # 账户里连这只共 4 只：和计划一样放宽到 2%，排名 0.9 再乘 1.4 -> 2000 × 1.4 = 2800 股
        few = compute_l7_position_sizing(
            symbol="600036.SH", entry_price=19.8, close=20.0, atr14=1.0, model_rank=0.9, capital=200_000.0, stock_count=4,
        )
        self.assertEqual(few["advised_shares"], 2800)
        self.assertEqual(few["risk_budget_pct"], 0.02)

        # 买入模型说不挂：不给股数，理由沿用执行层的说明
        no_buy = compute_l7_position_sizing(
            symbol="600036.SH", entry_price=None, close=20.0, atr14=1.0, model_rank=0.5, capital=200_000.0,
            no_buy_reason="买入模型今天判断不挂买单，不给买价",
        )
        self.assertFalse(no_buy["eligible"])
        self.assertEqual(no_buy["reason"], "买入模型今天判断不挂买单，不给买价")

        # 已经持有：以操作计划的量化股数为准，这里不另算
        held = compute_l7_position_sizing(
            symbol="600036.SH", entry_price=19.8, close=20.0, atr14=1.0, model_rank=0.5, capital=200_000.0, held_shares=1500,
        )
        self.assertFalse(held["eligible"])
        self.assertIn("操作计划", held["reason"])

        # 科创板最少 200 股：180 元、5 万预算算出来不到 200 股，不买
        star = compute_l7_position_sizing(
            symbol="688001.SH", entry_price=179.0, close=180.0, atr14=7.0, model_rank=0.5, capital=50_000.0,
        )
        self.assertFalse(star["eligible"])
        self.assertEqual(star["advised_shares"], 0)

    def test_execution_levels_use_the_plan_engine_and_never_invent_a_buy_price(self) -> None:
        days = _trading_days(80, "20260904")
        closes = [20.0 + (i % 4) * 0.05 for i in range(len(days))]
        bars = [Bar(trade_date=d, high=c + 0.25, low=c - 0.25, close=c) for d, c in zip(days, closes)]
        table = load_order_table()
        levels = plan_orders(bars, table, limit_ratio=0.1) if table is not None else None
        ref_close = levels.close if levels is not None else bars[-1].close

        priced = compute_execution_levels(bars, symbol="600036.SH", name="招商银行", buy_k=0.5, order_table=table)
        atr = levels.atr if levels is not None else priced["atr14"]
        self.assertEqual(priced["suggested_buy_price"], model_buy_price(ref_close, atr, 0.5, 0.1))
        # 止损只给下限，和计划同一个价，不比 3 倍 ATR 更近
        if levels is not None and levels.stop is not None:
            self.assertEqual(priced["stop_ref_price"], levels.stop.price)
        self.assertLessEqual(priced["stop_ref_price"], ref_close - 3.0 * atr + 0.011)

        refused = compute_execution_levels(bars, symbol="600036.SH", name="招商银行", buy_k=None, order_table=table)
        self.assertIsNone(refused["suggested_buy_price"])
        self.assertIn("不挂", refused["buy_basis"])
        silent = compute_execution_levels(
            bars, symbol="600036.SH", name="招商银行", buy_k=None, order_table=table, has_signal=False,
        )
        self.assertIsNone(silent["suggested_buy_price"])
        self.assertIn("没有买入模型信号", silent["buy_basis"])

    def test_end_to_end_snapshot_builder_downtrend_veto_and_api_endpoints(self) -> None:
        days = _trading_days(65, "20260904")
        target = days[-1]

        with self.SessionFactory() as db:
            # 写入沪深300与中证1000宽基多头日线
            for idx_sym in ("000300.SH", "000852.SH"):
                upsert_index_bars(
                    db,
                    [{"ts_code": idx_sym, "trade_date": d, "close": 3500.0 + i * 5.0} for i, d in enumerate(days)],
                )

            # 构造 12 只样本股：
            # - 600000.SH ~ 600008.SH：稳健上升 + 高 model_rank + 优质财报
            # - 600009.SH：深跌破位飞刀股（跌破 MA60 超 15% 且 20 日跌超 18%），测波段 downtrend_knife 否决
            # - 600010.SH：高分但收到证监会立案调查，测 event_critical_veto 否决
            for idx in range(11):
                sym = f"6000{idx:02d}.SH"
                db.add(
                    Instrument(
                        symbol=sym,
                        name=f"样本{idx:02d}",
                        market="SH",
                        type="stock",
                        list_status="L",
                        list_date="20200101",
                        industry="半导体" if idx < 6 else "银行",
                    )
                )
                db.flush()
                prev_px: float | None = None
                for d_i, d in enumerate(days):
                    if idx == 9:
                        # 前40天在 20 元，后25天暴跌至 13 元（MA60 约 18.2，MA20 约 15.2，跌破 MA60 且空头排列）
                        px = 20.0 if d_i < 40 else 20.0 - (d_i - 39) * 0.28
                    else:
                        px = 15.0 + d_i * (0.08 + idx * 0.01)
                    pre_c = prev_px if prev_px is not None else px * 0.996
                    pct_c = (px / pre_c - 1.0) * 100.0
                    prev_px = px
                    db.add(
                        DailyBar(
                            symbol=sym,
                            trade_date=d,
                            open=px * 0.995,
                            high=px * 1.015,
                            low=px * 0.985,
                            close=px,
                            pre_close=pre_c,
                            change_pct=pct_c,
                            volume=50_000.0,
                            amount=80_000.0,  # 8000 万元
                            source="tushare",
                        )
                    )
                db.add(
                    StockDailyBasic(
                        symbol=sym,
                        trade_date=target,
                        pe_ttm=15.0 + idx,
                        pb=1.8 + idx * 0.1,
                        total_mv=1_500_000.0,  # 150 亿元
                        turnover_rate=2.2,
                        turnover_rate_f=2.5,
                    )
                )
                db.add(
                    StockModelSignal(
                        symbol=sym,
                        trade_date=target,
                        model_rank=0.96 if idx in (0, 1, 9, 10) else 0.55,
                        buy_k=0.5,
                    )
                )
                upsert_financial_pit_rows(
                    db,
                    [
                        {
                            "ts_code": sym,
                            "end_date": "20260331",
                            "ann_date": "20260425",
                            "roe": 16.0 - idx * 0.5,
                            "grossprofit_margin": 38.0,
                            "debt_to_assets": 35.0,
                            "q_sales_yoy": 15.0,
                            "q_profit_yoy": 20.0,
                        },
                        {
                            "ts_code": sym,
                            "end_date": "20260630",
                            "ann_date": "20260820",
                            "roe": 18.0 - idx * 0.4,
                            "grossprofit_margin": 40.0,
                            "debt_to_assets": 34.0,
                            "q_sales_yoy": 25.0,
                            "q_profit_yoy": 36.0,
                        },
                    ],
                )

            # 给 600010.SH 注入立案调查公告
            record_public_event_signals(
                db,
                [{"ts_code": "600010.SH", "ann_date": "20260903", "title": "关于收到证监会立案调查通知书的公告"}],
            )
            db.commit()

            built = build_daily_selection_snapshots(db, trade_date=target)
            db.commit()

        self.assertEqual(built["universe"], 11)
        self.assertEqual(built["regime"]["stance"], "attack")
        self.assertEqual(built["regime"]["max_position_ratio"], 0.90)

        # 验证飞刀股 600009.SH 在 swing 被 downtrend_knife 一票否决，而在 long 未触发飞刀否决
        knife_detail = inspect_single_stock_layered("600009.SH", trade_date=target)
        self.assertTrue(knife_detail["plans"][PLAN_SWING]["vetoed"])
        self.assertIn("downtrend_knife", knife_detail["plans"][PLAN_SWING]["veto_codes"])
        self.assertEqual(knife_detail["plans"][PLAN_SWING]["verdict"], VERDICT_PASS)
        self.assertFalse(knife_detail["plans"][PLAN_LONG]["vetoed"])

        # 验证立案股 600010.SH 在全部周期均被 event_critical_veto 否决
        filing_detail = inspect_single_stock_layered("600010.SH", trade_date=target)
        for plan_key in ("swing", "long", "event"):
            self.assertTrue(filing_detail["plans"][plan_key]["vetoed"])
            self.assertIn("event_critical_veto", filing_detail["plans"][plan_key]["veto_codes"])
            self.assertEqual(filing_detail["plans"][plan_key]["verdict"], VERDICT_PASS)

        # 验证真实 JWT 鉴权下 API 路由全链路返回
        with self.SessionFactory() as db:
            db.add(
                User(
                    username="quant-tester",
                    display_name="量化测试",
                    role="admin",
                    permission_scope="all",
                    password_hash="test",
                )
            )
            db.commit()
            user = db.scalar(select(User).where(User.username == "quant-tester"))

        app = FastAPI()
        app.include_router(selection_router)
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {create_access_token(user)}"}

        r_regime = client.get("/api/quant/selection/regime", headers=headers)
        self.assertEqual(r_regime.status_code, 200)
        self.assertEqual(r_regime.json()["data"]["max_position_ratio"], 0.90)

        r_query = client.post(
            "/api/quant/selection/query",
            headers=headers,
            json={"plan_type": "swing", "verdict_filter": "action", "capital": 300000},
        )
        self.assertEqual(r_query.status_code, 200)
        items = r_query.json()["data"]["items"]
        self.assertGreaterEqual(len(items), 1)
        self.assertEqual(items[0]["symbol"], "600000.SH")
        self.assertGreater(items[0]["l7_risk"]["advised_shares"], 0)

        r_stock = client.get("/api/quant/selection/stock/600000.SH", headers=headers)
        self.assertEqual(r_stock.status_code, 200)
        self.assertIn("swing", r_stock.json()["data"]["plans"])
        # 没有回测的打分只说分数高低
        self.assertEqual(r_stock.json()["data"]["plans"]["swing"]["verdict_label"], "高分")

        # 偏乐观的前瞻净值对比已经去掉
        self.assertEqual(client.get("/api/quant/selection/shadow-nav", headers=headers).status_code, 404)

    def test_size_neutral_l5_prevents_microcap_dominance_and_enforces_liquidity_gate(self) -> None:
        # 构造 16 只 >=300 亿大盘股（大盘股原始 model_rank 普遍偏低：0.20~0.68），其中 600900.SH 为大盘最优 0.68
        items = []
        for i in range(16):
            items.append(
                {
                    "symbol": f"6009{i:02d}.SH",
                    "name": f"大盘龙头{i}",
                    "market": "SH",
                    "industry": "电力",
                    "list_status": "L",
                    "list_date": "20150101",
                    "trade_date": "20261117",
                    "close": 25.0,
                    "pct_chg": 0.5,
                    "volume": 500_000.0,
                    "avg_amount_20": 350_000.0,  # 3.5 亿元
                    "amt_ratio_5_20": 0.75,      # 温和缩量回踩
                    "ret_20": 0.02,
                    "vol_20": 0.015,
                    "ma20": 24.8,
                    "ma60": 23.5,
                    "ma60_slope": 0.015,
                    "atr14": 0.5,
                    "pe_ttm": 12.0 + i,
                    "pb": 1.6,
                    "total_mv_yi": 500.0,
                    "turnover_rate": 1.2,
                    "model_rank": 0.68 if i == 0 else (0.20 + i * 0.025),
                    "buy_k": 0.5,
                    "suggested_buy_price": 24.75,
                    "stop_ref_price": 24.0,
                    "buy_basis": "测试",
                    "fin": {"roe": 18.0 - i * 0.5, "tr_yoy_accel": 15.0 - i * 0.5, "profit_yoy_accel": 20.0 - i * 0.5},
                    "events": [],
                }
            )
        # 再塞入一只成交额仅 3000 万元（低于 4500 万高分门槛）的 25 亿冷门微盘股，哪怕原始 model_rank=0.99 也最高只到中等
        items.append(
            {
                **items[0],
                "symbol": "002999.SZ",
                "name": "冷门微盘",
                "total_mv_yi": 25.0,
                "avg_amount_20": 30_000.0,  # 3000 万 < 4500 万高分门槛
                "model_rank": 0.99,
            }
        )
        scored = layered_selection.score_cross_section_inputs(items, plan_types=(PLAN_SWING,))[PLAN_SWING]
        by_sym = {r["symbol"]: r for r in scored}
        # 大盘股 600900.SH 虽然原始 model_rank 只有 0.68，但在 >=300 亿同温层排第 1 + 多头缩量回踩满分，L5 高分进入 action！
        self.assertGreater(by_sym["600900.SH"]["l5_timing_score"], 90.0)
        self.assertEqual(by_sym["600900.SH"]["verdict"], VERDICT_ACTION)
        # 冷门微盘日均成交不足 4500 万元，被机构流动性门禁拦截在 watch（观察）
        self.assertEqual(by_sym["002999.SZ"]["verdict"], VERDICT_WATCH)

    def test_industry_neutral_l3_and_sector_crowding_guard(self) -> None:
        base = {
            "market": "SH",
            "list_status": "L",
            "list_date": "20160101",
            "trade_date": "20261117",
            "close": 25.0,
            "pct_chg": 0.6,
            "volume": 600_000.0,
            "avg_amount_20": 400_000.0,
            "amt_ratio_5_20": 0.75,
            "ret_20": 0.03,
            "vol_20": 0.016,
            "ma20": 24.8,
            "ma60": 23.5,
            "ma60_slope": 0.015,
            "atr14": 0.5,
            "total_mv_yi": 450.0,
            "turnover_rate": 1.5,
            "buy_k": 0.5,
            "suggested_buy_price": 24.75,
            "stop_ref_price": 24.0,
            "buy_basis": "测试",
            "events": [],
        }
        items = []
        # 12 只银行股：绝对 PE=5~6、PB=0.5~0.6 极低
        for i in range(12):
            items.append(
                {
                    **base,
                    "symbol": f"6010{i:02d}.SH",
                    "name": f"银行{i}",
                    "industry": "银行",
                    "pe_ttm": 5.0 + i * 0.1,
                    "pb": 0.50 + i * 0.02,
                    "model_rank": 0.85 - i * 0.015,
                    "fin": {
                        "roe": 11.5 - i * 0.2,
                        "grossprofit_margin": 42.0 - i * 0.3,
                        "debt_to_assets": 91.0 + i * 0.1,
                        "tr_yoy_accel": 5.0 - i * 0.2,
                        "profit_yoy_accel": 6.0 - i * 0.2,
                    },
                }
            )
        # 12 只半导体股：龙头 PE=26、PB=3.6，但 ROE=22%、业绩加速度领先
        for i in range(12):
            items.append(
                {
                    **base,
                    "symbol": f"6881{i:02d}.SH",
                    "name": f"芯片{i}",
                    "industry": "半导体",
                    "pe_ttm": 26.0 + i * 4.0,
                    "pb": 3.6 + i * 0.4,
                    "model_rank": 0.84 - i * 0.02,
                    "fin": {
                        "roe": 22.0 - i * 1.1,
                        "grossprofit_margin": 56.0 - i * 1.5,
                        "debt_to_assets": 22.0 + i * 2.0,
                        "tr_yoy_accel": 22.0 - i * 1.5,
                        "profit_yoy_accel": 28.0 - i * 2.0,
                    },
                }
            )
        # 10 只工程机械股：凑齐 >=30 只跨行业截面以触发行业集中度防霸榜软限
        for i in range(10):
            items.append(
                {
                    **base,
                    "symbol": f"6003{i:02d}.SH",
                    "name": f"机械{i}",
                    "industry": "工程机械",
                    "pe_ttm": 16.0 + i * 2.0,
                    "pb": 2.0 + i * 0.2,
                    "model_rank": 0.82 - i * 0.02,
                    "fin": {
                        "roe": 16.0 - i * 0.8,
                        "grossprofit_margin": 32.0 - i * 1.0,
                        "debt_to_assets": 45.0 + i * 1.5,
                        "tr_yoy_accel": 18.0 - i * 1.2,
                        "profit_yoy_accel": 20.0 - i * 1.5,
                    },
                }
            )
        scored = layered_selection.score_cross_section_inputs(items, plan_types=(PLAN_SWING,))[PLAN_SWING]
        by_sym = {r["symbol"]: r for r in scored}
        # 半导体龙头 688100.SH 在行业内估值/ROE/毛利均居首，L3 质地分应显著高于 70（不被银行 PE=5 压垮）
        self.assertGreater(by_sym["688100.SH"]["l3_fundamental_score"], 72.0)
        # 前 6 名绝不能被单一行业（如银行）全数垄断
        top6_industries = [r["industry"] for r in scored[:6]]
        self.assertLessEqual(top6_industries.count("银行"), 3)
        self.assertGreaterEqual(len(set(top6_industries)), 2)

    def test_skill_registry_registers_unified_quant_selection_tools(self) -> None:
        registry = SkillRegistry()
        registry.discover()
        for mode in ("research", "stock"):
            names = {
                t["function"]["name"]
                for t in registry.get_tools_for(mode, role_code="quant-analyst")
            }
            self.assertIn("get_market_regime_l1", names)
            self.assertIn("query_unified_quant_selection", names)
            self.assertIn("inspect_stock_layered_l1_l7", names)


if __name__ == "__main__":
    unittest.main()
