"""给用户开通默认任务，和多个用户的报告怎么排队。内存 SQLite，不连 MySQL。"""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.core.models import Base, JobRun, PositionAccount, PositionHoldingSnapshot, ScheduledJob, User
from backend.app.feishu.feishu import SCHEDULE_CHANNEL_KEY
from backend.app.plans.rules import plan_source_for_job
from backend.app.scheduler import rhythm_scheduler, user_jobs

ADMIN, ZHANG, WANG = 1, 8, 9


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.addCleanup(self.engine.dispose)
        with self.Session() as db:
            for user_id, role in ((ADMIN, "super_admin"), (ZHANG, "researcher"), (WANG, "researcher")):
                db.add(User(id=user_id, username=f"u{user_id}", display_name=f"u{user_id}", password_hash="x", role=role, is_enabled=True))
            db.add(PositionAccount(id=3, owner_user_id=ZHANG, broker_name="财信证券", available_cash=0.0, status="active"))
            db.add(PositionHoldingSnapshot(owner_user_id=ZHANG, account_id=3, symbol="003011.SZ", symbol_name="海象新材", position_quantity=200, source_type="manual", status="open"))
            for key, name, run_time, order, enabled, prompt in (
                ("agent-1-old", "早盘挂单清单", "09:00", 5, False, "旧的"),
                ("agent-1-morning", "早盘挂单清单", "08:40", 10, True, "三个账户分开写，今天是{date}"),
                ("agent-1-noon", "午盘挂单调整", "12:10", 30, True, "三个账户分开写"),
                ("evening-daily-2200", "晚间日报与实盘复盘", "22:05", 80, True, "晚间"),
                ("selection-action-plan-2000", "全自动选股", "20:30", 60, True, "选股"),
            ):
                db.add(ScheduledJob(job_key=key, name=name, job_kind="agent_report", schedule_type="daily", run_time=run_time, weekday_mask="1,2,3,4,5", trading_day_only=True, digital_human_code="stock-pm", prompt_template=prompt, push_channel_key=SCHEDULE_CHANNEL_KEY, owner_user_id=ADMIN, enabled=enabled, sort_order=order, delegation_mode="none"))
            db.add(ScheduledJob(job_key="agent-1-intraday", name="上午盘中短提醒", job_kind="agent_report", schedule_type="interval", interval_minutes=30, owner_user_id=ADMIN, enabled=True))
            db.add(ScheduledJob(job_key="message-watch-15m", name="持仓消息盯盘(每15分)", job_kind="message_watch", schedule_type="interval", interval_minutes=15, window_start="00:03", window_end="23:59", trading_day_only=False, owner_user_id=ADMIN, enabled=True, push_channel_key=SCHEDULE_CHANNEL_KEY))
            db.add(ScheduledJob(job_key="position-price-sync-1505", name="持仓价格分钟同步", job_kind="position_price_sync", schedule_type="interval", interval_minutes=1, enabled=True))
            db.commit()

    def _open(self, user_id: int = ZHANG) -> list[str]:
        with self.Session() as db:
            return user_jobs.open_default_jobs(db, user_id)

    def _job(self, kind: str, user_id: int = ZHANG) -> ScheduledJob | None:
        with self.Session() as db:
            return db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == user_jobs.job_key_for(user_id, kind)))


