from __future__ import annotations

import unittest

from backend.app.agents import stock_agent
from backend.app.agents.answer_style import ANSWER_STYLE_RULES
from backend.app.market import stock_tools
from backend.app.market.digital_human_tools import ROLE_SKILLS, digital_human_skill_prompt
from backend.app.skills.manifests import load_core_skill_manifests


class AnswerStylePromptTests(unittest.TestCase):
    def test_every_chat_mode_carries_the_plain_language_rules(self) -> None:
        prompts = {
            "research": stock_agent._system_prompt(None, None),
            "research-role": stock_agent._system_prompt("当前数字人：风控复盘官", "risk-officer"),
            "knowledge": stock_agent._knowledge_system_prompt(),
            "stock": stock_tools.stock_system_prompt_with_skills(),
            "stock-legacy": stock_tools.stock_system_prompt(),
        }
        for mode, prompt in prompts.items():
            with self.subTest(mode=mode):
                self.assertIn(ANSWER_STYLE_RULES, prompt)
                self.assertNotIn("固定包含", prompt)
                self.assertNotIn("严格执行已激活技能的专业报告结构", prompt)

    def test_role_contracts_list_content_not_fixed_headings(self) -> None:
        for code in ROLE_SKILLS:
            with self.subTest(role=code):
                prompt = digital_human_skill_prompt(code)
                self.assertIn("回答要讲到", prompt)
                self.assertIn("不是固定标题", prompt)
                self.assertNotIn("输出固定包含", prompt)

    def test_skill_catalog_does_not_prescribe_report_jargon(self) -> None:
        jargon = ("## 5. 多维决策矩阵", "Claim：", "chunk_id=", "单源，待交叉核验", "## 已确认事实", "标记 `incomplete`")
        manifests = {item.name: item for item in load_core_skill_manifests()}
        for manifest in manifests.values():
            for term in jargon:
                with self.subTest(skill=manifest.name, term=term):
                    self.assertNotIn(term, manifest.instructions)
            with self.subTest(skill=manifest.name, field="output_contract"):
                self.assertFalse({"执行摘要", "多维决策矩阵", "切块标识"} & set(manifest.output_contract))
        self.assertIn("## 回答结构", manifests["position-decision-support"].instructions)
        self.assertIn("不写“A级/C级”", manifests["event-intelligence"].instructions)


if __name__ == "__main__":
    unittest.main()
