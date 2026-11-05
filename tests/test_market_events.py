"""消息驱动：规则分级、去重、限额、解读兜底、推送时段、并入报告、消息待复核。

全部用内存 SQLite 和打桩的模型、飞书，不连 MySQL，不写生产库。
"""
from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.models import (
    Base,
    Document,
    DocumentSymbol,
    Instrument,
    MarketEvent,
    Position,
    PositionAccount,
    TradePlan,
    TradePlanItem,
    WatchlistItem,
)
from backend.app.events import rules, service
from backend.app.plans.service import to_utc

OWNER = 7
SYMBOL = "003011.SZ"
NAME = "海象新材"
DAYTIME = datetime(2026, 11, 5, 10, 3)
NIGHT = datetime(2026, 11, 5, 23, 3)
GOOD = {
    "summary": "海象新材：控股股东计划减持",
    "direction": "negative",
    "reliability": "高",
    "impact": "短期有抛压",
    "plan_effect": "影响今天的卖单",
    "important": True,
}


class RuleTests(unittest.TestCase):
    def test_serious_news_is_critical(self) -> None:
        for title in ("关于收到中国证监会立案告知书的公告", "公司股票可能被实施退市风险警示", "重大事项停牌公告"):
            self.assertEqual(rules.classify(title).severity, rules.CRITICAL, title)

    def test_business_news_is_major(self) -> None:
        cases = {
            "2026年度业绩预告": "业绩",
            "控股股东减持股份计划公告": "增减持",
            "关于以集中竞价方式回购公司股份的方案": "回购",
            "关于收到上交所问询函的公告": "监管",
            "关于重大合同的公告": "重大合同",
            "关于股份质押的公告": "质押冻结",
            "股票复牌公告": "复牌",
            "2026年半年度权益分派实施公告": "分红送转",
        }
        for title, event_type in cases.items():
            found = rules.classify(title)
            self.assertEqual((found.severity, found.event_type), (rules.MAJOR, event_type), title)

    def test_routine_filings_are_minor(self) -> None:
        self.assertEqual(rules.classify("关于回购股份的法律意见书").severity, rules.MINOR)
        self.assertEqual(rules.classify("关于召开2026年第三季度业绩说明会的公告").severity, rules.MINOR)

    def test_a_routine_filing_about_an_investigation_is_still_critical(self) -> None:
        self.assertEqual(rules.classify("关于公司被立案调查的法律意见书").severity, rules.CRITICAL)

    def test_real_titles_that_were_misjudged_at_first(self) -> None:
        # 2026-10-28 至 11-06 真实公告和新闻干跑时发现的
        for title, severity, event_type in (
            ("同力天启：第三季度净利润1634.5万元，同比下降78.76%", rules.MAJOR, "业绩"),
            ("民德电子：2026年前三季度亏损8561.99万元", rules.MAJOR, "业绩"),
            ("民德电子：向特定对象发行股票获证监会同意注册批复", rules.MAJOR, "再融资"),
            ("友阿股份：控股股东2710万股将被司法拍卖 占总股本1.94%", rules.MAJOR, "质押冻结"),
            ("辽宁鼎际得石化关于回购注销2024年激励计划部分限制性股票的公告", rules.MINOR, "回购"),
            ("关于回购公司股份的进展公告", rules.MINOR, "回购"),
            ("北京万东医疗科技股份有限公司 2026年第三季度报告|上海证券报", rules.MINOR, rules.OTHER_TYPE),
            ("机构风向标 | 海象新材2026年二季度持仓占比17.04%", rules.MINOR, rules.OTHER_TYPE),
            ("股价异常波动，公司股票不停牌", rules.MINOR, rules.OTHER_TYPE),
            ("*ST某某：签订日常经营合同", rules.MAJOR, "重大合同"),
        ):
            found = rules.classify(title)
            self.assertEqual((found.severity, found.event_type), (severity, event_type), title)

    def test_mentions_needs_a_real_name_or_code(self) -> None:
        self.assertTrue(rules.mentions("海象新材发布公告", SYMBOL, NAME))
        self.assertTrue(rules.mentions("代码 003011 大涨", SYMBOL, NAME))
        self.assertFalse(rules.mentions("代码 1003011 无关", SYMBOL, NAME))
        self.assertFalse(rules.mentions("今天大盘走强", SYMBOL, NAME))
        self.assertFalse(rules.mentions("工行今天分红", "601398.SH", "工行"))

    def test_only_the_head_of_a_long_article_counts(self) -> None:
        long_text = "开头说的是别的事。" + "。" * 300 + "海象新材"
        self.assertFalse(rules.mentions(rules.head_of("市场综述", long_text), SYMBOL, NAME))

    def test_the_same_story_with_different_punctuation_is_one_event(self) -> None:
        self.assertEqual(rules.event_hash(SYMBOL, "海象新材：拟减持 1%！"), rules.event_hash(SYMBOL, "海象新材 拟减持1%"))
        self.assertNotEqual(rules.event_hash(SYMBOL, "同一标题"), rules.event_hash("300656.SZ", "同一标题"))

    def test_evening_announcements_carry_the_next_date(self) -> None:
        self.assertEqual(rules.announcement_dates(date(2026, 11, 5)), ["20261105", "20261106"])

    def test_fetch_cadence(self) -> None:
        self.assertTrue(rules.should_fetch_announcements(datetime(2026, 11, 5, 10, 18)))
        self.assertTrue(rules.should_fetch_announcements(datetime(2026, 11, 5, 20, 3)))
        self.assertTrue(rules.should_fetch_announcements(datetime(2026, 11, 5, 20, 33)))
        self.assertFalse(rules.should_fetch_announcements(datetime(2026, 11, 5, 20, 18)))
        self.assertTrue(rules.should_fetch_announcements(datetime(2026, 11, 5, 2, 3)))
        self.assertFalse(rules.should_fetch_announcements(datetime(2026, 11, 5, 2, 18)))

    def test_push_hours(self) -> None:
        self.assertTrue(rules.should_push_now(rules.MAJOR, DAYTIME, night_push=True))
        self.assertFalse(rules.should_push_now(rules.MINOR, DAYTIME, night_push=True))
        self.assertFalse(rules.should_push_now(rules.MAJOR, NIGHT, night_push=True))
        self.assertTrue(rules.should_push_now(rules.CRITICAL, NIGHT, night_push=True))
        self.assertFalse(rules.should_push_now(rules.CRITICAL, NIGHT, night_push=False))

    def test_parse_reads_a_fenced_answer_and_rejects_junk(self) -> None:
        parsed = rules.parse_interpretation("```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```")
        self.assertEqual(parsed["summary"], "海象新材：控股股东计划减持")
        self.assertTrue(parsed["important"])
        self.assertIsNone(rules.parse_interpretation("不是 JSON"))
        self.assertIsNone(rules.parse_interpretation('{"direction": "negative"}'))
        self.assertIsNone(rules.parse_interpretation(""))

    def test_parse_falls_back_on_bad_labels(self) -> None:
        parsed = rules.parse_interpretation(json.dumps({"summary": "x", "direction": "涨", "reliability": "很高"}))
        self.assertEqual((parsed["direction"], parsed["reliability"], parsed["important"]), ("neutral", "中", True))

    def test_injected_links_cannot_reach_the_card(self) -> None:
        parsed = rules.parse_interpretation(json.dumps({"summary": "点[这里](http://evil)和<at id=all></at>"}))
        for char in "[]<>":
            self.assertNotIn(char, parsed["summary"])


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        with self.Session() as db:
            db.add(Instrument(symbol=SYMBOL, name=f" {NAME} ", market="SZ", type="stock"))
            db.add(Instrument(symbol="300656.SZ", name="民德电子", market="SZ", type="stock"))
            db.add(PositionAccount(id=3, owner_user_id=OWNER, broker_name="方正证券", status="active"))
            db.add(PositionAccount(id=2, owner_user_id=OWNER, broker_name="旧账户", status="archived"))
            db.add(Position(owner_user_id=OWNER, account_id=3, symbol=SYMBOL, position_pct=0, quantity=2200, status="open"))
            db.commit()
        self.targets = {OWNER: {SYMBOL: NAME}}

    def _announce(self, db, title: str = "控股股东减持股份计划公告", symbol: str = SYMBOL) -> int:
        rows = [{"ts_code": symbol, "ann_date": "20261105", "name": NAME, "title": title, "url": "http://static.cninfo.com.cn/a.PDF"}]
        created = service.record_events(db, service.store_announcements(db, rows, self.targets), self.targets)
        db.commit()
        return created

    def _events(self, db) -> list[MarketEvent]:
        return list(db.scalars(select(MarketEvent).order_by(MarketEvent.id)).all())

    def _interpret(self, now: datetime = DAYTIME, result=GOOD) -> tuple[int, list[str]]:
        seen: list[str] = []

        def fake(material: str):
            seen.append(material)
            return result

        return service.interpret_pending(now, owner_user_id=OWNER, session_factory=self.Session, interpret=fake), seen

    def _push(self, now: datetime = DAYTIME, status: str = "sent") -> tuple[int, list]:
        calls: list = []

        def fake_send(session, channel, title, content, **kwargs):
            calls.append(SimpleNamespace(channel=channel, title=title, content=content, **kwargs))
            return SimpleNamespace(status=status, error=None, id=1)

        with patch.object(service, "send_feishu", side_effect=fake_send):
            return service.push_ready(now, owner_user_id=OWNER, session_factory=self.Session), calls


