from __future__ import annotations

import math
import unittest
from unittest.mock import patch

import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth.auth import create_access_token
from backend.app.core.database import Base
from backend.app.core.models import DailyBar, Instrument, TushareFactorValue, User
from backend.app.quant import tushare_factor_service
from backend.app.quant.factor_service import benchmark_factors, get_factor, list_factors
from backend.app.quant.tushare_factor_api import router as tushare_factor_router

SVC = "backend.app.quant.tushare_factor_service"
FS = "backend.app.quant.factor_service"
SYMBOLS = [f"6000{index:02d}.SH" for index in range(1, 7)]
BAR_DATES = pd.bdate_range("2025-01-02", periods=100)


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


class TushareFactorTests(unittest.TestCase):
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
                db.add(Instrument(symbol=symbol, name=f"Test {index}", market="CN", type="stock"))
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

    def tearDown(self) -> None:
        self.engine.dispose()

    # ------------------------------------------------ 目录

    def test_sync_catalog_and_listing(self) -> None:
        result = tushare_factor_service.sync_catalog()
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["by_type"], {"基本面": 1, "动量": 1})

        items = tushare_factor_service.list_catalog()
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["id"].split(":")[0], "tushare")
        self.assertEqual(items[0]["cached_rows"], 0)

        merged = list_factors(zoo="tushare", limit=100)
        ids = [item["id"] for item in merged["items"]]
        self.assertEqual(ids, ["tushare:return_5d", "tushare:roe_ttm"])
        self.assertEqual(merged["items"][0]["zoo"], "tushare")

        meta = get_factor("tushare:roe_ttm")
        self.assertEqual(meta["factor_name"], "roe_ttm")
        with self.assertRaises(KeyError):
            get_factor("tushare:not_a_factor")

    # ------------------------------------------------ 因子值摄入

    def test_sync_rejects_unknown_factor(self) -> None:
        tushare_factor_service.sync_catalog()
        with self.assertRaises(ValueError) as ctx:
            tushare_factor_service.sync_factor_values(["nope_factor"])
        self.assertIn("不存在这些因子", str(ctx.exception))

    def test_sync_respects_call_cap(self) -> None:
        # 20250101~20250331 约 64 个交易日 × 1 因子 > 60 次上限
        with self.assertRaises(ValueError) as ctx:
            tushare_factor_service.sync_factor_values(
                ["roe_ttm"], start_date="20250101", end_date="20250331"
            )
        self.assertIn("超过单次上限", str(ctx.exception))

    def test_sync_values_then_load_panel_and_query(self) -> None:
        result = tushare_factor_service.sync_factor_values(
            ["roe_ttm"],
            start_date="20250101",
            end_date="20250228",
            symbols=SYMBOLS,
        )
        self.assertGreater(result["stored_rows"], 0)
        self.assertFalse(result["errors"])

        panel = tushare_factor_service.load_factor_panel(
            "roe_ttm", SYMBOLS, "20250101", "20250228"
        )
        self.assertIsNotNone(panel)
        self.assertEqual(list(panel.columns), SYMBOLS)
        self.assertGreater(panel.shape[0], 30)
        self.assertAlmostEqual(float(panel.iloc[0]["600006.SH"]), 6.0)

        values = tushare_factor_service.query_values("roe_ttm", symbol="600001.SH", limit=10)
        self.assertEqual(len(values), 10)
        self.assertEqual(values[0]["symbol"], "600001.SH")

        # 幂等：重复同步同一区间不产生重复行
        with self.SessionFactory() as db:
            before = db.scalar(select(func.count()).select_from(TushareFactorValue))
        tushare_factor_service.sync_factor_values(
            ["roe_ttm"], start_date="20250101", end_date="20250228", symbols=SYMBOLS
        )
        with self.SessionFactory() as db:
            after = db.scalar(select(func.count()).select_from(TushareFactorValue))
        self.assertEqual(before, after)

    # ------------------------------------------------ 评估管线集成

    def test_benchmark_mixed_tushare_and_local_factors(self) -> None:
        tushare_factor_service.sync_factor_values(
            ["roe_ttm"], start_date="20250101", end_date="20250315", symbols=SYMBOLS
        )
        result = benchmark_factors(
            ["tushare:roe_ttm", "qlib158_roc5"],
            SYMBOLS,
            start_date="20250101",
            end_date="20250315",
            dedup_threshold=0.999,
        )
        by_id = {item["id"]: item for item in result["results"]}
        self.assertIn("tushare:roe_ttm", by_id)
        self.assertIn("qlib158_roc5", by_id)
        self.assertEqual(by_id["tushare:roe_ttm"]["zoo"], "tushare")
        self.assertGreater(by_id["tushare:roe_ttm"]["ic_count"], 20)
        self.assertFalse(result["skipped"])
        self.assertIsNotNone(result["dedup"])

    def test_benchmark_missing_cache_is_skipped_with_reason(self) -> None:
        tushare_factor_service.sync_catalog()
        result = benchmark_factors(
            ["tushare:roe_ttm"],
            SYMBOLS,
            start_date="20250101",
            end_date="20250315",
        )
        self.assertEqual(result["results"], [])
        self.assertEqual(len(result["skipped"]), 1)
        self.assertIn("TuShare 因子值尚未摄入", result["skipped"][0]["reason"])

    # ------------------------------------------------ API 层

    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(tushare_factor_router)
        return TestClient(app)

    def _token(self) -> str:
        with self.SessionFactory() as db:
            user = db.scalar(select(User).where(User.username == "quant-researcher"))
        return create_access_token(user)

    def test_api_requires_token(self) -> None:
        client = self._client()
        self.assertEqual(client.get("/api/factors/tushare/catalog").status_code, 401)
        self.assertEqual(client.post("/api/factors/tushare/catalog/sync").status_code, 401)
        self.assertEqual(
            client.post("/api/factors/tushare/sync", json={"factor_names": ["roe_ttm"]}).status_code,
            401,
        )

    def test_api_catalog_and_values_envelope(self) -> None:
        client = self._client()
        headers = {"Authorization": f"Bearer {self._token()}"}

        response = client.post("/api/factors/tushare/catalog/sync", headers=headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(body["data"]["total"], 2)

        response = client.get("/api/factors/tushare/catalog", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total"], 2)

        response = client.post(
            "/api/factors/tushare/sync",
            headers=headers,
            json={
                "factor_names": ["roe_ttm"],
                "start_date": "20250101",
                "end_date": "20250131",
                "symbols": SYMBOLS,
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertGreater(response.json()["data"]["stored_rows"], 0)

        response = client.get(
            "/api/factors/tushare/values",
            headers=headers,
            params={"factor_name": "roe_ttm", "symbol": "600001.SH", "limit": 5},
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["total"], 5)

        response = client.post(
            "/api/factors/tushare/sync",
            headers=headers,
            json={"factor_names": ["ghost_factor"]},
        )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])


if __name__ == "__main__":
    unittest.main()
