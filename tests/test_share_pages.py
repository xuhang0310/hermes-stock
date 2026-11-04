"""手机版分享页：渲染与清洗、链接生命周期、飞书卡片按钮。

全部用内存 SQLite 和打桩的飞书发送，不连 MySQL，不写生产库。
"""
from __future__ import annotations

import dataclasses
import re
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth.auth import AuthContext, get_current_user
from backend.app.core.database import get_db
from backend.app.core.models import Base, Notification, NotifyChannel, ReportShare, User
from backend.app.core.settings import settings as real_settings
from backend.app.data import share_service
from backend.app.data.share_api import router
from backend.app.data.share_render import render_markdown, render_notice_page, render_share_page
from backend.app.data.share_service import ShareSpec, create_share_link, share_status
from backend.app.feishu import feishu
from backend.app.feishu.feishu import _build_payloads, encrypt_value, send_feishu

BASE_URL = "https://stock.example.com"
PLAN_TABLE = """\
| 股票（最新价） | 今天怎么做 | 价格 | 股数（股） |
| --- | --- | --- | --- |
| 甲 22.35 | 反弹卖一部分 | 限价 24.13 元 | 卖 500，留 1,000 |
| 乙 16.80 | 早盘止损已触发，下午清掉 | 15.98 元 | 卖 200，留 0 |
| 丙 8.48 | **准备买**：2000股，按条件 | 限价 8.30 元 | 买 2,000 |
| 丁 13.76 | 拿着，跌破 12 止损 | — | 持有 3,000 |
| 戊 4.15 | **不参与**：0股，不同时买第二只 | — | 0 |
"""


def _settings(**changes):
    return dataclasses.replace(real_settings, **changes)


class _DbCase(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        tables = [ReportShare.__table__, Notification.__table__, NotifyChannel.__table__, User.__table__]
        Base.metadata.create_all(self.engine, tables=tables)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        patcher = patch.object(share_service, "settings", _settings(public_base_url=BASE_URL, share_ttl_days=30))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.engine.dispose)


