from __future__ import annotations

import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth.auth import create_access_token
from backend.app.core.database import Base, get_db
from backend.app.core.models import (
    AiChatMessage,
    AiChatSession,
    Instrument,
    Position,
    User,
    WatchlistGroup,
    WatchlistItem,
)
from backend.app.data.ops_console_api import router as ops_console_router


class OpsConsoleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.SessionFactory() as db:
            self.ops_user = User(
                username="ops-zhangsan",
                display_name="张三",
                role="researcher",
                permission_scope="ops_console:view",
                password_hash="test",
            )
            self.plain_user = User(
                username="plain-researcher",
                display_name="普通研究员",
                role="researcher",
                permission_scope="",
                password_hash="test",
            )
            self.admin = User(
                username="root-admin",
                display_name="Root Admin",
                role="super_admin",
                permission_scope="",
                password_hash="test",
            )
            db.add_all([self.ops_user, self.plain_user, self.admin])
            db.flush()
            self.instrument = Instrument(
                symbol="000001",
                name="平安银行",
                market="SZ",
                type="stock",
            )
            db.add(self.instrument)
            db.flush()
            # 每个研究员各一只持仓 + 一个自选组 + 一条自选项 + 一个对话会话(两条消息)
            db.add_all([
                Position(
                    owner_user_id=self.ops_user.id,
                    symbol="000001",
                    position_pct=5.0,
                    status="open",
                ),
                Position(
                    owner_user_id=self.plain_user.id,
                    symbol="000001",
                    position_pct=8.0,
                    status="open",
                ),
                WatchlistGroup(
                    owner_user_id=self.ops_user.id,
                    name="默认",
                ),
                WatchlistGroup(
                    owner_user_id=self.plain_user.id,
                    name="默认",
                ),
            ])
            db.flush()
            ops_group = db.scalar(
                select(WatchlistGroup).where(WatchlistGroup.owner_user_id == self.ops_user.id)
            )
            plain_group = db.scalar(
                select(WatchlistGroup).where(WatchlistGroup.owner_user_id == self.plain_user.id)
            )
            db.add_all([
                WatchlistItem(
                    owner_user_id=self.ops_user.id,
                    symbol="000001",
                    group_id=ops_group.id,
                    status="active",
                ),
                WatchlistItem(
                    owner_user_id=self.plain_user.id,
                    symbol="000001",
                    group_id=plain_group.id,
                    status="active",
                ),
            ])
            ops_session = AiChatSession(
                user_id=self.ops_user.id,
                title="张三的会话",
                status="active",
                chat_mode="research",
                source_type="web",
            )
            plain_session = AiChatSession(
                user_id=self.plain_user.id,
                title="普通研究员的会话",
                status="active",
                chat_mode="research",
                source_type="web",
            )
            db.add_all([ops_session, plain_session])
            db.flush()
            db.add_all([
                AiChatMessage(session_id=ops_session.id, role="user", content="你好"),
                AiChatMessage(session_id=ops_session.id, role="assistant", content="你好，我是情报官"),
                AiChatMessage(session_id=plain_session.id, role="user", content="分析一下"),
                AiChatMessage(session_id=plain_session.id, role="assistant", content="这是分析结果"),
                AiChatMessage(session_id=plain_session.id, role="tool", content="应被过滤"),
            ])
            db.commit()
            self.ops_session_id = ops_session.id
            self.plain_session_id = plain_session.id
            for u in (self.ops_user, self.plain_user, self.admin):
                db.refresh(u)

        app = FastAPI()
        app.include_router(ops_console_router)

        def override_db():
            db = self.SessionFactory()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.engine.dispose()

    @staticmethod
    def _headers(user: User) -> dict[str, str]:
        return {"Authorization": f"Bearer {create_access_token(user)}"}

    def test_no_token_returns_401(self) -> None:
        for path in [
            "/api/ops-console/users",
            "/api/ops-console/positions",
            "/api/ops-console/watchlist",
            "/api/ops-console/chat-sessions",
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)

    def test_plain_researcher_gets_403(self) -> None:
        headers = self._headers(self.plain_user)
        for path in [
            "/api/ops-console/users",
            "/api/ops-console/positions",
            "/api/ops-console/watchlist",
            "/api/ops-console/chat-sessions",
        ]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path, headers=headers).status_code, 403)

    def test_ops_user_sees_cross_user_positions(self) -> None:
        headers = self._headers(self.ops_user)
        resp = self.client.get("/api/ops-console/positions", headers=headers)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["success"])
        owners = {row["owner_username"] for row in body["data"]}
        self.assertEqual(owners, {"ops-zhangsan", "plain-researcher"})
        for row in body["data"]:
            self.assertIn("owner_display_name", row)
            self.assertEqual(row["symbol"], "000001")

    def test_ops_user_watchlist_spans_users(self) -> None:
        headers = self._headers(self.ops_user)
        resp = self.client.get("/api/ops-console/watchlist", headers=headers)
        self.assertEqual(resp.status_code, 200)
        owners = {row["owner_username"] for row in resp.json()["data"]}
        self.assertEqual(owners, {"ops-zhangsan", "plain-researcher"})

    def test_user_filter_narrows_positions(self) -> None:
        headers = self._headers(self.ops_user)
        resp = self.client.get(
            "/api/ops-console/positions",
            params={"user_id": self.plain_user.id},
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["data"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["owner_username"], "plain-researcher")

    def test_chat_sessions_span_users_with_counts(self) -> None:
        headers = self._headers(self.ops_user)
        resp = self.client.get("/api/ops-console/chat-sessions", headers=headers)
        self.assertEqual(resp.status_code, 200)
        sessions = resp.json()["data"]
        self.assertEqual(len(sessions), 2)
        by_user = {s["username"]: s for s in sessions}
        self.assertEqual(by_user["ops-zhangsan"]["message_count"], 2)
        self.assertEqual(by_user["plain-researcher"]["message_count"], 2)

    def test_chat_session_detail_replays_messages_in_order(self) -> None:
        headers = self._headers(self.ops_user)
        resp = self.client.get(
            f"/api/ops-console/chat-sessions/{self.plain_session_id}",
            headers=headers,
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()["data"]
        self.assertEqual(body["username"], "plain-researcher")
        roles = [m["role"] for m in body["messages"]]
        self.assertEqual(roles, ["user", "assistant"])  # tool 消息被过滤，按 id 升序

    def test_missing_session_returns_404(self) -> None:
        headers = self._headers(self.ops_user)
        resp = self.client.get("/api/ops-console/chat-sessions/99999", headers=headers)
        self.assertEqual(resp.status_code, 404)

    def test_super_admin_bypasses(self) -> None:
        headers = self._headers(self.admin)
        resp = self.client.get("/api/ops-console/positions", headers=headers)
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
