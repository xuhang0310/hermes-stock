"""推理强度（reasoning_effort）每模型分级配置：取值校验、协议族映射与全链路接线护栏。

行业约定（OpenAI o 系列/gpt-5、AgentScope 引擎）用分级 reasoning_effort 而非
二值思考开关；通义/百炼系只认 enable_thinking，故按服务商映射。
UI 为七档分段选择：自动/关闭/低/中/高/超高/最大。
接线任缺一环，配置页的分级选择就会静默失效，因此用静态源码断言钉住：
数据库迁移 / active_llm_config / agentscope 引擎 / legacy 引擎 / 配置 API。
"""
from __future__ import annotations

import unittest
from pathlib import Path

import pydantic

from backend.app.ai.ai_api import ModelConfigIn
from backend.app.core.llm_config import reasoning_family, reasoning_params

REPO_ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


OPENAI_LIKE = {
    "provider_name": "OpenAI compatible",
    "base_url": "https://api.example.com/v1",
    "model": "gpt-5.6-sol",
}
QWEN_BY_PROVIDER = {
    "provider_name": "dashscope",
    "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "model": "qwen3-max",
}
QWEN_BY_MODEL = {
    "provider_name": "SiliconFlow",
    "base_url": "https://api.siliconflow.cn/v1",
    "model": "qwen3.7-plus",
}


class ReasoningParamsTests(unittest.TestCase):
    def test_auto_sends_nothing(self) -> None:
        for config in ({}, {"reasoning_effort": "auto"}, {"reasoning_effort": None}):
            self.assertEqual(
                reasoning_params(config),
                {"reasoning_effort": None, "enable_thinking": None},
            )

    def test_invalid_level_falls_back_to_auto(self) -> None:
        for legacy in ("on", "minimal", "thinking"):
            self.assertEqual(
                reasoning_params({**OPENAI_LIKE, "reasoning_effort": legacy}),
                {"reasoning_effort": None, "enable_thinking": None},
            )

    def test_openai_family_passes_level_through(self) -> None:
        for level in ("low", "medium", "high", "xhigh"):
            self.assertEqual(
                reasoning_params({**OPENAI_LIKE, "reasoning_effort": level}),
                {"reasoning_effort": level, "enable_thinking": None},
            )

    def test_openai_family_off_and_max_mapping(self) -> None:
        self.assertEqual(
            reasoning_params({**OPENAI_LIKE, "reasoning_effort": "off"}),
            {"reasoning_effort": "none", "enable_thinking": None},
        )
        self.assertEqual(
            reasoning_params({**OPENAI_LIKE, "reasoning_effort": "max"}),
            {"reasoning_effort": "xhigh", "enable_thinking": None},
        )

    def test_qwen_family_maps_level_to_enable_thinking(self) -> None:
        for config in (QWEN_BY_PROVIDER, QWEN_BY_MODEL):
            for level in ("low", "medium", "high", "xhigh", "max"):
                self.assertEqual(
                    reasoning_params({**config, "reasoning_effort": level}),
                    {"reasoning_effort": None, "enable_thinking": True},
                )
            self.assertEqual(
                reasoning_params({**config, "reasoning_effort": "off"}),
                {"reasoning_effort": None, "enable_thinking": False},
            )

    def test_family_detection(self) -> None:
        self.assertEqual(reasoning_family(OPENAI_LIKE), "openai")
        self.assertEqual(reasoning_family(QWEN_BY_PROVIDER), "qwen")
        self.assertEqual(reasoning_family(QWEN_BY_MODEL), "qwen")
        self.assertEqual(
            reasoning_family({"provider_name": "百炼", "base_url": "", "model": ""}),
            "qwen",
        )
        self.assertEqual(
            reasoning_family({"provider_name": "deepseek", "base_url": "https://api.deepseek.com", "model": "deepseek-v4-pro"}),
            "openai",
        )


class ModelConfigInReasoningEffortTests(unittest.TestCase):
    def _payload(self, **overrides) -> dict:
        base = {
            "display_name": "测试模型",
            "base_url": "https://example.invalid/v1",
            "model_name": "qwen3.7-plus",
        }
        base.update(overrides)
        return base

    def test_default_is_auto(self) -> None:
        payload = ModelConfigIn(**self._payload())
        self.assertEqual(payload.reasoning_effort, "auto")

    def test_accepts_all_levels(self) -> None:
        for level in ("auto", "off", "low", "medium", "high", "xhigh", "max"):
            parsed = ModelConfigIn(**self._payload(reasoning_effort=level))
            self.assertEqual(parsed.reasoning_effort, level)

    def test_rejects_legacy_and_unknown_values(self) -> None:
        for legacy in ("on", "minimal", "thinking"):
            with self.assertRaises(pydantic.ValidationError):
                ModelConfigIn(**self._payload(reasoning_effort=legacy))

    def test_max_steps_cap_is_60(self) -> None:
        parsed = ModelConfigIn(**self._payload(max_steps=60))
        self.assertEqual(parsed.max_steps, 60)
        with self.assertRaises(pydantic.ValidationError):
            ModelConfigIn(**self._payload(max_steps=61))


class ReasoningEffortWiringGuardTests(unittest.TestCase):
    def test_database_migration_renames_legacy_column(self) -> None:
        source = _source("backend/app/core/database.py")
        self.assertIn(
            "ALTER TABLE llm_model_configs CHANGE COLUMN thinking_mode", source
        )
        self.assertIn(
            'reasoning_effort VARCHAR(16) NOT NULL DEFAULT \'auto\'', source
        )

    def test_active_llm_config_returns_level(self) -> None:
        source = _source("backend/app/core/llm_config.py")
        self.assertIn('"reasoning_effort": str(row.reasoning_effort or "auto")', source)
        self.assertIn('"reasoning_effort": "auto"', source)

    def test_agentscope_engine_sends_level_and_extra_body(self) -> None:
        source = _source("backend/app/agent_runtime/agentscope_engine.py")
        self.assertIn("reasoning = reasoning_params(llm)", source)
        self.assertIn('reasoning_effort=reasoning["reasoning_effort"]', source)
        self.assertIn('{"enable_thinking": reasoning["enable_thinking"]}', source)
        self.assertIn("extra_body=extra_body", source)

    def test_legacy_engine_passes_reasoning_params(self) -> None:
        source = _source("backend/app/agents/stock_agent.py")
        self.assertIn("**reasoning_params(llm_config)", source)

    def test_model_config_api_exposes_field_and_caps(self) -> None:
        source = _source("backend/app/ai/ai_api.py")
        self.assertIn(
            'reasoning_effort: Literal["auto", "off", "low", "medium", "high", "xhigh", "max"]',
            source,
        )
        self.assertIn('"reasoning_effort": model.reasoning_effort or "auto"', source)
        self.assertIn("max_steps: int = Field(default=6, ge=1, le=60)", source)


if __name__ == "__main__":
    unittest.main()
