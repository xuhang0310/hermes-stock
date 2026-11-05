"""用内存 SQLite 验证影子账户规则、模拟成交、净值对比和重复结算。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.models import (
    Base,
    DailyBar,
    Instrument,
    Notification,
    PositionAccount,
    PositionHoldingSnapshot,
    ReportShare,
    ShadowAccount,
    ShadowNav,
    ShadowOrder,
    ShadowPosition,
    StockLatestPrice,
)
from backend.app.quant.order_engine import Level, OrderLevels
from backend.app.quant.order_table_build import CELLS
from backend.app.shadow import rules, service
from backend.app.shadow.page import render_shadow_page

OWNER = 7
ACCOUNT = 3
SYMBOL = "605286.SH"
ETF = "159650.SZ"
CASH = 100_000.0
TABLE = {
    "sell": {cell: {"1.0": {"fill": 0.1, "edge": -0.01, "raw": 0, "n": 1000}} for cell in CELLS},
    "buy": {},
    "stop": {cell: {"3.0": {"fill": 0.01, "edge": 0.0, "raw": 0, "n": 1000}} for cell in CELLS},
}


def _levels(close: float = 20.0, atr: float = 1.0, sell: float | None = 21.0) -> OrderLevels:
    return OrderLevels(
        close=close,
        atr=atr,
        trend="range",
        vol_bucket=1,
        sell=Level(sell, 1.0, 0.1, -0.01, 0.001) if sell else None,
        buy=None,
        stop=None,
    )


class RuleTests(unittest.TestCase):
    def test_a_volatile_stock_gets_fewer_shares(self) -> None:
        self.assertAlmostEqual(rules.target_shares(1_000_000, 20, 1.0), 5000)
        self.assertAlmostEqual(rules.target_shares(1_000_000, 20, 2.0), 2500)

    def test_a_calm_stock_is_capped_at_a_quarter_of_the_money(self) -> None:
        self.assertAlmostEqual(rules.target_shares(1_000_000, 20, 0.1), 12500)

    def test_over_target_sells_the_excess_at_the_engine_price(self) -> None:
        order = rules.plan_order(SYMBOL, 7300, _levels(), 1_000_000, 0)
        self.assertEqual((order.side, order.price, order.quantity), ("sell", 21.0, 2300))
        self.assertIn("超出风险预算", order.basis)

    def test_no_sell_when_the_engine_finds_no_edge(self) -> None:
        self.assertIsNone(rules.plan_order(SYMBOL, 7300, _levels(sell=None), 1_000_000, 0))

    def test_a_small_gap_does_nothing(self) -> None:
        self.assertIsNone(rules.plan_order(SYMBOL, 5200, _levels(), 1_000_000, 50_000))
        self.assertIsNone(rules.plan_order(SYMBOL, 4800, _levels(), 1_000_000, 50_000))

    def test_under_target_buys_back_at_one_percent_below_close(self) -> None:
        order = rules.plan_order(SYMBOL, 4000, _levels(), 1_000_000, 500_000)
        self.assertEqual((order.side, order.price, order.quantity), ("buy", 19.8, 1000))

    def test_buy_back_needs_sale_money(self) -> None:
        self.assertIsNone(rules.plan_order(SYMBOL, 4000, _levels(), 1_000_000, 0))

    def test_buy_back_is_limited_by_the_money_left(self) -> None:
        order = rules.plan_order(SYMBOL, 4000, _levels(), 1_000_000, 5_000)
        self.assertEqual(order.quantity, 200)

    def test_star_market_orders_are_at_least_two_hundred(self) -> None:
        star = "688091.SH"  # 投入 20 万时目标 1000 股
        self.assertIsNone(rules.plan_order(star, 1150, _levels(), 200_000, 0))  # 超出 150 股，取整后只有 100 股
        self.assertEqual(rules.plan_order(star, 1300, _levels(), 200_000, 0).quantity, 300)

    def test_fills_need_the_price_to_be_touched(self) -> None:
        self.assertEqual(rules.fill_price("sell", 21, 20, 21.5, 19.5), 21)
        self.assertEqual(rules.fill_price("sell", 21, 22, 22.5, 21.5), 22)
        self.assertIsNone(rules.fill_price("sell", 21, 20, 20.9, 19.5))
        self.assertEqual(rules.fill_price("buy", 19.8, 20, 20.5, 19.5), 19.8)
        self.assertEqual(rules.fill_price("buy", 19.8, 19.5, 20, 19.2), 19.5)
        self.assertIsNone(rules.fill_price("buy", 19.8, 20, 20.5, 19.9))

    def test_a_one_price_day_cannot_fill(self) -> None:
        self.assertIsNone(rules.fill_price("sell", 21, 22, 22, 22))
        self.assertIsNone(rules.fill_price("buy", 19.8, 18, 18, 18))

    def test_ex_rights_ratio(self) -> None:
        self.assertEqual(rules.ex_rights_ratio(20, 10), 2.0)
        self.assertEqual(rules.ex_rights_ratio(20, 20), 1.0)
        self.assertEqual(rules.ex_rights_ratio(None, 10), 1.0)
        self.assertEqual(rules.ex_rights_ratio(20, 0.5), 1.0)

    def test_annual_return_waits_for_sixty_days(self) -> None:
        self.assertIsNone(rules.annualized(100, 110, 59))
        self.assertAlmostEqual(rules.annualized(100, 110, rules.TRADING_DAYS), 0.10)

    def test_drawdown(self) -> None:
        self.assertAlmostEqual(rules.max_drawdown([100, 120, 90, 130]), -0.25)
        self.assertEqual(rules.max_drawdown([100, 110]), 0.0)


class _Case(unittest.TestCase):
    START = datetime(2026, 7, 1)

    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        patches = [
            patch.object(service, "load_table", return_value=TABLE),
            patch.object(service, "latest_ranks", return_value={SYMBOL: 0.5}),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with self.Session() as db:
            db.add(Instrument(symbol=SYMBOL, name="同力天启", market="SH", type="stock"))
            db.add(PositionAccount(id=ACCOUNT, owner_user_id=OWNER, broker_name="方正证券", available_cash=CASH, status="active"))
            db.add(self._snapshot(10_000))
            self.days = []
            for index in range(80):
                day = (self.START + timedelta(days=index)).strftime("%Y%m%d")
                self.days.append(day)
                self._bar(db, day)
            db.commit()
        self.today = self.days[-1]

    def _snapshot(self, quantity: float, status: str = "open", account: int = ACCOUNT) -> PositionHoldingSnapshot:
        return PositionHoldingSnapshot(
            owner_user_id=OWNER,
            account_id=account,
            symbol=SYMBOL,
            symbol_name="同力天启",
            position_quantity=quantity,
            source_type="manual",
            status=status,
        )

    def _bar(self, db, day: str, open_: float = 20.0, high: float = 20.5, low: float = 19.5, close: float = 20.0, pre_close: float = 20.0) -> None:
        db.add(DailyBar(symbol=SYMBOL, trade_date=day, open=open_, high=high, low=low, close=close, pre_close=pre_close, source="tushare"))

    def _add_day(self, **bar) -> datetime:
        day = (datetime.strptime(self.days[-1], "%Y%m%d") + timedelta(days=1))
        stamp = day.strftime("%Y%m%d")
        self.days.append(stamp)
        with self.Session() as db:
            self._bar(db, stamp, **bar)
            db.commit()
        return day.replace(hour=18, minute=5)

    def _run(self, now: datetime | None = None) -> str:
        now = now or datetime.strptime(self.today, "%Y%m%d").replace(hour=18, minute=5)
        return service.run_shadow_day(now, session_factory=self.Session)

    def _account(self, db) -> ShadowAccount:
        return db.scalar(select(ShadowAccount).where(ShadowAccount.strategy == service.RISK))

    def _position(self, db) -> ShadowPosition:
        return db.scalar(
            select(ShadowPosition)
            .join(ShadowAccount, ShadowAccount.id == ShadowPosition.shadow_account_id)
            .where(ShadowAccount.strategy == service.RISK, ShadowPosition.symbol == SYMBOL)
        )

    def _navs(self, db) -> list[ShadowNav]:
        return list(db.scalars(
            select(ShadowNav)
            .join(ShadowAccount, ShadowAccount.id == ShadowNav.shadow_account_id)
            .where(ShadowAccount.strategy == service.RISK)
            .order_by(ShadowNav.trade_date)
        ).all())

    def _orders(self, db) -> list[ShadowOrder]:
        return list(db.scalars(
            select(ShadowOrder)
            .join(ShadowAccount, ShadowAccount.id == ShadowOrder.shadow_account_id)
            .where(ShadowAccount.strategy == service.RISK)
            .order_by(ShadowOrder.id)
        ).all())


class StartTests(_Case):
    def test_the_first_day_copies_the_real_account(self) -> None:
        self._run()
        with self.Session() as db:
            account = self._account(db)
            self.assertEqual((account.real_account_id, account.started_on, account.idle_cash, account.pool), (ACCOUNT, self.today, CASH, 0.0))
            self.assertEqual(account.account_label, "方正证券 账户3")
            self.assertEqual(self._position(db).quantity, 10_000)
            nav = self._navs(db)
            self.assertEqual(len(nav), 1)
            self.assertAlmostEqual(nav[0].shadow_value, 10_000 * 20 + CASH)
            self.assertAlmostEqual(nav[0].real_value, 10_000 * 20 + CASH)

    def test_the_first_day_plans_tomorrows_order(self) -> None:
        self._run()
        with self.Session() as db:
            order = self._orders(db)[0]
        # 投入 20 万，ATR 1 元：目标 1% × 20 万 ÷ 2 = 1000 股，超出 9000 股
        self.assertEqual((order.side, order.quantity, order.price, order.status), ("sell", 9000, 21.0, "pending"))
        self.assertEqual((order.basis_date, order.stock_name), (self.today, "同力天启"))

    def test_idle_cash_is_not_in_the_risk_base(self) -> None:
        with self.Session() as db:
            db.get(PositionAccount, ACCOUNT).available_cash = 9_000_000
            db.commit()
        self._run()
        with self.Session() as db:
            self.assertEqual(self._orders(db)[0].quantity, 9000)

    def test_an_archived_account_gets_no_shadow(self) -> None:
        with self.Session() as db:
            db.get(PositionAccount, ACCOUNT).status = "archived"
            db.commit()
        self._run()
        with self.Session() as db:
            self.assertIsNone(self._account(db))

    def test_settlement_saves_both_strategies_without_sending_notifications(self) -> None:
        with patch.object(service, "latest_ranks", return_value={SYMBOL: 1.0}):
            self.assertIn("2 个影子账户", self._run())
        with self.Session() as db:
            orders = dict(db.execute(
                select(ShadowAccount.strategy, ShadowOrder.quantity)
                .join(ShadowOrder, ShadowOrder.shadow_account_id == ShadowAccount.id)
            ).all())
            self.assertEqual(orders, {service.RISK: 9000, service.RISK_RANK: 8500})
            self.assertIsNone(db.scalar(select(Notification)))
            self.assertIsNone(db.scalar(select(ReportShare)))

    def test_rank_strategy_can_settle_after_ranks_arrive(self) -> None:
        with patch.object(service, "latest_ranks", return_value={}):
            self.assertIn("1 个影子账户", self._run())
        with self.Session() as db:
            self.assertEqual(service.owner_summary(db, OWNER)["days"], [])
        self.assertIn("1 个影子账户", self._run())
        with self.Session() as db:
            self.assertEqual(service.owner_summary(db, OWNER)["days"], [self.today])

    def _hold_etf(self, price: float | None) -> None:
        with self.Session() as db:
            db.add(Instrument(symbol=ETF, name="国开ETF", market="SZ", type="etf"))
            db.add(PositionHoldingSnapshot(owner_user_id=OWNER, account_id=ACCOUNT, symbol=ETF, symbol_name="国开ETF", position_quantity=5000, source_type="manual", status="open"))
            if price is not None:
                db.add(StockLatestPrice(symbol=ETF, price=price, trade_date="20260826", source="tushare.rt_k", fetched_at=datetime(2026, 7, 1)))
            db.commit()

    def test_a_holding_without_bars_is_valued_at_its_latest_price(self) -> None:
        self._hold_etf(100.0)
        self._run()
        with self.Session() as db:
            nav = self._navs(db)
            self.assertEqual(len(nav), 1)
            self.assertAlmostEqual(nav[0].shadow_value, 10_000 * 20 + 5000 * 100 + CASH)
            self.assertAlmostEqual(nav[0].real_value, 10_000 * 20 + 5000 * 100 + CASH)

    def test_a_holding_without_bars_gets_no_order_and_stays_out_of_the_risk_base(self) -> None:
        self._hold_etf(100.0)
        self._run()
        with self.Session() as db:
            orders = self._orders(db)
            self.assertEqual([order.symbol for order in orders], [SYMBOL])
            self.assertEqual(orders[0].quantity, 9000)  # 和没有 ETF 时一样

    def test_a_holding_with_no_price_at_all_records_nothing(self) -> None:
        self._hold_etf(None)
        self._run()
        with self.Session() as db:
            self.assertEqual(self._navs(db), [])
            self.assertEqual(self._orders(db), [])

    def test_no_table_means_no_run(self) -> None:
        with patch.object(service, "load_table", return_value=None):
            self.assertIn("没有量化统计表", self._run())

    def test_no_bar_today_means_nothing_is_recorded(self) -> None:
        late = datetime.strptime(self.today, "%Y%m%d") + timedelta(days=1)
        self._run(late.replace(hour=18, minute=5))
        with self.Session() as db:
            self.assertEqual(self._navs(db), [])
            self.assertEqual(self._orders(db), [])

    def test_the_run_never_raises(self) -> None:
        def broken():
            raise RuntimeError("boom")

        self.assertIn("影子账户运行失败", service.run_shadow_day(datetime(2026, 11, 5, 18, 5), session_factory=broken))


class SettleTests(_Case):
    def setUp(self) -> None:
        super().setUp()
        self._run()

    def test_a_touched_sell_fills_and_the_money_waits_for_buy_back(self) -> None:
        self._run(self._add_day(open_=20.0, high=21.5, low=19.8, close=21.2))
        with self.Session() as db:
            order, account, position = self._orders(db)[0], self._account(db), self._position(db)
            self.assertEqual((order.status, order.fill_price), ("filled", 21.0))
            self.assertEqual(position.quantity, 1000)
            self.assertAlmostEqual(account.pool, 9000 * 21 * (1 - 0.0013))
            self.assertEqual(account.idle_cash, CASH)
            nav = self._navs(db)[-1]
            self.assertAlmostEqual(nav.shadow_value, 1000 * 21.2 + account.pool + CASH)
            self.assertAlmostEqual(nav.real_value, 10_000 * 21.2 + CASH)

    def test_a_late_held_stock_bar_leaves_pending_orders_for_a_retry(self) -> None:
        other_symbol = "000001.SZ"
        now = datetime.strptime(self.today, "%Y%m%d") + timedelta(days=1, hours=18, minutes=5)
        trade_date = now.strftime("%Y%m%d")
        with self.Session() as db:
            db.add(Instrument(symbol=other_symbol, name="测试股票", market="SZ", type="stock"))
            other = self._snapshot(100)
            other.symbol = other_symbol
            db.add(other)
            db.add(DailyBar(symbol=other_symbol, trade_date=trade_date, open=20, high=21, low=19, close=20,
                            pre_close=20, source="tushare"))
            db.commit()
        with patch.object(service, "_full_day_suspensions", return_value=set()):
            self._run(now)
        with self.Session() as db:
            self.assertEqual(self._orders(db)[0].status, "pending")
            self.assertEqual([nav.trade_date for nav in self._navs(db)], [self.today])
            self._bar(db, trade_date, high=21.5, close=21.2)
            db.commit()
        self._run(now + timedelta(minutes=30))
        with self.Session() as db:
            self.assertEqual(self._orders(db)[0].status, "filled")
            self.assertEqual([nav.trade_date for nav in self._navs(db)], [self.today, trade_date])

    def test_an_opening_gap_fills_at_the_open(self) -> None:
        self._run(self._add_day(open_=22.0, high=22.5, low=21.8, close=22.0))
        with self.Session() as db:
            self.assertEqual(self._orders(db)[0].fill_price, 22.0)

    def test_an_untouched_order_expires_after_one_day(self) -> None:
        self._run(self._add_day(high=20.5, close=20.2))
        with self.Session() as db:
            orders = self._orders(db)
            self.assertEqual(orders[0].status, "expired")
            self.assertEqual(self._position(db).quantity, 10_000)
            self.assertEqual(orders[1].status, "pending")

    def test_a_limit_up_day_cannot_sell(self) -> None:
        self._run(self._add_day(open_=22.0, high=22.0, low=22.0, close=22.0))
        with self.Session() as db:
            self.assertEqual(self._orders(db)[0].status, "expired")

    def test_running_twice_changes_nothing(self) -> None:
        now = self._add_day(open_=20.0, high=21.5, low=19.8, close=21.2)
        self._run(now)
        self._run(now)
        with self.Session() as db:
            self.assertEqual(len(self._navs(db)), 2)
            self.assertEqual(self._position(db).quantity, 1000)
            self.assertEqual(len(self._orders(db)), len({(o.basis_date, o.symbol, o.side) for o in self._orders(db)}))

    def test_buy_back_uses_only_sale_money(self) -> None:
        self._run(self._add_day(open_=20.0, high=21.5, low=19.8, close=21.0))
        with self.Session() as db:
            account = self._account(db)
            account.pool = 0.0
            db.commit()
        self._run(self._add_day(open_=20.0, high=20.1, low=19.9, close=20.0))
        with self.Session() as db:
            sides = [order.side for order in self._orders(db) if order.status == "pending"]
        self.assertNotIn("buy", sides)

    def test_buy_back_fills_after_the_price_falls(self) -> None:
        self._run(self._add_day(open_=20.0, high=21.5, low=19.8, close=21.0))
        with self.Session() as db:
            self._position(db).quantity = 500  # 明显低于目标，手上有卖出款
            db.commit()
        self._run(self._add_day(open_=20.0, high=20.1, low=19.9, close=20.0, pre_close=21.0))
        with self.Session() as db:
            order = next(item for item in self._orders(db) if item.status == "pending")
            pool = self._account(db).pool
        self.assertEqual((order.side, order.price), ("buy", 19.8))
        self.assertGreater(order.quantity, 0)
        self._run(self._add_day(open_=20.0, high=20.1, low=19.7, close=19.9))
        with self.Session() as db:
            self.assertEqual(self._position(db).quantity, 500 + order.quantity)
            self.assertAlmostEqual(self._account(db).pool, pool - order.quantity * 19.8 * 1.0008)

    def test_the_real_account_changing_does_not_touch_the_shadow(self) -> None:
        with self.Session() as db:
            db.add(self._snapshot(500))
            db.commit()
        self._run(self._add_day(high=20.5, close=20.0))
        with self.Session() as db:
            self.assertEqual(self._position(db).quantity, 10_000)
            nav = self._navs(db)[-1]
            self.assertAlmostEqual(nav.real_value, 500 * 20 + CASH)
            self.assertAlmostEqual(nav.shadow_value, 10_000 * 20 + CASH)

    def test_a_ten_for_ten_does_not_look_like_a_loss(self) -> None:
        with self.Session() as db:
            before = self._navs(db)[-1].shadow_value
        self._run(self._add_day(open_=10.0, high=10.2, low=9.8, close=10.0, pre_close=10.0))
        with self.Session() as db:
            self.assertEqual(self._position(db).quantity, 20_000)
            self.assertAlmostEqual(self._navs(db)[-1].shadow_value, before)


class ReportTests(_Case):
    def _page(self) -> str:
        with self.Session() as db:
            row = ReportShare(share_id="a" * 32, owner_user_id=OWNER, source_type="shadow_compare", source_ref="x", title="t", content_md="", expires_at=datetime(2026, 12, 5))
            return render_shadow_page(db, row)

    def test_the_page_shows_both_sides_and_the_next_orders(self) -> None:
        self._run()
        page = self._page()
        self.assertIn("影子 A", page)
        self.assertIn("影子 B", page)
        self.assertIn("你的账户", page)
        self.assertIn("方正证券 账户3", page)
        self.assertIn("下一个交易日挂单", page)
        self.assertIn("卖 9,000 股 @ 21.00", page)
        self.assertIn("超出风险预算", page)
        self.assertIn("不下真实单", page)

    def test_annual_return_stays_blank_early_on(self) -> None:
        self._run()
        self.assertIn("<td>年化</td><td>—</td><td>—</td>", self._page())

    def test_a_filled_order_shows_in_recent_fills(self) -> None:
        self._run()
        self._run(self._add_day(open_=20.0, high=21.5, low=19.8, close=21.2))
        page = self._page()
        self.assertIn("最近成交", page)
        self.assertIn("已卖 9,000 股 @ 21.00", page)

    def test_someone_without_a_shadow_gets_the_notice_page(self) -> None:
        with self.Session() as db:
            row = ReportShare(share_id="b" * 32, owner_user_id=99, source_type="shadow_compare", source_ref="x", title="t", content_md="", expires_at=datetime(2026, 12, 5))
            self.assertIsNone(render_shadow_page(db, row))


class WiringTests(unittest.TestCase):
    def test_the_job_retries_until_the_data_arrives(self) -> None:
        from backend.app.scheduler import rhythm_scheduler

        job = next(item for item in rhythm_scheduler.DEFAULT_JOBS if item["job_key"] == "shadow-account-1805")
        self.assertEqual((job["job_kind"], job["interval_minutes"], job["window_start"]), ("shadow_settle", 30, "18:05"))
        self.assertTrue(job["enabled"])
        self.assertTrue(job["trading_day_only"])
        self.assertIsNone(job.get("push_channel_key"))

    def test_agents_read_only_their_own_shadow(self) -> None:
        from backend.app.agents.sql_sandbox import prepare_query

        for table in ("shadow_accounts", "shadow_positions", "shadow_orders", "shadow_nav"):
            self.assertIn("owner_user_id", str(prepare_query(f"SELECT * FROM {table}", owner_user_id=OWNER, max_rows=10)))

    def test_the_comparison_page_is_live(self) -> None:
        from backend.app.data.share_service import LIVE_SHARE_SOURCES

        self.assertIn("shadow_compare", LIVE_SHARE_SOURCES)


if __name__ == "__main__":
    unittest.main()