class TargetTests(_Case):
    def test_holdings_and_watchlist_are_watched_but_archived_accounts_are_not(self) -> None:
        with self.Session() as db:
            db.add(WatchlistItem(owner_user_id=OWNER, symbol="300656.SZ", group_id=1, status="active"))
            db.add(Instrument(symbol="600055.SH", name="万东医疗", market="SH", type="stock"))
            db.add(Position(owner_user_id=OWNER, account_id=2, symbol="600055.SH", position_pct=0, quantity=100, status="open"))
            db.commit()
            targets = service.watch_targets(db, OWNER)
        self.assertEqual(targets, {OWNER: {SYMBOL: NAME, "300656.SZ": "民德电子"}})


class IngestTests(_Case):
    def test_only_watched_announcements_are_stored_and_tagged(self) -> None:
        rows = [
            {"ts_code": SYMBOL, "ann_date": "20261105", "title": "减持公告", "url": "http://a"},
            {"ts_code": "000001.SZ", "ann_date": "20261105", "title": "别人的公告", "url": "http://b"},
        ]
        with self.Session() as db:
            candidates = service.store_announcements(db, rows, self.targets)
            db.commit()
            doc = db.scalar(select(Document))
            tag = db.scalar(select(DocumentSymbol))
        self.assertEqual(len(candidates), 1)
        self.assertEqual((doc.doc_type, doc.source, doc.source_url), ("announcement", "tushare:anns_d", "http://a"))
        self.assertTrue(doc.is_external_untrusted)
        self.assertEqual((tag.doc_id, tag.symbol), (doc.id, SYMBOL))

    def test_the_same_announcement_twice_is_one_document_and_one_event(self) -> None:
        with self.Session() as db:
            self.assertEqual(self._announce(db), 1)
            self.assertEqual(self._announce(db), 0)
            self.assertEqual(len(list(db.scalars(select(Document)))), 1)
            self.assertEqual(len(list(db.scalars(select(DocumentSymbol)))), 1)
            self.assertEqual(len(self._events(db)), 1)

    def test_recent_news_is_tagged_and_old_news_is_not(self) -> None:
        now_utc = to_utc(DAYTIME)
        with self.Session() as db:
            db.add(Document(doc_type="news", title="海象新材中标大单", content="详情", source="s", fetched_at=now_utc))
            db.add(Document(doc_type="news", title="海象新材旧闻", content="详情", source="s", fetched_at=now_utc - timedelta(hours=4)))
            db.add(Document(doc_type="news", title="大盘收涨", content="详情", source="s", fetched_at=now_utc))
            db.commit()
            candidates = service.tag_recent_news(db, self.targets, now_utc=now_utc)
            db.commit()
            tags = list(db.scalars(select(DocumentSymbol)))
        self.assertEqual([c["title"] for c in candidates], ["海象新材中标大单"])
        self.assertEqual(len(tags), 1)

    def test_boilerplate_in_the_body_does_not_make_news_critical(self) -> None:
        now_utc = to_utc(DAYTIME)
        with self.Session() as db:
            db.add(
                Document(
                    doc_type="news", title="海象新材 2026年第三季度报告", content="本公司保证不存在虚假记载、误导性陈述，不存在立案调查", source="s", fetched_at=now_utc,
                )
            )
            db.commit()
            candidates = service.tag_recent_news(db, self.targets, now_utc=now_utc)
            service.record_events(db, candidates, self.targets)
            db.commit()
            self.assertEqual(self._events(db)[0].severity, rules.MINOR)

    def test_the_two_channels_for_one_story_become_one_event(self) -> None:
        with self.Session() as db:
            rows = [
                {"symbol": SYMBOL, "source": "news", "doc_id": 1, "title": "海象新材：拟减持！", "url": None, "published_at": None},
                {"symbol": SYMBOL, "source": "news", "doc_id": 2, "title": "海象新材 拟减持", "url": None, "published_at": None},
            ]
            self.assertEqual(service.record_events(db, rows, self.targets), 1)

    def test_minor_news_goes_straight_to_the_queue(self) -> None:
        with self.Session() as db:
            self._announce(db, "关于召开2026年第三季度业绩说明会的公告")
            event = self._events(db)[0]
        self.assertEqual((event.severity, event.status), (rules.MINOR, service.QUEUED))
        self.assertEqual(self._interpret()[0], 0)


