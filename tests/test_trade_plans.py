"""结构化计划：规则、保存校验、生效与替代、盯价提醒、成交对账、计划页。

全部用内存 SQLite 和打桩的飞书发送，不连 MySQL，不写生产库。
"""
from __future__ import annotations

import dataclasses
import json
import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import get_db
from backend.app.core.models import (
    Base,
    DigitalHumanProfile,
    Instrument,
    JobRun,
    NotifyChannel,
    PositionAccount,
    PositionHoldingSnapshot,
    PositionTrade,
    ReportShare,
    StockLatestPrice,
    TradePlan,
    TradePlanItem,
    User,
    WatchlistItem,
)
from backend.app.core.settings import settings as real_settings
from backend.app.data import share_service
from backend.app.data.share_api import router as share_router
from backend.app.feishu import feishu
from backend.app.feishu.feishu import encrypt_value
from backend.app.plans import rules, service, watcher
from backend.app.plans.service import activate_for_report, reconcile_owner, reconciliation_text, repair_request, save_trade_plan

OWNER = 7
ACCOUNT = 3
SELL_SYMBOL = "605286.SH"
BUY_SYMBOL = "601166.SH"
# 周二 2026-11-03 08:50（北京时间）
MORNING = datetime(2026, 11, 3, 8, 50)
MORNING_DATE = "2026-11-03"
# 时钟用 UTC：北京时间 08:52、12:10、22:00
BEIJING_0852 = datetime(2026, 11, 3, 0, 52)
BEIJING_1210 = datetime(2026, 11, 3, 4, 10)
BEIJING_2200 = datetime(2026, 11, 3, 14, 0)


def _always_open(day: date) -> bool:
    return day.isoweekday() < 6


def _quote(price: float, *, high: float | None = None, low: float | None = None, day: str = "20261103") -> dict:
    return {
        "price": price,
        "high": high if high is not None else price,
        "low": low if low is not None else price,
        "trade_date": day,
    }


class RuleTests(unittest.TestCase):
    def test_jobs_map_to_plan_sources(self) -> None:
        cases = {
            ("agent-1-a80afb924e40", "早盘挂单清单"): "premarket",
            ("agent-1-9080ccba4484", "午盘挂单调整"): "midday",
            ("evening-daily-2200", "改了名的日报"): "evening",
            ("weekend-review", "下周操作计划"): "weekly",
            ("month-end-review", "下月操作计划"): "monthly",
            ("selection-action-plan-2000", "全自动选股"): "selection",
            ("agent-1-68bb8799d1a4", "下午盘中短提醒"): None,
            ("automatic-selection-1610", "自动选股规则计算（报告准备）"): None,
            ("topnews-morning-0630", "早盘舆情简报"): None,
        }
        for (job_key, name), expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(rules.plan_source_for_job(job_key, name), expected)

    def test_plan_date_follows_the_trading_calendar(self) -> None:
        friday_evening = datetime(2026, 11, 6, 22, 0)
        self.assertEqual(rules.effective_plan_date("evening", friday_evening, _always_open), date(2026, 11, 9))
        self.assertEqual(rules.effective_plan_date("premarket", MORNING, _always_open), date(2026, 11, 3))
        self.assertEqual(rules.effective_plan_date("midday", datetime(2026, 11, 3, 12, 10), _always_open), date(2026, 11, 3))
        # 收盘后手动重跑早盘清单，是给下一个交易日的
        self.assertEqual(rules.effective_plan_date("premarket", datetime(2026, 11, 3, 16, 0), _always_open), date(2026, 11, 4))
        self.assertEqual(rules.effective_plan_date("premarket", datetime(2026, 11, 7, 8, 40), _always_open), date(2026, 11, 9))

    def test_lot_rules(self) -> None:
        check = rules.quantity_problem
        self.assertIsNone(check("600000.SH", "buy", 500))
        self.assertIn("100", check("600000.SH", "buy", 550))
        self.assertIn("200", check("688008.SH", "buy", 100))
        self.assertIsNone(check("688008.SH", "buy", 250))
        self.assertIsNone(check("688008.SH", "sell", 150, 150), "卖光零股是合规的")
        self.assertIsNone(check("600000.SH", "sell", 1240, 12940), "整手加上全部零股是合规的")
        self.assertIn("100", check("600000.SH", "sell", 1250, 12940))
        self.assertIn("100", check("600000.SH", "buy", 40, 40), "买入不能用卖光的例外")
        self.assertIsNotNone(check("600000.SH", "buy", 0))

    def test_price_limits_by_board(self) -> None:
        self.assertEqual(rules.price_limits("600000.SH", "浦发银行", 10), (9.0, 11.0))
        self.assertEqual(rules.price_limits("300656.SZ", "民德电子", 10), (8.0, 12.0))
        self.assertEqual(rules.price_limits("688008.SH", "澜起科技", 10), (8.0, 12.0))
        self.assertEqual(rules.price_limits("600000.SH", "*ST某某", 10), (9.5, 10.5))
        self.assertEqual(rules.price_limits("920001.BJ", "某某", 10), (7.0, 13.0))

    def test_trigger_directions(self) -> None:
        self.assertEqual(rules.watch_direction("sell", "limit"), "up")
        self.assertEqual(rules.watch_direction("sell", "rise_to"), "up")
        self.assertEqual(rules.watch_direction("stop_loss", "limit"), "down")
        self.assertEqual(rules.watch_direction("stop_loss", "fall_to"), "down")
        self.assertEqual(rules.watch_direction("buy", "limit"), "down")
        self.assertEqual(rules.watch_direction("buy", "rise_to"), "up")
        self.assertIsNone(rules.watch_direction("hold", "none"))

    def test_spike_needs_a_new_high_beyond_the_baseline(self) -> None:
        common = dict(direction="up", trigger_price=24.13, low=None, baseline_low=None)
        self.assertEqual(
            rules.evaluate_trigger(price=24.15, high=24.2, baseline_high=23.0, **common),
            rules.TriggerHit("price", 24.15),
        )
        self.assertEqual(
            rules.evaluate_trigger(price=24.0, high=24.2, baseline_high=23.0, **common),
            rules.TriggerHit("spike", 24.2),
            "两次刷新之间碰到过",
        )
        self.assertIsNone(
            rules.evaluate_trigger(price=24.0, high=24.5, baseline_high=24.5, **common),
            "午盘计划不能被上午的高点触发",
        )
        self.assertIsNone(
            rules.evaluate_trigger(price=24.0, high=24.5, baseline_high=None, **common),
            "第一次盯价还没有基准，只看现价",
        )

    def test_falling_spike(self) -> None:
        hit = rules.evaluate_trigger(
            direction="down", trigger_price=20.59, price=20.8, high=None, low=20.5, baseline_high=None, baseline_low=21.0
        )
        self.assertEqual(hit, rules.TriggerHit("spike", 20.5))

    def test_text_check_handles_thousands_separators_and_decimals(self) -> None:
        text = "同力天启605286（21.57元）反弹卖一部分，限价22.10元，卖2,400，留4,800"
        numbers = rules.text_numbers(text)
        ok = dict(symbol="605286.SH", name="同力天启", text=text, numbers=numbers)
        self.assertIsNone(rules.text_problem(price=22.1, quantity=2400, **ok))
        self.assertIn("22.18", rules.text_problem(price=22.18, quantity=2400, **ok))
        self.assertIn("2,500", rules.text_problem(price=22.1, quantity=2500, **ok))
        self.assertIn("没有这只股票", rules.text_problem(symbol="601166.SH", name="兴业银行", price=22.1, quantity=2400, text=text, numbers=numbers))
        self.assertIsNone(rules.text_problem(symbol="605286.SH", name=None, price=22.1, quantity=2400, text=text, numbers=numbers))

    def test_plan_descriptions(self) -> None:
        self.assertEqual(rules.describe_plan("sell", "rise_to", 24.13, 500), "涨到 24.13 卖 500 股")
        self.assertEqual(rules.describe_plan("sell", "limit", 24.13, 500), "挂 24.13 卖 500 股")
        self.assertEqual(rules.describe_plan("stop_loss", "fall_to", 20.59, 2400), "跌破 20.59 止损 2,400 股")
        self.assertEqual(rules.describe_plan("buy", "fall_to", 8.3, 2000), "跌到 8.30 买 2,000 股")
        self.assertEqual(rules.describe_plan("hold", "none", None, None), "拿着不动")


