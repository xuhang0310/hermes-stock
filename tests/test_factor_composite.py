from __future__ import annotations

import math
import unittest
from datetime import datetime
from unittest.mock import patch

import pandas as pd
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
    StockLatestPrice,
    TushareFactorCatalog,
    User,
)
from backend.app.quant import tushare_factor_service
from backend.app.quant.factor_composite_api import router as composite_router
from backend.app.quant.factor_service import composite_score_factors

SVC = "backend.app.quant.tushare_factor_service"
FS = "backend.app.quant.factor_service"
SYMBOLS = [f"6000{index:02d}.SH" for index in range(1, 7)]
BAR_DATES = pd.bdate_range("2025-01-02", periods=100)
LATEST_DATE = "20250424"


def _fake_tushare_request(api_name: str, params: dict | None = None, fields: str = ""):
    params = params or {}
    if api_name == "factor_list":
        return {
            "rows": [
                {"factor_name": "roe_ttm", "asset_type": "STK", "factor_type": "基本面", "factor_desc": "ROE_TTM 盈利能力"},
                {"factor_name": "return_5d", "asset_type": "STK", "factor_type": "动量", "factor_desc": "过去5日收益率"},
            ],
            "error": None,
        }
    if api_name == "trade_cal":
        days = pd.bdate_range(params.get("start_date"), params.get("end_date"))
        return {"rows": [{"cal_date": day.strftime("%Y%m%d")} for day in days], "error": None}
    if api_name == "factor_value":
        day = str(params.get("trade_date") or "")
        stamp = pd.Timestamp(day)
        if stamp not in set(BAR_DATES):
            return {"rows": [], "error": None}
        rows = [
            {
                "factor_name": params.get("factor_name"),
                "ts_code": symbol,
                "trade_date": day,
                "factor_value": float(index + 1),
            }
            for index, symbol in enumerate(SYMBOLS)
        ]
        return {"rows": rows, "error": None}
    return {"rows": [], "error": None}


