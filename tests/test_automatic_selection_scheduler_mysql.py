from __future__ import annotations

import json
import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.core.database import Base
from backend.app.core.models import InboxNotification, JobRun, PaperOrder, ScheduledJob, User
from backend.app.notify import notification_service
from backend.app.paper import paper_service
from backend.app.quant import automatic_selection_service
from backend.app.scheduler import rhythm_scheduler
from tests.automatic_selection_mysql import (
    create_isolated_mysql_engine,
    reset_isolated_schema,
)


class AutomaticSelectionSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_isolated_mysql_engine()
        reset_isolated_schema(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.scheduler_patch = patch.object(
            rhythm_scheduler,
            "SessionLocal",
            self.SessionFactory,
        )
        self.notification_patch = patch.object(
            notification_service,
            "SessionLocal",
            self.SessionFactory,
        )
        self.scheduler_patch.start()
        self.notification_patch.start()
        self.addCleanup(self.scheduler_patch.stop)
        self.addCleanup(self.notification_patch.stop)
        with self.SessionFactory() as db:
            user = User(
                username="admin",
                display_name="管理员",
                role="super_admin",
                password_hash="test",
                is_enabled=True,
            )
            db.add(user)
            db.commit()
            self.user_id = user.id

    def tearDown(self) -> None:
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()

    def test_default_job_runs_at_1610_on_trading_weekdays(self) -> None:
        config = next(
            item
            for item in rhythm_scheduler.DEFAULT_JOBS
            if item["job_key"] == rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY
        )
        self.assertEqual(config["run_time"], "16:10")
        self.assertEqual(config["weekday_mask"], "1,2,3,4,5")
        self.assertTrue(config["trading_day_only"])
        self.assertEqual(config["job_kind"], "automatic_selection")
        self.assertTrue(config["enabled"])

        with self.SessionFactory() as db:
            rhythm_scheduler.ensure_default_jobs(db)
            db.commit()
            job = db.scalar(
                select(ScheduledJob).where(
                    ScheduledJob.job_key == rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY
                )
            )
            self.assertEqual(job.owner_user_id, self.user_id)
            self.assertTrue(
                rhythm_scheduler._is_due(job, datetime(2026, 9, 7, 16, 10))
            )
            self.assertFalse(
                rhythm_scheduler._is_due(job, datetime(2026, 9, 7, 16, 9))
            )

    def test_executor_persists_summary_notifies_owner_and_never_places_orders(self) -> None:
        with self.SessionFactory() as db:
            db.add(
                ScheduledJob(
                    job_key=rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
                    name="全自动选股",
                    job_kind="automatic_selection",
                    schedule_type="daily",
                    run_time="16:10",
                    weekday_mask="1,2,3,4,5",
                    trading_day_only=True,
                    owner_user_id=self.user_id,
                    enabled=True,
                )
            )
            db.add(
                JobRun(
                    run_id="job-run-1",
                    job_key=rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
                    owner_user_id=self.user_id,
                    fire_key="auto@2026-09-07T16:10",
                    trigger_type="schedule",
                    status="running",
                    started_at=datetime(2026, 9, 7, 16, 10),
                )
            )
            db.commit()
        cycle_result = {
            "target_trade_date": "20260907",
            "strategy_count": 3,
            "completed_count": 2,
            "blocked_count": 1,
            "failed_count": 0,
            "selected_count": 60,
            "evaluation_updates": {
                "updated": 6,
                "completed": 2,
                "insufficient_data": 1,
                "pending": 3,
            },
        }

        with (
            patch.object(
                automatic_selection_service,
                "run_automatic_selection_cycle",
                return_value=cycle_result,
            ) as run_cycle,
            patch.object(paper_service, "place_order") as place_order,
        ):
            rhythm_scheduler._execute_job(
                rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
                "job-run-1",
            )

        run_cycle.assert_called_once_with(
            trigger_type="schedule",
            job_run_id="job-run-1",
        )
        place_order.assert_not_called()
        with self.SessionFactory() as db:
            run = db.scalar(select(JobRun).where(JobRun.run_id == "job-run-1"))
            self.assertEqual(run.status, "success")
            summary = json.loads(run.output_content)
            self.assertEqual(summary["target_trade_date"], "20260907")
            self.assertEqual(summary["completed_count"], 2)
            self.assertEqual(summary["blocked_count"], 1)
            self.assertEqual(summary["selected_count"], 60)
            self.assertEqual(summary["evaluation_updates"]["completed"], 2)
            notification = db.get(InboxNotification, run.notification_id)
            self.assertEqual(notification.level, "warning")
            self.assertEqual(notification.category, "automatic_selection")
            self.assertEqual(notification.link, "/automatic-selection")
            self.assertEqual(db.scalar(select(PaperOrder.id).limit(1)), None)


if __name__ == "__main__":
    unittest.main()