class _PlanCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        patcher = patch.object(
            share_service,
            "settings",
            dataclasses.replace(real_settings, public_base_url="https://stock.example.com", share_ttl_days=30),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)
        self.clock = BEIJING_0852
        clock = patch.object(service, "utc_now", side_effect=lambda: self.clock)
        clock.start()
        self.addCleanup(clock.stop)
        with self.Session() as db:
            db.add(PositionAccount(id=ACCOUNT, owner_user_id=OWNER, broker_name="方正证券", available_cash=100000.0))
            db.add(Instrument(symbol=SELL_SYMBOL, name="同力天启", market="SH", type="stock"))
            db.add(Instrument(symbol=BUY_SYMBOL, name="兴业银行", market="SH", type="stock"))
            db.add(Instrument(symbol="000001.SZ", name="不在名单", market="SZ", type="stock"))
            db.add(self._snapshot(SELL_SYMBOL, 7200))
            db.add(WatchlistItem(owner_user_id=OWNER, symbol=BUY_SYMBOL, group_id=1, status="active"))
            db.add(self._price(SELL_SYMBOL, 21.57))
            db.add(self._price(BUY_SYMBOL, 19.93))
            db.add(
                NotifyChannel(
                    channel_key="feishu-daily",
                    name="任务群",
                    purpose="daily",
                    webhook_url_cipher=encrypt_value("https://open.feishu.cn/open-apis/bot/v2/hook/x"),
                    status="enabled",
                )
            )
            db.commit()

    def _snapshot(self, symbol: str, quantity: float, status: str = "open") -> PositionHoldingSnapshot:
        return PositionHoldingSnapshot(
            owner_user_id=OWNER,
            account_id=ACCOUNT,
            symbol=symbol,
            symbol_name="同力天启",
            position_quantity=quantity,
            source_type="manual",
            status=status,
        )

    def _price(self, symbol: str, price: float, trade_date: str = "20261102") -> StockLatestPrice:
        return StockLatestPrice(symbol=symbol, price=price, pre_close=price, trade_date=trade_date, fetched_at=MORNING)

    def _sell(self, **overrides) -> dict:
        item = {
            "account_id": ACCOUNT,
            "symbol": SELL_SYMBOL,
            "action": "sell",
            "trigger_type": "limit",
            "trigger_price": 22.18,
            "quantity": 2400,
        }
        item.update(overrides)
        return item

    def _save(self, items: list[dict], *, job_run_id: str = "job-1", source: str = "premarket", now: datetime = MORNING) -> dict:
        with self.Session() as db:
            return save_trade_plan(
                db,
                owner_user_id=OWNER,
                source=source,
                job_key="agent-1-x",
                job_run_id=job_run_id,
                run_id="run-1",
                digital_human_code="stock-pm",
                push_channel_key="feishu-daily",
                raw_items=items,
                now=now,
                is_open=_always_open,
            )

    def _activate(self, text: str, *, job_run_id: str = "job-1", today: str = MORNING_DATE, session_live: bool = True):
        with self.Session() as db:
            return activate_for_report(
                db,
                job_run_id=job_run_id,
                report_id=f"rhythm-{job_run_id}",
                text=text,
                generated_at="2026-11-03 08:52:10",
                today=today,
                session_live=session_live,
            )

    def _plan_ready(self, items: list[dict] | None = None, text: str = "同力天启 限价22.18元 卖2,400") -> None:
        result = self._save(items or [self._sell()])
        self.assertTrue(result["saved"], result)
        self._activate(text)

    def _items(self) -> list[TradePlanItem]:
        with self.Session() as db:
            return list(db.scalars(select(TradePlanItem).order_by(TradePlanItem.id)).all())

    def _plan(self, job_run_id: str = "job-1") -> TradePlan:
        with self.Session() as db:
            return db.scalar(select(TradePlan).where(TradePlan.job_run_id == job_run_id))

    def _tick(self, quotes: dict, now: datetime, post_results: list | None = None) -> tuple[dict, list]:
        calls: list = []

        def fake_post(url, payload, attempts=3):
            calls.append(payload)
            if post_results:
                return post_results[min(len(calls) - 1, len(post_results) - 1)]
            return True, 200, ""

        feishu._rate_history.clear()
        with patch.object(feishu, "_post_with_retry", side_effect=fake_post), patch.object(feishu, "_log"):
            result = watcher.check_plan_triggers(quotes, now, session_factory=self.Session)
        return result, calls


