"""自动选股白话呈现层：维度分、入选理由、风险提示、选出后表现、历史战绩与状态文案。

默认用 SQLite 内存库；设置 STOCK_AUTOMATIC_SELECTION_TEST_DATABASE_URL 时改在隔离 MySQL 上跑。
"""
from __future__ import annotations

import copy
import json
import os
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth.auth import AuthContext, get_current_user
from backend.app.core.database import Base
from backend.app.core.models import (
    AutomaticSelectionEvaluation,
    AutomaticSelectionStrategy,
    DailyBar,
    Instrument,
    JobRun,
    MarketTradingCalendar,
    ScheduledJob,
    StockAdjFactor,
    User,
)
from backend.app.market import market_sync
from backend.app.quant import automatic_selection_api as selection_api
from backend.app.quant import automatic_selection_presentation as presentation
from backend.app.quant import automatic_selection_repository as repository
from backend.app.quant import automatic_selection_service as service
from backend.app.quant import tushare_factor_service
from backend.app.quant.automatic_selection_scoring import DEFAULT_STRATEGY_CONFIG, canonical_json, validate_config
from backend.app.scheduler import rhythm_scheduler
from tests.automatic_selection_fixtures import history_dates
from tests.automatic_selection_mysql import TEST_DATABASE_ENV, create_isolated_mysql_engine, reset_isolated_schema

TARGET_DATE = "20260828"
BLOCKED_DATE = "20260904"
ENTRY_DATE = "20260831"
LATEST_DATE = "20260903"


def _closes(start: float, *, growth: float = 0.0, noise: float = 0.0, count: int = 21) -> list[float]:
    return [
        round(start * (1 + growth) ** index * (1 + (noise if index % 2 else -noise)), 6)
        for index in range(count)
    ]


# symbol, 行业, PE, PB, 总市值(万元), 收盘序列, 日均成交额(千元), 当日涨跌幅(%), 选出后涨跌
STOCKS = [
    ("600001.SH", "银行", 4.0, 0.5, 5_000_000.0, _closes(10, growth=0.005), 900_000.0, 1.2, 0.10),
    ("600002.SH", "银行", 6.0, 0.7, 3_000_000.0, _closes(10, growth=0.001, noise=0.01), 400_000.0, 0.5, 0.02),
    ("600003.SH", "银行", 7.0, 0.8, 2_000_000.0, _closes(10, noise=0.012), 300_000.0, -0.3, -0.01),
    ("600004.SH", "电力", 12.0, 1.2, 1_500_000.0, _closes(10, growth=0.013, noise=0.02), 600_000.0, 3.0, 0.04),
    ("600005.SH", "电力", 15.0, 1.5, 800_000.0, _closes(10, growth=0.002, noise=0.015), 200_000.0, 0.8, 0.00),
    ("300006.SZ", "医药", 80.0, 8.0, 300_000.0, _closes(10, growth=0.008, noise=0.05), 150_000.0, 19.8, -0.05),
    ("600007.SH", "医药", 30.0, 3.0, 600_000.0, _closes(10, growth=-0.006, noise=0.02), 120_000.0, -1.5, 0.03),
    ("600008.SH", "电力", 20.0, 2.0, 200_000.0, _closes(10, noise=0.03), 60_000.0, 0.0, -0.02),
]


def _config() -> dict:
    config = copy.deepcopy(DEFAULT_STRATEGY_CONFIG)
    config["top_n"] = 4
    config["constraints"] = {"max_per_industry": 2, "max_per_market_cap_bucket": 0}
    config["quality"] = {
        "min_bar_coverage": 0.5,
        "min_basic_coverage": 0.5,
        "min_history_coverage": 0.5,
        "min_industry_coverage": 0.5,
        "min_factor_coverage": 1.0,
        "min_eligible_count": 1,
        "min_evaluation_coverage": 0.5,
    }
    return config