class RenderTests(unittest.TestCase):
    def test_raw_html_and_dangerous_links_are_neutralised(self) -> None:
        body, _ = render_markdown(
            "<script>alert(1)</script>\n\n"
            '<img src=x onerror="alert(2)">\n\n'
            "[点我](javascript:alert(3))\n\n"
            "![图](javascript:alert(4))\n\n"
            "[正常](https://example.com/a)\n"
        )
        self.assertNotIn("<script", body)
        self.assertNotIn("<img", body)
        self.assertNotIn('href="javascript', body)
        self.assertIn('href="https://example.com/a"', body)
        self.assertIn('rel="noopener noreferrer nofollow"', body)

    def test_html_smuggled_through_table_headers_stays_text(self) -> None:
        body, _ = render_markdown('| "><script>alert(1)</script> | 说明 | 价格 |\n| --- | --- | --- |\n| a | ' + "很长" * 20 + " | 1 |\n")
        self.assertNotIn("<script", body)
        self.assertIn("&lt;script&gt;", body)

    def test_bold_next_to_chinese_punctuation_renders(self) -> None:
        body, _ = render_markdown("**结论：**后文继续，**今天（周二）**再看。")
        self.assertIn("<strong>结论：</strong>后文", body)
        self.assertIn("<strong>今天（周二）</strong>再看", body)
        self.assertNotIn("**", body)

    def test_plan_table_becomes_ledger_rows_with_action_tags(self) -> None:
        body, _ = render_markdown(PLAN_TABLE)
        self.assertIn('<div class="ledger"><table class="plan">', body)
        rows = re.findall(r"<tr>.*?</tr>", body, flags=re.S)[1:]
        self.assertEqual(len(rows), 5)
        self.assertIn('data-tag="卖出"', rows[0])
        self.assertIn("act-sell", rows[0])
        self.assertIn('data-tag="止损"', rows[1])
        self.assertIn("act-stop", rows[1])
        self.assertIn('data-tag="买入"', rows[2])
        self.assertIn("act-buy", rows[2])
        # 第一小句是“拿着”或“不参与”的行不上买卖色，哪怕后面提到止损或买
        self.assertIn('data-tag="拿着"', rows[3])
        self.assertIn("act-none", rows[3])
        self.assertIn('data-tag="不参与"', rows[4])
        for row in rows[3:]:
            self.assertNotRegex(row, "act-(buy|sell|stop)")

    def test_ledger_row_splits_name_code_and_price(self) -> None:
        body, _ = render_markdown(
            "| 股票（最新价） | 今天怎么做 | 价格 | 股数（股） |\n| --- | --- | --- | --- |\n"
            "| 同力天启605286（21.57元） | 反弹卖一部分 | 限价22.18元 | 卖2,400，留4,800 |\n"
        )
        self.assertIn('<span class="nm">同力天启</span>', body)
        self.assertIn('<span class="cd fig">605286</span>', body)
        self.assertIn('现价 <span class="fig">21.57</span>', body)
        self.assertIn('<span class="lb">限价</span><span class="fig big">22.18</span>', body)
        self.assertNotIn("px-note", body, "“限价22.18元”拆完没有剩下别的话")
        self.assertIn("卖2,400，留4,800", body)
        self.assertIn("反弹卖一部分", body)

    def test_hold_phrased_differently_still_gets_the_same_tag(self) -> None:
        body, _ = render_markdown(
            "| 股票 | 怎么做 | 价格 | 股数 |\n| --- | --- | --- | --- |\n"
            "| 甲 | 按早盘安排持有，不加仓 | 不挂单 | 持有7,000 |\n"
        )
        self.assertIn('data-tag="拿着"', body)
        self.assertIn("按早盘安排持有，不加仓", body)

    def test_neutral_tag_is_not_repeated_on_the_action_line(self) -> None:
        body, _ = render_markdown(
            "| 原排名、股票和收盘价 | 为什么入选 | 11月4日怎么做 | 要留心什么 |\n| --- | --- | --- | --- |\n"
            "| #2 甲 | 低估值 | **不参与**：0股，财报压力较大。 | 回款 |\n"
            "| #1 乙 | 低波动 | **准备买**：2000股，按条件。 | 利润 |\n"
        )
        rows = re.findall(r"<tr>.*?</tr>", body, flags=re.S)[1:]
        self.assertNotIn("data-tag", rows[0])
        self.assertIn('data-tag="买入"', rows[1])

    def test_price_cell_never_loses_words(self) -> None:
        cases = {
            "9:15集合竞价，限价120.01元": "120.01",
            "22.45→**22.18**；止损触发20.59不变": "22.18",
            "开盘买一≥155.00时限价**155.00**；低于155.00不卖": "155.00",
        }
        for cell, price in cases.items():
            with self.subTest(cell=cell):
                body, _ = render_markdown(f"| 股票 | 价格 | 股数 |\n| --- | --- | --- |\n| 甲 | {cell} | 卖200 |\n")
                self.assertIn(f'<span class="fig big">{price}</span>', body)
                note = re.search(r'<span class="px-note">(.*?)</span>', body).group(1)
                self.assertEqual(note, cell.replace("**", "").replace("≥", "≥"))

    def test_price_cell_without_a_number_stays_a_labelled_line(self) -> None:
        body, _ = render_markdown("| 股票 | 价格 | 股数 |\n| --- | --- | --- |\n| 甲 | 不挂单 | 持有7,000 |\n| 乙 | — | 持有100 |\n")
        rows = re.findall(r"<tr>.*?</tr>", body, flags=re.S)[1:]
        self.assertIn('data-label="价格"', rows[0])
        self.assertIn("不挂单", rows[0])
        self.assertIn("dup", rows[1], "只有一个横杠就不用占一行")

    def test_cell_text_cannot_inject_markup_through_the_ledger(self) -> None:
        body, _ = render_markdown(
            "| 股票 | 价格 | 股数 |\n| --- | --- | --- |\n"
            "| <b>甲</b>605286（1.00元） | <img src=x onerror=1>限价22.18元 | 卖200 |\n"
        )
        self.assertNotIn("<b>", body)
        self.assertNotIn("<img", body)
        self.assertIn("&lt;b&gt;甲&lt;/b&gt;", body)
        self.assertIn("&lt;img", body)

    def test_real_plan_headers_are_recognised(self) -> None:
        for header in (
            "| 账户 | 股票 | 价格 | 股数 |",
            "| 股票（最新价） | 怎么做 | 卖价和止损价 | 股数安排 |",
            "| 原排名、股票和收盘价 | 为什么入选 | 11月4日怎么做 | 要留心什么 |",
        ):
            with self.subTest(header=header):
                separator = "|" + " --- |" * header.count(" | ") + " --- |"
                row = "| a | b | c | d |"
                body, _ = render_markdown(f"{header}\n{separator}\n{row}\n")
                self.assertIn('<table class="plan">', body)

    def test_plain_tables_scroll_and_keep_alignment(self) -> None:
        for header in ("| 账户 | 持股 | 成本价 | 浮亏及亏幅 |", "| 账户 | 登记现金 | 本次预留 | 都成交后的现金 |"):
            with self.subTest(header=header):
                body, _ = render_markdown(f"{header}\n| --- | --- | ---: | :---: |\n| 甲 | 乙 | 1 | 2 |\n")
                self.assertIn('<div class="table-wrap"><table>', body)
                self.assertNotIn("data-label", body)
                self.assertIn('class="ar"', body)
                self.assertIn('class="ac"', body)
                self.assertNotIn("style=", body)

    def test_long_text_table_becomes_lines_without_action_tags(self) -> None:
        long_cell = "这一格的文字很长，放进窄屏的横向表格里读起来很吃力，需要改成分行显示"
        body, _ = render_markdown(f"| 风险 | 什么时候算发生 | 说明 |\n| --- | --- | --- |\n| 甲 | {long_cell} | 丙 |\n")
        self.assertIn('<div class="ledger"><table class="lines">', body)
        self.assertIn('data-label="什么时候算发生"', body)
        self.assertNotIn("act-", body)
        self.assertNotIn("data-tag", body)

    def test_plain_table_numbers_use_the_figure_font(self) -> None:
        body, _ = render_markdown("| 账户 | 现金 | 说明 |\n| --- | --- | --- |\n| 甲 | 1,234.50 | 没动 |\n")
        self.assertIn('<td class="fig">1,234.50</td>', body)
        self.assertIn("<td>没动</td>", body)

    def test_two_column_table_with_long_text_stays_a_table(self) -> None:
        long_cell = "两列放得下，不需要改成卡片，也不用横向滑动，保持表格的样子就可以了"
        body, _ = render_markdown(f"| 项目 | 内容 |\n| --- | --- |\n| 甲 | {long_cell} |\n")
        self.assertIn('<div class="table-wrap"><table>', body)

    def test_toc_appears_only_for_long_reports(self) -> None:
        def page(sections: int) -> str:
            source = "\n\n".join(f"## 第{i}节 **重点**\n\n内容" for i in range(1, sections + 1))
            return render_share_page(title="t", meta={}, content_md=source, expires_on="2026-12-04")

        self.assertNotIn('class="toc"', page(3))
        long_page = page(4)
        self.assertIn('class="toc"', long_page)
        self.assertIn('<a href="#s4">第4节 重点</a>', long_page)
        self.assertIn('<h2 id="s4">', long_page)

    def test_lead_marks_only_an_opening_paragraph(self) -> None:
        single = render_markdown("结论在这。\n\n后面一段。")[0]
        self.assertIn('<p class="lead lead-big lead-end">结论', single)
        self.assertEqual(single.count("lead-big"), 1)
        self.assertIn('<p class="lead lead-big lead-end">结论', render_markdown("## 结论\n\n结论在这。")[0])
        self.assertNotIn("lead", render_markdown("| 账户 | 现金 |\n| --- | --- |\n| 甲 | 1 |\n\n后面一段。")[0])
        self.assertNotIn("lead", render_markdown("- 第一条\n\n后面一段。")[0])

    def test_lead_first_line_is_enlarged_and_the_rest_supports_it(self) -> None:
        body, _ = render_markdown("今天**减弱仓**。\n先处理账户3。\n新买只看一只。\n\n后面一段。")
        self.assertIn('<p class="lead lead-big">今天<strong>减弱仓</strong>。</p>', body)
        self.assertIn('<p class="lead-end">先处理账户3。<br>\n新买只看一只。</p>', body)

    def test_long_first_line_is_not_enlarged(self) -> None:
        long_line = "这一句很长" * 20
        self.assertIn('<p class="lead lead-end">', render_markdown(f"{long_line}\n第二句")[0])

    def test_bold_across_lines_keeps_the_lead_whole(self) -> None:
        body, _ = render_markdown("**跨行\n的粗体**还有后文\n\n后面。")
        self.assertIn('<p class="lead lead-end"><strong>跨行', body)
        self.assertEqual(body.count("<strong>"), 1)

    def test_stats_and_toc_come_after_the_conclusion(self) -> None:
        source = "结论。\n\n" + "\n\n".join(f"## 第{i}节\n\n内容" for i in range(1, 5))
        page = render_share_page(
            title="t",
            meta={"plan_stats": {"sell": 4, "buy": 1, "stop": 2, "hold": 1}},
            content_md=source,
            expires_on="2026-12-04",
        )
        self.assertLess(page.index("结论。"), page.index('class="kpi"'))
        self.assertLess(page.index('class="kpi"'), page.index('class="toc"'))
        self.assertLess(page.index('class="toc"'), page.index("第1节</h2>"))
        self.assertIn('<span class="fig">4</span><span class="kl">笔卖出</span>', page)
        self.assertIn('<span class="fig">2</span><span class="kl">条止损线</span>', page)

    def test_stats_without_any_action_or_a_plan_are_hidden(self) -> None:
        for meta in ({}, {"plan_stats": {"sell": 0, "buy": 0, "stop": 0, "hold": 0}}, {"plan_stats": "x"}):
            with self.subTest(meta=meta):
                page = render_share_page(title="t", meta=meta, content_md="结论。", expires_on="2026-12-04")
                self.assertNotIn('class="kpi"', page)

    def test_plan_link_sits_at_the_end_of_the_report(self) -> None:
        page = render_share_page(
            title="t",
            meta={"plan_url": "https://stock.example.com/api/share/abc"},
            content_md="结论。\n\n正文。",
            expires_on="2026-12-04",
        )
        self.assertLess(page.index("正文。"), page.index('class="plan-link"'))
        self.assertLess(page.index('class="plan-link"'), page.index('class="page-foot"'))
        unsafe = render_share_page(title="t", meta={"plan_url": "javascript:alert(1)"}, content_md="x", expires_on="2026-12-04")
        self.assertNotIn("plan-link", unsafe.split("</style>")[1])

    def test_page_is_self_contained_and_escapes_metadata(self) -> None:
        page = render_share_page(
            title="早盘清单<script>alert(1)</script> | 2026-11-04",
            meta={"digital_human": '总调度"><img src=x>', "generated_at": "2026-11-04 08:52:10"},
            content_md="结论。",
            expires_on="2026-12-04",
        )
        self.assertNotIn("<script", page)
        self.assertNotIn("<img", page)
        self.assertNotIn("<link", page)
        self.assertEqual(re.findall(r'src="([^"]*)"', page), [], "页面不加载任何外部资源")
        # 唯一内嵌的资源是数字字体
        urls = re.findall(r"url\(([^)]*)\)", page)
        self.assertEqual(len(urls), 1)
        self.assertTrue(urls[0].startswith("data:font/woff2;base64,"))
        self.assertNotRegex(page, r"https?://")
        self.assertIn('name="viewport"', page)
        self.assertIn("2026 年 11 月 4 日 周三　08:52　总调度&quot;&gt;&lt;img src=x&gt;", page)
        self.assertIn("生成于 11-04 08:52，链接有效期至 2026-12-04。", page)
        self.assertIn("No. 1104-0852", page)
        # 卡片标题里的日期不再重复放进页面大标题
        self.assertRegex(page, r'<h1 class="page-title">早盘清单&lt;script&gt;alert\(1\)&lt;/script&gt;</h1>')

    def test_page_shell_stays_small(self) -> None:
        self.assertLess(len(render_notice_page("链接已过期", "x").encode("utf-8")), 16 * 1024)

    def test_page_uses_the_same_design_tokens_as_the_website(self) -> None:
        site = Path(__file__).resolve().parents[1].joinpath("frontend/src/styles.css").read_text(encoding="utf-8")
        site_root = re.search(r":root \{(.*?)\}", site, flags=re.S).group(1)
        site_tokens = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]+)", site_root))
        page = render_notice_page("x", "y")
        self.assertGreater(len(site_tokens), 15)
        for name in ("--bg", "--text", "--muted", "--line", "--line-strong", "--gold", "--gold-2", "--gold-dim", "--danger", "--ok"):
            with self.subTest(token=name):
                self.assertIn(f"{name}:{site_tokens[name]}", page)
        self.assertIn("color-scheme:dark", page)
        self.assertIn("SIXRABBITS RESEARCH", page)