class InterpretTests(_Case):
    def test_the_model_sees_the_position_and_the_plan(self) -> None:
        with self.Session() as db:
            db.add(TradePlan(plan_id="p", owner_user_id=OWNER, plan_date="2026-11-05", source="premarket", job_run_id="r", status="active"))
            db.add(
                TradePlanItem(
                    plan_id="p", owner_user_id=OWNER, account_id=3, symbol=SYMBOL, name=NAME, action="reduce",
                    trigger_type="limit", trigger_price=16.55, quantity=2200, valid_until="2026-11-05",
                )
            )
            db.commit()
            self._announce(db)
        calls, seen = self._interpret()
        self.assertEqual(calls, 1)
        self.assertIn("客户持有 2200 股", seen[0])
        self.assertIn("16.55", seen[0])
        self.assertIn("<消息>", seen[0])
        with self.Session() as db:
            event = self._events(db)[0]
        self.assertEqual((event.status, event.summary, event.direction), (service.READY, GOOD["summary"], "negative"))

    def test_the_model_can_downgrade_major_but_not_critical(self) -> None:
        unimportant = {**GOOD, "important": False}
        with self.Session() as db:
            self._announce(db, "关于回购股份的进展公告")
        self._interpret(result=unimportant)
        with self.Session() as db:
            event = self._events(db)[0]
            self.assertEqual((event.severity, event.status), (rules.MINOR, service.QUEUED))
            self._announce(db, "收到立案告知书")
        self._interpret(result=unimportant)
        with self.Session() as db:
            event = self._events(db)[1]
        self.assertEqual((event.severity, event.status), (rules.CRITICAL, service.READY))

    def test_a_failed_interpretation_still_gets_critical_news_out(self) -> None:
        with self.Session() as db:
            self._announce(db, "收到立案告知书")
            self._announce(db, "控股股东减持公告")
        self._interpret(result=None)
        with self.Session() as db:
            critical, major = self._events(db)
        self.assertEqual(critical.status, service.READY)
        self.assertEqual(critical.summary, f"{NAME}：收到立案告知书")
        self.assertEqual(major.status, service.QUEUED)

    def test_one_run_interprets_at_most_three(self) -> None:
        with self.Session() as db:
            for index in range(5):
                self._announce(db, f"第{index}号减持公告")
        self.assertEqual(self._interpret()[0], service.MAX_INTERPRET_PER_RUN)
        with self.Session() as db:
            statuses = [event.status for event in self._events(db)]
        self.assertEqual(statuses.count(service.NEW), 2)

    def test_the_daily_limit_queues_major_but_not_critical(self) -> None:
        with self.Session() as db:
            for index in range(service.MAX_INTERPRET_PER_DAY):
                db.add(
                    MarketEvent(
                        owner_user_id=OWNER, symbol="300656.SZ", stock_name="民德电子", source="news", title=f"t{index}",
                        content_hash=f"h{index}", event_type="回购", severity=rules.MAJOR, status=service.PUSHED, interpreted_at=to_utc(DAYTIME),
                    )
                )
            db.commit()
            self._announce(db, "控股股东减持公告")
            self._announce(db, "收到立案告知书")
        calls, _ = self._interpret()
        self.assertEqual(calls, 0)
        with self.Session() as db:
            events = {event.title: event for event in self._events(db)}
        self.assertEqual(events["控股股东减持公告"].status, service.QUEUED)
        self.assertEqual(events["收到立案告知书"].status, service.READY)

    def test_yesterdays_interpretations_do_not_count(self) -> None:
        with self.Session() as db:
            for index in range(service.MAX_INTERPRET_PER_DAY):
                db.add(
                    MarketEvent(
                        owner_user_id=OWNER, symbol="300656.SZ", stock_name="民德电子", source="news", title=f"t{index}", content_hash=f"h{index}",
                        event_type="回购", severity=rules.MAJOR, status=service.PUSHED, interpreted_at=to_utc(DAYTIME - timedelta(days=1)),
                    )
                )
            db.commit()
            self._announce(db)
        self.assertEqual(self._interpret()[0], 1)

    def test_one_symbol_is_pushed_at_most_once_an_hour_unless_critical(self) -> None:
        with self.Session() as db:
            db.add(
                MarketEvent(
                    owner_user_id=OWNER, symbol=SYMBOL, stock_name=NAME, source="news", title="刚推过", content_hash="h",
                    event_type="回购", severity=rules.MAJOR, status=service.PUSHED, notified_at=to_utc(DAYTIME - timedelta(minutes=30)),
                )
            )
            db.commit()
            self._announce(db, "控股股东减持公告")
            self._announce(db, "收到立案告知书")
        self.assertEqual(self._interpret()[0], 1)
        with self.Session() as db:
            events = {event.title: event for event in self._events(db)}
        self.assertEqual(events["控股股东减持公告"].status, service.QUEUED)
        self.assertEqual(events["收到立案告知书"].status, service.READY)


