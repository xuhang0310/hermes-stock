from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.core.llm_config import (
    model_supports_vision,
    resolve_model_config_ids_by_priority,
    resolve_vision_model_config_id,
)
from backend.app.core.models import LlmModelConfig, LlmModelPlatform


class LlmVisionRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def tearDown(self) -> None:
        self.engine.dispose()

    def _seed_models(self) -> tuple[int, int]:
        with self.SessionFactory() as db:
            platform = LlmModelPlatform(
                name="百炼测试",
                provider_type="OpenAI compatible",
                base_url="https://example.invalid/v1",
                status="enabled",
            )
            db.add(platform)
            db.flush()
            text_model = LlmModelConfig(
                platform_id=platform.id,
                display_name="Qwen Max Test",
                provider_name="百炼测试",
                base_url=platform.base_url,
                model_name="qwen3.7-max",
                is_active=True,
                status="enabled",
            )
            vision_model = LlmModelConfig(
                platform_id=platform.id,
                display_name="Qwen Plus Test",
                provider_name="百炼测试",
                base_url=platform.base_url,
                model_name="qwen3.7-plus",
                is_active=False,
                status="enabled",
            )
            db.add_all([text_model, vision_model])
            db.commit()
            return text_model.id, vision_model.id

    def test_known_model_capabilities_are_conservative(self) -> None:
        self.assertTrue(model_supports_vision("qwen3.7-plus"))
        self.assertTrue(model_supports_vision("qwen3-vl-plus"))
        self.assertFalse(model_supports_vision("qwen3.7-max"))
        self.assertFalse(model_supports_vision("deepseek-v4-pro"))

    def test_text_model_routes_to_enabled_vision_model_on_same_platform(self) -> None:
        text_model_id, vision_model_id = self._seed_models()
        with patch("backend.app.core.llm_config.SessionLocal", self.SessionFactory):
            resolved = resolve_vision_model_config_id(text_model_id)
            self.assertEqual(resolved, vision_model_id)
            self.assertEqual(
                resolve_vision_model_config_id(vision_model_id),
                vision_model_id,
            )

    def test_resolve_model_config_ids_preserves_requested_priority(self) -> None:
        with self.SessionFactory() as db:
            platform = LlmModelPlatform(
                name="百炼推荐测试",
                provider_type="OpenAI compatible",
                base_url="https://example.invalid/v1",
                status="enabled",
            )
            disabled_platform = LlmModelPlatform(
                name="停用平台",
                provider_type="OpenAI compatible",
                base_url="https://disabled.invalid/v1",
                status="disabled",
            )
            db.add_all([platform, disabled_platform])
            db.flush()
            primary = LlmModelConfig(
                platform_id=platform.id,
                display_name="DeepSeek Flash",
                provider_name=platform.name,
                base_url=platform.base_url,
                api_key="test-key",
                model_name="deepseek-v4-flash-0731",
                is_active=False,
                status="enabled",
            )
            fallback = LlmModelConfig(
                platform_id=platform.id,
                display_name="DeepSeek Pro",
                provider_name=platform.name,
                base_url=platform.base_url,
                api_key="test-key",
                model_name="deepseek-v4-pro",
                is_active=True,
                status="enabled",
            )
            ignored = LlmModelConfig(
                platform_id=disabled_platform.id,
                display_name="Disabled Flash",
                provider_name=disabled_platform.name,
                base_url=disabled_platform.base_url,
                api_key="test-key",
                model_name="deepseek-v4-flash-0731",
                is_active=True,
                status="enabled",
            )
            db.add_all([primary, fallback, ignored])
            db.commit()

        with patch("backend.app.core.llm_config.SessionLocal", self.SessionFactory):
            resolved = resolve_model_config_ids_by_priority(
                ("deepseek-v4-flash-0731", "deepseek-v4-pro")
            )
        self.assertEqual(resolved, [primary.id, fallback.id])


if __name__ == "__main__":
    unittest.main()
