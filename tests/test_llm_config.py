from __future__ import annotations

import unittest
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.database import Base
from backend.app.core.llm_config import (
    purge_retired_model_configs,
    model_supports_vision,
    persist_chat_session_model,
    resolve_chat_model_config_ids,
    resolve_model_config_ids_by_priority,
    resolve_vision_model_config_id,
)
from backend.app.ai.ai_api import ModelConfigIn
from backend.app.core.models import AiChatSession, LlmModelConfig, LlmModelPlatform, User


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

    def test_chat_failover_prefers_another_platform(self) -> None:
        with self.SessionFactory() as db:
            primary_platform = LlmModelPlatform(
                name="Primary provider",
                provider_type="OpenAI compatible",
                base_url="https://primary.invalid/v1",
                status="enabled",
            )
            fallback_platform = LlmModelPlatform(
                name="Fallback provider",
                provider_type="OpenAI compatible",
                base_url="https://fallback.invalid/v1",
                status="enabled",
            )
            db.add_all([primary_platform, fallback_platform])
            db.flush()
            primary = LlmModelConfig(
                platform_id=primary_platform.id,
                display_name="Primary active",
                provider_name=primary_platform.name,
                base_url=primary_platform.base_url,
                model_name="primary-active",
                is_active=True,
                status="enabled",
            )
            same_platform = LlmModelConfig(
                platform_id=primary_platform.id,
                display_name="Primary sibling",
                provider_name=primary_platform.name,
                base_url=primary_platform.base_url,
                model_name="primary-sibling",
                is_active=False,
                status="enabled",
            )
            cross_platform = LlmModelConfig(
                platform_id=fallback_platform.id,
                display_name="Cross-provider fallback",
                provider_name=fallback_platform.name,
                base_url=fallback_platform.base_url,
                model_name="cross-provider",
                is_active=False,
                status="enabled",
            )
            db.add_all([primary, same_platform, cross_platform])
            db.commit()

        with patch("backend.app.core.llm_config.SessionLocal", self.SessionFactory):
            resolved = resolve_chat_model_config_ids(primary.id)

        self.assertEqual(resolved, [primary.id, cross_platform.id, same_platform.id])

    def test_persist_chat_session_model_enforces_session_owner(self) -> None:
        with self.SessionFactory() as db:
            owner = User(
                username="owner",
                display_name="Owner",
                role="researcher",
                permission_scope="agent:use",
                password_hash="test",
            )
            other = User(
                username="other",
                display_name="Other",
                role="researcher",
                permission_scope="agent:use",
                password_hash="test",
            )
            platform = LlmModelPlatform(
                name="Session model provider",
                provider_type="OpenAI compatible",
                base_url="https://session.invalid/v1",
                status="enabled",
            )
            db.add_all([owner, other, platform])
            db.flush()
            primary = LlmModelConfig(
                platform_id=platform.id,
                display_name="Session primary",
                provider_name=platform.name,
                base_url=platform.base_url,
                model_name="session-primary",
                is_active=True,
                status="enabled",
            )
            fallback = LlmModelConfig(
                platform_id=platform.id,
                display_name="Session fallback",
                provider_name=platform.name,
                base_url=platform.base_url,
                model_name="session-fallback",
                is_active=False,
                status="enabled",
            )
            db.add_all([primary, fallback])
            db.flush()
            chat_session = AiChatSession(
                user_id=owner.id,
                model_config_id=primary.id,
                title="Owned session",
                status="active",
                chat_mode="research",
                source_type="web",
            )
            db.add(chat_session)
            db.commit()
            session_id = chat_session.id
            owner_id = owner.id
            other_id = other.id
            primary_id = primary.id
            fallback_id = fallback.id

        with patch("backend.app.core.llm_config.SessionLocal", self.SessionFactory):
            self.assertFalse(
                persist_chat_session_model(
                    session_id=session_id,
                    user_id=other_id,
                    model_config_id=fallback_id,
                )
            )
            with self.SessionFactory() as db:
                self.assertEqual(db.get(AiChatSession, session_id).model_config_id, primary_id)
            self.assertTrue(
                persist_chat_session_model(
                    session_id=session_id,
                    user_id=owner_id,
                    model_config_id=fallback_id,
                )
            )

        with self.SessionFactory() as db:
            self.assertEqual(db.get(AiChatSession, session_id).model_config_id, fallback_id)

    def test_retired_model_is_never_routed_and_cleanup_releases_sessions(self) -> None:
        with self.SessionFactory() as db:
            user = User(
                username="retired-model-owner",
                display_name="Retired model owner",
                role="researcher",
                permission_scope="agent:use",
                password_hash="test",
            )
            platform = LlmModelPlatform(
                name="Retired model provider",
                provider_type="OpenAI compatible",
                base_url="https://retired.invalid/v1",
                status="enabled",
            )
            db.add_all([user, platform])
            db.flush()
            retired = LlmModelConfig(
                platform_id=platform.id,
                display_name="Retired GPT",
                provider_name=platform.name,
                base_url=platform.base_url,
                model_name=" GPT-5.6-SOL ",
                is_active=True,
                status="enabled",
            )
            fallback = LlmModelConfig(
                platform_id=platform.id,
                display_name="Available fallback",
                provider_name=platform.name,
                base_url=platform.base_url,
                model_name="available-fallback",
                is_active=False,
                status="enabled",
            )
            db.add_all([retired, fallback])
            db.flush()
            chat_session = AiChatSession(
                user_id=user.id,
                model_config_id=retired.id,
                title="Stale retired session",
                status="active",
                chat_mode="research",
                source_type="web",
            )
            db.add(chat_session)
            db.commit()
            retired_id = retired.id
            fallback_id = fallback.id
            session_id = chat_session.id

        with patch("backend.app.core.llm_config.SessionLocal", self.SessionFactory):
            self.assertEqual(resolve_chat_model_config_ids(retired_id), [fallback_id])

        with self.SessionFactory() as db:
            self.assertEqual(purge_retired_model_configs(db), 1)
            db.commit()

        with self.SessionFactory() as db:
            self.assertIsNone(db.get(LlmModelConfig, retired_id))
            self.assertIsNone(db.get(AiChatSession, session_id).model_config_id)

    def test_retired_model_cannot_be_added_again(self) -> None:
        with self.assertRaises(ValidationError):
            ModelConfigIn(
                display_name="Retired GPT",
                provider_name="OpenAI compatible",
                base_url="https://retired.invalid/v1",
                model_name="gpt-5.6-sol",
            )