class FactorCompositeTests(unittest.TestCase):
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
                        fetched_at=datetime(2025, 4, 24, 15, 0, 0),
                    )
                )
                for offset, trade_date in enumerate(BAR_DATES):
                    trend = 0.0008 * offset * (index + 1)
                    cycle = 0.02 * math.sin((offset + index) / 7)
                    close = 10.0 + index + trend + cycle
                    db.add(
                        DailyBar(
                            symbol=symbol,
                            trade_date=trade_date.strftime("%Y%m%d"),
                            open=close * 0.998,
                            high=close * 1.012,
                            low=close * 0.988,
                            close=close,
                            pre_close=close * 0.999,
                            change_pct=0.1,
                            volume=100000 + offset * 100 + index * 1000,
                            amount=close * (100000 + offset * 100 + index * 1000) / 10,
                            source="tushare",
                        )
                    )
            db.add(TushareFactorCatalog(factor_name="roe_ttm", factor_type="基本面", factor_desc="ROE_TTM"))
            db.add(TushareFactorCatalog(factor_name="return_5d", factor_type="动量", factor_desc="过去5日收益率"))
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
            patch(f"{SVC}.SessionLocal", self.SessionFactory),
            patch(f"{FS}.SessionLocal", self.SessionFactory),
            patch(f"{SVC}._tushare_request", _fake_tushare_request),
            patch(f"{SVC}.API_PAUSE_SECONDS", 0),
        ]
        for item in self._patches:
            item.start()
        self.addCleanup(lambda: [item.stop() for item in self._patches])

        tushare_factor_service.sync_catalog()

    def tearDown(self) -> None:
        self.engine.dispose()

    def _sync_roe(self) -> None:
        tushare_factor_service.sync_factor_values(
            ["roe_ttm"], start_date="20250101", end_date="20250228", symbols=SYMBOLS
        )

    # ------------------------------------------------ 服务层

    def test_single_tushare_factor_full_cache_ranking(self) -> None:
        self._sync_roe()
        result = composite_score_factors(["tushare:roe_ttm"], start_date="20250101", end_date="20250228")
        # 全市场缓存截面：6 只都在；因子值单调 → 排名单调
        self.assertEqual(result["universe"], 6)
        self.assertEqual(len(result["rows"]), 6)
        self.assertEqual(result["rows"][0]["symbol"], "600006.SH")
        self.assertEqual(result["rows"][-1]["symbol"], "600001.SH")
        self.assertAlmostEqual(result["rows"][0]["percentile"], 1.0)
        # 富化最新价/名称
        self.assertEqual(result["rows"][0]["name"], "测试 5")
        self.assertAlmostEqual(result["rows"][0]["price"], 15.0)
        # 等权归一：单因子权重 1.0
        self.assertEqual(result["factors_used"][0]["weight"], 1.0)
        self.assertEqual(result["factors_used"][0]["zoo"], "tushare")
        self.assertIn("买入建议", result["interpretation"])

    def test_negative_weight_flips_ranking(self) -> None:
        self._sync_roe()
        result = composite_score_factors(
            ["tushare:roe_ttm"], weights={"tushare:roe_ttm": -2.0}, start_date="20250101", end_date="20250228"
        )
        self.assertEqual(result["rows"][0]["symbol"], "600001.SH")
        self.assertAlmostEqual(result["factors_used"][0]["weight"], -1.0)

    def test_two_tushare_factors_equal_weights(self) -> None:
        tushare_factor_service.sync_factor_values(
            ["roe_ttm", "return_5d"], start_date="20250101", end_date="20250131", symbols=SYMBOLS
        )
        result = composite_score_factors(["tushare:roe_ttm", "tushare:return_5d"], start_date="20250101", end_date="20250131")
        self.assertEqual(result["universe"], 6)
        weights = {item["id"]: item["weight"] for item in result["factors_used"]}
        self.assertAlmostEqual(weights["tushare:roe_ttm"], 0.5)
        self.assertAlmostEqual(weights["tushare:return_5d"], 0.5)
        # 两个因子值同为 index+1（伪数据同向）→ z 相同 → 复合分 = z
        top = result["rows"][0]
        self.assertAlmostEqual(top["composite_score"], top["factor_scores"]["tushare:roe_ttm"]["z"])
        self.assertEqual(result["skipped"], [])

    def test_mixed_local_and_tushare_with_symbols(self) -> None:
        self._sync_roe()
        result = composite_score_factors(
            ["tushare:roe_ttm", "qlib158_roc5"], symbols=SYMBOLS, start_date="20250101", end_date="20250228"
        )
        used = {item["id"] for item in result["factors_used"]}
        self.assertEqual(used, {"tushare:roe_ttm", "qlib158_roc5"})
        self.assertEqual(result["universe"], 6)
        zoos = {item["id"]: item["zoo"] for item in result["factors_used"]}
        self.assertEqual(zoos["tushare:roe_ttm"], "tushare")
        self.assertEqual(zoos["qlib158_roc5"], "qlib158")

    def test_local_factor_requires_symbols(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            composite_score_factors(["qlib158_roc5"], start_date="20250101", end_date="20250228")
        self.assertIn("股票池", str(ctx.exception))

    def test_unsynced_factor_skipped_with_reason(self) -> None:
        self._sync_roe()
        result = composite_score_factors(["tushare:roe_ttm", "tushare:return_5d"], start_date="20250101", end_date="20250228")
        self.assertEqual([item["id"] for item in result["factors_used"]], ["tushare:roe_ttm"])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertEqual(result["skipped"][0]["id"], "tushare:return_5d")
        self.assertIn("尚未摄入", result["skipped"][0]["reason"])

    def test_all_factors_failed_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            composite_score_factors(["tushare:return_5d"], start_date="20250101", end_date="20250228")
        self.assertIn("没有任何因子", str(ctx.exception))

    def test_unknown_factor_skipped(self) -> None:
        self._sync_roe()
        result = composite_score_factors(["tushare:roe_ttm", "tushare:ghost_factor"], start_date="20250101", end_date="20250228")
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("不存在", result["skipped"][0]["reason"])

    # ------------------------------------------------ API 层

    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(composite_router)
        return TestClient(app)

    def _token(self) -> str:
        with self.SessionFactory() as db:
            user = db.scalar(select(User).where(User.username == "quant-researcher"))
        return create_access_token(user)

    def test_api_requires_token(self) -> None:
        client = self._client()
        response = client.post("/api/factors/composite-score", json={"factor_ids": ["tushare:roe_ttm"]})
        self.assertEqual(response.status_code, 401)

    def test_api_composite_envelope(self) -> None:
        self._sync_roe()
        client = self._client()
        headers = {"Authorization": f"Bearer {self._token()}"}
        response = client.post(
            "/api/factors/composite-score",
            headers=headers,
            json={"factor_ids": ["tushare:roe_ttm"], "limit": 3, "start_date": "20250101", "end_date": "20250228"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["total"], 3)
        self.assertEqual(body["data"]["rows"][0]["symbol"], "600006.SH")

        response = client.post(
            "/api/factors/composite-score",
            headers=headers,
            json={"factor_ids": ["qlib158_roc5"], "start_date": "20250101", "end_date": "20250228"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])


if __name__ == "__main__":
    unittest.main()
