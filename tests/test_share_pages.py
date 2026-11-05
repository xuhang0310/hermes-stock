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
from backend.app.data.share_render import lead_brief, render_markdown, render_notice_page, render_share_page
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
            "| #2 甲 601668.SH；4.73元 | 低估值 | **不参与**：0股，财报压力较大。 | 回款 |\n"
            "| #1 乙 601166.SH；19.93元 | 低波动 | **准备买**：2000股，按条件。 | 利润 |\n"
        )
        rows = re.findall(r"<tr>.*?</tr>", body, flags=re.S)[1:]
        self.assertNotIn('class="tag', rows[0])
        self.assertIn('<span class="tag act-buy">买入</span>', rows[1])
        self.assertIn('<span class="cd">#1</span><span class="nm">乙</span><span class="cd fig">601166</span>', rows[1])

    def test_six_digits_in_brackets_are_a_code_not_a_price(self) -> None:
        body, _ = render_markdown(
            "| 股票 | 现价 | 涨跌幅 | 接下来怎么做 |\n|---|---|---|---|\n"
            "| 民德电子（300656） | 19.89 | +0.71% | 反弹卖价19.92，现价还差0.03；止损价19.29。 |\n"
        )
        self.assertNotIn("300656</span></span>", body.replace('<span class="cd fig">300656</span>', ""))
        self.assertIn('<span class="cd fig">300656</span>', body)
        self.assertIn('现价 <span class="fig">19.89</span>', body)
        self.assertIn('<span class="chg up fig">+0.71%</span>', body)

    def test_watch_table_keeps_current_price_small_and_shows_the_plan_price_big(self) -> None:
        body, _ = render_markdown(
            "| 股票 | 现价 | 涨跌幅 | 接下来怎么做 |\n|---|---|---|---|\n"
            "| 民德电子（300656） | 19.89 | +0.71% | 反弹卖价19.92，现价还差0.03；止损价19.29。 |\n"
            "| 万东医疗（600055） | 10.49 | -0.57% | 午盘没设交易价，持有、不加仓。 |\n"
            "| 普莱柯（603566） | 9.32 | 0.00% | 午盘没设交易价，持有、不加仓。 |\n"
        )
        rows = re.findall(r"<tr>.*?</tr>", body, flags=re.S)[1:]
        self.assertIn('<span class="fig big">19.92</span>', rows[0], "金色大字只给计划价")
        self.assertNotIn('<span class="fig big">19.89</span>', rows[0])
        self.assertIn('<span class="tag act-sell">卖出</span>', rows[0])
        self.assertIn('止损价<span class="fig key">19.29</span>', rows[0])
        self.assertNotIn("big", rows[1], "午盘没设交易价，不能编一个计划价")
        self.assertIn('<span class="tag act-none">拿着</span>', rows[1])
        self.assertIn('<span class="chg down fig">-0.57%</span>', rows[1])
        self.assertIn('<span class="chg flat fig">0.00%</span>', rows[2])

    def test_previous_close_is_never_called_the_current_price(self) -> None:
        body, _ = render_markdown(
            "| 股票（账户） | 昨收 | 早盘计划怎么执行 |\n|---|---|---|\n"
            "| ★澜起科技（688008，方正） | 150.01 | 原止损退出计划中的200股，按早盘安排先撤旧单。 |\n"
            "| 海象新材（003011） | 16.45 | 反弹卖16.95／止损15.98。 |\n"
            "| 万东医疗（600055） | 10.55 | 早盘没设交易价，继续持有，不加仓。 |\n"
        )
        rows = re.findall(r"<tr>.*?</tr>", body, flags=re.S)[1:]
        self.assertIn('<table class="plan">', body)
        self.assertNotIn("现价", body)
        self.assertIn('昨收 <span class="fig">150.01</span>', rows[0])
        self.assertIn('<span class="tag act-stop">止损</span>', rows[0])
        self.assertIn('<span class="tag act-sell">卖出</span>', rows[1], "是卖单，止损只是备用出口，不能标成止损")
        self.assertIn('<span class="fig big">16.95</span>', rows[1])
        self.assertNotIn('<span class="fig big">15.98</span>', rows[1], "止损价不是放大的那个计划价")
        self.assertIn('<span class="fig key">15.98</span>', rows[1])
        self.assertIn('<span class="tag act-none">拿着</span>', rows[2])

    def test_cost_price_column_stays_a_labelled_line(self) -> None:
        body, _ = render_markdown("| 股票 | 成本价 | 怎么做 |\n|---|---|---|\n| 甲（600000） | 12.34 | 拿着 |\n")
        self.assertIn('data-label="成本价"', body)
        self.assertNotIn("现价", body)
        self.assertNotIn("big", body)

    def test_stock_list_becomes_ledger_rows(self) -> None:
        body, _ = render_markdown(
            "- **★上海谊众（688091）｜35.64｜+2.44%**  \n"
            "  已达到35.40减仓条件。原计划为方正、中信各1,400股。\n\n"
            "- **★澜起科技（688008，方正）｜153.29｜+2.19%**  \n"
            "  已高于151.00退出限价。按午盘计划先撤旧单。\n"
        )
        self.assertIn('<ul class="stocks">', body)
        self.assertIn('<span class="star">★</span><span class="nm">上海谊众</span><span class="cd fig">688091</span>', body)
        self.assertIn('<span class="cd">方正</span>', body)
        self.assertIn('现价 <span class="fig">35.64</span> <span class="chg up fig">+2.44%</span>', body)
        self.assertIn('<span class="fig big">35.40</span>', body)
        self.assertIn('<span class="fig big">151.00</span>', body)
        self.assertIn('<span class="row-tag"><span class="tag act-sell">卖出</span></span>', body)
        self.assertIn('<span class="sl-note">已达到', body)
        self.assertIn("原计划为方正、中信各1,400股。", body)
        self.assertIn("按午盘计划先撤旧单。", body)

    def test_star_written_outside_the_bold_still_counts(self) -> None:
        body, _ = render_markdown(
            "- ★ **澜起科技（688008，方正）｜150.98｜+0.65%**  \n  继续盯。\n"
            "- **海象新材（003011）｜16.12｜-2.01%**  \n  继续盯。\n"
        )
        self.assertIn('<ul class="stocks">', body)
        self.assertEqual(body.count('<span class="star">★</span>'), 1)
        self.assertNotIn("<li>★", body)
        self.assertIn('<span class="chg down fig">-2.01%</span>', body)

    def test_pipe_written_stock_list_becomes_ledger_rows(self) -> None:
        body, _ = render_markdown(
            "- ★同力天启｜21.78｜+0.97%｜已到21.68反弹卖价；若未完成，三账户各按原2,400股的剩余量处理；止损20.59。\n"
            "- ★澜起科技（方正）｜164.72｜+9.81%｜较13:43明显拉升，仍高于151.00退出限价；不因上涨取消计划。\n"
            "- 万东医疗｜10.59｜-0.38%｜午盘未设交易价，持有、不加仓。\n"
        )
        self.assertIn('<ul class="stocks">', body)
        items = re.findall(r"<li>.*?</li>", body, flags=re.S)
        self.assertEqual(len(items), 3)
        self.assertIn('<span class="star">★</span><span class="nm">同力天启</span>', items[0])
        self.assertIn('现价 <span class="fig">21.78</span> <span class="chg up fig">+0.97%</span>', items[0])
        self.assertIn('<span class="fig big">21.68</span>', items[0])
        self.assertIn('<span class="row-tag"><span class="tag act-sell">卖出</span></span>', items[0])
        self.assertIn('<span class="sl-note">已到', items[0])
        self.assertIn('止损<span class="fig key">20.59</span>', items[0])
        self.assertNotIn("｜", items[0])
        self.assertIn('<span class="cd">方正</span>', items[1])
        self.assertIn('<span class="fig big">151.00</span>', items[1], "开头的 13:43 不是价格")
        self.assertIn('<span class="tag act-sell">卖出</span>', items[1], "开头只是描述，往后找到退出")
        self.assertIn('<span class="sl-note wide"><span class="tag act-none">拿着</span>午盘未设', items[2])
        self.assertNotIn("big", items[2])
        self.assertIn('<span class="chg down fig">-0.38%</span>', items[2])

    def test_process_notes_never_reach_the_page(self) -> None:
        body, _ = render_markdown(
            "结论。\n\n正文。\n\n"
            "已保存至 [notes/reviews/2026-11-04/intraday-14:05.md](notes/reviews/2026-11-04/intraday-14:05.md)，含本轮价格及复盘依据。\n\n"
            "<details>\n<summary>留档信息</summary>\n\n版本1｜5,117字节｜文件验证通过  \nSHA-256：`585fb27f`\n\n</details>\n\n"
            "以上是研究参考，不构成投资建议。\n"
        )
        for leaked in ("notes/reviews", "details", "留档信息", "SHA-256", "已保存至"):
            self.assertNotIn(leaked, body)
        self.assertIn("不构成投资建议", body)
        other, _ = render_markdown("结论。\n\n已保存：`notes/reviews/2026-11-04/intraday-13:35.md`（v1，4,965字节，验证通过）。\nSHA-256：`ea795a93`\n\n后文。")
        for leaked in ("notes/reviews", "SHA-256", "已保存"):
            self.assertNotIn(leaked, other)
        self.assertIn("后文。", other)

    def test_combined_price_and_change_column(self) -> None:
        body, _ = render_markdown(
            "| 股票 | 现价 / 涨跌幅 | 反弹卖价 | 止损价 |\n|---|---|---|---|\n"
            "| 同力天启（605286） | 21.11 / -2.13% | 22.18 | 20.59 |\n"
        )
        self.assertIn('<table class="plan">', body)
        self.assertIn('现价 <span class="fig">21.11</span> <span class="chg down fig">-2.13%</span>', body)
        self.assertIn('<span class="lb">反弹卖价</span><span class="fig big">22.18</span>', body)
        self.assertIn('data-label="止损价"', body)

    def test_watch_table_without_any_plan_column(self) -> None:
        body, _ = render_markdown("| 股票 | 现价 | 涨跌幅 |\n|---|---|---|\n| 万东医疗（600055） | 10.39 | -1.52% |\n")
        self.assertIn('<table class="plan">', body)
        self.assertIn('<span class="cd fig">600055</span>', body)
        self.assertNotIn("big", body)

    def test_unparsable_price_cell_is_kept_not_dropped(self) -> None:
        body, _ = render_markdown("| 股票 | 现价 | 怎么做 |\n|---|---|---|\n| 甲（600000） | 盘中停牌 | 拿着 |\n")
        self.assertIn("盘中停牌", body)
        self.assertIn('data-label="现价"', body)

    def test_cost_to_close_header_is_not_a_current_price(self) -> None:
        body, _ = render_markdown("| 股票 | 合计持股 | 加权成本→收盘价 | 浮亏及亏幅 |\n|---|---|---|---|\n| 甲（600000） | 100 | 38.24→21.92元 | -1元 |\n")
        self.assertNotIn('<table class="plan">', body)

    def test_ordinary_lists_are_left_alone(self) -> None:
        for source in (
            "- **重点**：今天不追高\n- 第二条\n",
            "- 第一条\n- 第二条\n",
            "- **★甲（688091）**\n- 普通一条\n",
            "- 资金｜流入较多，继续观察\n- 情绪｜偏弱\n",
        ):
            with self.subTest(source=source):
                self.assertNotIn("stocks", render_markdown(source)[0])

    def test_lead_splits_at_the_first_sentence(self) -> None:
        body, _ = render_markdown("上海谊众已达到35.40元减仓条件，澜起也高于151.00元。你先盯这两只，再处理没完成的部分。\n\n后面。")
        self.assertIn('<p class="lead lead-big">上海谊众已达到35.40元减仓条件，澜起也高于151.00元。</p>', body)
        self.assertIn('<p class="lead-end">你先盯这两只，再处理没完成的部分。</p>', body)

    def test_whole_bold_lead_loses_the_bold(self) -> None:
        body, _ = render_markdown("**今天减仓。先处理账户3。**\n\n后面。")
        self.assertIn('<p class="lead lead-big">今天减仓。</p>', body)
        self.assertNotIn("<strong>", body)

    def test_data_notes_and_disclaimers_become_fine_print(self) -> None:
        body, _ = render_markdown(
            "结论。\n\n"
            "**2026-11-04 13:05｜行情更新于13:05:09，价格单位：元。以下沿用午盘调整后的计划。**\n\n"
            "**如果触及止损价，先撤反弹卖单。**\n\n"
            "以上是研究参考，不构成投资建议。\n"
        )
        self.assertIn('<p class="fine">2026-11-04 13:05｜行情更新于', body)
        self.assertIn("<strong>如果触及止损价，先撤反弹卖单。</strong>", body, "操作要求还是粗体")
        self.assertIn('<p class="fine">以上是研究参考，不构成投资建议。</p>', body)

    def test_long_paragraphs_with_instructions_stay_normal_text(self) -> None:
        text = "下面的最新价均为2026年11月3日收盘价，新闻和公告更新至今天盘前约08:43。所有旧仓今天加仓0股；表里写拿着的股票，今天不挂单，其他股票按原定价位处理。"
        self.assertNotIn('class="fine"', render_markdown(f"结论。\n\n{text}")[0])

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

    def _send(
        self,
        session: Session,
        post_results: list,
        *,
        share: ShareSpec | None,
        content: str = "正文",
        card_brief: bool | str = False,
    ) -> tuple[Notification, list]:
        calls: list = []

        def fake_post(url, payload, attempts=3):
            calls.append(payload)
            return post_results[len(calls) - 1]

        feishu._rate_history.clear()
        with patch.object(feishu, "_post_with_retry", side_effect=fake_post), patch.object(feishu, "_log"):
            notification = send_feishu(
                session,
                "feishu-daily",
                "早盘 | 2026-11-04",
                content,
                report_id="rhythm-1",
                card_metadata=[("任务", "早盘")],
                share=share,
                card_brief=card_brief,
            )
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


