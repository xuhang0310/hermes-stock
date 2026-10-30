from __future__ import annotations

import unittest
from datetime import timedelta
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.auth.auth import AuthContext, get_current_user, permissions_for
from backend.app.core.database import Base, get_db
from backend.app.core.models import McpAccessToken, User
from backend.app.mcp_gateway.auth import authenticate_mcp_token, token_digest, utc_now
from backend.app.mcp_gateway.token_api import router


class McpTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch("pymysql.connections.Connection.connect", side_effect=AssertionError("unexpected MySQL")))
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.db.close)
        self.owner = User(username="owner", display_name="Owner", role="researcher", password_hash="test")
        self.other = User(username="other", display_name="Other", role="viewer", password_hash="test")
        self.db.add_all([self.owner, self.other])
        self.db.commit()
        self.current = self.owner
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: self.db
        app.dependency_overrides[get_current_user] = lambda: AuthContext(
            user=self.current, permissions=permissions_for(self.current),
        )
        self.client = self.enterContext(TestClient(app))

    def _create(self, **kwargs) -> dict:
        response = self.client.post("/api/mcp-tokens", json={"name": "External agent", **kwargs})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def _assert_invalid(self, raw_token: str) -> None:
        with self.assertRaises(HTTPException) as caught:
            authenticate_mcp_token(raw_token, self.db)
        self.assertEqual(caught.exception.status_code, 401)

    def test_raw_secret_is_returned_once_and_only_digest_is_stored(self) -> None:
        output = self._create()
        row = self.db.scalar(select(McpAccessToken))
        self.assertEqual(row.token_hash, token_digest(output["token"]))
        self.assertNotIn(output["token"], repr(row.__dict__))
        self.assertEqual(set(output["credential"]["scopes"]), {"market:read", "portfolio:read", "schedules:read"})
        self.assertAlmostEqual((row.expires_at - utc_now()).total_seconds(), 90 * 86400, delta=5)
        listing = self.client.get("/api/mcp-tokens")
        self.assertNotIn(output["token"], listing.text)
        self.assertNotIn(row.token_hash, listing.text)
        principal = authenticate_mcp_token(output["token"], self.db)
        self.assertEqual((principal.user_id, principal.token_id), (self.owner.id, row.id))
        principal.require_scope("portfolio:read")
        with self.assertRaises(HTTPException) as caught:
            principal.require_scope("portfolio:write")
        self.assertEqual(caught.exception.status_code, 403)

    def test_viewer_defaults_are_read_only_and_cannot_escalate(self) -> None:
        self.current = self.other
        output = self._create()
        self.assertEqual(set(output["credential"]["scopes"]), {"market:read", "portfolio:read"})
        for scope in ("portfolio:write", "schedules:read", "schedules:write"):
            with self.subTest(scope=scope):
                response = self.client.post("/api/mcp-tokens", json={"name": "Denied", "scopes": [scope]})
                self.assertEqual(response.status_code, 403)

    def test_unknown_scopes_wildcards_and_invalid_expiry_are_rejected(self) -> None:
        for payload in (
            {"scopes": ["*"]}, {"scopes": ["all"]}, {"scopes": []},
            {"expires_in_days": 0}, {"expires_in_days": 366}, {"expires_in_days": 1.5},
            {"user_id": self.other.id}, {"name": "  "},
        ):
            with self.subTest(payload=payload):
                response = self.client.post("/api/mcp-tokens", json={"name": "Bad", **payload})
                self.assertEqual(response.status_code, 422)

    def test_list_and_revoke_are_strictly_owner_scoped(self) -> None:
        output = self._create()
        token_id = output["credential"]["id"]
        self.current = self.other
        self.assertEqual(self.client.get("/api/mcp-tokens").json()["tokens"], [])
        self.assertEqual(self.client.delete(f"/api/mcp-tokens/{token_id}").status_code, 404)
        authenticate_mcp_token(output["token"], self.db)
        self.current = self.owner
        for _ in range(2):
            self.assertEqual(self.client.delete(f"/api/mcp-tokens/{token_id}").status_code, 200)
        self._assert_invalid(output["token"])

    def test_expired_token_disabled_user_and_password_reset_invalidate_access(self) -> None:
        output = self._create()
        row = self.db.get(McpAccessToken, output["credential"]["id"])
        for field, value in (
            ("expired", utc_now() - timedelta(seconds=1)),
            ("disabled", False),
            ("password_reset", 2),
        ):
            with self.subTest(reason=field):
                row.expires_at = utc_now() + timedelta(days=1)
                self.owner.is_enabled = True
                self.owner.token_version = 1
                if field == "expired":
                    row.expires_at = value
                elif field == "disabled":
                    self.owner.is_enabled = value
                else:
                    self.owner.token_version = value
                self.db.commit()
                self._assert_invalid(output["token"])

    def test_role_downgrade_removes_write_scope_on_next_request(self) -> None:
        output = self._create(scopes=["portfolio:read", "portfolio:write", "schedules:write"])
        authenticate_mcp_token(output["token"], self.db).require_scope("portfolio:write")
        self.owner.role = "viewer"
        self.db.commit()
        principal = authenticate_mcp_token(output["token"], self.db)
        self.assertEqual(principal.scopes, frozenset({"portfolio:read"}))

    def test_web_jwt_malformed_tokens_and_modified_tokens_are_not_accepted(self) -> None:
        output = self._create()
        for raw in ("", "a.b.c", "hs_mcp_", "密钥", output["token"] + "x", "hs_mcp_" + "A" * 43):
            with self.subTest(raw=raw[:10]):
                self._assert_invalid(raw)

    def test_token_management_requires_web_login(self) -> None:
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[get_db] = lambda: self.db
        output = self._create()
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/mcp-tokens").status_code, 401)
            self.assertEqual(
                client.get("/api/mcp-tokens", headers={"Authorization": "Bearer " + output["token"]}).status_code,
                401,
            )


if __name__ == "__main__":
    unittest.main()
