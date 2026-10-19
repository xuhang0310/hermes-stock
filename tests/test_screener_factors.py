from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth.auth import create_access_token
from backend.app.core.database import Base
from backend.app.core.models import (
    Instrument,
    StockLatestPrice,
    TushareFactorCatalog,
    TushareFactorValue,
    User,
)
from backend.app.quant import screener_ai, screener_service
from backend.app.quant.screener_api import router as screener_router
from backend.app.quant.tushare_factor_api import router as tushare_factor_router

SVC = "backend.app.quant.screener_service"
TSVC = "backend.app.quant.tushare_factor_service"
SYMBOLS = [f"6000{index:02d}.SH" for index in range(1, 7)]
LATEST_DATE = "20260828"
OLD_DATE = "20260827"
# 最新截面：600006.SH 缺值（inner join 应剔除它）
LATEST_VALUES = {
    "600001.SH": 0.05,
    "600002.SH": 0.10,
    "600003.SH": 0.15,
    "600004.SH": 0.20,
    "600005.SH": 0.30,
}


class ScreenerFactorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.SessionFactory() as db:
            for index, symbol in enumerate(SYMBOLS):
                db.add(Instrument(symbol=symbol, name=f"测试 {index}", market="CN", type="stock"))
                db.add(
                    StockLatestPrice(
                        symbol=symbol,
                        name=f"测试 {index}",
                        price=10.0 + index,
                        pct_chg=float(index - 2),
                        trade_date=LATEST_DATE,
                        source="tushare",
                        fetched_at=datetime(2026, 8, 28, 15, 0, 0),
                    )
                )
            db.add(TushareFactorCatalog(factor_name="roe_ttm", factor_type="基本面", factor_desc="ROE_TTM"))
            db.add(TushareFactorCatalog(factor_name="return_5d", factor_type="动量", factor_desc="过去5日收益率"))
            for symbol, value in LATEST_VALUES.items():
                db.add(
                    TushareFactorValue(
                        factor_name="roe_ttm", symbol=symbol, trade_date=LATEST_DATE, factor_value=value
                    )
                )
            # 旧截面：含 600006.SH，验证只取最新日期
            for symbol in SYMBOLS:
                db.add(
                    TushareFactorValue(
                        factor_name="roe_ttm", symbol=symbol, trade_date=OLD_DATE, factor_value=0.99
                    )
                )
            db.add(
                User(
                    username="quant-researcher",
                    display_name="量化研究员",
                    role="researcher",
                    permission_scope="",
                    password_hash="test",
                )
            )
            db.commit()

        self._patches = [
            patch("backend.app.core.database.SessionLocal", self.SessionFactory),
            patch(f"{SVC}.SessionLocal", self.SessionFactory),
            patch(f"{TSVC}.SessionLocal", self.SessionFactory),
        ]
        for item in self._patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in self._patches])

    def tearDown(self) -> None:
        self.engine.dispose()

    # ------------------------------------------------ 服务层

    def test_factor_condition_filters_and_shapes_rows(self) -> None:
        result = screener_service.run_screener(factor_name="roe_ttm", factor_min=0.12, factor_max=0.25)
        self.assertEqual(result["factor_as_of"], LATEST_DATE)
        self.assertEqual(result["universe"], 6)  # 候选池：全部可交易股票
        self.assertEqual(result["total"], 2)  # 命中数：满足因子区间的股票
        got = {item["symbol"]: item["factor_value"] for item in result["items"]}
        self.assertEqual(got, {"600003.SH": 0.15, "600004.SH": 0.20})

    def test_factor_without_range_returns_latest_cross_section(self) -> None:
        result = screener_service.run_screener(factor_name="roe_ttm")
        # 600006.SH 最新截面无值，被 inner join 剔除；旧截面的 0.99 不得出现
        self.assertEqual(result["universe"], 6)
        self.assertEqual(result["total"], 5)
        self.assertNotIn("600006.SH", {item["symbol"] for item in result["items"]})
        self.assertTrue(all("factor_value" in item for item in result["items"]))
        self.assertFalse(any(abs(item["factor_value"] - 0.99) < 1e-9 for item in result["items"]))

    def test_plain_run_has_no_factor_keys(self) -> None:
        result = screener_service.run_screener()
        self.assertIsNone(result["factor_as_of"])
        self.assertEqual(result["universe"], 6)
        self.assertFalse(any("factor_value" in item for item in result["items"]))

    def test_unknown_factor_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            screener_service.run_screener(factor_name="ghost_factor")
        self.assertIn("不存在", str(ctx.exception))

    def test_unsynced_factor_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            screener_service.run_screener(factor_name="return_5d")
        self.assertIn("尚未同步", str(ctx.exception))

    def test_count_candidates_includes_factor_condition(self) -> None:
        count = screener_service.count_screener_candidates({"factor_name": "roe_ttm", "factor_min": 0.12})
        self.assertEqual(count, 3)  # 0.15 / 0.20 / 0.30

    # ------------------------------------------------ AI 清洗/回显

    def test_sanitize_keeps_valid_factor_fields(self) -> None:
        out = screener_ai._sanitize({"factor_name": "roe_ttm", "factor_min": "0.2", "factor_max": 0.5})
        self.assertEqual(out["factor_name"], "roe_ttm")
        self.assertEqual(out["factor_min"], 0.2)
        self.assertEqual(out["factor_max"], 0.5)

    def test_sanitize_allows_negative_factor_min(self) -> None:
        out = screener_ai._sanitize({"factor_name": "return_5d", "factor_min": -0.1})
        self.assertEqual(out["factor_min"], -0.1)

    def test_sanitize_drops_bad_factor_name_with_ranges(self) -> None:
        out = screener_ai._sanitize({"factor_name": "坏因子!", "factor_min": 0.1})
        self.assertNotIn("factor_name", out)
        self.assertNotIn("factor_min", out)
        out = screener_ai._sanitize({"factor_min": 0.1})
        self.assertNotIn("factor_min", out)

    def test_summarize_factor_echo(self) -> None:
        summary = screener_ai._summarize({"factor_name": "roe_ttm", "factor_min": 0.2})
        self.assertIn("因子 roe_ttm ≥ 0.2", summary)
        summary = screener_ai._summarize({"factor_name": "roe_ttm", "factor_min": 0.1, "factor_max": 0.3})
        self.assertIn("介于", summary)

    # ------------------------------------------------ API 层

    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(screener_router)
        app.include_router(tushare_factor_router)
        return TestClient(app)

    def _token(self) -> str:
        with self.SessionFactory() as db:
            user = db.scalar(select(User).where(User.username == "quant-researcher"))
        return create_access_token(user)

    def test_api_run_with_factor_condition(self) -> None:
        client = self._client()
        headers = {"Authorization": f"Bearer {self._token()}"}
        response = client.post(
            "/api/screener/run",
            headers=headers,
            json={"factor_name": "roe_ttm", "factor_min": 0.12, "factor_max": 0.25},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["factor_as_of"], LATEST_DATE)
        self.assertEqual(body["data"]["total"], 2)
        self.assertTrue(all("factor_value" in item for item in body["data"]["items"]))

    def test_api_run_rejects_unknown_or_unsynced_factor(self) -> None:
        client = self._client()
        headers = {"Authorization": f"Bearer {self._token()}"}
        response = client.post("/api/screener/run", headers=headers, json={"factor_name": "ghost"})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])
        response = client.post("/api/screener/run", headers=headers, json={"factor_name": "return_5d"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("尚未同步", response.json()["message"])

    def test_api_run_requires_token(self) -> None:
        client = self._client()
        response = client.post("/api/screener/run", json={"factor_name": "roe_ttm"})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
