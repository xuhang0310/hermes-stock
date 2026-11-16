"""成绩单的登记和结算：只算给客户看过的建议，只用到期后的行情，重复跑不会变。用内存库，不连 MySQL。"""
from __future__ import annotations

import unittest
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.core.models import AdviceOutcome, DailyBar, Instrument, MarketDailyReturn, MarketTradingCalendar, PositionAccount, StockModelSignal, TradePlan, TradePlanItem, User
from backend.app.scorecard import rules, service

DAYS = [f"202611{day:02d}" for day in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13, 16, 17, 18, 19, 20, 23, 24, 25, 26, 27)] + ["20261130", "20261201"]
STOCK = "600000.SH"
OTHERS = ["600001.SH", "600002.SH", "000001.SZ"]
OWNER = 7
OTHER_OWNER = 99
ACCOUNT_A = 31
ACCOUNT_B = 32
OTHER_ACCOUNT = 41


def _iso(day: str) -> str:
    return f"{day[:4]}-{day[4:6]}-{day[6:]}"


class ScorecardServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        with self.Session() as db:
            for symbol in [STOCK, *OTHERS]:
                db.add(Instrument(symbol=symbol, name=symbol, market=symbol[-2:], type="stock"))
            db.add(Instrument(symbol="510300.SH", name="沪深300ETF", market="SH", type="etf"))
            for day in DAYS:
                db.add(MarketTradingCalendar(exchange="SSE", cal_date=day, is_open=True))
            for user_id in (OWNER, OTHER_OWNER):
                db.add(User(id=user_id, username=f"user{user_id}", display_name=f"用户{user_id}", password_hash="x", role="researcher"))
            db.commit()
            for account_id, owner, broker in ((ACCOUNT_A, OWNER, "华泰证券"), (ACCOUNT_B, OWNER, "中信证券"), (OTHER_ACCOUNT, OTHER_OWNER, "国泰君安")):
                db.add(PositionAccount(id=account_id, owner_user_id=owner, broker_name=broker, account_no_masked=str(account_id)))
            db.commit()
            # STOCK 每天跌 1%，其他三只每天涨 1%：全市场平均 +0.5%
            for symbol, move in [(STOCK, -0.01), *[(other, 0.01) for other in OTHERS]]:
                price = 10.0
                for day in DAYS:
                    db.add(DailyBar(symbol=symbol, trade_date=day, close=price * (1 + move), pre_close=price, source="tushare"))
                    price *= 1 + move
            db.commit()

    def _plan(self, plan_id: str, day: str, *, status: str = "active", source: str = "premarket", activated: datetime | None = None, owner: int = OWNER) -> None:
        with self.Session() as db:
            db.add(TradePlan(plan_id=plan_id, owner_user_id=owner, plan_date=_iso(day), source=source, job_run_id=plan_id, status=status, activated_at=activated or datetime(2026, 11, 2, 1, 0)))
            db.commit()

    def _item(self, plan_id: str, action: str, *, symbol: str = STOCK, owner: int = OWNER, account: int | None = ACCOUNT_A, **fields) -> int:
        with self.Session() as db:
            item = TradePlanItem(plan_id=plan_id, owner_user_id=owner, account_id=account, symbol=symbol, name="测试", action=action, valid_until="2026-11-30", **fields)
            db.add(item)
            db.commit()
            return item.id

    def _run(self, last: str = DAYS[-1]) -> None:
        with self.Session() as db:
            service.ensure_market_returns(db, DAYS[0], last, min_stocks=3)
            service.record_advice(db, DAYS[0])
            service.settle(db, last)
            db.commit()

    def _outcomes(self, horizon: int = 5) -> list[AdviceOutcome]:
        with self.Session() as db:
            return list(db.scalars(select(AdviceOutcome).where(AdviceOutcome.horizon_days == horizon).order_by(AdviceOutcome.id)).all())

    def test_the_market_average_is_the_equal_weight_mean(self) -> None:
        with self.Session() as db:
            self.assertEqual(service.ensure_market_returns(db, DAYS[0], DAYS[-1], min_stocks=3), len(DAYS))
            db.commit()
            row = db.get(MarketDailyReturn, DAYS[3])
            self.assertAlmostEqual(row.mean_return, 0.005)
            self.assertEqual(row.stock_count, 4)
            self.assertEqual(service.ensure_market_returns(db, DAYS[0], DAYS[-1], min_stocks=3), 0)  # 已经有的不重算

    def test_days_without_enough_stocks_wait_for_the_data(self) -> None:
        with self.Session() as db:
            self.assertEqual(service.ensure_market_returns(db, DAYS[0], DAYS[-1], min_stocks=10), 0)
            self.assertIsNone(db.get(MarketDailyReturn, DAYS[0]))

    def test_a_sell_before_a_fall_is_right(self) -> None:
        self._plan("p1", DAYS[0])
        self._item("p1", "sell", trigger_price=10.5)
        self._run()
        row = self._outcomes(5)[0]
        self.assertEqual(row.verdict, rules.RIGHT)
        self.assertEqual(row.due_date, DAYS[5])
        self.assertAlmostEqual(row.stock_return, 0.99**5 - 1)
        self.assertAlmostEqual(row.market_return, 1.005**5 - 1)
        self.assertAlmostEqual(row.excess_return, row.stock_return - row.market_return)

    def test_a_buy_before_a_fall_is_wrong(self) -> None:
        self._plan("p1", DAYS[0])
        self._item("p1", "buy")
        self._run()
        self.assertEqual(self._outcomes(5)[0].verdict, rules.WRONG)

    def test_the_result_only_uses_days_after_the_advice(self) -> None:
        """建议当天的涨跌不算：结果从当天收盘算起。"""
        self._plan("p1", DAYS[2])
        self._item("p1", "sell")
        with self.Session() as db:
            bar = db.scalar(select(DailyBar).where(DailyBar.symbol == STOCK, DailyBar.trade_date == DAYS[2]))
            bar.close = bar.pre_close * 1.2  # 建议当天大涨
            db.commit()
        self._run()
        self.assertAlmostEqual(self._outcomes(5)[0].stock_return, 0.99**5 - 1)

    def test_advice_stays_pending_until_the_horizon_has_passed(self) -> None:
        self._plan("p1", DAYS[0])
        self._item("p1", "sell")
        self._run(last=DAYS[4])  # 只过了 4 个交易日
        self.assertIsNone(self._outcomes(5)[0].verdict)
        self._run(last=DAYS[5])
        self.assertEqual(self._outcomes(5)[0].verdict, rules.RIGHT)
        self.assertIsNone(self._outcomes(10)[0].verdict)

    def test_each_horizon_gets_its_own_row(self) -> None:
        self._plan("p1", DAYS[0])
        self._item("p1", "sell")
        self._run()
        with self.Session() as db:
            rows = db.scalars(select(AdviceOutcome)).all()
        self.assertEqual(sorted(row.horizon_days for row in rows), list(rules.HORIZONS))

    def test_drafts_and_unmatched_items_are_not_advice(self) -> None:
        self._plan("draft", DAYS[0], status="draft")
        self._item("draft", "sell")
        self._plan("p1", DAYS[0])
        self._item("p1", "sell", text_matched=False)
        self._item("p1", "cancel", symbol=OTHERS[0])
        self._run()
        self.assertEqual(self._outcomes(5), [])

    def test_the_same_stock_on_the_same_day_counts_once_and_the_later_plan_wins(self) -> None:
        self._plan("morning", DAYS[0], status="superseded", activated=datetime(2026, 11, 2, 1, 0))
        self._item("morning", "sell", trigger_price=10.5)
        self._plan("noon", DAYS[0], source="midday", activated=datetime(2026, 11, 2, 4, 0))
        self._item("noon", "reduce", trigger_price=10.2)
        self._run()
        rows = self._outcomes(5)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0].plan_id, rows[0].action, rows[0].trigger_price), ("noon", "reduce", 10.2))

    def test_running_twice_changes_nothing(self) -> None:
        self._plan("p1", DAYS[0])
        self._item("p1", "sell")
        self._run()
        first = [(row.id, row.verdict, row.excess_return) for row in self._outcomes(5)]
        self._run()
        with self.Session() as db:
            total = len(db.scalars(select(AdviceOutcome)).all())
        self.assertEqual(total, len(rules.HORIZONS))
        self.assertEqual([(row.id, row.verdict, row.excess_return) for row in self._outcomes(5)], first)

    def test_a_later_fill_marks_the_advice_as_followed_without_touching_the_result(self) -> None:
        self._plan("p1", DAYS[0])
        item_id = self._item("p1", "sell")
        self._run()
        self.assertFalse(self._outcomes(5)[0].followed)
        with self.Session() as db:
            db.get(TradePlanItem, item_id).executed_qty = 300
            db.commit()
        self._run()
        row = self._outcomes(5)[0]
        self.assertTrue(row.followed)
        self.assertEqual(row.verdict, rules.RIGHT)

    def test_a_stock_with_no_bars_is_marked_instead_of_guessed(self) -> None:
        self._plan("p1", DAYS[0])
        self._item("p1", "hold", symbol="510300.SH")
        self._run()
        row = self._outcomes(5)[0]
        self.assertEqual(row.verdict, rules.NO_DATA)
        self.assertIsNone(row.excess_return)

    def test_the_model_rank_is_taken_from_the_day_before_the_advice(self) -> None:
        with self.Session() as db:
            db.add(StockModelSignal(trade_date=DAYS[1], symbol=STOCK, model_rank=0.1))
            db.add(StockModelSignal(trade_date=DAYS[2], symbol=STOCK, model_rank=0.9))  # 建议当天的不能用
            db.commit()
        self._plan("p1", DAYS[2])
        self._item("p1", "sell")
        self._run()
        self.assertAlmostEqual(self._outcomes(5)[0].model_rank, 0.1)

    def test_the_summary_is_per_user_and_groups_the_results(self) -> None:
        self._plan("p1", DAYS[0])
        self._item("p1", "sell", size_note="客户要求多卖")
        self._item("p1", "buy", symbol=OTHERS[0])
        self._plan("other", DAYS[0], owner=OTHER_OWNER)
        self._item("other", "buy", owner=OTHER_OWNER, account=OTHER_ACCOUNT)
        self._run()
        with self.Session() as db:
            mine = service.summary(db, OWNER, horizon=5, days=60, now=datetime(2026, 12, 2))
            theirs = service.summary(db, OTHER_OWNER, horizon=5, days=60, now=datetime(2026, 12, 2))
        self.assertEqual((mine["overall"]["total"], mine["overall"]["right"]), (2, 2))
        self.assertEqual((theirs["overall"]["total"], theirs["overall"]["wrong"]), (1, 1))
        self.assertFalse(mine["overall"]["enough"])
        self.assertEqual({group["label"] for group in mine["by_action"]}, {"卖出", "买入"})
        self.assertEqual(mine["by_source"][0]["label"], "早盘计划")
        self.assertEqual({group["label"]: group["total"] for group in mine["by_size"]}, {"数字人改了股数": 1, "按代码建议的股数": 1})
        self.assertEqual(len(mine["recent"]), 2)
        self.assertEqual(mine["pending"], 0)
        self.assertEqual([account["account_id"] for account in mine["accounts"]], [ACCOUNT_A])
        self.assertEqual([account["account_id"] for account in theirs["accounts"]], [OTHER_ACCOUNT])
        self.assertTrue(all(item["account_id"] == ACCOUNT_A for item in mine["recent"]))

    def test_two_accounts_of_one_user_are_scored_separately(self) -> None:
        """同一位用户两个账户同一天都卖同一只股票：是两条建议，各自记有没有照做。"""
        self._plan("p1", DAYS[0])
        self._item("p1", "sell", account=ACCOUNT_A, executed_qty=300)
        self._item("p1", "sell", account=ACCOUNT_B)
        self._item("p1", "buy", symbol=OTHERS[0], account=ACCOUNT_B)
        self._run()
        rows = self._outcomes(5)
        self.assertCountEqual([(row.account_id, row.symbol) for row in rows], [(ACCOUNT_A, STOCK), (ACCOUNT_B, OTHERS[0]), (ACCOUNT_B, STOCK)])
        self.assertEqual({row.account_id: row.followed for row in rows if row.symbol == STOCK}, {ACCOUNT_A: True, ACCOUNT_B: False})
        with self.Session() as db:
            everything = service.summary(db, OWNER, horizon=5, days=60, now=datetime(2026, 12, 2))
            only_a = service.summary(db, OWNER, account_id=ACCOUNT_A, horizon=5, days=60, now=datetime(2026, 12, 2))
            only_b = service.summary(db, OWNER, account_id=ACCOUNT_B, horizon=5, days=60, now=datetime(2026, 12, 2))
        self.assertEqual((everything["overall"]["total"], only_a["overall"]["total"], only_b["overall"]["total"]), (3, 1, 2))
        self.assertEqual({group["label"]: group["total"] for group in everything["by_account"]}, {f"华泰证券 账户{ACCOUNT_A}": 1, f"中信证券 账户{ACCOUNT_B}": 2})
        self.assertEqual([account["account_id"] for account in only_a["accounts"]], [ACCOUNT_A, ACCOUNT_B])  # 筛了账户，下拉里还是自己的全部账户
        self.assertTrue(all(item["account_id"] == ACCOUNT_B for item in only_b["recent"]))

    def test_asking_for_someone_elses_account_returns_nothing(self) -> None:
        self._plan("other", DAYS[0], owner=OTHER_OWNER)
        self._item("other", "buy", owner=OTHER_OWNER, account=OTHER_ACCOUNT)
        self._run()
        with self.Session() as db:
            peek = service.summary(db, OWNER, account_id=OTHER_ACCOUNT, horizon=5, days=60, now=datetime(2026, 12, 2))
        self.assertEqual(peek["overall"]["total"], 0)
        self.assertEqual(peek["recent"], [])
        self.assertEqual(peek["accounts"], [])
        self.assertEqual(peek["by_account"], [])

    def test_an_item_whose_owner_differs_from_its_plan_is_not_recorded(self) -> None:
        self._plan("p1", DAYS[0], owner=OWNER)
        self._item("p1", "sell", owner=OTHER_OWNER, account=OTHER_ACCOUNT)
        self._run()
        self.assertEqual(self._outcomes(5), [])

    def test_a_manual_refresh_only_touches_the_callers_advice(self) -> None:
        self._plan("p1", DAYS[0])
        self._item("p1", "sell")
        self._plan("other", DAYS[0], owner=OTHER_OWNER)
        self._item("other", "buy", owner=OTHER_OWNER, account=OTHER_ACCOUNT)
        with self.Session() as db:
            service.ensure_market_returns(db, DAYS[0], DAYS[-1], min_stocks=3)
            db.commit()
        service.run_daily(datetime(2026, 12, 1, 18, 30), owner_user_id=OWNER, session_factory=self.Session)
        self.assertEqual({row.owner_user_id for row in self._outcomes(5)}, {OWNER})
        service.run_daily(datetime(2026, 12, 1, 18, 30), session_factory=self.Session)  # 定时任务处理所有人
        self.assertEqual({row.owner_user_id for row in self._outcomes(5)}, {OWNER, OTHER_OWNER})

    def test_the_daily_job_reports_what_it_did_and_survives_errors(self) -> None:
        self._plan("p1", DAYS[0])
        self._item("p1", "sell")
        with self.Session() as db:
            service.ensure_market_returns(db, DAYS[0], DAYS[-1], min_stocks=3)
            db.commit()
        message = service.run_daily(datetime(2026, 12, 1, 18, 30), session_factory=self.Session)
        self.assertIn("新登记 1 条建议", message)
        self.assertIn("算出 3 条结果", message)

        def broken():
            raise RuntimeError("库连不上")

        self.assertIn("成绩单更新失败", service.run_daily(datetime(2026, 12, 1, 18, 30), session_factory=broken))


if __name__ == "__main__":
    unittest.main()