class BriefCardTests(_DbCase):
    REPORT = "**今天不加仓，只减海象新材。**  \n最先盯 16.55 元卖单。\n\n" + "\n\n".join(
        f"第 {i} 段。" + "内容" * 800 for i in range(24)
    )
    MANY_OK = [(True, 200, "")] * 40

    def _send(self, session: Session, post_results: list, *, content: str = REPORT, card_brief: bool | str = True):
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
        calls: list = []

        def fake_post(url, payload, attempts=3):
            calls.append(payload)
            return post_results[len(calls) - 1]

        feishu._rate_history.clear()
        with patch.object(feishu, "_post_with_retry", side_effect=fake_post), patch.object(feishu, "_log"):
            notification = send_feishu(
                session,
                "feishu-daily",
                "早盘 | 2026-11-04",
                content,
                report_id="rhythm-1",
                card_metadata=[("任务", "早盘")],
                share=ShareSpec(source_type="agent_report", source_ref="rhythm-1", owner_user_id=7),
                card_brief=card_brief,
            )
        return notification, calls

    @staticmethod
    def _elements(payload: dict) -> list[dict]:
        return payload["card"]["body"]["elements"]

    def _text(self, payloads: list[dict]) -> str:
        return "\n".join(e.get("content", "") for payload in payloads for e in self._elements(payload))

    def _has_button(self, payloads: list[dict]) -> bool:
        return any(e["tag"] == "button" for payload in payloads for e in self._elements(payload))

    def test_a_long_report_becomes_one_card_with_the_sentence_and_the_button(self) -> None:
        with self.Session() as session:
            notification, calls = self._send(session, [(True, 200, "")])
            self.assertEqual(notification.status, "sent")
            self.assertEqual(notification.content, self.REPORT)
            row = session.scalar(select(ReportShare))
            self.assertEqual(row.content_md, self.REPORT)
        self.assertEqual(len(calls), 1)
        elements = self._elements(calls[0])
        self.assertEqual([e["tag"] for e in elements], ["markdown", "button", "markdown"])
        self.assertEqual(elements[0]["content"], "今天不加仓，只减海象新材。")
        self.assertTrue(elements[1]["behaviors"][0]["default_url"].endswith(row.share_id))
        self.assertIn("不构成投资建议", elements[2]["content"])
        self.assertEqual(calls[0]["card"]["header"]["title"]["content"], "早盘 | 2026-11-04")
        self.assertNotIn("任务", self._text(calls))

    def test_a_given_sentence_is_used_as_it_is(self) -> None:
        with self.Session() as session:
            _, calls = self._send(session, [(True, 200, "")], card_brief="市场定性：偏多。")
        self.assertEqual(self._elements(calls[0])[0]["content"], "市场定性：偏多。")

    def test_a_report_with_no_paragraph_still_points_to_the_page(self) -> None:
        with self.Session() as session:
            _, calls = self._send(session, [(True, 200, "")], content="| 名称 | 价 |\n|---|---|\n| 甲 | 1 |")
        self.assertEqual(self._elements(calls[0])[0]["content"], feishu.BRIEF_FALLBACK_TEXT)
        self.assertTrue(self._has_button(calls))

    def test_no_link_means_the_full_text_is_sent(self) -> None:
        with patch.object(share_service, "settings", _settings(public_base_url="")), self.Session() as session:
            notification, calls = self._send(session, self.MANY_OK)
            self.assertEqual(notification.status, "sent")
        self.assertGreater(len(calls), 1)
        self.assertFalse(self._has_button(calls))
        self.assertIn("第 23 段", self._text(calls))

    def test_a_rejected_brief_card_is_resent_in_full_without_the_button(self) -> None:
        with self.Session() as session:
            notification, calls = self._send(session, [(False, 200, "飞书返回错误 code=200621")] + self.MANY_OK)
            self.assertEqual(notification.status, "sent")
        self.assertTrue(self._has_button(calls[:1]))
        self.assertNotIn("第 23 段", self._text(calls[:1]))
        self.assertFalse(self._has_button(calls[1:]))
        self.assertIn("第 23 段", self._text(calls[1:]))

    def test_the_switch_restores_full_text_cards(self) -> None:
        with patch.object(feishu, "settings", _settings(feishu_brief_card=False)), self.Session() as session:
            _, calls = self._send(session, self.MANY_OK)
        self.assertGreater(len(calls), 1)
        self.assertTrue(self._has_button(calls))
        self.assertIn("第 23 段", self._text(calls))

    def test_a_broken_extraction_falls_back_to_the_full_text(self) -> None:
        with patch.object(feishu, "lead_brief", side_effect=RuntimeError("boom")), self.Session() as session:
            notification, calls = self._send(session, self.MANY_OK)
            self.assertEqual(notification.status, "sent")
        self.assertGreater(len(calls), 1)
        self.assertIn("第 23 段", self._text(calls))

    def test_without_card_brief_pushes_stay_as_they_were(self) -> None:
        with self.Session() as session:
            _, calls = self._send(session, self.MANY_OK, card_brief=False)
        self.assertGreater(len(calls), 1)
        self.assertIn("第 23 段", self._text(calls))

    def test_the_card_is_signed_when_the_channel_has_a_secret(self) -> None:
        payload = feishu._brief_payload(
            "t", "一句话", "secret", "daily", footer=None, template=None, share_url="https://x/a", share_text="看"
        )
        self.assertIn("sign", payload)
        self.assertEqual(payload["msg_type"], "interactive")


