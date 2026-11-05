"""模型排名：树模型计算、线上面板、排名结果、影子 B 怎么用它、以及老表升级。内存 SQLite，不连 MySQL。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core import database
from backend.app.core.models import Base, DailyBar, Instrument, MarketTradingCalendar, ShadowNav, StockDailyBasic
from backend.app.quant import tree_model
from backend.app.quant.research_models import RANK_MODEL_FEATURES
from backend.app.shadow import rank_signal, service

NODE = {"decision_type": "<=", "default_left": True, "missing_type": "NaN"}
DUMP = {
    **NODE,
    "split_feature": 0,
    "threshold": 0.5,
    "left_child": {"leaf_value": 1.0},
    "right_child": {
        **NODE,
        "split_feature": 1,
        "threshold": 0.3,
        "default_left": False,
        "missing_type": "None",
        "left_child": {"leaf_value": 2.0},
        "right_child": {"leaf_value": 3.0},
    },
}


def _model(*trees: dict) -> dict:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "model.json"
        path.write_text(json.dumps({"names": ["a", "b"], "trees": [tree_model.flatten_tree(tree) for tree in trees]}))
        return tree_model.load_model(path)


class TreeModelTests(unittest.TestCase):
    def test_splits_follow_the_threshold(self) -> None:
        x = np.array([[0.5, 9.0], [0.6, 0.3], [0.6, 0.31]])
        self.assertEqual(tree_model.predict(_model(DUMP), x).tolist(), [1.0, 2.0, 3.0])

    def test_a_missing_value_goes_the_default_way(self) -> None:
        self.assertEqual(tree_model.predict(_model(DUMP), np.array([[np.nan, 0.0]])).tolist(), [1.0])

    def test_a_feature_without_missing_in_training_treats_it_as_zero(self) -> None:
        # 第二层 missing_type=None：缺失当 0，0 <= 0.3 走左边，即使 default_left 是 False
        self.assertEqual(tree_model.predict(_model(DUMP), np.array([[0.9, np.nan]])).tolist(), [2.0])

    def test_trees_add_up(self) -> None:
        self.assertEqual(tree_model.predict(_model(DUMP, {"leaf_value": 0.5}), np.array([[0.1, 0.1]])).tolist(), [1.5])

    def test_a_single_leaf_tree_works(self) -> None:
        self.assertEqual(tree_model.predict(_model({"leaf_value": 0.25}), np.zeros((2, 2))).tolist(), [0.25, 0.25])

    def test_unsupported_splits_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            tree_model.flatten_tree({**DUMP, "decision_type": "=="})
        with self.assertRaises(ValueError):
            tree_model.flatten_tree({**DUMP, "missing_type": "Zero"})

    def test_a_missing_file_is_no_model(self) -> None:
        self.assertIsNone(tree_model.load_model(Path("/nonexistent/model.json")))

    def test_the_shipped_model_loads_and_scores(self) -> None:
        model = tree_model.load_model(rank_signal.MODEL_PATH)
        self.assertEqual(len(model["trees"]), 200)
        self.assertEqual(tuple(model["names"]), RANK_MODEL_FEATURES)
        scores = tree_model.predict(model, np.array([[0.2] * 7, [0.8] * 7, [np.nan] * 7], dtype=np.float32))
        self.assertTrue(np.isfinite(scores).all())
        self.assertGreater(len(set(scores.tolist())), 1)


GOOD, ST, NEW, THIN, OTHER = "000001.SZ", "000002.SZ", "000003.SZ", "000004.SZ", "920001.BJ"
TODAY = "20261105"
NAMES = list(RANK_MODEL_FEATURES)


def _days(count: int = 100) -> list[str]:
    days: list[str] = []
    stamp = datetime(2026, 11, 5)
    while len(days) < count:
        if stamp.weekday() < 5:
            days.append(stamp.strftime("%Y%m%d"))
        stamp -= timedelta(days=1)
    return days[::-1]


class _Db(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        self.days = _days()
        rng = np.random.default_rng(1)
        with self.Session() as db:
            for day in self.days:
                db.add(MarketTradingCalendar(exchange="SSE", cal_date=day, is_open=True, source="tushare"))
            stocks = {f"{600000 + i:06d}.SH": ("普通股", "20100101") for i in range(20)}
            stocks.update({GOOD: ("平安银行", "19910403"), ST: ("*ST 某某", "20000101"), NEW: ("新股", "20260901"), THIN: ("停牌很久", "20000101"), OTHER: ("北交所", "20200101")})
            for symbol, (name, listed) in stocks.items():
                db.add(Instrument(symbol=symbol, name=name, market="SH", type="stock", list_date=listed))
                price = 10.0
                bars = self.days[-10:] if symbol == THIN else self.days
                for day in bars:
                    previous = price
                    price = round(price * (1 + float(rng.normal(0, 0.02))), 2)
                    db.add(DailyBar(symbol=symbol, trade_date=day, open=previous, high=max(previous, price) + 0.1, low=min(previous, price) - 0.1, close=price, pre_close=previous, volume=1000 + float(rng.integers(0, 500)), amount=10000 + float(rng.integers(0, 5000)), source="tushare"))
                db.add(StockDailyBasic(symbol=symbol, trade_date=TODAY, turnover_rate=1.0, turnover_rate_f=float(rng.uniform(0.5, 5))))
            db.commit()
        limits = patch.object(rank_signal, "_MIN_STOCKS_TODAY", 10)
        limits.start()
        self.addCleanup(limits.stop)
        # 一棵按 20 日成交额排名打分的树：排名结果可以预期
        model = _model({**NODE, "split_feature": 0, "threshold": 0.5, "left_child": {"leaf_value": 1.0}, "right_child": {"leaf_value": 2.0}})
        model["names"] = NAMES
        cache = patch.dict(rank_signal._cache, {"model": model})
        cache.start()
        self.addCleanup(cache.stop)

    def _ranks(self, trade_date: str = TODAY) -> dict[str, float]:
        with self.Session() as db:
            return rank_signal.latest_ranks(db, trade_date)


class LatestRankTests(_Db):
    def test_ranks_cover_the_universe_between_zero_and_one(self) -> None:
        ranks = self._ranks()
        self.assertIn(GOOD, ranks)
        self.assertEqual(len(ranks), 21)
        self.assertTrue(all(0 < value <= 1 for value in ranks.values()))

    def test_st_new_thin_and_other_markets_are_left_out(self) -> None:
        ranks = self._ranks()
        for symbol in (ST, NEW, THIN, OTHER):
            self.assertNotIn(symbol, ranks)

    def test_higher_scores_rank_higher(self) -> None:
        ranks = self._ranks()
        self.assertEqual(len(set(ranks.values())), 2)  # 两个叶子，两档排名

    def test_a_day_the_market_is_closed_has_no_ranks(self) -> None:
        self.assertEqual(self._ranks("20261108"), {})

    def test_no_turnover_yet_means_no_ranks_and_no_bar_scan(self) -> None:
        with self.Session() as db:
            for row in db.scalars(select(StockDailyBasic)).all():
                row.turnover_rate_f = None
            db.commit()
        with patch.object(rank_signal, "panel_from_rows") as panel:
            self.assertEqual(self._ranks(), {})
        panel.assert_not_called()

    def test_too_few_bars_today_means_no_ranks(self) -> None:
        with self.Session() as db:
            for row in db.scalars(select(DailyBar).where(DailyBar.trade_date == TODAY)).all():
                db.delete(row)
            db.commit()
        self.assertEqual(self._ranks(), {})

    def test_no_model_file_means_no_ranks(self) -> None:
        with patch.dict(rank_signal._cache, {"model": None}):
            self.assertEqual(self._ranks(), {})

    def test_live_panel_matches_the_research_columns(self) -> None:
        panel, today = rank_signal.panel_from_rows(
            ["20261104", TODAY],
            [GOOD, ST],
            [(GOOD, TODAY, 1.0, 2.0, 0.5, 1.5, 1.0, 100.0, None), (OTHER, TODAY, 1, 1, 1, 1, 1, 1, 1), (GOOD, "19990101", 1, 1, 1, 1, 1, 1, 1)],
            {GOOD: 3.0, OTHER: 9.0},
            {ST},
        )
        self.assertEqual(today, 1)
        self.assertEqual(float(panel.field("daily", "vol")[1, 0]), 100.0)
        self.assertTrue(np.isnan(panel.field("daily", "amount")[1, 0]))
        self.assertEqual(float(panel.field("daily_basic", "turnover_rate_f")[1, 0]), 3.0)
        self.assertTrue(panel.st[1, 1])
        self.assertEqual(panel.min_listed, rank_signal.LIVE_MIN_BARS)

    def test_listing_age_uses_the_trade_date(self) -> None:
        instruments = [("a", "平安", "20260601"), ("b", "平安", "20260401"), ("c", "ST 甲", "19900101"), ("d", "平安", None), ("e", None, "2026-10-01")]
        self.assertEqual(rank_signal.excluded_symbols(instruments, TODAY), {"a", "c", "e"})


class RanksForTodayTests(_Db):
    def _account(self, strategy: str = service.RISK_RANK) -> int:
        with self.Session() as db:
            account = service.ShadowAccount(owner_user_id=1, real_account_id=1, strategy=strategy, account_label="x", started_on=TODAY, idle_cash=0.0, pool=0.0)
            db.add(account)
            db.commit()
            return account.id

    def test_runs_while_a_rank_account_is_unsettled(self) -> None:
        self._account()
        with patch.object(service, "latest_ranks", return_value={GOOD: 0.5}) as ranks:
            self.assertEqual(service._ranks_for_today(self.Session, TODAY), {GOOD: 0.5})
        ranks.assert_called_once()

    def test_skips_the_market_scan_once_settled(self) -> None:
        account = self._account()
        with self.Session() as db:
            db.add(ShadowNav(shadow_account_id=account, owner_user_id=1, trade_date=TODAY, shadow_value=1.0, real_value=1.0))
            db.commit()
        with patch.object(service, "latest_ranks") as ranks:
            self.assertEqual(service._ranks_for_today(self.Session, TODAY), {})
        ranks.assert_not_called()

    def test_plain_risk_accounts_do_not_need_ranks(self) -> None:
        self._account(service.RISK)
        with patch.object(service, "latest_ranks") as ranks:
            service._ranks_for_today(self.Session, TODAY)
        ranks.assert_not_called()

    def test_a_failure_means_no_tilt_today(self) -> None:
        self._account()
        with patch.object(service, "latest_ranks", side_effect=RuntimeError("boom")):
            self.assertEqual(service._ranks_for_today(self.Session, TODAY), {})


class CompareTests(unittest.TestCase):
    def _nav(self, day: str, shadow: float, real: float = 100.0) -> SimpleNamespace:
        return SimpleNamespace(trade_date=day, shadow_value=shadow, real_value=real)

    def test_a_later_start_lines_everyone_up_from_that_day(self) -> None:
        result = service.compare({"risk": [self._nav("1", 90), self._nav("2", 91), self._nav("3", 92)], "risk_rank": [self._nav("2", 100), self._nav("3", 101)]})
        self.assertEqual(result["days"], ["2", "3"])
        self.assertEqual(result["series"], {"risk": [91, 92], "risk_rank": [100, 101]})
        self.assertEqual(result["real"], [100.0, 100.0])

    def test_nothing_recorded_is_empty(self) -> None:
        self.assertEqual(service.compare({})["days"], [])


class _FakeConnection:
    def __init__(self, columns: set[tuple[str, str]], indexes: set[tuple[str, str]]) -> None:
        self.columns, self.indexes, self.altered = columns, indexes, []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, statement, params=None):
        sql = str(statement)
        if "information_schema.columns" in sql:
            found = (params["table"], params["column"]) in self.columns
        elif "information_schema.statistics" in sql:
            found = (params["table"], params["index"]) in self.indexes
        else:
            self.altered.append(sql)
            return None
        return SimpleNamespace(scalar=lambda: int(found))


class MigrationTests(unittest.TestCase):
    def _run(self, connection: _FakeConnection, dialect: str = "mysql") -> list[str]:
        engine = SimpleNamespace(dialect=SimpleNamespace(name=dialect), begin=lambda: connection)
        database._ensure_quant_upgrades(engine)
        return connection.altered

    def test_an_old_database_is_upgraded(self) -> None:
        altered = self._run(_FakeConnection(set(), {("shadow_accounts", "uq_shadow_real_account")}))
        self.assertEqual(len(altered), 4)
        self.assertIn("ADD COLUMN turnover_rate_f", altered[0])
        self.assertIn("ADD COLUMN strategy", altered[1])
        self.assertIn("DROP INDEX uq_shadow_real_account", altered[2])
        self.assertIn("uq_shadow_account_strategy", altered[3])

    def test_a_current_database_is_left_alone(self) -> None:
        connection = _FakeConnection({("stock_daily_basic", "turnover_rate_f"), ("shadow_accounts", "strategy")}, {("shadow_accounts", "uq_shadow_account_strategy")})
        self.assertEqual(self._run(connection), [])

    def test_only_mysql_is_touched(self) -> None:
        self.assertEqual(self._run(_FakeConnection(set(), set()), "sqlite"), [])


if __name__ == "__main__":
    unittest.main()