class PushTests(_Case):
    def _ready(self, title: str = "控股股东减持公告") -> None:
        with self.Session() as db:
            self._announce(db, title)
        self._interpret()

    def test_a_replayed_announcement_is_pushed_once_with_one_sentence_and_a_link(self) -> None:
        self._ready()
        pushed, calls = self._push()
        self.assertEqual(pushed, 1)
        call = calls[0]
        self.assertEqual(call.title, f"🔔 {NAME} · 增减持")
        self.assertEqual(call.card_brief, GOOD["summary"])
        self.assertEqual((call.share.source_type, call.share.owner_user_id), ("market_event", OWNER))
        self.assertEqual(call.card_template, "orange")
        self.assertIn("影响今天的卖单", call.content)
        self.assertIn("http://static.cninfo.com.cn/a.PDF", call.content)
        with self.Session() as db:
            self._announce(db)  # 下一轮又拉到同一条
            self.assertEqual(self._events(db)[0].status, service.PUSHED)
        self.assertEqual(self._interpret()[0], 0)
        self.assertEqual(self._push()[0], 0)

    def test_critical_news_is_red(self) -> None:
        self._ready("收到立案告知书")
        _, calls = self._push()
        self.assertEqual(calls[0].card_template, "red")
        self.assertTrue(calls[0].title.startswith("🚨"))

    def test_major_news_waits_overnight_but_critical_does_not(self) -> None:
        with self.Session() as db:
            self._announce(db, "控股股东减持公告")
            self._announce(db, "收到立案告知书")
        self._interpret(NIGHT)
        pushed, calls = self._push(NIGHT)
        self.assertEqual(pushed, 1)
        self.assertIn("立案", calls[0].title)
        with self.Session() as db:
            events = {event.title: event.status for event in self._events(db)}
        self.assertEqual(events["控股股东减持公告"], service.QUEUED)

    def test_the_night_switch_holds_critical_news_too(self) -> None:
        self._ready("收到立案告知书")
        with patch.object(service, "settings", SimpleNamespace(event_night_push=False)):
            pushed, _ = self._push(NIGHT)
        self.assertEqual(pushed, 0)

    def test_a_failed_push_retries_then_joins_the_next_report(self) -> None:
        self._ready()
        self.assertEqual(self._push(status="failed")[0], 0)
        with self.Session() as db:
            self.assertEqual(self._events(db)[0].status, service.READY)
        self.assertEqual(self._push(DAYTIME + timedelta(minutes=10), status="sent")[0], 1)

    def test_a_push_that_keeps_failing_is_queued_after_thirty_minutes(self) -> None:
        self._ready()
        self._push(DAYTIME + timedelta(minutes=31), status="failed")
        with self.Session() as db:
            self.assertEqual(self._events(db)[0].status, service.QUEUED)

    def test_the_page_body_never_links_a_non_web_url(self) -> None:
        event = MarketEvent(stock_name=NAME, title="标[题]", source="news", url="javascript:alert(1)", summary="一句话")
        body = service.event_markdown(event)
        self.assertNotIn("javascript", body)
        self.assertIn("标（题）", body)