class SaveValidationTests(_PlanCase):
    def _issues(self, items: list[dict], **kwargs) -> str:
        result = self._save(items, **kwargs)
        self.assertFalse(result["saved"], result)
        self.assertEqual(result["error_code"], "trade_plan_validation_error")
        return "\n".join(result["issues"])

    def test_a_valid_plan_is_saved_as_a_draft(self) -> None:
        result = self._save([self._sell(), {"account_id": ACCOUNT, "symbol": SELL_SYMBOL, "action": "hold"}])
        self.assertTrue(result["saved"], result)
        self.assertEqual(result["plan_date"], MORNING_DATE)
        self.assertEqual(result["item_count"], 2)
        self.assertEqual(self._plan().status, "draft")
        items = self._items()
        self.assertEqual(items[0].name, "同力天启")
        self.assertEqual(items[0].valid_until, MORNING_DATE)
        self.assertEqual((items[1].trigger_type, items[1].trigger_price, items[1].quantity), ("none", None, None))

    def test_bare_six_digit_codes_are_normalised(self) -> None:
        self.assertTrue(self._save([self._sell(symbol="605286")])["saved"])
        self.assertEqual(self._items()[0].symbol, SELL_SYMBOL)

    def test_stop_loss_defaults_to_falling_trigger(self) -> None:
        self._save([self._sell(action="stop_loss", trigger_type=None, trigger_price=20.59)])
        self.assertEqual(self._items()[0].trigger_type, "fall_to")

    def test_resaving_in_the_same_job_replaces_the_draft(self) -> None:
        self._save([self._sell(), self._sell(action="stop_loss", trigger_type="fall_to", trigger_price=20.59)])
        self._save([self._sell(trigger_price=22.0)])
        items = self._items()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].trigger_price, 22.0)
        with self.Session() as db:
            self.assertEqual(len(db.scalars(select(TradePlan)).all()), 1)

    def test_an_active_plan_cannot_be_overwritten(self) -> None:
        self._plan_ready()
        result = self._save([self._sell(trigger_price=22.0)])
        self.assertFalse(result["saved"])
        self.assertEqual(self._items()[0].trigger_price, 22.18)

    def test_nothing_is_saved_when_any_item_fails(self) -> None:
        self._issues([self._sell(), self._sell(quantity=550, trigger_price=22.0)])
        self.assertEqual(self._items(), [])
        self.assertIsNone(self._plan())

    def test_stock_must_be_held_watched_or_selected(self) -> None:
        issues = self._issues(
            [{"account_id": ACCOUNT, "symbol": "000001.SZ", "action": "buy", "trigger_price": 10.0, "quantity": 100}]
        )
        self.assertIn("不在持仓、自选或最新选股名单里", issues)

    def test_unknown_account_and_bad_fields_are_rejected(self) -> None:
        self.assertIn("账户 ID 99 不存在", self._issues([self._sell(account_id=99)]))
        with self.Session() as db:
            db.add(PositionAccount(id=8, owner_user_id=OWNER, broker_name="招商证券", status="archived"))
            db.commit()
        self.assertIn("账户 ID 8 不存在", self._issues([self._sell(account_id=8)]))
        self.assertIn("操作类型不对", self._issues([self._sell(action="short")]))
        self.assertIn("缺计划价", self._issues([self._sell(trigger_price=None)]))
        self.assertIn("缺股数", self._issues([self._sell(quantity=None)]))
        self.assertIn("写清楚触发方式", self._issues([self._sell(trigger_type="none")]))
        self.assertIn("YYYY-MM-DD", self._issues([self._sell(valid_until="明天")]))
        self.assertIn("不能早于", self._issues([self._sell(valid_until="2026-11-02")]))

    def test_cannot_sell_more_than_held_or_what_is_not_held(self) -> None:
        self.assertIn("超过可卖", self._issues([self._sell(quantity=7300)]))
        self.assertIn(
            "没有这只股票",
            self._issues([{"account_id": ACCOUNT, "symbol": BUY_SYMBOL, "action": "sell", "trigger_price": 20.0, "quantity": 100}]),
        )

    def test_available_quantity_limits_same_day_sells_only(self) -> None:
        with self.Session() as db:
            snap = db.scalar(select(PositionHoldingSnapshot))
            snap.available_quantity = 1000
            db.commit()
        self.assertIn("超过可卖", self._issues([self._sell()]))
        evening = self._save([self._sell()], source="evening", now=datetime(2026, 11, 3, 22, 0), job_run_id="job-2")
        self.assertTrue(evening["saved"], "明天生效的计划按全部持仓算")

    def test_sells_of_one_stock_add_up_but_stop_loss_is_an_alternative_exit(self) -> None:
        stop = self._sell(action="stop_loss", trigger_type="fall_to", trigger_price=20.59, quantity=7200)
        self.assertTrue(self._save([self._sell(quantity=7200), stop])["saved"])
        issues = self._issues(
            [self._sell(quantity=4000), self._sell(quantity=4000, trigger_price=23.0)],
            job_run_id="job-2",
        )
        self.assertIn("卖出合计 8,000 股", issues)

    def test_price_must_be_inside_the_daily_limit(self) -> None:
        issues = self._issues([self._sell(trigger_price=24.0)])
        self.assertIn("超出 2026-11-03 的涨跌停范围", issues)
        self.assertIn("19.41–23.73", issues)

    def test_limit_uses_pre_close_once_the_quote_is_from_the_plan_day(self) -> None:
        with self.Session() as db:
            quote = db.get(StockLatestPrice, SELL_SYMBOL)
            quote.trade_date, quote.price, quote.pre_close = "20261103", 25.0, 21.57
            db.commit()
        self.assertIn("涨跌停范围", self._issues([self._sell(trigger_price=24.0)], now=datetime(2026, 11, 3, 12, 10), source="midday"))

    def test_multi_day_plans_skip_the_daily_limit_check(self) -> None:
        result = self._save([self._sell(trigger_price=30.0)], source="weekly", now=datetime(2026, 11, 8, 22, 0))
        self.assertTrue(result["saved"], result)
        self.assertEqual(result["plan_date"], "2026-11-09")

    def test_buying_within_cash_needs_no_funding_note(self) -> None:
        buy = {"account_id": ACCOUNT, "symbol": BUY_SYMBOL, "action": "buy", "trigger_price": 19.85, "quantity": 2000}
        self.assertTrue(self._save([buy])["saved"])

    def test_buying_beyond_cash_must_name_the_sale_that_pays_for_it(self) -> None:
        buy = {"account_id": ACCOUNT, "symbol": BUY_SYMBOL, "action": "buy", "trigger_price": 19.85, "quantity": 6000}
        issues = self._issues([buy, self._sell()])
        self.assertIn("要写明钱从哪来", issues)
        self.assertTrue(self._save([{**buy, "funding_note": "用卖同力天启的钱"}, self._sell()])["saved"])

    def test_the_same_money_cannot_pay_twice(self) -> None:
        # 现金 100,000 + 卖出款 53,232 = 153,232。两笔买入单看都够，合起来 158,600 就不够了。
        first = {"account_id": ACCOUNT, "symbol": BUY_SYMBOL, "action": "buy", "trigger_price": 19.85, "quantity": 4000, "funding_note": "卖同力天启"}
        second = {**first, "trigger_price": 19.80}
        self.assertTrue(self._save([first, self._sell()])["saved"])
        self.assertTrue(self._save([second, self._sell()], job_run_id="job-2")["saved"])
        self.assertIn("钱不够", self._issues([first, second, self._sell()], job_run_id="job-3"))

    def test_stop_loss_proceeds_do_not_count_as_funding(self) -> None:
        buy = {"account_id": ACCOUNT, "symbol": BUY_SYMBOL, "action": "buy", "trigger_price": 19.85, "quantity": 6000, "funding_note": "止损的钱"}
        stop = self._sell(action="stop_loss", trigger_type="fall_to", trigger_price=20.59)
        self.assertIn("钱不够", self._issues([buy, stop]))

    def test_tool_refuses_jobs_that_do_not_make_plans(self) -> None:
        self.assertFalse(service.execute_save_tool({"items": []}, {"source_ref": "x", "user_id": OWNER})["saved"])

    def test_empty_plan_is_allowed(self) -> None:
        self.assertTrue(self._save([])["saved"])
        result = self._activate("今天没有操作。")
        self.assertIsNone(result.plan_url)
        self.assertEqual(result.note, "")


class ActivationTests(_PlanCase):
    def test_missing_plan_is_noted_in_the_report(self) -> None:
        self.assertIn("没有存成结构化计划", self._activate("正文").note)
        with self.Session() as db:
            self.assertIn("还没有存", repair_request(db, "job-1", "正文"))

    def test_mismatched_items_are_reported_for_repair(self) -> None:
        self._save([self._sell()])
        with self.Session() as db:
            problem = repair_request(db, "job-1", "同力天启 限价22.20元 卖2,400")
            self.assertIn("22.18", problem)
            self.assertIsNone(repair_request(db, "job-1", "同力天启 限价22.18元 卖2,400"))

    def test_hold_rows_are_not_checked_against_the_text(self) -> None:
        self._save([{"account_id": ACCOUNT, "symbol": SELL_SYMBOL, "action": "hold"}])
        with self.Session() as db:
            self.assertIsNone(repair_request(db, "job-1", "这份报告没提它"))

    def test_activation_links_the_report_and_creates_a_plan_page(self) -> None:
        self._save([self._sell()])
        result = self._activate("同力天启 限价22.18元 卖2,400")
        self.assertEqual(result.note, "")
        self.assertRegex(result.plan_url, r"/api/share/[0-9a-f]{32}$")
        plan = self._plan()
        self.assertEqual((plan.status, plan.report_id), ("active", "rhythm-job-1"))
        self.assertIsNotNone(plan.activated_at)

    def test_unmatched_items_are_noted_and_never_watched(self) -> None:
        self._save([self._sell()])
        result = self._activate("同力天启 限价22.20元 卖2,400")
        self.assertIn("对不上", result.note)
        self.assertIn("同力天启", result.note)
        self.assertFalse(self._items()[0].text_matched)
        outcome, calls = self._tick({SELL_SYMBOL: _quote(22.5)}, datetime(2026, 11, 3, 9, 31))
        self.assertEqual(outcome["triggered"], 0)
        self.assertEqual(calls, [])

    def test_midday_plan_replaces_the_morning_plan(self) -> None:
        self._plan_ready()
        self.clock = BEIJING_1210
        self._save([self._sell(trigger_price=22.0)], job_run_id="job-2", source="midday", now=datetime(2026, 11, 3, 12, 10))
        self._activate("同力天启 限价22.00元 卖2,400", job_run_id="job-2")
        morning, midday = self._plan("job-1"), self._plan("job-2")
        self.assertEqual(morning.status, "superseded")
        self.assertEqual(morning.superseded_by, midday.plan_id)
        self.assertEqual(self._items()[0].status, "superseded")
        self.assertEqual(self._items()[1].status, "pending")

    def test_a_triggered_morning_item_keeps_its_status_when_replaced(self) -> None:
        self._plan_ready()
        self._tick({SELL_SYMBOL: _quote(22.2)}, datetime(2026, 11, 3, 9, 47))
        self.clock = BEIJING_1210
        self._save([self._sell(trigger_price=22.0)], job_run_id="job-2", source="midday", now=datetime(2026, 11, 3, 12, 10))
        self._activate("同力天启 限价22.00元 卖2,400", job_run_id="job-2")
        self.assertEqual(self._items()[0].status, "triggered")

    def test_a_tomorrow_plan_run_during_trading_does_not_stop_todays_alerts(self) -> None:
        self._plan_ready()
        self._save([self._sell(trigger_price=23.0)], job_run_id="job-2", source="selection", now=datetime(2026, 11, 3, 10, 0))
        self._activate("同力天启 限价23.00元 卖2,400", job_run_id="job-2", session_live=True)
        self.assertEqual(self._plan("job-1").status, "active")
        outcome, _ = self._tick({SELL_SYMBOL: _quote(22.5)}, datetime(2026, 11, 3, 10, 1))
        self.assertEqual(outcome["triggered"], 1)
        self.assertEqual(self._items()[0].status, "triggered")
        self.assertEqual(self._items()[1].status, "pending")

    def test_tomorrows_plan_takes_over_on_its_date(self) -> None:
        self._plan_ready()
        self.clock = BEIJING_2200
        self._save([self._sell(trigger_price=22.0)], job_run_id="job-2", source="evening", now=datetime(2026, 11, 3, 22, 0))
        self._activate("同力天启 限价22.00元 卖2,400", job_run_id="job-2", session_live=False)
        self.assertEqual(self._plan("job-1").status, "superseded")
        self.assertEqual(self._items()[0].status, "expired", "收盘后才被替代的，算过期不算被替代")
        with self.Session() as db:
            self.assertEqual(watcher.watch_symbols("2026-11-03", session_factory=self.Session), set())
            self.assertEqual(db.scalar(select(TradePlan.plan_date).where(TradePlan.job_run_id == "job-2")), "2026-11-04")
        self.assertEqual(watcher.watch_symbols("2026-11-04", session_factory=self.Session), {SELL_SYMBOL})


