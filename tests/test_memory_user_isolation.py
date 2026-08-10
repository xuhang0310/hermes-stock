from __future__ import annotations

import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.ai import experience_store, memory
from backend.app.core.database import Base
from backend.app.core.models import (
    AiChatMessage,
    AiChatSession,
    ExperienceEntry,
    ExperienceInjection,
    User,
    UserMemory,
)


class MemoryUserIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.SessionFactory() as db:
            user_a = User(
                username="memory-a",
                display_name="Memory A",
                password_hash="x",
            )
            user_b = User(
                username="memory-b",
                display_name="Memory B",
                password_hash="x",
            )
            db.add_all([user_a, user_b])
            db.commit()
            self.user_a_id = user_a.id
            self.user_b_id = user_b.id

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_long_term_memory_recall_rechecks_vector_hits_by_user(self) -> None:
        with self.SessionFactory() as db:
            own = UserMemory(
                user_id=self.user_a_id,
                memory_type="preference",
                content="owner-only-memory common-keyword",
                importance=0.8,
                confidence=0.9,
            )
            other = UserMemory(
                user_id=self.user_b_id,
                memory_type="preference",
                content="other-user-secret common-keyword",
                importance=0.8,
                confidence=0.9,
            )
            db.add_all([own, other])
            db.commit()

            with patch.object(
                memory,
                "_vector_search",
                return_value=[(other.id, 0.99), (own.id, 0.8)],
            ), patch.object(memory, "_resolve_rerank_cfg", return_value=(None, None, None)):
                context = memory.retrieve_memories(
                    db,
                    user_id=self.user_a_id,
                    query="common-keyword",
                )

            self.assertIn("owner-only-memory", context)
            self.assertNotIn("other-user-secret", context)

    def test_experience_recall_and_injection_are_private(self) -> None:
        with self.SessionFactory() as db:
            own = ExperienceEntry(
                user_id=self.user_a_id,
                content="owner-only-experience",
                status="active",
            )
            other = ExperienceEntry(
                user_id=self.user_b_id,
                content="other-user-experience",
                status="active",
            )
            db.add_all([own, other])
            db.commit()

            with patch.object(
                experience_store,
                "_vector_search_entries",
                return_value=[(other.id, 0.99), (own.id, 0.8)],
            ), patch.object(
                experience_store,
                "_resolve_rerank_cfg",
                return_value=(None, None, None),
            ):
                context = experience_store.retrieve_experiences(
                    db,
                    user_id=self.user_a_id,
                    query="experience",
                )

            self.assertIn("owner-only-experience", context)
            self.assertNotIn("other-user-experience", context)
            injections = db.scalars(select(ExperienceInjection)).all()
            self.assertEqual(len(injections), 1)
            self.assertEqual(injections[0].user_id, self.user_a_id)
            self.assertEqual(injections[0].entry_id, own.id)

    def test_digestion_rejects_a_session_owned_by_another_user(self) -> None:
        with self.SessionFactory() as db:
            session = AiChatSession(
                user_id=self.user_b_id,
                title="private conversation",
            )
            db.add(session)
            db.flush()
            for index in range(4):
                db.add(
                    AiChatMessage(
                        session_id=session.id,
                        role="user",
                        content=f"private message {index}",
                    )
                )
            db.commit()

            with patch.object(memory, "_call_llm_for_digestion") as digest_llm:
                count = memory.digest_conversation(
                    db,
                    session_id=session.id,
                    user_id=self.user_a_id,
                )

            self.assertEqual(count, 0)
            digest_llm.assert_not_called()
            self.assertEqual(db.scalar(select(func.count()).select_from(UserMemory)), 0)


if __name__ == "__main__":
    unittest.main()