class DigestTests(_Case):
    def test_queued_news_joins_the_next_report_once(self) -> None:
        with self.Session() as db:
            self._announce(db, "关于召开业绩说明会的公告")
            text, ids = service.report_digest(db, OWNER, DAYTIME)
            self.assertIn("关于召开业绩说明会的公告", text)
            self.assertIn("只当资料看", text)
            self.assertEqual(len(ids), 1)
            service.mark_merged(db, ids)
            db.commit()
            self.assertEqual(service.report_digest(db, OWNER, DAYTIME), ("", []))

    def test_important_news_goes_first_and_old_news_drops_out(self) -> None:
        with self.Session() as db:
            for index in range(service._DIGEST_LIMIT + 2):
                db.add(
                    MarketEvent(
                        owner_user_id=OWNER, symbol=SYMBOL, stock_name=NAME, source="news", title=f"一般消息{index}", content_hash=f"m{index}",
                        event_type="其他", severity=rules.MINOR, status=service.QUEUED, created_at=to_utc(DAYTIME),
                    )
                )
            db.add(
                MarketEvent(
                    owner_user_id=OWNER, symbol=SYMBOL, stock_name=NAME, source="news", title="夜里的减持公告", content_hash="major",
                    event_type="增减持", severity=rules.MAJOR, status=service.QUEUED, created_at=to_utc(DAYTIME),
                )
            )
            db.add(
                MarketEvent(
                    owner_user_id=OWNER, symbol=SYMBOL, stock_name=NAME, source="news", title="前天的消息", content_hash="old",
                    event_type="其他", severity=rules.MINOR, status=service.QUEUED, created_at=to_utc(DAYTIME - timedelta(hours=25)),
                )
            )
            db.commit()
            text, ids = service.report_digest(db, OWNER, DAYTIME)
        self.assertEqual(len(ids), service._DIGEST_LIMIT)
        self.assertLess(text.index("夜里的减持公告"), text.index("一般消息"))
        self.assertNotIn("前天的消息", text)

    def test_other_peoples_news_stays_out(self) -> None:
        with self.Session() as db:
            self._announce(db, "关于召开业绩说明会的公告")
            self.assertEqual(service.report_digest(db, OWNER + 1, DAYTIME), ("", []))

    def _digest_for(self, plan_source, name, owner=OWNER):
        from backend.app.scheduler import rhythm_scheduler

        with patch.object(rhythm_scheduler, "SessionLocal", self.Session):
            return rhythm_scheduler._event_digest({"owner_user_id": owner, "name": name}, plan_source, DAYTIME)

    def test_reports_that_take_the_digest(self) -> None:
        with self.Session() as db:
            self._announce(db, "关于召开业绩说明会的公告")
        self.assertTrue(self._digest_for("premarket", "早盘挂单清单")[0])
        self.assertTrue(self._digest_for(None, "上午盘中短提醒")[0])
        self.assertEqual(self._digest_for("selection", "全自动选股"), ("", []))
        self.assertEqual(self._digest_for(None, "别的报告"), ("", []))
        self.assertEqual(self._digest_for("premarket", "早盘挂单清单", owner=None), ("", []))