class WatcherTests(_PlanCase):
    def test_watch_list_includes_planned_buys_that_are_not_held(self) -> None:
        buy = {"account_id": ACCOUNT, "symbol": BUY_SYMBOL, "action": "buy", "trigger_price": 19.85, "quantity": 2000}
        self._plan_ready([buy, self._sell()], "兴业银行 19.85 2000 股；同力天启 22.18 2,400")
        self.assertEqual(watcher.watch_symbols(MORNING_DATE, session_factory=self.Session), {BUY_SYMBOL, SELL_SYMBOL})

    def test_draft_plans_are_not_watched(self) -> None:
        self._save([self._sell()])
        self.assertEqual(watcher.watch_symbols(MORNING_DATE, session_factory=self.Session), set())
        outcome, calls = self._tick({SELL_SYMBOL: _quote(22.5)}, datetime(2026, 11, 3, 9, 31))
        self.assertEqual(outcome["triggered"], 0)
        self.assertEqual(calls, [])

    def test_touching_the_plan_price_alerts_once(self) -> None:
        self._plan_ready()
        now = datetime(2026, 11, 3, 9, 47)
        outcome, calls = self._tick({SELL_SYMBOL: _quote(22.2)}, now)
        self.assertEqual(outcome, {"triggered": 1, "alerts_sent": 1})
        self.assertEqual(len(calls), 1)
        card = calls[0]["card"]
        self.assertEqual(card["header"]["title"]["content"], "⚡ 触价提醒 · 09:47")
        self.assertEqual(card["header"]["template"], "orange")
        body = json.dumps(card, ensure_ascii=False)
        self.assertIn("方正证券 账户3｜同力天启 605286", body)
        self.assertIn("碰到早盘计划：挂 22.18 卖 2,400 股", body)
        self.assertIn("价格碰到不等于成交", body)
        self.assertNotIn("回一句", body)
        button = next(e for e in card["body"]["elements"] if e["tag"] == "button")
        self.assertEqual(button["text"]["content"], "📱 打开今天的计划")
        item = self._items()[0]
        self.assertEqual((item.status, item.triggered_price, item.trigger_via), ("triggered", 22.2, "price"))
        self.assertEqual(item.triggered_at, datetime(2026, 11, 3, 1, 47), "按 UTC 存")
        self.assertIsNotNone(item.notified_at)

        again, repeat_calls = self._tick({SELL_SYMBOL: _quote(22.5)}, now + timedelta(minutes=1))
        self.assertEqual(again, {"triggered": 0, "alerts_sent": 0})
        self.assertEqual(repeat_calls, [])

    def test_items_triggered_in_the_same_minute_share_one_red_card_when_stop_is_involved(self) -> None:
        stop = self._sell(action="stop_loss", trigger_type="fall_to", trigger_price=20.59, quantity=2400)
        buy = {"account_id": ACCOUNT, "symbol": BUY_SYMBOL, "action": "buy", "trigger_price": 19.85, "quantity": 2000}
        self._plan_ready([stop, buy], "同力天启 20.59 卖2,400；兴业银行 19.85 买2,000")
        outcome, calls = self._tick(
            {SELL_SYMBOL: _quote(20.5), BUY_SYMBOL: _quote(19.8)},
            datetime(2026, 11, 3, 10, 0),
        )
        self.assertEqual(outcome, {"triggered": 2, "alerts_sent": 1})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["card"]["header"]["template"], "red")
        body = json.dumps(calls[0]["card"], ensure_ascii=False)
        self.assertIn("跌破 20.59 止损", body)
        self.assertIn("跌到", body.replace("挂 19.85", "跌到"))

    def test_first_observation_sets_the_baseline_and_ignores_earlier_highs(self) -> None:
        self._plan_ready()
        outcome, _ = self._tick({SELL_SYMBOL: _quote(21.9, high=22.5)}, datetime(2026, 11, 3, 12, 11))
        self.assertEqual(outcome["triggered"], 0, "上午的高点不算")
        self.assertEqual(self._items()[0].baseline_high, 22.5)

        outcome, _ = self._tick({SELL_SYMBOL: _quote(21.95, high=22.5)}, datetime(2026, 11, 3, 13, 1))
        self.assertEqual(outcome["triggered"], 0)

        outcome, calls = self._tick({SELL_SYMBOL: _quote(22.0, high=22.6)}, datetime(2026, 11, 3, 13, 2))
        self.assertEqual(outcome["triggered"], 1, "新高超过基准，说明这两分钟之间碰到过")
        item = self._items()[0]
        self.assertEqual((item.trigger_via, item.triggered_price), ("spike", 22.6))
        body = json.dumps(calls[0]["card"], ensure_ascii=False)
        self.assertIn("盘中碰到过 22.60", body)
        self.assertIn("现价 22.00", body)

    def test_stale_or_missing_quotes_are_ignored(self) -> None:
        self._plan_ready()
        outcome, _ = self._tick({SELL_SYMBOL: _quote(23.0, day="20261102")}, datetime(2026, 11, 3, 9, 31))
        self.assertEqual(outcome["triggered"], 0)
        outcome, _ = self._tick({}, datetime(2026, 11, 3, 9, 32))
        self.assertEqual(outcome["triggered"], 0)

    def test_failed_push_is_retried_next_minute_without_retriggering(self) -> None:
        self._plan_ready()
        now = datetime(2026, 11, 3, 9, 47)
        outcome, calls = self._tick({SELL_SYMBOL: _quote(22.2)}, now, post_results=[(False, None, "网络错误")])
        self.assertEqual(outcome, {"triggered": 1, "alerts_sent": 0})
        self.assertIsNone(self._items()[0].notified_at)
        self.assertEqual(self._items()[0].status, "triggered")

        outcome, calls = self._tick({SELL_SYMBOL: _quote(22.1)}, now + timedelta(minutes=1))
        self.assertEqual(outcome, {"triggered": 0, "alerts_sent": 1})
        self.assertIsNotNone(self._items()[0].notified_at)
        self.assertEqual(self._items()[0].triggered_price, 22.2, "碰到时的价格不变")

    def test_retries_stop_after_half_an_hour(self) -> None:
        self._plan_ready()
        now = datetime(2026, 11, 3, 9, 47)
        self._tick({SELL_SYMBOL: _quote(22.2)}, now, post_results=[(False, None, "网络错误")])
        outcome, calls = self._tick({SELL_SYMBOL: _quote(22.1)}, now + timedelta(minutes=31))
        self.assertEqual(outcome["alerts_sent"], 0)
        self.assertEqual(calls, [])

    def test_no_channel_means_no_push_and_no_retry(self) -> None:
        self._plan_ready()
        with self.Session() as db:
            db.scalar(select(TradePlan)).push_channel_key = None
            db.commit()
        outcome, calls = self._tick({SELL_SYMBOL: _quote(22.2)}, datetime(2026, 11, 3, 9, 47))
        self.assertEqual(outcome, {"triggered": 1, "alerts_sent": 0})
        self.assertEqual(calls, [])
        self.assertIsNotNone(self._items()[0].notified_at)

    def test_sold_out_positions_do_not_trigger_the_other_exit(self) -> None:
        stop = self._sell(action="stop_loss", trigger_type="fall_to", trigger_price=20.59, quantity=7200)
        self._plan_ready([self._sell(quantity=7200), stop], "同力天启 22.18 卖7,200；跌破20.59 卖7,200")
        with self.Session() as db:
            db.add(self._snapshot(SELL_SYMBOL, 0, status="closed"))
            db.commit()
        outcome, calls = self._tick({SELL_SYMBOL: _quote(20.0)}, datetime(2026, 11, 3, 10, 0))
        self.assertEqual(outcome["triggered"], 0)
        self.assertEqual(calls, [])

    def test_plan_after_validity_is_not_watched(self) -> None:
        self._plan_ready()
        self.assertEqual(watcher.watch_symbols("2026-11-04", session_factory=self.Session), set())