def _rows(target_trade_date: str, *, drop_valuation: bool = False) -> list[dict]:
    rows = []
    dates = history_dates(target_trade_date)
    for index, (symbol, industry, pe, pb, mv, closes, amount, pct_chg, _) in enumerate(STOCKS):
        rows.append(
            {
                "symbol": symbol,
                "name": f"样本{symbol[:6]}",
                "market": "主板",
                "list_status": "L",
                "list_date": "20100101",
                "industry": industry,
                "close_price": closes[-1],
                "pct_chg": pct_chg,
                # 数据不全场景：大半股票缺估值，基础数据覆盖率跌破门槛。
                "pe_ttm": None if drop_valuation and index >= 2 else pe,
                "pb": pb,
                "total_mv": mv,
                "circ_mv": mv * 0.8,
                "history": [
                    {"trade_date": day, "close": closes[position], "adj_factor": 1.0, "amount": amount}
                    for position, day in enumerate(dates)
                ],
                "expected_history_dates": dates,
                "factor_values": {},
            }
        )
    return rows


def _create_engine():
    if os.getenv(TEST_DATABASE_ENV, "").strip():
        engine = create_isolated_mysql_engine()
        reset_isolated_schema(engine)
        return engine
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine


class AutomaticSelectionPresentationFlowTests(unittest.TestCase):
    def setUp(self) -> None:
        presentation.clear_caches()
        self.addCleanup(presentation.clear_caches)
        self.engine = _create_engine()
        self.addCleanup(self.engine.dispose)
        self.addCleanup(Base.metadata.drop_all, self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        for module in (repository, service, presentation, market_sync, tushare_factor_service):
            db_patch = patch.object(module, "SessionLocal", self.SessionFactory)
            db_patch.start()
            self.addCleanup(db_patch.stop)
        self.strategy = repository.create_strategy(
            strategy_key="plain-view-test",
            name="白话呈现测试策略",
            config=_config(),
            lifecycle_state="challenger",
            actor_user_id=1,
        )
        with self.SessionFactory() as db:
            row = db.get(AutomaticSelectionStrategy, self.strategy["id"])
            row.lifecycle_state = "official"
            row.official_slot = 1
            db.commit()
        self._seed_market()
        self.run = service.run_strategy_from_rows(
            self.strategy,
            _rows(TARGET_DATE),
            target_trade_date=TARGET_DATE,
            trigger_type="test",
            generate_explanation=False,
        )

    def _seed_market(self) -> None:
        with self.SessionFactory() as db:
            for symbol, *_ in STOCKS:
                db.add(Instrument(symbol=symbol, name=f"样本{symbol[:6]}", market="SH", type="stock"))
            db.flush()
            day = datetime.strptime(TARGET_DATE, "%Y%m%d").date()
            last = max(date.today(), day + timedelta(days=120))
            while day <= last:
                db.add(
                    MarketTradingCalendar(
                        exchange="SSE",
                        cal_date=day.strftime("%Y%m%d"),
                        is_open=day.weekday() < 5,
                        source="tushare",
                    )
                )
                day += timedelta(days=1)
            for symbol, *_, forward_return in STOCKS:
                # 600001 在观察期内 10 送 10：原始收盘价减半，复权因子翻倍。
                split = symbol == "600001.SH"
                latest_close = 10 * (1 + forward_return) / (2 if split else 1)
                db.add(DailyBar(symbol=symbol, trade_date=ENTRY_DATE, open=10.0, close=10.0, source="tushare"))
                db.add(DailyBar(symbol=symbol, trade_date=LATEST_DATE, open=latest_close, close=latest_close, source="tushare"))
                db.add(StockAdjFactor(symbol=symbol, trade_date=ENTRY_DATE, adj_factor=1.0))
                db.add(StockAdjFactor(symbol=symbol, trade_date=LATEST_DATE, adj_factor=2.0 if split else 1.0))
            db.commit()

    def test_daily_view_explains_picks_in_plain_language(self) -> None:
        view = presentation.daily_view()

        self.assertEqual(self.run["status"], "completed")
        self.assertTrue(view["strategy"]["is_official"])
        self.assertEqual(view["trade_date"], TARGET_DATE)
        self.assertTrue(view["is_latest"])
        self.assertEqual(view["status"]["code"], "ok")
        self.assertIn("8 月 28 日", view["status"]["title"])
        picks = view["picks"]
        self.assertEqual(len(picks), 4)
        self.assertEqual([pick["rank"] for pick in picks], sorted(pick["rank"] for pick in picks))
        industries = [pick["industry"] for pick in picks]
        self.assertTrue(all(industries.count(name) <= 2 for name in industries))

        best = picks[0]
        self.assertEqual(best["symbol"], "600001.SH")
        scores = {dim["key"]: dim["score"] for dim in best["dimensions"]}
        self.assertEqual(set(scores), {"value", "trend", "activity", "stability"})
        self.assertEqual(scores["value"], 100)
        self.assertEqual(scores["activity"], 100)
        self.assertEqual(scores["stability"], 100)
        self.assertIn("比 100% 的合格股票便宜", next(d["detail"] for d in best["dimensions"] if d["key"] == "value"))
        self.assertRegex(best["headline"], r"\d+% 的股票")
        self.assertEqual(best["market_cap_label"], "大盘")
        self.assertEqual(best["total_mv_yi"], 500.0)
        self.assertEqual(best["avg_amount_yi"], 9.0)
        self.assertEqual(best["change_label"], "新进")
        self.assertEqual(len(best["price_path"]), 21)
        self.assertEqual(best["price_path"][0]["value"], 1.0)
        for pick in picks:
            for dim in pick["dimensions"]:
                self.assertTrue(0 <= dim["score"] <= 100)
            self.assertNotIn("z_scores", pick)

        summary = view["summary"]
        self.assertEqual(summary["added"], 4)
        self.assertTrue(summary["headline"].startswith("入选 4 只"))
        self.assertEqual([step["count"] for step in view["funnel"]["steps"]], [8, 8, 4])
        self.assertEqual(
            {dim["key"]: dim["weight_percent"] for dim in view["method"]["dimensions"]},
            {"value": 35, "trend": 25, "activity": 15, "stability": 25},
        )
        self.assertIn("5,000 万元", view["method"]["filters"][2])
        self.assertIn("不构成投资建议", view["disclaimer"])

    def test_since_selection_uses_adjusted_prices_and_pool_average(self) -> None:
        view = presentation.daily_view()

        since = view["performance"]["since_selection"]
        self.assertEqual(since["status"], "observing")
        self.assertEqual(since["entry_trade_date"], ENTRY_DATE)
        self.assertEqual(since["as_of"], LATEST_DATE)
        self.assertEqual(since["trading_days"], 4)
        expected = {symbol: forward for symbol, *_, forward in STOCKS}
        picked = [pick["symbol"] for pick in view["picks"]]
        self.assertAlmostEqual(since["basket_return"], sum(expected[s] for s in picked) / len(picked))
        self.assertAlmostEqual(since["benchmark_return"], sum(expected.values()) / len(expected))
        self.assertAlmostEqual(since["excess_return"], since["basket_return"] - since["benchmark_return"])
        self.assertNotIn("per_symbol", since)
        best = view["picks"][0]
        # 送转后原始价减半，复权后仍是 +10%，不能算成 -45%。
        self.assertAlmostEqual(best["performance"]["since_selection"], 0.10)

    def test_blocked_day_explains_why_and_points_to_last_result(self) -> None:
        blocked = service.run_strategy_from_rows(
            self.strategy,
            _rows(BLOCKED_DATE, drop_valuation=True),
            target_trade_date=BLOCKED_DATE,
            trigger_type="test",
            generate_explanation=False,
        )
        self.assertEqual(blocked["status"], "blocked")

        latest = presentation.daily_view()
        self.assertEqual(latest["trade_date"], BLOCKED_DATE)
        self.assertEqual(latest["status"]["code"], "blocked")
        self.assertIn("暂停出结果", latest["status"]["title"])
        self.assertTrue(any("基础数据只覆盖 25%" in issue for issue in latest["status"]["issues"]))
        self.assertEqual(latest["status"]["fallback_trade_date"], TARGET_DATE)
        self.assertEqual(latest["picks"], [])
        self.assertEqual([item["trade_date"] for item in latest["available_dates"]], [BLOCKED_DATE, TARGET_DATE])
        self.assertEqual(latest["available_dates"][0]["status_label"], "数据不全，暂停")

        earlier = presentation.daily_view(trade_date=TARGET_DATE)
        self.assertFalse(earlier["is_latest"])
        self.assertEqual(len(earlier["picks"]), 4)
        with self.assertRaises(LookupError):
            presentation.daily_view(trade_date="20200101")

    def test_track_record_summarizes_matured_windows(self) -> None:
        with self.SessionFactory() as db:
            evaluation = db.scalar(
                select(AutomaticSelectionEvaluation).where(
                    AutomaticSelectionEvaluation.run_id == self.run["run_id"],
                    AutomaticSelectionEvaluation.horizon == 5,
                )
            )
            evaluation.status = "completed"
            evaluation.exit_trade_date = "20260904"
            evaluation.basket_return = 0.03
            evaluation.benchmark_return = 0.018
            evaluation.excess_return = 0.012
            evaluation.per_symbol_json = canonical_json([{"symbol": "600001.SH", "net_return": 0.05}])
            db.commit()

        record = presentation.track_record()
        self.assertEqual(len(record["periods"]), 1)
        five_day = record["summary"][0]
        self.assertEqual((five_day["horizon"], five_day["completed"], five_day["wins"]), (5, 1, 1))
        self.assertEqual(five_day["win_rate"], 1.0)
        self.assertAlmostEqual(five_day["avg_excess_return"], 0.012)
        self.assertEqual(record["summary"][1]["pending"], 1)
        period = record["periods"][0]["horizons"]
        self.assertTrue(period["5"]["beat"])
        self.assertEqual(period["20"]["status_label"], "观察中")
        self.assertIn("样本还少", record["notes"][0])

        view = presentation.daily_view()
        self.assertEqual(view["track_brief"], {"horizon": 5, "periods": 1, "wins": 1, "avg_excess_return": 0.012})
        best = view["picks"][0]
        horizon = next(item for item in best["performance"]["horizons"] if item["horizon"] == 5)
        self.assertEqual(horizon["net_return"], 0.05)
        self.assertEqual(horizon["status_label"], "已到期")

    def test_explanation_evidence_is_plain_and_serializable(self) -> None:
        detail = repository.run_detail(self.run["run_id"], candidate_scope="top")
        evidence = presentation.explanation_evidence(detail, detail["candidates"])

        payload = canonical_json(evidence)
        self.assertNotIn("z_scores", payload)
        self.assertNotIn("config_hash", payload)
        self.assertEqual(len(evidence["picks"]), 4)
        self.assertEqual(set(evidence["picks"][0]["dimension_scores"]), {"估值", "走势", "活跃度", "稳定性"})

    def _data_job_run(self, *, status: str, trigger: str, started: datetime, error: str | None = None, output=None) -> None:
        with self.SessionFactory() as db:
            if db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY)) is None:
                db.add(ScheduledJob(
                    job_key=rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY, name="自动选股数据入库",
                    job_kind="automatic_selection_data", schedule_type="daily", run_time="17:30",
                    weekday_mask="1,2,3,4,5", trading_day_only=True, enabled=True,
                ))
            db.add(JobRun(
                run_id=f"data-{started:%H%M%S}-{status}", job_key=rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
                fire_key=f"fire-{started:%Y%m%d%H%M%S}", trigger_type=trigger, status=status,
                started_at=started, finished_at=started, error=error,
                output_content=json.dumps(output or {}, ensure_ascii=False),
            ))
            db.commit()

    def test_data_sync_failure_is_explained_with_the_next_step(self) -> None:
        self.assertEqual(presentation.data_sync_view(datetime(2026, 10, 20, 10, 30))["status"], "never")
        production_error = (
            "daily(20261020): 全市场响应不完整，上市标的 0/5575，至少需 5018；"
            "daily_basic(20261020): 全市场响应不完整，上市标的 0/5575，至少需 4739；"
            "日线水位 20261019 未对齐交易日 20261020；daily_basic 水位 20261019 未对齐交易日 20261020"
        )
        self._data_job_run(
            status="failed", trigger="manual_pipeline", started=datetime(2026, 10, 20, 10, 8),
            error=production_error, output={"ready": False, "target_trade_date": "20261020"},
        )
        manual = presentation.data_sync_view(datetime(2026, 10, 20, 10, 30))
        self.assertEqual((manual["status"], manual["ok"], manual["trigger_label"]), ("failed", False, "手动"))
        self.assertEqual(manual["issues"][0], "数据源还没有返回这一天的全市场行情，通常是当天数据尚未发布（收盘后约 15:00–17:00 陆续入库）")
        self.assertIn("日线行情不全", manual["issues"])
        self.assertIn("17:00 前会使用上一个交易日的收盘数据", manual["next_step"])
        self.assertEqual(manual["detail"], production_error)

        self._data_job_run(status="failed", trigger="schedule", started=datetime(2026, 10, 20, 17, 30), error="daily_basic(20261020): 全市场响应不完整，上市标的 0/5575")
        scheduled = presentation.data_sync_view(datetime(2026, 10, 20, 17, 45))
        self.assertEqual(scheduled["trigger_label"], "定时")
        self.assertIn("每 30 分钟自动重试，直到 22:00", scheduled["next_step"])
        late = presentation.data_sync_view(datetime(2026, 10, 20, 22, 30))
        self.assertIn("每个交易日 17:30 自动同步", late["next_step"])

        self._data_job_run(status="success", trigger="schedule", started=datetime(2026, 10, 20, 18, 0), output={"ready": True, "target_trade_date": "20261020"})
        done = presentation.data_sync_view(datetime(2026, 10, 20, 18, 5))
        self.assertTrue(done["ok"])
        self.assertEqual(done["title"], "10 月 20 日 收盘数据已同步")
        self.assertIsNotNone(presentation.daily_view()["data_sync"])

    def test_undated_cache_failure_is_shown_only_when_nothing_else_exists(self) -> None:
        other = repository.create_strategy(
            strategy_key="undated-failure", name="未就绪策略", config=_config(),
            lifecycle_state="challenger", actor_user_id=1,
        )
        service._persist_failed_strategy_run(
            other,
            target_trade_date="",
            trigger_type="schedule",
            job_run_id=None,
            sync_summary={},
            error=RuntimeError(
                "自动选股本地缓存未就绪：本地 SSE 权威交易日历未覆盖今日 20260905，无法确定目标交易日；"
                "本地历史交易日不足：0/21；本地上市 A 股目录不完整：0 只，至少需 3000；"
                "目标日 daily_basic 覆盖不足：0/0，至少需 3000"
            ),
        )

        view = presentation.daily_view(strategy_id=other["id"])
        self.assertEqual(view["available_dates"], [])
        self.assertIsNone(view["trade_date"])
        self.assertIsNone(view["funnel"])
        status = view["status"]
        self.assertEqual(status["code"], "failed")
        self.assertRegex(status["title"], r"^最近一次选股没能运行（\d+ 月 \d+ 日）$")
        self.assertIn("行情数据还没准备好", status["message"])
        self.assertEqual(status["issues"], ["交易日历没有同步到最新", "股票名单没有同步完整", "市盈率、市值等每日指标不全"])
        self.assertTrue(status["detail"].startswith("自动选股本地缓存未就绪"))

        # 正式策略已有结果时，这条无日期失败不会混进日期列表或抢占默认视图。
        official = presentation.daily_view()
        self.assertEqual(official["trade_date"], TARGET_DATE)
        self.assertNotIn("", [item["trade_date"] for item in official["available_dates"]])

    def test_api_serves_daily_view_and_track_record(self) -> None:
        app = FastAPI()
        app.include_router(selection_api.router)
        user = User(id=5, username="viewer", display_name="观察者", role="viewer", password_hash="x")
        app.dependency_overrides[get_current_user] = lambda: AuthContext(user=user, permissions={"data:read"})
        client = TestClient(app)
        self.addCleanup(client.close)

        daily = client.get("/api/automatic-selection/daily")
        self.assertEqual(daily.status_code, 200)
        self.assertTrue(daily.json()["success"])
        self.assertEqual(len(daily.json()["data"]["picks"]), 4)
        self.assertEqual(client.get("/api/automatic-selection/daily?trade_date=20200101").status_code, 404)
        self.assertEqual(client.get("/api/automatic-selection/daily?trade_date=2026-08-28").status_code, 422)
        self.assertEqual(client.get("/api/automatic-selection/daily?strategy_id=999").status_code, 404)
        record = client.get("/api/automatic-selection/track-record")
        self.assertEqual(record.status_code, 200)
        self.assertEqual(len(record.json()["data"]["periods"]), 1)


class PresentPicksUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        presentation.clear_caches()
        self.addCleanup(presentation.clear_caches)

    def _run(self, config: dict | None = None) -> dict:
        return {
            "run_id": "unit-run",
            "data_fingerprint": "f" * 64,
            "created_at": "2026-08-28T16:10:00",
            "target_trade_date": TARGET_DATE,
            "eligible_count": 5,
            "config": validate_config(config or _config()),
        }

    def _candidate(self, symbol: str, z: float, **overrides) -> dict:
        item = {
            "symbol": symbol,
            "name": symbol,
            "industry": "银行",
            "selected": True,
            "rank": 1,
            "change_state": "retained",
            "market_cap_bucket": "mid",
            "total_mv": 1_000_000.0,
            "close_price": 10.0,
            "pct_chg": 1.0,
            "pe_ttm": 10.0,
            "pb": 1.0,
            "avg_amount_20": 500_000.0,
            "momentum_20": 0.05,
            "volatility_20": 0.01,
            "list_date": "20100101",
            "z_scores": {feature: z for feature in ("value_pe", "value_pb", "momentum_20", "liquidity_20", "low_volatility_20")},
            "input_evidence": {},
        }
        item.update(overrides)
        return item

    def _present(self, candidates: list[dict], pool_z: list[dict], config: dict | None = None) -> list[dict]:
        pool = [{"symbol": f"P{index}", "z_scores": z} for index, z in enumerate(pool_z)]
        with patch.object(repository, "eligible_score_pool", return_value=pool):
            return presentation.present_picks(self._run(config), candidates)

    def test_scores_rank_against_the_eligible_pool(self) -> None:
        features = ("value_pe", "value_pb", "momentum_20", "liquidity_20", "low_volatility_20")
        pool = [{feature: value for feature in features} for value in (-2.0, -1.0, 0.0, 1.0, 2.0)]
        picks = self._present([self._candidate("A", 2.0), self._candidate("B", 0.0, rank=3)], pool)

        self.assertEqual([dim["score"] for dim in picks[0]["dimensions"]], [100, 100, 100, 100])
        self.assertEqual([dim["score"] for dim in picks[1]["dimensions"]], [50, 50, 50, 50])
        self.assertEqual(picks[0]["top_percent"], 20.0)
        self.assertEqual(picks[0]["headline"], "估值比 100% 的股票便宜，走势强于 100% 的股票")
        self.assertEqual(picks[1]["headline"], "各项比较均衡，综合得分靠前")
        self.assertEqual(picks[0]["market_cap_label"], "中盘")

    def test_rule_based_risks_use_business_units(self) -> None:
        features = ("value_pe", "value_pb", "momentum_20", "liquidity_20", "low_volatility_20")
        pool = [{feature: value for feature in features} for value in (-2.0, -1.0, 0.0, 1.0, 2.0)]
        risky = self._candidate(
            "300750.SZ",
            0.0,
            momentum_20=0.25,
            pct_chg=19.9,
            volatility_20=0.05,
            avg_amount_20=60_000.0,
            pe_ttm=75.0,
            list_date="20260101",
            z_scores={"value_pe": 0.0, "value_pb": 0.0, "momentum_20": 2.0, "liquidity_20": 0.0, "low_volatility_20": -2.0},
        )
        texts = [risk["text"] for risk in self._present([risky], pool)[0]["risks"]]

        self.assertIn("近 20 个交易日已上涨 25.0%，短期涨幅较大，注意回调", texts)
        self.assertTrue(any("接近涨停" in text for text in texts))
        self.assertTrue(any("波动约 5.0%" in text for text in texts))
        self.assertTrue(any("6,000 万元" in text for text in texts))
        self.assertTrue(any("市盈率 75 倍" in text for text in texts))
        self.assertIn("上市不满一年，可参考的历史较短", texts)
        self.assertIn("稳定性偏弱：只好于 0% 的合格股票", texts)

    def test_industry_cap_and_adjusted_price_path(self) -> None:
        pool = [{"value_pe": 0.0, "value_pb": 0.0, "momentum_20": 0.0, "liquidity_20": 0.0, "low_volatility_20": 0.0}]
        history = [
            {"trade_date": "20260826", "close": 20.0, "adj_factor": 1.0},
            {"trade_date": "20260827", "close": 20.0, "adj_factor": 1.0},
            {"trade_date": "20260828", "close": 10.5, "adj_factor": 2.0},
        ]
        picks = self._present(
            [
                self._candidate("A", 0.0, input_evidence={"history": history}),
                self._candidate("B", 0.0, rank=2),
            ],
            pool,
        )

        self.assertEqual([point["value"] for point in picks[0]["price_path"]], [1.0, 1.0, 1.05])
        self.assertEqual(picks[1]["price_path"], [])
        for pick in picks:
            self.assertTrue(any("达到行业上限" in risk["text"] for risk in pick["risks"]))

    def test_reverse_weight_is_labelled_and_not_used_as_a_reason(self) -> None:
        config = _config()
        config["feature_weights"] = {"value_pe": 0.5, "momentum_20": -0.5}
        pool = [{"value_pe": value, "momentum_20": value} for value in (-1.0, 0.0, 1.0)]
        pick = self._present(
            [self._candidate("A", 1.0, z_scores={"value_pe": 1.0, "momentum_20": -1.0})],
            pool,
            config,
        )[0]

        trend = next(dim for dim in pick["dimensions"] if dim["key"] == "trend")
        self.assertTrue(trend["reverse"])
        self.assertIn("反向", trend["description"])
        self.assertEqual(pick["headline"], "估值比 100% 的股票便宜")