class FlagTests(_Case):
    ACTIVATED = to_utc(DAYTIME - timedelta(hours=2))

    def _plan(self, db) -> None:
        db.add(TradePlan(plan_id="p", owner_user_id=OWNER, plan_date="2026-11-05", source="premarket", job_run_id="r", status="active", activated_at=self.ACTIVATED))
        db.commit()

    def _event(self, db, **changes) -> None:
        values = dict(
            owner_user_id=OWNER, symbol=SYMBOL, stock_name=NAME, source="news", title="减持", content_hash=str(changes),
            event_type="增减持", severity=rules.MAJOR, status=service.PUSHED, summary="海象新材减持", created_at=to_utc(DAYTIME), direction="negative",
        )
        values.update(changes)
        db.add(MarketEvent(**values))
        db.commit()

    def _flags(self, db):
        return service.open_flags(db, OWNER, {SYMBOL}, self.ACTIVATED)

    def test_news_after_the_plan_is_flagged(self) -> None:
        with self.Session() as db:
            self._event(db)
            self.assertEqual(service.flag_text(self._flags(db)[SYMBOL]), "增减持：海象新材减持")

    def test_news_before_the_plan_is_not_flagged(self) -> None:
        with self.Session() as db:
            self._event(db, created_at=self.ACTIVATED - timedelta(hours=1))
            self.assertEqual(self._flags(db), {})

    def test_good_news_minor_news_and_unread_news_are_not_flagged(self) -> None:
        with self.Session() as db:
            self._event(db, direction="positive")
            self._event(db, severity=rules.MINOR)
            self._event(db, status=service.NEW)
            self.assertEqual(self._flags(db), {})

    def test_the_plan_page_shows_the_flag(self) -> None:
        from backend.app.core.models import ReportShare
        from backend.app.plans.page import render_plan_page

        with self.Session() as db:
            self._plan(db)
            db.add(
                TradePlanItem(
                    plan_id="p", owner_user_id=OWNER, account_id=3, symbol=SYMBOL, name=NAME, action="reduce",
                    trigger_type="limit", trigger_price=16.55, quantity=2200, valid_until="2026-11-05",
                )
            )
            db.commit()
            self._event(db)
            share = ReportShare(
                share_id="a" * 32, owner_user_id=OWNER, source_type="trade_plan", source_ref="p", title="计划", content_md="",
                expires_at=datetime(2026, 12, 5),
            )
            html = render_plan_page(db, share)
        self.assertIn("消息待复核｜增减持：海象新材减持", html)

    def test_a_price_alert_mentions_news_the_plan_has_not_seen(self) -> None:
        from backend.app.plans.watcher import alert_content

        with self.Session() as db:
            self._plan(db)
            item = TradePlanItem(
                plan_id="p", owner_user_id=OWNER, account_id=3, symbol=SYMBOL, name=NAME, action="reduce",
                trigger_type="limit", trigger_price=16.55, quantity=2200, valid_until="2026-11-05", status="triggered",
            )
            db.add(item)
            db.commit()
            plan = db.get(TradePlan, "p")
            self.assertNotIn("计划还没考虑", alert_content(db, [(item, plan)], {SYMBOL: {"price": 16.6}}))
            self._event(db)
            text = alert_content(db, [(item, plan)], {SYMBOL: {"price": 16.6}})
        self.assertIn("计划之后出了消息，计划还没考虑：增减持：海象新材减持", text)


