"""选股器候选池、真实命中数、全量排序与复权均线回归（SQLite 内存库，无需 MySQL/TuShare）。"""
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
    Instrument,
    StockAdjFactor,
    StockDailyBasic,
    StockLatestPrice,
    User,
)
from backend.app.quant import screener_service
from backend.app.quant.screener_api import router as screener_router

SVC = "backend.app.quant.screener_service"
SNAPSHOT = "20260904"
FETCHED_AT = datetime(2026, 9, 4, 15, 30)


def _trade_dates(count: int, end: str = SNAPSHOT) -> list[str]:
    """工作日近似交易日（升序），只用于构造夹具。"""
    day = datetime.strptime(end, "%Y%m%d")
    dates: list[str] = []
    while len(dates) < count:
        if day.weekday() < 5:
            dates.append(day.strftime("%Y%m%d"))
        day -= timedelta(days=1)
    return list(reversed(dates))


class ScreenerPoolAndRankingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        for target in (f"{SVC}.SessionLocal", "backend.app.core.database.SessionLocal"):
            patcher = patch(target, self.SessionFactory)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)

    def add_stock(
        self,
        symbol: str,
        *,
        name: str | None = None,
        pct_chg: float = 0.0,
        trade_date: str = SNAPSHOT,
        pe: float | None = None,
        mv_wan: float | None = None,
        turnover: float | None = None,
        volume_ratio: float | None = None,
        closes: list[float] | None = None,
        factors: list[float] | None = None,
    ) -> None:
        closes = closes or [10.0]
        with self.SessionFactory() as db:
            db.add(Instrument(symbol=symbol, name=name or symbol, market="SH", type="stock"))
            db.flush()  # 外键父表先落库（MySQL 会校验 daily_bars / daily_basic 外键）
            db.add(
                StockLatestPrice(
                    symbol=symbol,
                    name=name or symbol,
                    price=closes[-1],
                    pct_chg=pct_chg,
                    trade_date=trade_date,
                    source="tushare",
                    fetched_at=FETCHED_AT,
                )
            )
            db.add(
                StockDailyBasic(
                    symbol=symbol,
                    trade_date=SNAPSHOT,
                    pe_ttm=pe,
                    total_mv=mv_wan,
                    turnover_rate=turnover,
                    volume_ratio=volume_ratio,
                )
            )
            for index, day in enumerate(_trade_dates(len(closes))):
                db.add(DailyBar(symbol=symbol, trade_date=day, close=closes[index], volume=1000.0, source="tushare"))
                if factors is not None:
                    db.add(StockAdjFactor(symbol=symbol, trade_date=day, adj_factor=factors[index]))
            db.commit()

    def test_moving_average_filter_scans_every_match_not_only_top_gainers(self) -> None:
        # 旧实现只在当日涨幅前 300 名里判断均线：这里前 310 名全部跌破 20 日线。
        for index in range(320):
            rising = index >= 310
            closes = [10 + step * 0.1 for step in range(21)] if rising else [12 - step * 0.1 for step in range(21)]
            self.add_stock(f"{600000 + index}.SH", pct_chg=9.9 - index * 0.03, closes=closes)

        result = screener_service.run_screener(above_ma=20, limit=5)

        self.assertEqual(result["total"], 10)
        self.assertEqual(len(result["items"]), 5)
        self.assertTrue(all(int(item["symbol"][:6]) >= 600310 for item in result["items"]))
        self.assertTrue(all(item["ma_value"] is not None for item in result["items"]))

    def test_total_is_the_real_hit_count_not_the_page_size(self) -> None:
        for index in range(30):
            self.add_stock(f"{600000 + index}.SH", pct_chg=float(index))

        result = screener_service.run_screener(limit=5)

        self.assertEqual(result["universe"], 30)
        self.assertEqual(result["total"], 30)
        self.assertEqual(len(result["items"]), 5)

    def test_pool_excludes_st_and_suspended_but_keeps_intraday_refreshed_quotes(self) -> None:
        for index in range(4):
            self.add_stock(f"60000{index}.SH")
        self.add_stock("600010.SH", name="*ST 样本")
        self.add_stock("600011.SH", name="ST样本")
        self.add_stock("600012.SH", trade_date="20260828")  # 停牌：最新价停在更早的交易日
        self.add_stock("600013.SH", trade_date="20260907")  # 盘中按需刷新到更新日期

        result = screener_service.run_screener()
        symbols = {item["symbol"] for item in result["items"]}

        self.assertEqual(result["as_of"], SNAPSHOT)
        self.assertEqual(symbols, {"600000.SH", "600001.SH", "600002.SH", "600003.SH", "600013.SH"})
        self.assertEqual(result["universe"], 5)
        self.assertEqual(result["excluded_count"], 3)
        self.assertEqual(screener_service.count_screener_candidates({}), 5)

    def test_composite_ranking_is_computed_over_the_full_match_set(self) -> None:
        self.add_stock("600001.SH", pct_chg=9.9, pe=80.0, mv_wan=30_000_000.0, turnover=1.0, volume_ratio=0.8)
        self.add_stock("600002.SH", pct_chg=0.1, pe=8.0, mv_wan=500_000.0, turnover=6.0, volume_ratio=2.0)
        self.add_stock("600003.SH", pct_chg=5.0, pe=30.0, mv_wan=3_000_000.0, turnover=3.0, volume_ratio=1.2)

        by_value = screener_service.run_screener(
            limit=1, score_weights={"value": 100, "momentum": 0, "liquidity": 0},
        )
        by_momentum = screener_service.run_screener(
            limit=1, score_weights={"value": 0, "momentum": 100, "liquidity": 0},
        )
        by_change = screener_service.run_screener(limit=1, sort_by="pct_desc")

        self.assertEqual(by_value["items"][0]["symbol"], "600002.SH")
        self.assertEqual(by_value["items"][0]["value_score"], 100.0)
        self.assertEqual(by_momentum["items"][0]["symbol"], "600001.SH")
        self.assertEqual(by_change["items"][0]["symbol"], "600001.SH")
        self.assertEqual(by_value["total"], 3)

    def test_pe_ascending_puts_loss_makers_and_missing_values_last(self) -> None:
        self.add_stock("600001.SH", pe=-5.0)
        self.add_stock("600002.SH", pe=12.0)
        self.add_stock("600003.SH", pe=None)
        self.add_stock("600004.SH", pe=30.0)

        items = screener_service.run_screener(sort_by="pe_asc")["items"]

        self.assertEqual([item["symbol"] for item in items], ["600002.SH", "600004.SH", "600001.SH", "600003.SH"])

    def test_moving_average_uses_adjusted_closes_across_ex_rights(self) -> None:
        # 10 送 10：原始价 20 → 10.5。不复权 MA20=15.25 会把真实走强的股票判成跌破均线。
        closes = [20.0] * 11 + [10.5] * 10
        self.add_stock("600001.SH", closes=closes, factors=[1.0] * 11 + [2.0] * 10)
        self.add_stock("600002.SH", closes=closes)  # 无复权因子：退回不复权口径
        self.add_stock("600003.SH", closes=[10 + step * 0.1 for step in range(21)])

        result = screener_service.run_screener(above_ma=20)
        by_symbol = {item["symbol"]: item for item in result["items"]}

        self.assertIn("600001.SH", by_symbol)
        self.assertAlmostEqual(by_symbol["600001.SH"]["ma_value"], 10.25)
        self.assertTrue(by_symbol["600001.SH"]["ma_adjusted"])
        self.assertNotIn("600002.SH", by_symbol)
        self.assertFalse(by_symbol["600003.SH"]["ma_adjusted"])

    def test_invalid_sort_or_weights_are_rejected(self) -> None:
        self.add_stock("600001.SH")
        with self.assertRaises(ValueError):
            screener_service.run_screener(sort_by="random")
        with self.assertRaises(ValueError):
            screener_service.run_screener(score_weights={"quality": 50})
        with self.assertRaises(ValueError):
            screener_service.run_screener(score_weights={"value": -1})

    def test_api_passes_sort_and_weights_through(self) -> None:
        self.add_stock("600001.SH", pct_chg=9.9, pe=80.0, mv_wan=30_000_000.0)
        self.add_stock("600002.SH", pct_chg=0.1, pe=8.0, mv_wan=500_000.0)
        with self.SessionFactory() as db:
            db.add(User(username="screener-user", display_name="选股", role="researcher",
                        permission_scope="", password_hash="test"))
            db.commit()
            user = db.scalar(select(User).where(User.username == "screener-user"))
        app = FastAPI()
        app.include_router(screener_router)
        client = TestClient(app)
        headers = {"Authorization": f"Bearer {create_access_token(user)}"}

        response = client.post(
            "/api/screener/run",
            headers=headers,
            json={"limit": 1, "score_weights": {"value": 100, "momentum": 0, "liquidity": 0}},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()["data"]
        self.assertEqual(body["items"][0]["symbol"], "600002.SH")
        self.assertEqual(body["total"], 2)

        response = client.post("/api/screener/run", headers=headers, json={"sort_by": "random"})
        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
