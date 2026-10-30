from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from backend.app.core.models import Base, DigitalHumanProfile, McpMutationReceipt, ScheduledJob, User
from backend.app.mcp_gateway import schedule_tools
from backend.app.scheduler.schedule_service import AgentScheduleService


class McpScheduleToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch("pymysql.connections.Connection.connect", side_effect=AssertionError("unexpected MySQL")))
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.enterContext(patch.object(schedule_tools, "SessionLocal", self.sessions))
        with self.sessions() as db:
            owner = User(username="owner", display_name="Owner", role="researcher", password_hash="test")
            other = User(username="other", display_name="Other", role="researcher", password_hash="test")
            db.add_all([owner, other])
            db.flush()
            self.owner, self.other = owner.id, other.id
            for code, roles, status in (
                ("stock-pm", "*", "enabled"),
                ("admin-only", "admin", "enabled"),
                ("disabled", "*", "disabled"),
                ("position-entry-agent", "*", "enabled"),
            ):
                db.add(DigitalHumanProfile(
                    code=code, name=code, target_roles=roles, status=status,
                    description="test", system_prompt="test", quick_prompts_json="[]",
                ))
            db.add_all([
                ScheduledJob(job_key="system", name="System", owner_user_id=None, schedule_type="daily"),
                ScheduledJob(job_key="other", name="Other", owner_user_id=self.other, schedule_type="daily"),
            ])
            db.commit()

    def _create(self, **changes) -> dict:
        args = {
            "user_id": self.owner,
            "name": "收盘持仓研究",
            "instruction": "分析持仓和市场行情，说明减仓或持有的依据。",
            "target_agent_code": "stock-pm",
            "schedule": {"type": "daily", "run_time": "17:30"},
            "idempotency_key": "schedule-request-1",
            "trading_day_only": True,
        }
        return schedule_tools.create_schedule(**{**args, **changes})

    def test_create_is_enabled_owned_and_has_no_notification_or_code_permission(self) -> None:
        result = self._create()
        job = result["job"]
        self.assertTrue(result["created"])
        self.assertTrue(job["enabled"])
        self.assertEqual(job["timezone"], "Asia/Shanghai")
        self.assertTrue(job["created_at"].endswith("+08:00"))
        self.assertFalse(job["push_to_feishu"])
        self.assertFalse(job["allow_code_execution"])
        with self.sessions() as db:
            persisted = db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == job["job_key"]))
            self.assertEqual(persisted.owner_user_id, self.owner)
            self.assertIsNone(persisted.push_channel_key)
            self.assertEqual(persisted.timezone, "Asia/Shanghai")
            self.assertEqual(db.scalar(select(func.count()).select_from(McpMutationReceipt)), 1)

    def test_retries_return_original_receipt_and_changed_payload_is_rejected(self) -> None:
        created = self._create()
        self.assertEqual(created, self._create())
        with self.assertRaisesRegex(ValueError, "idempotency_key"):
            self._create(instruction="不同指令")
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(ScheduledJob).where(
                ScheduledJob.owner_user_id == self.owner,
            )), 1)

    def test_same_key_is_isolated_between_users(self) -> None:
        first = self._create()
        second = self._create(user_id=self.other)
        self.assertNotEqual(first["job"]["job_key"], second["job"]["job_key"])

    def test_list_and_get_exclude_system_and_other_user(self) -> None:
        created = self._create()
        jobs = schedule_tools.list_schedules(self.owner)
        self.assertEqual([job["job_key"] for job in jobs["jobs"]], [created["job"]["job_key"]])
        self.assertEqual(schedule_tools.get_schedule(self.owner, created["job"]["job_key"])["job"], created["job"])
        for key in ["system", "other"]:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "不存在"):
                schedule_tools.get_schedule(self.owner, key)
        with self.sessions() as db:
            row = db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == created["job"]["job_key"]))
            row.enabled = False
            db.commit()
        self.assertEqual(schedule_tools.list_schedules(self.owner, include_disabled=False)["jobs"], [])

    def test_profiles_and_create_enforce_access_and_forbidden_targets(self) -> None:
        self.assertEqual(schedule_tools.list_research_profiles(self.owner)["profiles"], [{"code": "stock-pm", "name": "stock-pm"}])
        for code in ["admin-only", "disabled", "position-entry-agent"]:
            with self.subTest(code=code), self.assertRaises((ValueError, PermissionError)):
                self._create(target_agent_code=code)
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(McpMutationReceipt)), 0)

    def test_failure_after_job_flush_rolls_back_job_and_receipt(self) -> None:
        with patch.object(AgentScheduleService, "_matches_create", return_value=False):
            with self.assertRaises(RuntimeError):
                self._create()
        self.assertEqual(schedule_tools.list_schedules(self.owner)["jobs"], [])
        with self.sessions() as db:
            self.assertEqual(db.scalar(select(func.count()).select_from(McpMutationReceipt)), 0)

    def test_external_transaction_remains_under_caller_control(self) -> None:
        service = AgentScheduleService(self.sessions)
        args = {
            "name": "事务测试", "instruction": "研究", "target_agent_code": "stock-pm",
            "schedule": {"type": "daily", "run_time": "10:00"},
        }
        with self.sessions() as db:
            result = service.create_job(user_id=self.owner, run_id=None, args=args, db=db, commit=False)
            self.assertTrue(result["created"])
            db.rollback()
        self.assertEqual(service.list_jobs(self.owner), [])
        with self.assertRaises(ValueError):
            service.create_job(user_id=self.owner, run_id=None, args=args, commit=False)

    def test_timezones_and_all_supported_schedule_types(self) -> None:
        run_at = (datetime.now(timezone.utc) + timedelta(days=2)).replace(microsecond=0)
        schedules = [
            {"type": "once", "run_at": run_at.isoformat()},
            {"type": "weekly", "run_time": "12:30", "weekdays": [5, 1, 3]},
            {"type": "monthly_last", "run_time": "17:30"},
            {"type": "interval", "interval_minutes": 30, "window_start": "09:30", "window_end": "15:00"},
        ]
        for index, schedule in enumerate(schedules):
            with self.subTest(schedule=schedule):
                result = self._create(name=f"task-{index}", schedule=schedule, idempotency_key=f"request-{index}")
                self.assertEqual(result["job"]["schedule_type"], schedule["type"])
                if schedule["type"] == "once":
                    self.assertEqual(datetime.fromisoformat(result["job"]["run_once_at"]), run_at)
                if schedule["type"] == "weekly":
                    self.assertEqual(result["job"]["weekday_mask"], "1,3,5")

    def test_strict_schedule_rejects_invalid_fields_and_values(self) -> None:
        bad = [
            {"type": "daily", "run_time": "25:30"},
            {"type": "daily", "run_time": "09:30", "owner_user_id": self.other},
            {"type": "daily", "run_time": "09:30", "push_to_feishu": True},
            {"type": "daily", "run_time": "09:30", "allow_code_execution": True},
            {"type": "daily", "run_time": "09:30", "timezone": "UTC"},
            {"type": "weekly", "run_time": "09:30", "weekdays": [True]},
            {"type": "weekly", "run_time": "09:30", "weekdays": [8]},
            {"type": "interval", "interval_minutes": 1.5},
            {"type": "interval", "interval_minutes": 0},
            {"type": "interval", "interval_minutes": 5, "window_start": "15:00", "window_end": "09:00"},
            {"type": "once", "run_at": "nonsense"},
        ]
        for schedule in bad:
            with self.subTest(schedule=schedule), self.assertRaises(ValidationError):
                self._create(schedule=schedule)
        with self.assertRaisesRegex(ValueError, "晚于当前时间"):
            self._create(schedule={"type": "once", "run_at": "2000-01-01T09:30:00+08:00"})


if __name__ == "__main__":
    unittest.main()
