from __future__ import annotations

import os
import unittest

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url

from backend.app.core.database import Base

TEST_DATABASE_ENV = "STOCK_AUTOMATIC_SELECTION_TEST_DATABASE_URL"


def create_isolated_mysql_engine() -> Engine:
    raw_url = os.getenv(TEST_DATABASE_ENV, "").strip()
    if not raw_url:
        raise unittest.SkipTest(
            f"未配置 {TEST_DATABASE_ENV}，跳过自动选股 MySQL 集成测试"
        )
    url = make_url(raw_url)
    database = (url.database or "").lower()
    if url.drivername != "mysql+pymysql":
        raise RuntimeError(f"{TEST_DATABASE_ENV} 必须使用 mysql+pymysql")
    if database in {"hermes-stock", "hermes_stock"} or not database.endswith(
        ("_test", "-test")
    ):
        raise RuntimeError(
            f"{TEST_DATABASE_ENV} 必须指向以 _test 或 -test 结尾的隔离数据库"
        )
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        engine.dispose()
        raise unittest.SkipTest(f"自动选股 MySQL 测试库不可达：{exc}") from exc
    return engine


def reset_isolated_schema(engine: Engine) -> None:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
