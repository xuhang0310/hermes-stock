from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from backend.app.ai import ai_api
from backend.app.auth.auth import AuthContext
from backend.app.core.database import Base
from backend.app.core.models import AiChatMessage, AiChatSession, DigitalHumanProfile, User
from backend.app.feishu import feishu_bot


class FeishuChatSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.SessionFactory() as db:
            admin = User(
                username="admin",
                display_name="系统管理员",
                role="super_admin",
                permission_scope="all",
                password_hash="test",
                is_enabled=True,
            )
            viewer = User(
                username="viewer",
                display_name="访客",
                role="viewer",
                permission_scope="agent:use",
                password_hash="test",
                is_enabled=True,
            )
            profile = DigitalHumanProfile(
                code="research-agent",
                name="研究员",
                avatar_tone="calm",
                target_roles="*",
                description="测试数字人",
                system_prompt="测试提示词",
                quick_prompts_json="[]",
                status="enabled",
            )
            db.add_all([admin, viewer, profile])
            db.commit()
            self.admin_id = admin.id
            self.viewer_id = viewer.id
            self.profile_id = profile.id

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_same_feishu_chat_reuses_session_and_restores_history(self) -> None:
        with patch.object(feishu_bot, "SessionLocal", self.SessionFactory):
            session_id, history = feishu_bot._open_or_create_feishu_session(
                profile_id=self.profile_id,
                chat_id="oc_group_123",
                chat_type="group",
                question="第一轮问题",
            )
            self.assertIsNotNone(session_id)
            self.assertEqual(history, [])

            feishu_bot._complete_feishu_session(
                session_id,
                answer="第一轮回答",
                error=None,
                duration_ms=1234,
            )
            reused_id, restored_history = feishu_bot._open_or_create_feishu_session(
                profile_id=self.profile_id,
                chat_id="oc_group_123",
                chat_type="group",
                question="第二轮问题",
            )

        self.assertEqual(reused_id, session_id)
        self.assertEqual(
            restored_history,
            [
                {"role": "user", "content": "第一轮问题"},
                {"role": "assistant", "content": "第一轮回答"},
            ],
        )
        with self.SessionFactory() as db:
            chat_session = db.get(AiChatSession, session_id)
            messages = db.scalars(
                select(AiChatMessage)
                .where(AiChatMessage.session_id == session_id)
                .order_by(AiChatMessage.id)
            ).all()
            self.assertEqual(chat_session.source_type, "feishu")
            self.assertEqual(chat_session.source_label, "飞书群聊")
            self.assertEqual(chat_session.external_conversation_id, "oc_group_123")
            self.assertEqual([item.content for item in messages], ["第一轮问题", "第一轮回答", "第二轮问题"])
            self.assertEqual(messages[1].duration_ms, 1234)

    def test_source_marker_is_stable_and_external_sessions_are_admin_only(self) -> None:
        with self.SessionFactory() as db:
            feishu_session = AiChatSession(
                user_id=self.admin_id,
                digital_human_id=self.profile_id,
                title="飞书群聊 · 测试",
                status="active",
                chat_mode="research",
                source_type="feishu",
                external_conversation_id="oc_private_value",
                source_label="飞书群聊",
            )
            viewer_web_session = AiChatSession(
                user_id=self.viewer_id,
                digital_human_id=self.profile_id,
                title="网页会话",
                status="active",
                chat_mode="research",
                source_type="web",
            )
            db.add_all([feishu_session, viewer_web_session])
            db.commit()

            marker = ai_api._session_source_out(feishu_session)
            self.assertRegex(marker["source_identifier"], r"^FS-[0-9A-F]{8}$")
            self.assertNotIn("oc_private_value", marker["source_identifier"])
            self.assertTrue(marker["read_only"])

            admin = db.get(User, self.admin_id)
            viewer = db.get(User, self.viewer_id)
            admin_rows = db.scalars(
                select(AiChatSession).where(
                    ai_api._session_visibility(AuthContext(user=admin, permissions={"all"}))
                )
            ).all()
            viewer_rows = db.scalars(
                select(AiChatSession).where(
                    ai_api._session_visibility(AuthContext(user=viewer, permissions={"agent:use"}))
                )
            ).all()

        self.assertEqual({row.id for row in admin_rows}, {feishu_session.id})
        self.assertEqual({row.id for row in viewer_rows}, {viewer_web_session.id})


if __name__ == "__main__":
    unittest.main()