class ShareLifecycleTests(_DbCase):
    def _client(self, user_id: int = 7, permissions: set[str] | None = None) -> TestClient:
        app = FastAPI()
        app.include_router(router)

        def _db():
            with self.Session() as session:
                yield session

        app.dependency_overrides[get_db] = _db
        user = User(id=user_id, username=f"u{user_id}", role="researcher")
        context = AuthContext(user=user, permissions=permissions or {"data:read", "data:write"})
        app.dependency_overrides[get_current_user] = lambda: context
        return TestClient(app)

    def _create(self, **overrides) -> str:
        spec = dict(source_type="agent_report", source_ref="rhythm-1", owner_user_id=7, meta={"generated_at": "2026-11-04 08:52:10"})
        spec.update(overrides)
        with self.Session() as session:
            url = create_share_link(session, ShareSpec(**spec), title="早盘清单 | 2026-11-04", content="今天**不追高**。")
            session.commit()
        return url

    def _row(self, share_id: str) -> ReportShare:
        with self.Session() as session:
            return session.get(ReportShare, share_id)

    def test_no_public_base_url_means_no_link_and_no_row(self) -> None:
        with patch.object(share_service, "settings", _settings(public_base_url="")):
            self.assertIsNone(self._create())
        with self.Session() as session:
            self.assertEqual(session.scalars(select(ReportShare)).all(), [])

    def test_link_format_and_default_expiry(self) -> None:
        url = self._create()
        self.assertRegex(url, rf"^{re.escape(BASE_URL)}/api/share/[0-9a-f]{{32}}$")
        row = self._row(url.rsplit("/", 1)[1])
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.assertAlmostEqual((row.expires_at - now).total_seconds(), 30 * 86400, delta=60)
        self.assertEqual(row.view_count, 0)
        self.assertEqual(share_status(row), "active")

    def test_same_source_reuses_the_link_and_refreshes_content(self) -> None:
        first = self._create()
        with self.Session() as session:
            second = create_share_link(
                session,
                ShareSpec(source_type="agent_report", source_ref="rhythm-1", owner_user_id=7),
                title="新标题",
                content="新内容",
            )
            session.commit()
        self.assertEqual(first, second)
        self.assertEqual(self._row(first.rsplit("/", 1)[1]).content_md, "新内容")

    def test_sources_without_ref_always_get_a_new_link(self) -> None:
        self.assertNotEqual(self._create(source_ref=None), self._create(source_ref=None))

    def test_page_serves_html_with_security_headers_and_counts_views(self) -> None:
        share_id = self._create().rsplit("/", 1)[1]
        client = self._client()
        response = client.get(f"/api/share/{share_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("<strong>不追高</strong>", response.text)
        self.assertEqual(response.headers["x-robots-tag"], "noindex, nofollow")
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertIn("default-src 'none'", response.headers["content-security-policy"])
        self.assertIn("font-src data:", response.headers["content-security-policy"])
        self.assertNotIn("script-src", response.headers["content-security-policy"])
        client.get(f"/api/share/{share_id}")
        row = self._row(share_id)
        self.assertEqual(row.view_count, 2)
        self.assertIsNotNone(row.last_viewed_at)

    def test_page_needs_no_login(self) -> None:
        share_id = self._create().rsplit("/", 1)[1]
        app = FastAPI()
        app.include_router(router)

        def _db():
            with self.Session() as session:
                yield session

        app.dependency_overrides[get_db] = _db
        client = TestClient(app)
        self.assertEqual(client.get(f"/api/share/{share_id}").status_code, 200)
        self.assertEqual(client.get("/api/shares").status_code, 401)

    def test_expired_revoked_and_unknown_links_show_a_notice(self) -> None:
        share_id = self._create().rsplit("/", 1)[1]
        with self.Session() as session:
            session.get(ReportShare, share_id).expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
            session.commit()
        client = self._client()
        expired = client.get(f"/api/share/{share_id}")
        self.assertEqual(expired.status_code, 410)
        self.assertIn("链接已过期", expired.text)
        self.assertNotIn("不追高", expired.text)
        self.assertEqual(self._row(share_id).view_count, 0)

        revoked_id = self._create(source_ref="rhythm-2").rsplit("/", 1)[1]
        self.assertEqual(client.post(f"/api/shares/{revoked_id}/revoke").json()["data"]["status"], "revoked")
        revoked = client.get(f"/api/share/{revoked_id}")
        self.assertEqual(revoked.status_code, 410)
        self.assertIn("链接已作废", revoked.text)
        self.assertIn("X-Robots-Tag".lower(), revoked.headers)

        for bad in ("0" * 32, "not-a-share", share_id.upper()):
            with self.subTest(bad=bad):
                self.assertEqual(client.get(f"/api/share/{bad}").status_code, 404)

    def test_owners_only_see_and_revoke_their_own_links(self) -> None:
        mine = self._create().rsplit("/", 1)[1]
        other = self._create(source_ref="rhythm-2", owner_user_id=8).rsplit("/", 1)[1]
        builtin = self._create(source_ref="rhythm-3", owner_user_id=None).rsplit("/", 1)[1]
        client = self._client()

        listed = client.get("/api/shares").json()
        self.assertTrue(listed["success"])
        self.assertEqual([item["share_id"] for item in listed["data"]], [mine])
        self.assertNotIn("content_md", listed["data"][0])
        self.assertTrue(listed["data"][0]["created_at"].endswith("+00:00"))

        for target in (other, builtin, "0" * 32):
            with self.subTest(target=target):
                self.assertEqual(client.post(f"/api/shares/{target}/revoke").status_code, 404)
        self.assertIsNone(self._row(other).revoked_at)
        self.assertEqual(client.post(f"/api/shares/{mine}/revoke").status_code, 200)

    def test_admins_see_and_revoke_everyones_links(self) -> None:
        other = self._create(owner_user_id=8).rsplit("/", 1)[1]
        builtin = self._create(source_ref="rhythm-3", owner_user_id=None).rsplit("/", 1)[1]
        client = self._client(permissions={"all"})
        self.assertEqual({item["share_id"] for item in client.get("/api/shares").json()["data"]}, {other, builtin})
        self.assertEqual(client.post(f"/api/shares/{builtin}/revoke").status_code, 200)


class CardButtonTests(_DbCase):
    URL = f"{BASE_URL}/api/share/{'a' * 32}"

    def _tags(self, payload: dict) -> list[str]:
        return [element["tag"] for element in payload["card"]["body"]["elements"]]

    def test_short_message_has_one_button_under_the_metadata(self) -> None:
        payloads = _build_payloads("t", "正文", None, "schedule", metadata=[("任务", "x")], share_url=self.URL)
        self.assertEqual(len(payloads), 1)
        self.assertEqual(self._tags(payloads[0]), ["markdown", "button", "hr", "markdown", "hr", "markdown"])
        button = payloads[0]["card"]["body"]["elements"][1]
        self.assertEqual(button["element_id"], "share_top")
        self.assertEqual(button["text"], {"tag": "plain_text", "content": "📱 手机查看完整版"})
        self.assertEqual(button["width"], "fill")
        behavior = button["behaviors"][0]
        self.assertEqual(behavior["type"], "open_url")
        for key in ("default_url", "pc_url", "ios_url", "android_url"):
            self.assertEqual(behavior[key], self.URL)

    def test_long_message_has_buttons_on_first_and_last_card_only(self) -> None:
        content = "\n\n".join(f"第 {i} 段。" + "内容" * 800 for i in range(24))
        payloads = _build_payloads("t", content, None, "schedule", metadata=[("任务", "x")], share_url=self.URL)
        self.assertGreater(len(payloads), 2)
        self.assertIn("button", self._tags(payloads[0]))
        for middle in payloads[1:-1]:
            self.assertNotIn("button", self._tags(middle))
        last = payloads[-1]["card"]["body"]["elements"]
        self.assertEqual([element["tag"] for element in last[-3:]], ["hr", "button", "markdown"])
        self.assertEqual(last[-2]["element_id"], "share_bottom")

    def test_without_share_url_the_card_is_unchanged(self) -> None:
        payload = _build_payloads("t", "正文", None, "schedule", metadata=[("任务", "x")])[0]
        self.assertEqual(self._tags(payload), ["markdown", "hr", "markdown", "hr", "markdown"])

    def _channel(self, session: Session) -> None:
        session.add(
            NotifyChannel(
                channel_key="feishu-daily",
                name="任务群",
                purpose="daily",
                webhook_url_cipher=encrypt_value("https://open.feishu.cn/open-apis/bot/v2/hook/x"),
                status="enabled",
            )
        )
        session.commit()

    def _send(self, session: Session, post_results: list, *, share: ShareSpec | None) -> tuple[Notification, list]:
        calls: list = []

        def fake_post(url, payload, attempts=3):
            calls.append(payload)
            return post_results[len(calls) - 1]

        feishu._rate_history.clear()
        with patch.object(feishu, "_post_with_retry", side_effect=fake_post), patch.object(feishu, "_log"):
            notification = send_feishu(session, "feishu-daily", "早盘 | 2026-11-04", "正文", report_id="rhythm-1", share=share)
        return notification, calls

    def test_push_stores_the_same_text_as_the_card(self) -> None:
        with self.Session() as session:
            self._channel(session)
            notification, calls = self._send(
                session,
                [(True, 200, "")],
                share=ShareSpec(source_type="agent_report", source_ref="rhythm-1", owner_user_id=7),
            )
            self.assertEqual(notification.status, "sent")
            row = session.scalar(select(ReportShare))
            self.assertEqual(row.content_md, "正文")
            self.assertEqual(row.title, "早盘 | 2026-11-04")
        button = next(e for e in calls[0]["card"]["body"]["elements"] if e["tag"] == "button")
        self.assertTrue(button["behaviors"][0]["default_url"].endswith(row.share_id))

    def test_card_rejected_by_feishu_is_resent_without_the_button(self) -> None:
        with self.Session() as session:
            self._channel(session)
            notification, calls = self._send(
                session,
                [(False, 200, "飞书返回错误 code=200621: parse card json err"), (True, 200, "")],
                share=ShareSpec(source_type="agent_report", source_ref="rhythm-1"),
            )
            self.assertEqual(notification.status, "sent")
        self.assertEqual(len(calls), 2)
        self.assertIn("button", self._tags(calls[0]))
        self.assertNotIn("button", self._tags(calls[1]))

    def test_network_failure_is_not_retried_without_the_button(self) -> None:
        with self.Session() as session:
            self._channel(session)
            notification, calls = self._send(
                session,
                [(False, None, "网络错误: timeout")],
                share=ShareSpec(source_type="agent_report", source_ref="rhythm-1"),
            )
            self.assertEqual(notification.status, "failed")
        self.assertEqual(len(calls), 1)

    def test_share_failure_never_blocks_the_push(self) -> None:
        with self.Session() as session:
            self._channel(session)
            with patch.object(feishu, "create_share_link", side_effect=RuntimeError("boom")):
                notification, calls = self._send(
                    session,
                    [(True, 200, "")],
                    share=ShareSpec(source_type="agent_report"),
                )
            self.assertEqual(notification.status, "sent")
        self.assertNotIn("button", self._tags(calls[0]))

    def test_unconfigured_base_url_keeps_push_unchanged(self) -> None:
        with patch.object(share_service, "settings", _settings(public_base_url="")):
            with self.Session() as session:
                self._channel(session)
                notification, calls = self._send(
                    session,
                    [(True, 200, "")],
                    share=ShareSpec(source_type="agent_report", source_ref="rhythm-1"),
                )
                self.assertEqual(notification.status, "sent")
                self.assertEqual(session.scalars(select(ReportShare)).all(), [])
        self.assertNotIn("button", self._tags(calls[0]))

    def test_channel_test_sends_a_button_and_a_one_day_sample(self) -> None:
        with self.Session() as session:
            self._channel(session)
            calls: list = []

            def fake_post(url, payload, attempts=3):
                calls.append(payload)
                return True, 200, ""

            with patch.object(feishu, "_post_with_retry", side_effect=fake_post), patch.object(feishu, "_log"):
                result = feishu.test_channel(session, channel_key="feishu-daily")
            self.assertTrue(result["ok"])
            row = session.scalar(select(ReportShare))
            self.assertEqual(row.source_type, "channel_test")
            self.assertIn("内容全部虚构", row.content_md)
            remaining = row.expires_at - datetime.now(timezone.utc).replace(tzinfo=None)
            self.assertLess(remaining, timedelta(days=1, minutes=1))
        self.assertIn("button", self._tags(calls[0]))


class ScheduledPushTests(unittest.TestCase):
    def test_topnews_brief_carries_a_share_without_reusing_old_links(self) -> None:
        from backend.app.scheduler import rhythm_scheduler

        handler = SimpleNamespace(run_topnews_brief=lambda period: "简报正文")
        session = MagicMock()
        session.__enter__.return_value = "db-session"
        session.__exit__.return_value = False
        with (
            patch.dict(sys.modules, {"backend.app.topnews.handler": handler}),
            patch.object(rhythm_scheduler, "SessionLocal", return_value=session),
            patch.object(rhythm_scheduler, "send_feishu") as push,
        ):
            rhythm_scheduler._run_topnews_brief(
                {"job_key": "topnews-morning", "name": "早盘舆情简报", "owner_user_id": 7},
                datetime(2026, 11, 4, 7, 30),
            )
        share = push.call_args.kwargs["share"]
        self.assertEqual((share.source_type, share.source_ref, share.owner_user_id), ("topnews_brief", None, 7))


class SqlSandboxTests(unittest.TestCase):
    def test_agents_cannot_read_share_ids(self) -> None:
        from backend.app.agents.sql_sandbox import SqlRejected, prepare_query

        with self.assertRaises(SqlRejected) as caught:
            prepare_query("SELECT share_id FROM report_shares", owner_user_id=1, max_rows=10)
        self.assertIn("不在可查询范围", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
