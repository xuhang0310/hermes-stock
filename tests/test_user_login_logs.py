from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth.auth import BEIJING_TZ, create_access_token, hash_password
from backend.app.auth.auth_api import router as auth_router
from backend.app.auth.user_api import router as user_router
from backend.app.core.database import Base, get_db
from backend.app.core.models import LoginLog, User


@pytest.fixture
def login_audit():
    # Standalone app and an explicit in-memory engine: never runs app startup or the configured DB.
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine, tables=[User.__table__, LoginLog.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        admin = User(
            username="audit-admin", display_name="管理员", role="admin",
            password_hash=hash_password("audit-password"),
            last_login_at=datetime(2026, 10, 29, 9, 15),
        )
        researcher = User(
            username="audit-researcher", display_name="研究员", role="researcher",
            password_hash="unused",
        )
        db.add_all([admin, researcher])
        db.flush()
        db.add_all([
            LoginLog(username="before", success=True, created_at=datetime(2026, 10, 28, 15, 59, 59)),
            LoginLog(
                username="unknown-account", success=False, reason="bad_credentials",
                ip="192.0.2.1", user_agent="Test Browser", trace_id="trace-test",
                created_at=datetime(2026, 10, 28, 16),
            ),
            LoginLog(
                username=admin.username, user_id=admin.id, role=admin.role,
                success=True, created_at=datetime(2026, 10, 29, 15, 59, 59),
            ),
            LoginLog(
                username="unknown-account", success=False, reason="rate_limited",
                created_at=datetime(2026, 10, 29, 16),
            ),
        ])
        db.commit()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(user_router)

    def override_db():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = override_db
    headers = {"Authorization": f"Bearer {create_access_token(admin)}"}
    with TestClient(app) as client:
        yield client, factory, headers, researcher
    engine.dispose()


def test_login_logs_require_user_management_permission(login_audit):
    client, _, headers, researcher = login_audit
    assert client.get("/api/users/login-logs").status_code == 401
    assert client.get(
        "/api/users/login-logs",
        headers={"Authorization": f"Bearer {create_access_token(researcher)}"},
    ).status_code == 403
    assert client.get("/api/users/login-logs", headers=headers).status_code == 200


def test_login_logs_paginate_newest_first_with_explicit_utc(login_audit):
    client, factory, headers, _ = login_audit
    with factory() as db:
        db.add(LoginLog(username="same-time", success=True, created_at=datetime(2026, 10, 29, 16)))
        db.commit()
    first = client.get("/api/users/login-logs?page_size=2", headers=headers).json()
    second = client.get("/api/users/login-logs?page_size=2&page=2", headers=headers).json()
    assert (first["total"], first["page"], first["page_size"]) == (5, 1, 2)
    assert [item["username"] for item in first["items"]] == ["same-time", "unknown-account"]
    assert [item["username"] for item in second["items"]] == ["audit-admin", "unknown-account"]
    assert first["items"][0]["created_at"] == "2026-10-29T16:00:00+00:00"
    empty = client.get("/api/users/login-logs?page=99", headers=headers).json()
    assert empty["items"] == []
    assert empty["total"] == 5


def test_filter_failed_unknown_username_without_joining_users(login_audit):
    client, _, headers, _ = login_audit
    payload = client.get(
        "/api/users/login-logs",
        params={"username": " UNKNOWN-ACCOUNT ", "success": "false"}, headers=headers,
    ).json()
    assert payload["total"] == 2
    assert all(item["user_id"] is None for item in payload["items"])
    assert {item["reason"] for item in payload["items"]} == {"bad_credentials", "rate_limited"}
    assert payload["items"][1]["ip"] == "192.0.2.1"
    assert payload["items"][1]["user_agent"] == "Test Browser"
    assert payload["items"][1]["trace_id"] == "trace-test"
    assert "password" not in payload["items"][1]
    assert client.get(
        "/api/users/login-logs", params={"username": "unknown%"}, headers=headers,
    ).json()["total"] == 0


def test_beijing_date_range_includes_whole_day_and_excludes_next_midnight(login_audit):
    client, _, headers, _ = login_audit
    payload = client.get(
        "/api/users/login-logs",
        params={"start_date": "2026-10-29", "end_date": "2026-10-29"}, headers=headers,
    ).json()
    assert payload["total"] == 2
    assert [item["username"] for item in payload["items"]] == ["audit-admin", "unknown-account"]
    assert client.get(
        "/api/users/login-logs", params={"success": "true"}, headers=headers,
    ).json()["total"] == 2


@pytest.mark.parametrize("params", [
    {"page": 0}, {"page_size": 101}, {"page_size": 0}, {"success": "invalid"},
    {"start_date": "not-a-date"}, {"start_date": "2026-10-30", "end_date": "2026-10-29"},
    {"start_date": "0001-01-01"}, {"end_date": "9999-12-31"}, {"username": "a" * 81},
])
def test_login_log_query_validation(login_audit, params):
    client, _, headers, _ = login_audit
    assert client.get("/api/users/login-logs", params=params, headers=headers).status_code == 422


def test_auth_records_success_failure_and_rate_limit_without_credentials(login_audit):
    client, factory, headers, _ = login_audit
    with factory() as db:
        db.execute(delete(LoginLog))
        db.commit()

    before = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=1)
    with patch("backend.app.auth.auth_api.login_retry_after", return_value=0), patch(
        "backend.app.auth.auth_api.record_login_failure"
    ), patch("backend.app.auth.auth_api.clear_login_failures"):
        success = client.post("/api/auth/login", json={"username": "audit-admin", "password": "audit-password"})
        assert success.status_code == 200
        assert success.json()["user"]["last_login_at"].endswith("+08:00")
        assert client.post(
            "/api/auth/login", json={"username": "unknown-account", "password": "bad-secret"},
        ).status_code == 401
    with patch("backend.app.auth.auth_api.login_retry_after", return_value=90):
        assert client.post(
            "/api/auth/login", json={"username": "unknown-account", "password": "bad-secret"},
        ).status_code == 429

    after = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)
    with factory() as db:
        logs = db.scalars(select(LoginLog).order_by(LoginLog.id)).all()
        assert len(logs) == 3
        assert [(row.success, row.reason) for row in logs] == [
            (True, None), (False, "bad_credentials"), (False, "rate_limited"),
        ]
        assert all(before <= row.created_at <= after for row in logs)
        user = db.scalar(select(User).where(User.username == "audit-admin"))
        assert abs((datetime.now(BEIJING_TZ).replace(tzinfo=None) - user.last_login_at).total_seconds()) < 10
    payload = client.get("/api/users/login-logs", headers=headers).text
    assert "audit-password" not in payload
    assert "bad-secret" not in payload


def test_user_last_login_serialization_preserves_beijing_wall_clock(login_audit):
    client, _, headers, _ = login_audit
    users = client.get("/api/users", headers=headers).json()
    admin = next(user for user in users if user["username"] == "audit-admin")
    assert admin["last_login_at"] == "2026-10-29T09:15:00+08:00"
    me = client.get("/api/auth/me", headers=headers).json()
    assert me["last_login_at"] == admin["last_login_at"]