class ReconcileTests(_PlanCase):
    def _trade(self, action: str, quantity: float, price: float = 22.2, *, at: datetime, source: str = "manual", symbol: str = SELL_SYMBOL, account: int = ACCOUNT) -> int:
        with self.Session() as db:
            trade = PositionTrade(
                owner_user_id=OWNER,
                account_id=account,
                symbol=symbol,
                action=action,
                quantity=quantity,
                price=price,
                source_type=source,
                created_at=at,
            )
            db.add(trade)
            db.commit()
            return trade.id

    def _reconcile(self, now: datetime | None = None) -> None:
        with self.Session() as db:
            reconcile_owner(db, OWNER, since=MORNING_DATE, now=now)
            db.commit()

    def _ready(self, items: list[dict] | None = None, text: str = "同力天启 限价22.18元 卖2,400") -> None:
        self._plan_ready(items, text)

    def test_full_fill_marks_executed_and_keeps_trade_ids(self) -> None:
        self._ready()
        trade_id = self._trade("sell", 2400, at=datetime(2026, 11, 3, 2, 0))
        self._reconcile()
        item = self._items()[0]
        self.assertEqual((item.status, item.executed_qty), ("executed", 2400))
        self.assertEqual(json.loads(item.trade_ids), [trade_id])

    def test_partial_fill(self) -> None:
        self._ready()
        self._trade("reduce", 1000, at=datetime(2026, 11, 3, 2, 0))
        self._reconcile()
        item = self._items()[0]
        self.assertEqual((item.status, item.executed_qty), ("partially_executed", 1000))

    def test_deleting_a_trade_rolls_the_status_back(self) -> None:
        self._ready()
        trade_id = self._trade("sell", 2400, at=datetime(2026, 11, 3, 2, 0))
        self._reconcile()
        with self.Session() as db:
            db.delete(db.get(PositionTrade, trade_id))
            db.commit()
        self._reconcile(now=datetime(2026, 11, 3, 3, 0))
        item = self._items()[0]
        self.assertEqual((item.status, item.executed_qty, item.trade_ids), ("pending", 0, None))

    def test_only_same_account_stock_direction_and_time_match(self) -> None:
        self._ready()
        self._trade("buy", 2400, at=datetime(2026, 11, 3, 2, 0))
        self._trade("sell", 2400, at=datetime(2026, 11, 3, 2, 0), symbol=BUY_SYMBOL)
        self._trade("sell", 2400, at=datetime(2026, 11, 3, 2, 0), account=99)
        self._trade("sell", 2400, at=datetime(2026, 11, 3, 0, 30))  # 计划生效之前
        self._trade("sell", 2400, at=datetime(2026, 11, 5, 2, 0))  # 过了有效期
        self._reconcile(now=datetime(2026, 11, 3, 3, 0))
        self.assertEqual(self._items()[0].executed_qty, 0)

    def test_screenshot_imports_are_not_trades(self) -> None:
        self._ready()
        self._trade("sell", 2400, at=datetime(2026, 11, 3, 2, 0), source="screenshot_import")
        self._reconcile(now=datetime(2026, 11, 3, 3, 0))
        self.assertEqual(self._items()[0].executed_qty, 0)

    def test_trades_recorded_after_the_close_still_count(self) -> None:
        self._ready()
        self._trade("sell", 2400, at=datetime(2026, 11, 3, 14, 0))  # 北京时间 22:00
        self._reconcile(now=datetime(2026, 11, 3, 14, 1))
        self.assertEqual(self._items()[0].status, "executed")

    def test_fill_goes_to_the_triggered_exit_not_its_alternative(self) -> None:
        stop = self._sell(action="stop_loss", trigger_type="fall_to", trigger_price=20.59)
        self._ready([stop, self._sell()], "同力天启 22.18 卖2,400；20.59 卖2,400")
        with self.Session() as db:
            sell = db.scalar(select(TradePlanItem).where(TradePlanItem.action == "sell"))
            sell.triggered_at = datetime(2026, 11, 3, 1, 50)
            db.commit()
        self._trade("sell", 2400, price=22.2, at=datetime(2026, 11, 3, 2, 0))
        self._reconcile(now=datetime(2026, 11, 3, 3, 0))
        by_action = {item.action: item for item in self._items()}
        self.assertEqual(by_action["sell"].status, "executed")
        self.assertEqual(by_action["stop_loss"].executed_qty, 0)

    def test_fill_without_a_trigger_goes_to_the_closest_price(self) -> None:
        stop = self._sell(action="stop_loss", trigger_type="fall_to", trigger_price=20.59)
        self._ready([self._sell(), stop], "同力天启 22.18 卖2,400；20.59 卖2,400")
        self._trade("sell", 2400, price=20.6, at=datetime(2026, 11, 3, 2, 0))
        self._reconcile(now=datetime(2026, 11, 3, 3, 0))
        by_action = {item.action: item for item in self._items()}
        self.assertEqual(by_action["stop_loss"].status, "executed")
        self.assertEqual(by_action["sell"].executed_qty, 0)

    def test_fill_goes_to_the_newest_plan(self) -> None:
        self._ready()
        self.clock = BEIJING_1210
        self._save([self._sell(trigger_price=22.0)], job_run_id="job-2", source="midday", now=datetime(2026, 11, 3, 12, 10))
        self._activate("同力天启 限价22.00元 卖2,400", job_run_id="job-2")
        self._trade("sell", 2400, at=datetime(2026, 11, 3, 5, 0))
        self._reconcile(now=datetime(2026, 11, 3, 5, 1))
        morning, midday = self._items()
        self.assertEqual((morning.status, morning.executed_qty), ("superseded", 0))
        self.assertEqual((midday.status, midday.executed_qty), ("executed", 2400))

    def test_morning_fill_stays_with_the_morning_plan_after_replacement(self) -> None:
        self._ready()
        self._trade("sell", 2400, at=datetime(2026, 11, 3, 2, 0))
        self.clock = BEIJING_1210
        self._save([self._sell(trigger_price=22.0)], job_run_id="job-2", source="midday", now=datetime(2026, 11, 3, 12, 10))
        self._activate("同力天启 限价22.00元 卖2,400", job_run_id="job-2")
        morning, midday = self._items()
        self.assertEqual(morning.status, "executed")
        self.assertEqual(midday.executed_qty, 0)

    def test_executed_items_are_not_alerted(self) -> None:
        self._ready()
        self._trade("sell", 2400, at=datetime(2026, 11, 3, 1, 40))
        outcome, calls = self._tick({SELL_SYMBOL: _quote(22.5)}, datetime(2026, 11, 3, 9, 47))
        self.assertEqual(outcome["triggered"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(self._items()[0].status, "executed")

    def test_items_expire_at_the_close(self) -> None:
        self._ready()
        self._reconcile(now=datetime(2026, 11, 3, 6, 59))
        self.assertEqual(self._items()[0].status, "pending")
        self._reconcile(now=datetime(2026, 11, 3, 7, 0))
        self.assertEqual(self._items()[0].status, "expired")

    def test_evening_text_reports_what_actually_happened(self) -> None:
        stop = self._sell(action="stop_loss", trigger_type="fall_to", trigger_price=20.59)
        hold = {"account_id": ACCOUNT, "symbol": SELL_SYMBOL, "action": "hold"}
        self._ready([self._sell(), stop, hold], "同力天启 22.18 卖2,400；20.59 卖2,400")
        self._tick({SELL_SYMBOL: _quote(22.2)}, datetime(2026, 11, 3, 9, 47))
        self._trade("sell", 1000, at=datetime(2026, 11, 3, 2, 0))
        with self.Session() as db:
            text = reconciliation_text(db, OWNER, MORNING_DATE)
        self.assertIn("早盘计划：", text)
        self.assertIn("方正证券 账户3｜同力天启 605286.SH：计划挂 22.18 卖 2,400 股，09:47 碰到过 22.20，已登记成交 1,000 股", text)
        self.assertIn("计划跌破 20.59 止损 2,400 股", text)
        self.assertNotIn("拿着不动", text)
        self.assertIn("价格碰到不算成交", text)

    def test_evening_text_is_empty_without_a_plan(self) -> None:
        with self.Session() as db:
            self.assertEqual(reconciliation_text(db, OWNER, MORNING_DATE), "")

    def test_drafts_are_not_reconciled_or_reported(self) -> None:
        self._save([self._sell()])
        self._trade("sell", 2400, at=datetime(2026, 11, 3, 2, 0))
        self._reconcile()
        self.assertEqual(self._items()[0].executed_qty, 0)
        with self.Session() as db:
            self.assertEqual(reconciliation_text(db, OWNER, MORNING_DATE), "")


class PlanPageTests(_PlanCase):
    def _client(self) -> TestClient:
        app = FastAPI()
        app.include_router(share_router)

        def _db():
            with self.Session() as session:
                yield session

        app.dependency_overrides[get_db] = _db
        return TestClient(app)

    def _share_id(self, url: str) -> str:
        return url.rsplit("/", 1)[1]

    def _ready(self) -> str:
        stop = self._sell(action="stop_loss", trigger_type="fall_to", trigger_price=20.59)
        buy = {
            "account_id": ACCOUNT,
            "symbol": BUY_SYMBOL,
            "action": "buy",
            "trigger_price": 19.85,
            "quantity": 2000,
            "funding_note": "<script>x</script>用现金",
        }
        hold = {"account_id": ACCOUNT, "symbol": SELL_SYMBOL, "action": "hold"}
        self._save([self._sell(), stop, buy, hold])
        return self._activate("同力天启 22.18 卖2,400；20.59；兴业银行 19.85 买2,000").plan_url

    def test_page_lists_live_status_by_account_without_login(self) -> None:
        url = self._ready()
        self._tick({SELL_SYMBOL: _quote(22.2)}, datetime(2026, 11, 3, 9, 47))
        response = self._client().get(f"/api/share/{self._share_id(url)}")
        self.assertEqual(response.status_code, 200)
        page = response.text
        self.assertIn("早盘计划进度", page)
        self.assertIn("方正证券 账户3", page)
        self.assertIn("已触发 09:47", page)
        self.assertIn("09:47 碰到 22.20，还没有登记成交", page)
        self.assertIn("待触发", page)
        self.assertIn("拿着不动", page)
        self.assertIn('现价 <span class="fig">21.57</span>', page)
        self.assertIn("离计划价还差", page)
        self.assertIn('<div class="bar"><div style="width:', page)
        self.assertIn("2026 年 11 月 3 日 周二　08:52 生成", page)
        self.assertIn("价格碰到不等于成交", page)
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertIn("default-src 'none'", response.headers["content-security-policy"])

    def test_page_escapes_agent_text_and_runs_no_script(self) -> None:
        page = self._client().get(f"/api/share/{self._share_id(self._ready())}").text
        self.assertNotIn("<script", page)
        self.assertIn("&lt;script&gt;", page)

    def test_page_recomputes_on_open(self) -> None:
        url = self._ready()
        with self.Session() as db:
            db.add(
                PositionTrade(
                    owner_user_id=OWNER,
                    account_id=ACCOUNT,
                    symbol=SELL_SYMBOL,
                    action="sell",
                    quantity=1000,
                    price=22.2,
                    source_type="manual",
                    created_at=datetime(2026, 11, 3, 2, 0),
                )
            )
            db.commit()
        page = self._client().get(f"/api/share/{self._share_id(url)}").text
        self.assertIn("部分成交", page)
        self.assertIn("已登记 1,000 / 2,400 股", page)

    def test_replaced_plan_points_to_the_newest_one(self) -> None:
        first = self._ready()
        self.clock = BEIJING_1210
        self._save([self._sell(trigger_price=22.0)], job_run_id="job-2", source="midday", now=datetime(2026, 11, 3, 12, 10))
        second = self._activate("同力天启 限价22.00元 卖2,400", job_run_id="job-2").plan_url
        page = self._client().get(f"/api/share/{self._share_id(first)}").text
        self.assertIn("已被新计划替代", page)
        self.assertIn(second, page)
        self.assertIn("已被替代", page)

    def test_later_pushes_do_not_overwrite_the_plan_share(self) -> None:
        url = self._ready()
        share_id = self._share_id(url)
        with self.Session() as db:
            before = db.get(ReportShare, share_id).title
        self._tick({SELL_SYMBOL: _quote(22.2)}, datetime(2026, 11, 3, 9, 47))
        with self.Session() as db:
            self.assertEqual(db.get(ReportShare, share_id).title, before)
            self.assertEqual(len(db.scalars(select(ReportShare)).all()), 1)

    def test_alert_button_opens_the_same_plan_page(self) -> None:
        url = self._ready()
        _, calls = self._tick({SELL_SYMBOL: _quote(22.2)}, datetime(2026, 11, 3, 9, 47))
        button = next(e for e in calls[0]["card"]["body"]["elements"] if e["tag"] == "button")
        self.assertEqual(button["behaviors"][0]["default_url"], url)

    def test_alert_card_keeps_the_whole_alert_text(self) -> None:
        # 触价提醒本身就是要马上执行的内容，不走一句话卡片
        self._ready()
        _, calls = self._tick({SELL_SYMBOL: _quote(22.2)}, datetime(2026, 11, 3, 9, 47))
        ids = [e.get("element_id") for e in calls[0]["card"]["body"]["elements"]]
        self.assertNotIn("brief", ids)
        self.assertIn("content_001", ids)

    def test_missing_plan_shows_the_notice_page(self) -> None:
        url = self._ready()
        with self.Session() as db:
            db.delete(db.scalar(select(TradePlan)))
            db.commit()
        self.assertEqual(self._client().get(f"/api/share/{self._share_id(url)}").status_code, 404)


class SchedulerIntegrationTests(_PlanCase):
    def _report(self, *, job_key: str, name: str, answer: str, agent_side_effect=None, now: datetime = MORNING, owner: int = OWNER):
        from backend.app.scheduler import rhythm_scheduler

        calls: list[tuple] = []

        def fake_agent(snap, question, job_run_id, **kwargs):
            calls.append((snap, question, kwargs))
            if agent_side_effect:
                agent_side_effect(snap, question, kwargs)
            return answer, "model"

        pushed = {}

        def fake_send(session, channel, title, content, **kwargs):
            pushed.update(content=content, **kwargs)
            from types import SimpleNamespace

            return SimpleNamespace(id=1, status="sent", error=None)

        snap = {
            "job_key": job_key,
            "name": name,
            "prompt_template": "写报告",
            "digital_human_code": "stock-pm",
            "push_channel_key": "feishu-daily",
            "owner_user_id": owner,
        }
        with (
            patch.object(rhythm_scheduler, "SessionLocal", self.Session),
            patch.object(rhythm_scheduler, "_run_scheduled_agent", side_effect=fake_agent),
            patch.object(rhythm_scheduler, "_plain_report", side_effect=lambda snapshot, text: text),
            patch.object(rhythm_scheduler, "send_feishu", side_effect=fake_send),
            patch.object(rhythm_scheduler, "_log"),
            patch.object(rhythm_scheduler, "is_trading_day", side_effect=_always_open),
        ):
            result = rhythm_scheduler._run_agent_report(snap, now, "job-1")
        return result, calls, pushed

    def test_non_plan_jobs_are_unchanged(self) -> None:
        _, calls, pushed = self._report(job_key="agent-1-x", name="下午盘中短提醒", answer="短提醒")
        snap, question, kwargs = calls[0]
        self.assertNotIn("trade_plan_source", snap)
        self.assertNotIn("save_trade_plan", question)
        self.assertEqual(len(calls), 1)
        self.assertEqual(pushed["content"], "短提醒")
        self.assertNotIn("plan_url", pushed["share"].meta)

    def test_a_report_without_delegation_is_not_abandoned_while_the_agent_works(self) -> None:
        from backend.app.scheduler import rhythm_scheduler

        self.assertEqual(rhythm_scheduler._scheduled_agent_hard_timeout_seconds({"delegation_mode": "none"}), 2760)
        long_children = {"delegation_mode": "execute_and_wait", "child_timeout_seconds": 3000}
        self.assertGreater(rhythm_scheduler._scheduled_agent_hard_timeout_seconds(long_children), 2760)

    def test_evening_report_links_to_its_owners_shadow_comparison(self) -> None:
        from backend.app.core.models import ShadowAccount, ShadowNav

        with self.Session() as db:
            for strategy in ("risk", "risk_rank"):
                account = ShadowAccount(owner_user_id=OWNER, real_account_id=1, strategy=strategy,
                                        account_label="测试账户", started_on="20261103")
                db.add(account)
                db.flush()
                db.add(ShadowNav(shadow_account_id=account.id, owner_user_id=OWNER,
                                 trade_date="20261103", shadow_value=100_000, real_value=100_000))
            db.commit()

        _, calls, pushed = self._report(
            job_key="evening-daily-2200", name="晚间日报", answer="今天已经完成复盘。",
            now=datetime(2026, 11, 3, 22),
        )
        with self.Session() as db:
            share = db.scalar(select(ReportShare).where(ReportShare.source_type == "shadow_compare"))
            self.assertIsNotNone(share)
            self.assertEqual(share.owner_user_id, OWNER)
            url = share_service.share_url(share.share_id)
        self.assertTrue(pushed["content"].endswith(f"[查看影子账户对比]({url})"))
        self.assertNotIn(url, calls[0][1])

    def test_plan_job_gets_the_tool_and_activates_its_plan(self) -> None:
        def saves(snap, question, kwargs):
            self._save([self._sell()])

        _, calls, pushed = self._report(
            job_key="agent-1-a80afb924e40",
            name="早盘挂单清单",
            answer="同力天启 限价22.18元 卖2,400",
            agent_side_effect=saves,
        )
        snap, question, _ = calls[0]
        self.assertEqual(snap["trade_plan_source"], "premarket")
        self.assertIn("save_trade_plan", question)
        self.assertEqual(len(calls), 1, "对得上就不用补存")
        self.assertEqual(self._plan().status, "active")
        self.assertEqual(self._plan().report_id, "rhythm-job-1")
        self.assertEqual(pushed["content"], "同力天启 限价22.18元 卖2,400")
        self.assertRegex(pushed["share"].meta["plan_url"], r"/api/share/[0-9a-f]{32}$")
        self.assertEqual(pushed["share"].meta["plan_stats"], {"sell": 1, "buy": 0, "stop": 0, "hold": 0})

    def test_forgetting_to_save_triggers_one_repair(self) -> None:
        attempts: list[str] = []

        def repairs(snap, question, kwargs):
            attempts.append(question)
            if kwargs.get("repair_timeout_seconds"):
                self._save([self._sell()])

        _, calls, pushed = self._report(
            job_key="agent-1-a80afb924e40",
            name="早盘挂单清单",
            answer="同力天启 限价22.18元 卖2,400",
            agent_side_effect=repairs,
        )
        self.assertEqual(len(calls), 2)
        repair_snap, repair_text, repair_kwargs = calls[1]
        self.assertEqual(repair_kwargs["repair_timeout_seconds"], 240)
        self.assertEqual(repair_snap["delegation_mode"], "none")
        self.assertIn("还没有存进系统", repair_text)
        self.assertIn("同力天启 限价22.18元 卖2,400", repair_text)
        self.assertEqual(self._plan().status, "active")
        self.assertEqual(pushed["content"], "同力天启 限价22.18元 卖2,400", "补存不改正文，也不加说明")

    def test_unresolved_mismatch_is_noted_at_the_end_of_the_report(self) -> None:
        def saves_wrong(snap, question, kwargs):
            self._save([self._sell()])

        _, calls, pushed = self._report(
            job_key="agent-1-a80afb924e40",
            name="早盘挂单清单",
            answer="同力天启 限价22.20元 卖2,400",
            agent_side_effect=saves_wrong,
        )
        self.assertEqual(len(calls), 2, "只补存一次")
        self.assertIn("同力天启 限价22.20元 卖2,400\n\n---\n\n有计划和正文对不上", pushed["content"])
        self.assertFalse(self._items()[0].text_matched)

    def test_repair_failure_does_not_stop_the_report(self) -> None:
        def fails_on_repair(snap, question, kwargs):
            if kwargs.get("repair_timeout_seconds"):
                raise RuntimeError("超时")

        result, calls, pushed = self._report(
            job_key="agent-1-a80afb924e40",
            name="早盘挂单清单",
            answer="同力天启 限价22.18元 卖2,400",
            agent_side_effect=fails_on_repair,
        )
        self.assertEqual(result[3], None)
        self.assertIn("没有存成结构化计划", pushed["content"])

    def test_activation_crash_does_not_stop_the_report(self) -> None:
        from backend.app.scheduler import rhythm_scheduler

        with patch.object(rhythm_scheduler, "activate_for_report", side_effect=RuntimeError("boom")):
            _, _, pushed = self._report(
                job_key="agent-1-a80afb924e40",
                name="早盘挂单清单",
                answer="正文",
            )
        self.assertIn("没能生效", pushed["content"])

    def test_evening_report_receives_the_reconciliation(self) -> None:
        self._plan_ready()
        evening = datetime(2026, 11, 3, 22, 0)
        self._tick({SELL_SYMBOL: _quote(22.2)}, datetime(2026, 11, 3, 9, 47))

        def saves(snap, question, kwargs):
            with self.Session() as db:
                save_trade_plan(
                    db,
                    owner_user_id=OWNER,
                    source="evening",
                    job_key="evening-daily-2200",
                    job_run_id="job-evening",
                    run_id="run",
                    digital_human_code="stock-pm",
                    push_channel_key="feishu-daily",
                    raw_items=[],
                    now=evening,
                    is_open=_always_open,
                )

        _, calls, _ = self._report(
            job_key="evening-daily-2200",
            name="晚间日报与实盘复盘",
            answer="日报",
            agent_side_effect=saves,
            now=evening,
        )
        question = calls[0][1]
        self.assertIn("计划和实际对照", question)
        self.assertIn("09:47 碰到过 22.20", question)
        self.assertIn("没有登记成交", question)

    def test_reports_without_their_own_plan_link_todays_plan(self) -> None:
        self._plan_ready()
        _, _, pushed = self._report(job_key="agent-1-x", name="下午盘中短提醒", answer="短提醒", now=datetime(2026, 11, 3, 13, 5))
        self.assertRegex(pushed["share"].meta["plan_url"], r"/api/share/[0-9a-f]{32}$")
        self.assertEqual(pushed["share"].meta["plan_stats"], {"sell": 1, "buy": 0, "stop": 0, "hold": 0})

    def test_only_todays_active_plan_is_linked(self) -> None:
        self._plan_ready()
        _, _, pushed = self._report(job_key="agent-1-x", name="下午盘中短提醒", answer="短提醒", now=datetime(2026, 11, 4, 13, 5))
        self.assertNotIn("plan_url", pushed["share"].meta)

    def test_only_evening_gets_the_reconciliation(self) -> None:
        self._plan_ready()
        _, calls, _ = self._report(job_key="agent-1-b", name="午盘挂单调整", answer="正文", now=datetime(2026, 11, 3, 12, 10))
        self.assertNotIn("计划和实际对照", calls[0][1])


class ScheduledRunGlueTests(_PlanCase):
    """定时运行把计划信息带给 Agent 的那一段，测真实的 _run_scheduled_agent。"""

    def _run(self, snap_extra: dict, **kwargs) -> tuple[dict, str, dict]:
        from backend.app.scheduler import rhythm_scheduler

        with self.Session() as db:
            if db.scalar(select(User).where(User.id == OWNER)) is None:
                db.add(User(id=OWNER, username="u", display_name="U", role="researcher", password_hash="x"))
                db.add(
                    DigitalHumanProfile(
                        code="stock-pm",
                        name="总调度",
                        description="d",
                        system_prompt="p",
                        quick_prompts_json="[]",
                    )
                )
                db.add(JobRun(run_id="job-1", job_key="k", fire_key="f", status="running", trigger_type="schedule", started_at=MORNING))
                db.commit()
        captured: dict = {}

        def fake_create(payload, idempotency_key):
            captured.update(payload=payload, key=idempotency_key)
            return {"run_id": "agent-run-1", "created": False}

        def fake_wait(run_id, **wait_kwargs):
            captured["wait"] = wait_kwargs
            return "答复", "model", None

        snap = {
            "job_key": "agent-1-a80afb924e40",
            "name": "早盘挂单清单",
            "digital_human_code": "stock-pm",
            "owner_user_id": OWNER,
            "push_channel_key": "feishu-daily",
            **snap_extra,
        }
        with (
            patch.object(rhythm_scheduler, "SessionLocal", self.Session),
            patch.object(rhythm_scheduler, "build_channel_request", return_value={"source_ref": "job-1"}),
            patch.object(rhythm_scheduler, "create_channel_run", side_effect=fake_create),
            patch.object(rhythm_scheduler, "wait_for_channel_run", side_effect=fake_wait),
        ):
            rhythm_scheduler._run_scheduled_agent(snap, "问题", "job-1", **kwargs)
        return captured["payload"], captured["key"], captured["wait"]

    def _leader(self) -> str | None:
        with self.Session() as db:
            return db.scalar(select(JobRun.leader_agent_run_id).where(JobRun.run_id == "job-1"))

    def test_plan_runs_carry_the_source_and_channel(self) -> None:
        payload, key, wait = self._run({"trade_plan_source": "premarket"})
        self.assertEqual(payload["trade_plan_source"], "premarket")
        self.assertEqual(
            payload["trade_plan_context"],
            {"job_key": "agent-1-a80afb924e40", "push_channel_key": "feishu-daily"},
        )
        self.assertEqual(key, "schedule:job-1")
        self.assertEqual(self._leader(), "agent-run-1")
        self.assertGreater(wait["timeout_seconds"], 240)

    def test_other_runs_stay_as_they_were(self) -> None:
        payload, key, _ = self._run({})
        self.assertNotIn("trade_plan_source", payload)
        self.assertNotIn("trade_plan_context", payload)
        self.assertEqual(key, "schedule:job-1")

    def test_repair_runs_are_separate_short_and_keep_the_leader(self) -> None:
        payload, key, wait = self._run({"trade_plan_source": "premarket"}, repair_timeout_seconds=240)
        self.assertEqual(payload["trade_plan_source"], "premarket")
        self.assertEqual(key, "schedule:job-1:plan-repair")
        self.assertEqual((wait["timeout_seconds"], wait["hard_timeout_seconds"]), (240, 240))
        self.assertIsNone(self._leader())

    def test_tool_entry_saves_with_the_runs_context(self) -> None:
        payload = {
            "trade_plan_source": "premarket",
            "trade_plan_context": {"job_key": "agent-1-x", "push_channel_key": "feishu-daily"},
            "source_ref": "job-1",
            "user_id": OWNER,
            "run_id": "agent-run-1",
            "digital_human_code": "stock-pm",
        }
        from backend.app.scheduler import rhythm_scheduler

        with (
            patch("backend.app.core.database.SessionLocal", self.Session),
            patch.object(rhythm_scheduler, "is_trading_day", side_effect=_always_open),
            patch.object(rhythm_scheduler, "now_sh", return_value=MORNING),
        ):
            result = service.execute_save_tool({"items": [self._sell()]}, payload)
        self.assertTrue(result["saved"], result)
        plan = self._plan()
        self.assertEqual(
            (plan.source, plan.job_key, plan.run_id, plan.push_channel_key, plan.status),
            ("premarket", "agent-1-x", "agent-run-1", "feishu-daily", "draft"),
        )


class PriceSyncHookTests(unittest.TestCase):
    def _call(self, now: datetime, *, is_open: bool = True, side_effect=None) -> tuple[str, object]:
        from backend.app.scheduler import rhythm_scheduler

        with (
            patch.object(rhythm_scheduler, "is_trading_day", return_value=is_open),
            patch.object(watcher, "check_plan_triggers", side_effect=side_effect, return_value={"triggered": 2, "alerts_sent": 1}) as check,
            patch.object(rhythm_scheduler, "_log"),
        ):
            return rhythm_scheduler._check_trade_plans({"x": {}}, now), check

    def test_only_checks_during_the_session(self) -> None:
        text, check = self._call(datetime(2026, 11, 3, 9, 47))
        self.assertEqual(text, "；2 条计划碰到计划价")
        check.assert_called_once()
        for now in (datetime(2026, 11, 3, 8, 50), datetime(2026, 11, 3, 12, 0), datetime(2026, 11, 3, 20, 0)):
            with self.subTest(now=now):
                self.assertEqual(self._call(now)[0], "")
                self.assertFalse(self._call(now)[1].called)
        self.assertFalse(self._call(datetime(2026, 11, 3, 9, 47), is_open=False)[1].called)

    def test_watcher_failure_never_fails_the_price_sync(self) -> None:
        text, _ = self._call(datetime(2026, 11, 3, 9, 47), side_effect=RuntimeError("boom"))
        self.assertEqual(text, "；计划盯价失败")


class WiringTests(unittest.TestCase):
    def test_plans_are_personal_in_the_sql_sandbox(self) -> None:
        from backend.app.agents.sql_sandbox import is_personal, prepare_query

        self.assertTrue(is_personal("trade_plans"))
        self.assertTrue(is_personal("trade_plan_items"))
        prepared = prepare_query("SELECT symbol FROM trade_plan_items", owner_user_id=OWNER, max_rows=10)
        self.assertIn(f"owner_user_id = {OWNER}", prepared.sql)

    def test_tool_policy_allows_a_safe_retry(self) -> None:
        from backend.app.agent_runtime.tool_specs import should_externalize, tool_spec

        spec = tool_spec("save_trade_plan")
        self.assertTrue(spec.retry_safe)
        self.assertFalse(spec.is_read_only)
        self.assertFalse(should_externalize(spec, enabled=True), "在 Agent 进程内执行，存的只是草稿")

    def _tool_names(self, **payload_extra) -> set[str]:
        from backend.app.agent_runtime import agentscope_engine as engine

        payload = {
            "chat_mode": "research",
            "digital_human_code": "stock-pm",
            "user_id": OWNER,
            "session_id": 1,
            "source_type": "scheduler",
            "delegation_config": {"mode": "none"},
            **payload_extra,
        }
        with (
            patch.object(engine.artifact_service, "active_context", return_value=""),
            patch("backend.app.market.digital_human_tools._stored_role_prompt", return_value=""),
        ):
            _, schemas, _ = engine._build_definition(payload)
        return {schema["function"]["name"] for schema in schemas}

    def test_only_plan_runs_see_the_tool(self) -> None:
        self.assertIn("save_trade_plan", self._tool_names(trade_plan_source="premarket"))
        self.assertNotIn("save_trade_plan", self._tool_names())

    def test_engine_routes_the_tool_to_the_service(self) -> None:
        from backend.app.agent_runtime import agentscope_engine as engine

        payload = {"trade_plan_source": "premarket", "user_id": OWNER}
        with patch.object(service, "execute_save_tool", return_value={"saved": True}) as save:
            result = engine._execute_tool("save_trade_plan", {"items": []}, payload, set())
        self.assertEqual(result, {"saved": True})
        save.assert_called_once_with({"items": []}, payload)


if __name__ == "__main__":
    unittest.main()
