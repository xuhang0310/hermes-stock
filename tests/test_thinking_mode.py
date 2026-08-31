"""推理强度（thinking_mode）每模型配置：取值校验、标志映射与全链路接线护栏。

接线任缺一环，配置页的开关就会静默失效，因此用静态源码断言钉住：
数据库迁移 / active_llm_config / agentscope 引擎 / legacy 引擎 / 配置 API。
"""
from __future__ import annotations

import unittest
from pathlib import Path

import pydantic

from backend.app.ai.ai_api import ModelConfigIn
from backend.app.core.llm_config import thinking_flag

REPO_ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class ThinkingFlagTests(unittest.TestCase):
    def test_auto_mode_returns_none(self) -> None:
        self.assertIsNone(thinking_flag({}))
        self.assertIsNone(thinking_flag({"thinking_mode": "auto"}))
        self.assertIsNone(thinking_flag({"thinking_mode": None}))

    def test_on_off_map_to_boolean(self) -> None:
        self.assertIs(thinking_flag({"thinking_mode": "on"}), True)
        self.assertIs(thinking_flag({"thinking_mode": "off"}), False)


class ModelConfigInThinkingModeTests(unittest.TestCase):
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
        self.assertEqual(payload.thinking_mode, "auto")

    def test_accepts_on_and_off(self) -> None:
        for mode in ("on", "off"):
            parsed = ModelConfigIn(**self._payload(thinking_mode=mode))
            self.assertEqual(parsed.thinking_mode, mode)

    def test_rejects_invalid_mode(self) -> None:
        with self.assertRaises(pydantic.ValidationError):
            ModelConfigIn(**self._payload(thinking_mode="high"))


class ThinkingModeWiringGuardTests(unittest.TestCase):
    def test_database_migration_adds_column(self) -> None:
        source = _source("backend/app/core/database.py")
        self.assertIn('_add_column("llm_model_configs", "thinking_mode"', source)
        self.assertIn("thinking_mode VARCHAR(16) NOT NULL DEFAULT 'auto'", source)

    def test_active_llm_config_returns_mode(self) -> None:
        source = _source("backend/app/core/llm_config.py")
        self.assertIn('"thinking_mode": str(row.thinking_mode or "auto")', source)
        self.assertIn('"thinking_mode": "auto"', source)

    def test_agentscope_engine_sends_extra_body(self) -> None:
        source = _source("backend/app/agent_runtime/agentscope_engine.py")
        self.assertIn("enable_thinking = thinking_flag(llm)", source)
        self.assertIn('extra_body = {"enable_thinking": enable_thinking}', source)
        self.assertIn("extra_body=extra_body", source)

    def test_legacy_engine_passes_flag(self) -> None:
        source = _source("backend/app/agents/stock_agent.py")
        self.assertIn("enable_thinking=thinking_flag(llm_config)", source)

    def test_model_config_api_exposes_field(self) -> None:
        source = _source("backend/app/ai/ai_api.py")
        self.assertIn('thinking_mode: Literal["auto", "on", "off"]', source)
        self.assertIn('"thinking_mode": model.thinking_mode or "auto"', source)


if __name__ == "__main__":
    unittest.main()