class WiringTests(_Case):
    def test_message_watch_only_processes_events_for_its_owner(self) -> None:
        other_owner = OWNER + 1
        other_symbol = "300656.SZ"
        with self.Session() as db:
            db.add(WatchlistItem(owner_user_id=other_owner, symbol=other_symbol, group_id=1, status="active"))
            for status in (service.NEW, service.READY):
                db.add(MarketEvent(
                    owner_user_id=other_owner, symbol=other_symbol, stock_name="民德电子",
                    source="news", title="外部账户事件", content_hash=status,
                    event_type="增减持", severity=rules.MAJOR, status=status,
                ))
            db.commit()

        announcements = [
            {"ts_code": symbol, "ann_date": "20261105", "title": "控股股东减持股份计划公告"}
            for symbol in (SYMBOL, other_symbol)
        ]
        with (
            patch.object(service, "SessionLocal", self.Session),
            patch.object(service, "fetch_announcements", return_value=announcements),
            patch.object(service, "interpret_with_model", return_value=GOOD) as interpret,
            patch.object(service, "send_feishu", return_value=SimpleNamespace(status="sent")) as push,
        ):
            self.assertIn("新事件 1 条，解读 1 条，推送 1 条", service.run_message_watch(DAYTIME, owner_user_id=OWNER))
            interpret.assert_called_once()
            self.assertIn(NAME, interpret.call_args.args[0])
            push.assert_called_once()
            self.assertEqual(push.call_args.kwargs["share"].owner_user_id, OWNER)

        with self.Session() as db:
            self.assertEqual(
                [(event.owner_user_id, event.status) for event in self._events(db)],
                [(other_owner, service.NEW), (other_owner, service.READY), (OWNER, service.PUSHED)],
            )

    def test_the_job_is_on_the_default_list_and_avoids_the_news_minutes(self) -> None:
        from backend.app.scheduler import rhythm_scheduler

        job = next(item for item in rhythm_scheduler.DEFAULT_JOBS if item["job_key"] == "message-watch-15m")
        self.assertEqual((job["job_kind"], job["interval_minutes"], job["window_start"]), ("message_watch", 15, "00:03"))
        self.assertTrue(job["enabled"])
        self.assertFalse(job["trading_day_only"])

    def test_digital_humans_only_read_their_own_events(self) -> None:
        from backend.app.agents.sql_sandbox import prepare_query

        prepared = str(prepare_query("SELECT title FROM market_events", owner_user_id=OWNER, max_rows=10))
        self.assertIn("owner_user_id", prepared)

    def test_the_watch_never_raises(self) -> None:
        with patch.object(service, "SessionLocal", side_effect=RuntimeError("boom")):
            self.assertIn("消息盯盘失败", service.run_message_watch(DAYTIME, owner_user_id=OWNER))

    def test_get_anns_asks_for_fields_that_exist(self) -> None:
        from backend.app.market import stock_tools

        with patch.object(stock_tools, "_semantic_tushare", return_value={}) as call:
            stock_tools.get_anns("003011.SZ")
        self.assertIn("title", call.call_args.args[2].split(","))
        self.assertNotIn("ann_title", call.call_args.args[2].split(","))


if __name__ == "__main__":
    unittest.main()