class StatusViewTests(unittest.TestCase):
    def test_blocked_failures_are_translated(self) -> None:
        view = presentation.status_view(
            {
                "status": "blocked",
                "target_trade_date": "20261016",
                "quality_gate": {
                    "failures": [
                        {"code": "bar_coverage", "actual": 0.42, "required": 0.9},
                        {"code": "eligible_count", "actual": 120, "required": 1000},
                        {"code": "factor_coverage:tushare:roe_ttm", "actual": 0.5, "required": 1.0},
                    ]
                },
            },
            last_completed_trade_date="20261015",
        )

        self.assertEqual(view["code"], "blocked")
        self.assertEqual(view["title"], "10 月 16 日 的数据不完整，系统暂停出结果")
        self.assertEqual(view["issues"][0], "当天行情只拿到 42% 的股票（至少要 90%）")
        self.assertIn("只有 120 只（至少要 1000 只）", view["issues"][1])
        self.assertIn("roe_ttm 只覆盖 50%", view["issues"][2])
        self.assertEqual(view["fallback_trade_date"], "20261015")

    def test_failed_and_empty_states(self) -> None:
        failed = presentation.status_view({"status": "failed", "target_trade_date": "20261016", "error": "评分失败"})
        self.assertEqual((failed["code"], failed["issues"]), ("failed", ["评分失败"]))
        empty = presentation.status_view(None)
        self.assertEqual(empty["code"], "empty")
        self.assertIn("17:30", empty["message"])


if __name__ == "__main__":
    unittest.main()
