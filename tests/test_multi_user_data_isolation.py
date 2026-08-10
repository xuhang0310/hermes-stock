from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.auth.auth import create_access_token
from backend.app.auth.user_api import router as user_router
from backend.app.core.database import Base, get_db
from backend.app.core.models import (
    JobRun,
    DataSourceConfig,
    LlmModelConfig,
    Position,
    ScheduledJob,
    User,
    WatchlistItem,
)
from backend.app.scheduler.rhythm_api import router as rhythm_router


class MultiUserDataIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.SessionFactory() as db:
            self.admin = User(
                username="root-admin",
                display_name="Root Admin",
                role="super_admin",
                permission_scope="",
                password_hash="test",
            )
            self.user_a = User(
                username="isolated-a",
                display_name="User A",
                role="researcher",
                permission_scope="",
                password_hash="test",
            )
            self.user_b = User(
                username="isolated-b",
                display_name="User B",
                role="researcher",
                permission_scope="",
                password_hash="test",
            )
            db.add_all([self.admin, self.user_a, self.user_b])
            db.add_all(
                [
                    LlmModelConfig(
                        display_name="Shared Qwen",
                        provider_name="bailian",
                        base_url="https://example.invalid/v1",
                        model_name="qwen-shared",
                        is_active=True,
                        status="enabled",
                    ),
                    DataSourceConfig(
                        provider="tushare",
                        display_name="Shared TuShare",
                        provider_type="market_data",
                        status="enabled",
                    ),
                ]
            )
            db.commit()
            for user in (self.admin, self.user_a, self.user_b):
                db.refresh(user)

            db.add_all(
                [
                    ScheduledJob(
                        job_key="private-a",
                        name="A 的任务",
                        job_kind="agent_task",
                        schedule_type="daily",
                        run_time="09:00",
                        owner_user_id=self.user_a.id,
                        created_via="user",
                        enabled=True,
                    ),
                    ScheduledJob(
                        job_key="private-b",
                        name="B 的任务",
                        job_kind="agent_task",
                        schedule_type="daily",
                        run_time="10:00",
                        owner_user_id=self.user_b.id,
                        created_via="user",
                        enabled=True,
                    ),
                    ScheduledJob(
                        job_key="system-maintenance",
                        name="系统维护",
                        job_kind="health_check",
                        schedule_type="daily",
                        run_time="08:00",
                        owner_user_id=None,
                        created_via="admin",
                        enabled=True,
                    ),
                    JobRun(
                        run_id="run-a",
                        job_key="private-a",
                        owner_user_id=self.user_a.id,
                        fire_key="private-a@1",
                        trigger_type="schedule",
                        status="success",
                        started_at=datetime(2026, 8, 10, 9, 0),
                    ),
                    JobRun(
                        run_id="run-b",
                        job_key="private-b",
                        owner_user_id=self.user_b.id,
                        fire_key="private-b@1",
                        trigger_type="schedule",
                        status="failed",
                        started_at=datetime(2026, 8, 10, 10, 0),
                    ),
                ]
            )
            db.commit()

        app = FastAPI()
        app.include_router(user_router)
        app.include_router(rhythm_router)

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

    def test_schedule_jobs_and_runs_are_strictly_private_even_for_super_admin(self) -> None:
        response_a = self.client.get("/api/schedule/jobs", headers=self._headers(self.user_a))
        self.assertEqual(response_a.status_code, 200)
        self.assertEqual([item["job_key"] for item in response_a.json()], ["private-a"])

        response_b = self.client.get("/api/schedule/jobs", headers=self._headers(self.user_b))
        self.assertEqual(response_b.status_code, 200)
        self.assertEqual([item["job_key"] for item in response_b.json()], ["private-b"])

        response_admin = self.client.get("/api/schedule/jobs", headers=self._headers(self.admin))
        self.assertEqual(response_admin.status_code, 200)
        self.assertEqual(response_admin.json(), [])

        runs_a = self.client.get("/api/schedule/runs", headers=self._headers(self.user_a))
        self.assertEqual([item["run_id"] for item in runs_a.json()], ["run-a"])
        self.assertEqual(
            self.client.get("/api/schedule/runs/run-b", headers=self._headers(self.user_a)).status_code,
            404,
        )

        foreign_toggle = self.client.post(
            "/api/schedule/jobs/private-a/toggle",
            headers=self._headers(self.user_b),
        )
        self.assertEqual(foreign_toggle.status_code, 404)

        cleared = self.client.delete("/api/schedule/runs", headers=self._headers(self.user_a))
        self.assertEqual(cleared.status_code, 200)
        self.assertEqual(cleared.json()["deleted"], 1)
        with self.SessionFactory() as db:
            self.assertIsNone(db.scalar(select(JobRun).where(JobRun.run_id == "run-a")))
            self.assertIsNotNone(db.scalar(select(JobRun).where(JobRun.run_id == "run-b")))

    def test_created_user_starts_without_personal_data_and_can_create_own_task(self) -> None:
        with patch("backend.app.auth.user_api.log_event"):
            created = self.client.post(
                "/api/users",
                headers=self._headers(self.admin),
                json={
                    "username": "fresh-user",
                    "display_name": "Fresh User",
                    "password": "StrongPass123",
                    "role": "researcher",
                },
            )
        self.assertEqual(created.status_code, 201, created.text)
        fresh_id = int(created.json()["id"])

        with self.SessionFactory() as db:
            fresh = db.get(User, fresh_id)
            self.assertIsNotNone(fresh)
            self.assertEqual(
                int(db.scalar(select(func.count()).select_from(Position).where(Position.owner_user_id == fresh_id)) or 0),
                0,
            )
            self.assertEqual(
                int(db.scalar(select(func.count()).select_from(WatchlistItem).where(WatchlistItem.owner_user_id == fresh_id)) or 0),
                0,
            )
            self.assertEqual(
                int(db.scalar(select(func.count()).select_from(ScheduledJob).where(ScheduledJob.owner_user_id == fresh_id)) or 0),
                0,
            )
            self.assertEqual(int(db.scalar(select(func.count()).select_from(LlmModelConfig)) or 0), 1)
            self.assertEqual(int(db.scalar(select(func.count()).select_from(DataSourceConfig)) or 0), 1)

            fresh_headers = self._headers(fresh)

        empty_jobs = self.client.get("/api/schedule/jobs", headers=fresh_headers)
        self.assertEqual(empty_jobs.status_code, 200)
        self.assertEqual(empty_jobs.json(), [])

        created_job = self.client.post(
            "/api/schedule/jobs",
            headers=fresh_headers,
            json={
                "name": "我的盘前研究",
                "description": "只属于新用户",
                "job_kind": "agent_task",
                "schedule_type": "daily",
                "run_time": "08:30",
                "trading_day_only": True,
                "enabled": True,
            },
        )
        self.assertEqual(created_job.status_code, 200, created_job.text)
        self.assertEqual(created_job.json()["owner_user_id"], fresh_id)
        self.assertTrue(created_job.json()["job_key"].startswith(f"user-{fresh_id}-"))
        self.assertEqual(
            self.client.get("/api/schedule/jobs", headers=self._headers(self.user_a)).json()[0]["job_key"],
            "private-a",
        )


if __name__ == "__main__":
    unittest.main()