class SeedAiConfigsRerunTests(unittest.TestCase):
    """部署重跑 seed 不得覆盖配置页已保存的密钥与模型选择。"""

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

    def test_rerun_preserves_ui_edited_key_and_active_model(self) -> None:
        from backend.app.seed.seed import seed_auth_and_ai_configs

        with self.SessionFactory() as db:
            seed_auth_and_ai_configs(db)
            db.commit()

        with self.SessionFactory() as db:
            platform = db.scalar(select(LlmModelPlatform).where(LlmModelPlatform.name == "百炼"))
            platform.api_key = "sk-ui-edited"
            db.scalar(
                select(LlmModelConfig).where(LlmModelConfig.display_name == "百炼 Qwen3.7 Plus")
            ).is_active = False
            db.scalar(
                select(LlmModelConfig).where(LlmModelConfig.display_name == "百炼 Qwen3.7 Max")
            ).is_active = True
            db.commit()

        with self.SessionFactory() as db:
            seed_auth_and_ai_configs(db)
            db.commit()

        with self.SessionFactory() as db:
            platform = db.scalar(select(LlmModelPlatform).where(LlmModelPlatform.name == "百炼"))
            self.assertEqual(platform.api_key, "sk-ui-edited")
            active = db.scalars(
                select(LlmModelConfig).where(LlmModelConfig.is_active.is_(True))
            ).all()
            self.assertEqual([model.display_name for model in active], ["百炼 Qwen3.7 Max"])


if __name__ == "__main__":
    unittest.main()