class LeadBriefTests(unittest.TestCase):
    def test_a_complete_first_line_stands_alone(self) -> None:
        self.assertEqual(
            lead_brief("**今天不加仓，只减海象新材、同力天启、民德电子。**  \n最先盯**海象新材16.55元卖单**。\n\n## 小标题"),
            "今天不加仓，只减海象新材、同力天启、民德电子。",
        )
        self.assertEqual(
            lead_brief("澜起科技继续拉升至涨12.35%，午盘退出安排不变。  \n同力天启也已到减仓价。"),
            "澜起科技继续拉升至涨12.35%，午盘退出安排不变。",
        )

    def test_bold_inside_the_line_is_kept(self) -> None:
        self.assertEqual(
            lead_brief("上午走弱，下午继续减弱势仓位，**不加仓，自选下午不买**。  \n你重点盯海象新材。"),
            "上午走弱，下午继续减弱势仓位，**不加仓，自选下午不买**。",
        )

    def test_a_line_that_is_not_a_sentence_takes_the_next_one(self) -> None:
        self.assertEqual(
            lead_brief("**2026-11-04 13:43｜暂无需要调整的新变化**\n你重点盯住民德电子：现价仍高于原定卖出价。\n行情更新于13:43:05。"),
            "**2026-11-04 13:43｜暂无需要调整的新变化**。你重点盯住民德电子：现价仍高于原定卖出价。",
        )
        self.assertEqual(lead_brief("结论：\n今天不动，等反弹再卖。"), "结论：今天不动，等反弹再卖。")

    def test_a_short_opening_sentence_keeps_the_rest_of_its_line(self) -> None:
        text = "盘中报价暂时取不到。你先看券商，确认**海象新材15.98元止损是否触发**。\n\n**价格单位：元。**"
        self.assertEqual(lead_brief(text), "盘中报价暂时取不到。你先看券商，确认**海象新材15.98元止损是否触发**。")

    def test_a_long_paragraph_is_cut_at_a_sentence(self) -> None:
        text = (
            "今天的系统选股已完成，共30只。**明天（11月5日，周四）只准备一笔有条件的新买入：京沪高铁；"
            "兴业银行已有的2,000股暂不加仓。**其余股票等待或不参与，不因为入选就全部买。"
        )
        self.assertEqual(
            lead_brief(text),
            "今天的系统选股已完成，共30只。**明天（11月5日，周四）只准备一笔有条件的新买入：京沪高铁；"
            "兴业银行已有的2,000股暂不加仓。**",
        )

    def test_a_sentence_with_no_stop_is_cut_at_a_clause(self) -> None:
        result = lead_brief("甲乙丙丁戊己庚辛壬癸，" * 12)
        self.assertTrue(result.endswith("癸…"), result)
        self.assertLessEqual(len(result), 80)

    def test_a_cut_inside_bold_still_closes_the_bold(self) -> None:
        result = lead_brief("开头**" + "甲乙丙丁戊己庚辛壬癸，" * 12 + "甲乙**后面")
        self.assertEqual(result.count("**"), 2)
        self.assertTrue(result.endswith("癸**…"), result)

    def test_a_table_first_report_skips_the_fine_print(self) -> None:
        text = "| a | b |\n|---|---|\n| 1 | 2 |\n\n行情更新于 10:35。\n\n澜起科技继续退出，其余按计划等。"
        self.assertEqual(lead_brief(text), "澜起科技继续退出，其余按计划等。")

    def test_process_notes_and_decoration_never_reach_the_card(self) -> None:
        text = "<details><summary>x</summary>y</details>\n\n已保存至 notes/reviews/a.md\n\nSHA-256：abc\n\n今天不动。"
        self.assertEqual(lead_brief(text), "今天不动。")
        self.assertEqual(lead_brief("━━━━\n今天不动，等反弹再卖，不追高。\n━━━━"), "今天不动，等反弹再卖，不追高。")

    def test_a_heading_before_the_opening_is_skipped(self) -> None:
        self.assertEqual(lead_brief("## 结论\n\n今天不动，等反弹。"), "今天不动，等反弹。")

    def test_nothing_to_show_returns_empty(self) -> None:
        self.assertEqual(lead_brief(""), "")
        self.assertEqual(lead_brief("| a | b |\n|---|---|\n| 1 | 2 |"), "")


class ScheduledPushTests(unittest.TestCase):
    def test_topnews_card_says_the_market_verdict(self) -> None:
        from backend.app.scheduler import rhythm_scheduler
        from backend.app.topnews.report.generator import ReportGenerator

        generator = ReportGenerator()
        verdict = {
            "verdict_text": "偏多",
            "avg_sentiment": 0.12,
            "sentiment_label": "偏暖",
            "impact_grade": "中",
            "us_items": [{"direction": "up"}],
            "futures_items": [],
            "all_news": [{"sentiment": "negative"}],
        }
        report = "\n".join([generator._header("pre_market"), generator._verdict_section(verdict), "\n━━━ ⭐ 重要新闻 ━━━"])
        self.assertEqual(rhythm_scheduler._topnews_brief(report), "市场定性：偏多，美股涨多跌少，有1条利空。")
        self.assertEqual(rhythm_scheduler._topnews_brief("没有定性的文字"), "")

        handler = SimpleNamespace(run_topnews_brief=lambda period: report)
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
        self.assertEqual(push.call_args.kwargs["card_brief"], "市场定性：偏多，美股涨多跌少，有1条利空。")

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