class OpenJobsTests(_Case):
    def test_opens_three_plans_and_message_watch(self) -> None:
        self.assertEqual(set(self._open()), {"早盘挂单清单", "午盘挂单调整", "晚间日报与实盘复盘", "持仓消息盯盘(每15分)"})

    def test_expensive_and_system_jobs_are_not_copied(self) -> None:
        self._open()
        with self.Session() as db:
            names = set(db.scalars(select(ScheduledJob.name).where(ScheduledJob.owner_user_id == ZHANG)).all())
        self.assertNotIn("全自动选股", names)
        self.assertNotIn("上午盘中短提醒", names)
        self.assertNotIn("持仓价格分钟同步", names)

    def test_the_job_belongs_to_the_user_and_pushes(self) -> None:
        self._open()
        job = self._job("evening")
        self.assertEqual((job.owner_user_id, job.push_channel_key, job.created_via, job.enabled), (ZHANG, SCHEDULE_CHANNEL_KEY, "default", True))
        self.assertEqual(job.delegation_mode, "none")

    def test_the_copy_uses_the_enabled_template_at_its_time(self) -> None:
        self._open()
        self.assertEqual(self._job("premarket").run_time, "08:40")

    def test_the_prompt_is_not_tied_to_the_admins_three_accounts(self) -> None:
        self._open()
        self.assertEqual(self._job("premarket").prompt_template, "每个账户分开写，今天是{date}")

    def test_the_copies_still_count_as_plan_jobs(self) -> None:
        self._open()
        for kind in user_jobs.REPORT_SOURCES:
            job = self._job(kind)
            self.assertEqual(plan_source_for_job(job.job_key, job.name), kind)

    def test_message_watch_keeps_its_kind_and_window(self) -> None:
        self._open()
        job = self._job(user_jobs.MESSAGE_WATCH)
        self.assertEqual((job.job_kind, job.interval_minutes, job.window_start), ("message_watch", 15, "00:03"))

    def test_opening_twice_does_not_touch_what_the_user_changed(self) -> None:
        self._open()
        with self.Session() as db:
            job = db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == user_jobs.job_key_for(ZHANG, "evening")))
            job.run_time, job.enabled = "21:00", False
            db.commit()
        self.assertEqual(self._open(), [])
        self.assertEqual((self._job("evening").run_time, self._job("evening").enabled), ("21:00", False))

    def test_no_holdings_cannot_open(self) -> None:
        with self.assertRaisesRegex(ValueError, "先录入持仓"):
            self._open(WANG)
        self.assertIsNone(self._job("evening", WANG))

    def test_the_system_owner_already_has_them(self) -> None:
        with self.assertRaisesRegex(ValueError, "已经有"):
            self._open(ADMIN)
        with self.Session() as db:
            self.assertEqual(user_jobs.default_jobs_status(db, ADMIN), {"available": False, "jobs": []})

    def test_status_before_and_after(self) -> None:
        with self.Session() as db:
            before = user_jobs.default_jobs_status(db, ZHANG)
        self.assertTrue(before["has_holdings"])
        self.assertEqual(len(before["jobs"]), 4)
        self.assertFalse(any(job["opened"] for job in before["jobs"]))
        self._open()
        with self.Session() as db:
            after = user_jobs.default_jobs_status(db, ZHANG)
        self.assertTrue(all(job["opened"] and job["enabled"] for job in after["jobs"]))

    def test_the_run_is_recorded_under_the_user(self) -> None:
        self._open()
        with patch.object(rhythm_scheduler, "SessionLocal", self.Session), patch.object(rhythm_scheduler, "is_trading_day", return_value=True):
            claimed = rhythm_scheduler._claim_due_jobs(datetime(2026, 11, 6, 22, 5), 5, 5)
        keys = {key for key, _ in claimed}
        self.assertIn(user_jobs.job_key_for(ZHANG, "evening"), keys)
        with self.Session() as db:
            run = db.scalar(select(JobRun).where(JobRun.job_key == user_jobs.job_key_for(ZHANG, "evening")))
        self.assertEqual(run.owner_user_id, ZHANG)


class QueueTests(_Case):
    """22:05 三个用户的晚间日报同时到点，报告名额只有 2 个。"""

    def setUp(self) -> None:
        super().setUp()
        self._open(ZHANG)
        with self.Session() as db:
            db.add(PositionAccount(id=4, owner_user_id=WANG, broker_name="方正证券", available_cash=0.0, status="active"))
            db.add(PositionHoldingSnapshot(owner_user_id=WANG, account_id=4, symbol="003011.SZ", symbol_name="海象新材", position_quantity=200, source_type="manual", status="open"))
            # 20:30 的选股报告这里算已经跑过，不然它也在 22:05 的排队窗口里
            db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == "selection-action-plan-2000")).enabled = False
            db.commit()
        self._open(WANG)
        patcher = patch.object(rhythm_scheduler, "is_trading_day", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _claim(self, hour: int, minute: int, others: int, reports: int) -> list[str]:
        with patch.object(rhythm_scheduler, "SessionLocal", self.Session):
            return [key for key, _ in rhythm_scheduler._claim_due_jobs(datetime(2026, 11, 6, hour, minute), others, reports)]

    def _evenings(self, keys: list[str]) -> list[str]:
        return [key for key in keys if "evening" in key]

    def test_two_reports_start_and_the_third_waits(self) -> None:
        self.assertEqual(len(self._evenings(self._claim(22, 5, 2, 2))), 2)

    def test_the_third_runs_when_a_slot_frees_up(self) -> None:
        self._claim(22, 5, 2, 2)
        self.assertEqual(self._evenings(self._claim(22, 40, 2, 1)), [user_jobs.job_key_for(WANG, "evening")])  # 管理员和张三先跑

    def test_each_report_only_runs_once(self) -> None:
        self._claim(22, 5, 2, 2)
        self._claim(22, 40, 2, 1)
        self.assertEqual(self._evenings(self._claim(22, 50, 2, 2)), [])

    def test_a_full_report_pool_does_not_block_system_jobs(self) -> None:
        keys = self._claim(22, 18, 2, 0)
        self.assertIn("message-watch-15m", keys)
        self.assertEqual(self._evenings(keys), [])

    def test_full_system_slots_do_not_block_reports(self) -> None:
        self.assertEqual(len(self._evenings(self._claim(22, 5, 0, 2))), 2)

    def test_the_wait_ends_two_hours_after_the_scheduled_time(self) -> None:
        job = ScheduledJob(job_key="evening", job_kind="agent_report", schedule_type="daily", run_time="20:00", trading_day_only=False)
        self.assertTrue(rhythm_scheduler._is_due(job, datetime(2026, 11, 5, 22, 0)))
        self.assertFalse(rhythm_scheduler._is_due(job, datetime(2026, 11, 5, 22, 1)))


if __name__ == "__main__":
    unittest.main()
