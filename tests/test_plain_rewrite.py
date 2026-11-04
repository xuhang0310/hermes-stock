from __future__ import annotations

import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from backend.app.ai import plain_rewrite
from backend.app.ai.plain_rewrite import PlainRewrite, check_facts_kept, rewrite_plain
from backend.app.core.database import Base
from backend.app.core.models import AgentProfile, AgentReport, DigitalHumanProfile, User
from backend.app.scheduler import rhythm_scheduler

# 摘自 2026-10-27 午间定时任务的真实输出：复述规矩、反复免责、底稿用语、过程汇报都在。
ORIGINAL = """今天下午先防守，暂不新增买入。普莱柯的业绩压力、万东医疗的反弹失守，是最值得先盯的风险。

本次是 2026 年 10 月 27 日午间研究的延迟更新：实仓标的已补到 13:05 分钟报价，不冒充 13:00 前已完成的方案。
19 条非样例持仓全部覆盖；但今日可卖量、上午成交及挂单、原调整剩余额度未核实，核实前第一批暂按 0 股处理。
账户资金分别核算；未成交卖出款不算现金，也不跨账户挪用预算。约 25% 只作为研究假设，不构成新授权。

| 持仓 | 旧数量／成本价 | 重估浮亏 | 第一批待核实测算 |
|---|---|---|---|
| 海象新材 003011 | 8,800股／23.7234元 | 57,670元／27.62% | 800股×17.19元；13,752元 |
| 万东医疗 600055 | 16,000股／11.7096元 | 19,194元／10.24% | 1,600股×10.49元；16,784元 |
| 普莱柯 603566 | 17,600股／10.9544元 | 26,829元／13.92% | 1,700股×9.37元；15,929元 |
| 澜起科技 688008 | 200股／275.6380元 | 21,056元／38.19% | 0股／0元 |

澜起旧数量只有 200 股，按约 25% 假设是 50 股，低于科创板通常的单笔最低卖出申报数量。不能为了凑够 200 股而扩大调整比例，所以安排 0 股。

普莱柯的跌势有业绩压力支撑，不宜只按超跌反弹看。2025 年收入 10.90 亿元、归母净利 1.85 亿元；2026 年前三季度收入约 7.38 亿元，同比下降约10.3%，经营现金流约 1.47 亿元，低于去年同期约 2.78 亿元。9.88 元已从原支撑变成反抽压力，不能继续写成“守住原箱底”。

万东不能把 11.26 元突破当成已经成立，应先看能否收回 10.64、10.83 元。这些是研究判断的更新，不是取消、修改了任何真实委托。

飞书推送未完成：当前渠道没有可用的发送入口，未取得送达回执，本轮不标记为已推送。

以上是研究参考，不构成投资建议。"""

EDITED = """今天下午先守住，不加仓。最该盯普莱柯和万东医疗：一个业绩在变差，一个反弹没站稳。

下面的价格都是 10 月 27 日 13:05 的。

**跌破这些价位，就先卖一部分**

| 股票 | 现在的情况 | 跌破就先卖 | 要注意 |
|---|---|---|---|
| 海象新材 003011 | 8,800股，成本 23.7234元，亏 57,670元（27.62%） | 参考 17.19元卖 800股，约 13,752元 | 先看能不能收回 |
| 万东医疗 600055 | 16,000股，成本 11.7096元，亏 19,194元（10.24%） | 参考 10.49元卖 1,600股，约 16,784元 | 11.26 元的突破还不算数，先看 10.64、10.83 元 |
| 普莱柯 603566 | 17,600股，成本 10.9544元，亏 26,829元（13.92%） | 参考 9.37元卖 1,700股，约 15,929元 | 9.88 元从支撑变成了压力 |
| 澜起科技 688008 | 200股，成本 275.6380元，亏 21,056元（38.19%） | 暂不卖，0股 | 只有 200 股，按比例算出的 50 股不够科创板一次最少卖的数量 |

**普莱柯是真的业绩在变差，不只是跌多了**
今年前三季度收入约 7.38 亿元，比去年同期少了约 10.3%；经营赚到的现金约 1.47 亿元，去年同期是 2.78 亿元。2025 年全年收入 10.90 亿元，利润 1.85 亿元。

**还没核实的**
今天每个账户能卖多少、上午有没有成交、手里有多少现金，都还没确认。卖出股数按每只大约 25% 的比例上限算，只是参考，确认能卖再动。

以上是研究参考，不构成投资建议。"""


class FactGuardTests(unittest.TestCase):
    def test_plain_edit_that_keeps_every_fact_passes(self) -> None:
        self.assertGreaterEqual(len(ORIGINAL), plain_rewrite.MIN_CHARS)
        self.assertEqual(check_facts_kept(ORIGINAL, EDITED), "")

    def test_rounded_or_invented_numbers_are_rejected(self) -> None:
        rounded = EDITED.replace("亏 57,670元", "亏约 5.8 万元")
        self.assertIn("原文没有的数字 5.8", check_facts_kept(ORIGINAL, rounded))
        invented = EDITED.replace("先看能不能收回", "先看能不能收回 17.20 元")
        self.assertIn("17.2", check_facts_kept(ORIGINAL, invented))

    def test_dropping_a_stock_or_most_of_the_text_is_rejected(self) -> None:
        without_lanqi = "\n".join(line for line in EDITED.splitlines() if "澜起" not in line)
        self.assertIn("688008", check_facts_kept(ORIGINAL, without_lanqi))
        self.assertIn("删得太多", check_facts_kept(ORIGINAL, EDITED[:200]))
        self.assertEqual(check_facts_kept(ORIGINAL, "  "), "改稿为空")

    def test_separators_trailing_zeros_and_clock_times_are_not_changes(self) -> None:
        self.assertEqual(
            check_facts_kept("13:05 持有 8,800股，收入 10.90 亿元，代码 003011。", "持有 8800 股（003011），收入 10.9 亿元。"),
            "",
        )


class RewritePlainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.calls: list[list[dict]] = []
        self.response: dict | Exception = {
            "choices": [{"message": {"content": f"```markdown\n{EDITED}\n```"}, "finish_reason": "stop"}]
        }
        for name, value in (
            ("active_llm_config", lambda: {"model": "editor"}),
            ("reasoning_params", lambda config: {}),
            ("chat_completion", self._fake_completion),
        ):
            patcher = patch.object(plain_rewrite, name, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _fake_completion(self, messages, llm_config, **kwargs):
        self.calls.append(messages)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    def test_a_faithful_edit_replaces_the_report(self) -> None:
        result = rewrite_plain(ORIGINAL)
        self.assertEqual(result, PlainRewrite(EDITED, True))
        system, user = self.calls[0]
        self.assertIn("不跨账户挪用", system["content"])
        self.assertIn("数字原样照抄", system["content"])
        self.assertEqual(user["content"], ORIGINAL)

    def test_truncated_failed_or_unfaithful_edits_keep_the_original(self) -> None:
        self.response = {"choices": [{"message": {"content": EDITED[:900]}, "finish_reason": "length"}]}
        self.assertEqual(rewrite_plain(ORIGINAL), PlainRewrite(ORIGINAL, False, "改稿被长度截断"))

        self.response = RuntimeError("LLM 接口返回 503")
        result = rewrite_plain(ORIGINAL)
        self.assertEqual((result.text, result.applied), (ORIGINAL, False))
        self.assertIn("503", result.reason)

        self.response = {"choices": [{"message": {"content": EDITED.replace("15,929元", "1.6 万元")}}]}
        result = rewrite_plain(ORIGINAL)
        self.assertEqual((result.text, result.applied), (ORIGINAL, False))
        self.assertIn("1.6", result.reason)

    def test_short_reports_and_the_off_switch_skip_the_model(self) -> None:
        self.assertEqual(rewrite_plain("今天无异动。"), PlainRewrite("今天无异动。", False))
        with patch.object(plain_rewrite, "settings", SimpleNamespace(plain_rewrite_enabled=False)):
            self.assertEqual(rewrite_plain(ORIGINAL), PlainRewrite(ORIGINAL, False))
        self.assertEqual(self.calls, [])


class ScheduledReportRewriteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.SessionFactory() as db:
            owner = User(username="owner", display_name="Owner", password_hash="x", role="researcher")
            db.add_all(
                [
                    owner,
                    AgentProfile(agent_name="pm", display_name="投研总监", enabled=True),
                    DigitalHumanProfile(
                        code="stock-pm",
                        name="投研总监",
                        avatar_tone="professional",
                        target_roles="*",
                        description="测试",
                        system_prompt="测试",
                        quick_prompts_json="[]",
                        status="enabled",
                    ),
                ]
            )
            db.commit()
            self.snap = {
                "owner_user_id": owner.id,
                "job_key": "midday-plan-1230",
                "name": "午间持仓策略",
                "prompt_template": "生成午间策略，完成后推送飞书",
                "digital_human_code": "stock-pm",
                "push_channel_key": "feishu-daily",
            }

    def _run(self, rewrite: PlainRewrite):
        questions: list[str] = []
        logs: list[tuple] = []

        def fake_agent(snap, question, job_run_id):
            questions.append(question)
            return ORIGINAL, "model"

        with patch.object(rhythm_scheduler, "SessionLocal", self.SessionFactory), patch.object(
            rhythm_scheduler, "_run_scheduled_agent", side_effect=fake_agent
        ), patch.object(rhythm_scheduler, "rewrite_plain", return_value=rewrite) as editor, patch.object(
            rhythm_scheduler, "send_feishu", return_value=SimpleNamespace(id=7, status="sent", error=None)
        ) as push, patch.object(rhythm_scheduler, "_log", side_effect=lambda *args: logs.append(args)):
            result = rhythm_scheduler._run_agent_report(self.snap, datetime(2026, 10, 27, 12, 30), "run-1")
        editor.assert_called_once_with(ORIGINAL)
        with self.SessionFactory() as db:
            report = db.query(AgentReport).one()
        return result, report, push, questions[0], logs

    def test_the_plain_version_is_stored_and_pushed(self) -> None:
        result, report, push, question, logs = self._run(PlainRewrite(EDITED, True))

        self.assertEqual(result, ("rhythm-run-1", 7, EDITED, None))
        self.assertEqual(report.content, EDITED)
        self.assertEqual(push.call_args.args[3], EDITED)
        share = push.call_args.kwargs["share"]
        self.assertEqual((share.source_type, share.source_ref), ("agent_report", "rhythm-run-1"))
        self.assertEqual(share.meta["generated_at"], "2026-10-27 12:30:00")
        self.assertEqual(logs[0][:2], ("INFO", "定时报告已改成白话"))
        self.assertIn("由系统自动推送飞书，你不用自己推送", question)
        self.assertIn("不要在报告里逐条复述", question)

    def test_a_rejected_edit_pushes_the_original_and_says_why(self) -> None:
        result, report, push, _, logs = self._run(PlainRewrite(ORIGINAL, False, "改稿出现原文没有的数字 5.8"))

        self.assertEqual(report.content, ORIGINAL)
        self.assertEqual(push.call_args.args[3], ORIGINAL)
        self.assertEqual(logs, [("WARNING", "定时报告沿用原文：改稿出现原文没有的数字 5.8", {"job_key": "midday-plan-1230"})])


if __name__ == "__main__":
    unittest.main()
