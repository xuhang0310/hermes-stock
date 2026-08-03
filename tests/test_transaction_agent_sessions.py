from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.agent_runtime.transaction_channels import _open_transaction_session
from backend.app.core.database import Base
from backend.app.core.models import AiChatMessage, AiChatSession, DigitalHumanProfile, User


class TransactionAgentSessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.SessionFactory() as db:
            user = User(
                username="transaction-session-user",
                display_name="Transaction Session User",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            profile = DigitalHumanProfile(
                code="position-entry-agent",
                name="持仓录入 Agent",
                description="test",
                system_prompt="test",
                quick_prompts_json="[]",
            )
            db.add_all([user, profile])
            db.commit()
            self.user_id = user.id

    def tearDown(self) -> None:
        self.engine.dispose()

    def open_session(self, client_session_id: str) -> tuple[int, list[dict[str, str]]]:
        with patch(
            "backend.app.agent_runtime.transaction_channels.SessionLocal",
            self.SessionFactory,
        ):
            return _open_transaction_session(
                user_id=self.user_id,
                code="position-entry-agent",
                question="test",
                model_config_id=None,
                client_session_id=client_session_id,
            )

    def test_same_click_session_reuses_history(self) -> None:
        session_id, history = self.open_session("click-one")
        self.assertEqual(history, [])

        with self.SessionFactory() as db:
            db.add_all(
                [
                    AiChatMessage(session_id=session_id, role="user", content="第一轮问题"),
                    AiChatMessage(session_id=session_id, role="assistant", content="第一轮回答"),
                ]
            )
            db.commit()

        reused_id, restored_history = self.open_session("click-one")

        self.assertEqual(reused_id, session_id)
        self.assertEqual(
            restored_history,
            [
                {"role": "user", "content": "第一轮问题"},
                {"role": "assistant", "content": "第一轮回答"},
            ],
        )

    def test_new_click_session_is_isolated(self) -> None:
        first_id, _ = self.open_session("click-one")
        second_id, second_history = self.open_session("click-two")

        self.assertNotEqual(second_id, first_id)
        self.assertEqual(second_history, [])
        with self.SessionFactory() as db:
            external_ids = set(db.scalars(select(AiChatSession.external_conversation_id)).all())
        self.assertEqual(
            external_ids,
            {
                f"transaction:{self.user_id}:position-entry-agent:click-one",
                f"transaction:{self.user_id}:position-entry-agent:click-two",
            },
        )


if __name__ == "__main__":
    unittest.main()
