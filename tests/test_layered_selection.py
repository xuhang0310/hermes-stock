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
    compute_l7_position_sizing,
    inspect_single_stock_layered,
    query_layered_selection,
    update_shadow_nav_comparison,
)
from backend.app.quant.layered_selection_api import router as selection_router
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

        # 2) 全覆盖高分股：正常进入 action（动手）
        strong = combine_layers_with_shrinkage(
            {"l2_industry": 82.0, "l3_fundamental": 78.0, "l4_expectation": 80.0, "l5_timing": 90.0, "l6_event": 75.0},
            plan_type=PLAN_SWING,
            veto_codes=[],
        )
        self.assertGreaterEqual(strong["total_score"], 70.0)
        self.assertEqual(strong["verdict"], VERDICT_ACTION)

        # 3) 全部 95 分但触发立案调查一票否决 -> 强制打入 pass（放弃）！
        vetoed = combine_layers_with_shrinkage(
            {"l2_industry": 95.0, "l3_fundamental": 95.0, "l4_expectation": 95.0, "l5_timing": 95.0, "l6_event": 0.0},
            plan_type=PLAN_SWING,
            veto_codes=["event_critical_veto"],
        )
        self.assertTrue(vetoed["vetoed"])
        self.assertEqual(vetoed["verdict"], VERDICT_PASS)

    def test_l7_position_sizing_respects_board_lots_budget_and_l1_cap(self) -> None:
        # 科创板 688 最小 200 股：若股价 180 元，账户仅 5 万元，200 股 = 3.6万 超出单票上限 -> 诚实拒买
        star_expensive = compute_l7_position_sizing(
            symbol="688001.SH",
            entry_price=180.0,
            stop_ref_price=165.0,
            atr14=7.0,
            capital=50_000.0,
            l1_max_position_ratio=0.60,
        )
        self.assertFalse(star_expensive["eligible"])
        self.assertEqual(star_expensive["advised_shares"], 0)

        # 主板正常标的：20 万资金，1% 单笔风险（2000元），买入价 20 元，证伪价 18 元（每股风险 2 元）
        # 风险股数 = 2000 / 2 = 1000 股（市值 2万 = 10% <= 单票20% 且 <= L1 60%）
        normal = compute_l7_position_sizing(
            symbol="600036.SH",
            entry_price=20.0,
            stop_ref_price=18.0,
            atr14=1.0,
            capital=200_000.0,
            risk_per_trade=0.01,
            l1_max_position_ratio=0.60,
        )
        self.assertTrue(normal["eligible"])
        self.assertEqual(normal["advised_shares"], 1000)
        self.assertAlmostEqual(normal["max_loss_amount"], 2000.0)

        # L1 防守期（上限 30% = 6 万），若当前已持仓 5.9 万 -> 剩余额度不足 1 手，拦截新开仓！
        capped = compute_l7_position_sizing(
            symbol="600036.SH",
            entry_price=20.0,
            stop_ref_price=18.0,
            atr14=1.0,
            capital=200_000.0,
            current_invested=59_000.0,
            l1_max_position_ratio=0.30,
        )
        self.assertFalse(capped["eligible"])
        self.assertEqual(capped["advised_shares"], 0)
        self.assertIn("L1", capped["reason"])

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

            # 构建前一日与当日快照以验证影子净值对账
            prev_day = days[-2]
            for idx in range(5):
                db.add(StockModelSignal(symbol=f"6000{idx:02d}.SH", trade_date=prev_day, model_rank=0.90 - idx * 0.05, buy_k=0.5))
            db.commit()
            build_daily_selection_snapshots(db, trade_date=prev_day)
            built = build_daily_selection_snapshots(db, trade_date=target)
            nav = update_shadow_nav_comparison(db, target)
            db.commit()

        self.assertEqual(built["universe"], 11)
        self.assertEqual(built["regime"]["stance"], "attack")
        self.assertEqual(built["regime"]["max_position_ratio"], 0.90)
        self.assertIn("model-rank-30", nav)
        self.assertIn("layered-swing-30", nav)

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

        r_nav = client.get("/api/quant/selection/shadow-nav", headers=headers)
        self.assertEqual(r_nav.status_code, 200)
        self.assertIn("layered-swing-30", r_nav.json()["data"]["series"])

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
