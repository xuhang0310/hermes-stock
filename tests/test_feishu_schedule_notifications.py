"""Regression coverage for the single Feishu schedule bot and card delivery."""
from __future__ import annotations

import unittest
from datetime import datetime
from unittest.mock import patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.models import Base, JobRun, NotifyChannel, ScheduledJob
from backend.app.feishu.feishu import (
    BUILTIN_CHANNEL_KEYS,
    SCHEDULE_CHANNEL_KEY,
    _build_payload,
    _gen_sign,
)
from backend.app.scheduler import rhythm_scheduler
from backend.app.scheduler.rhythm_api import _activate_agent_report_jobs, _looks_like_utf8_mojibake
from backend.app.seed.seed import seed_notify_channels


class FeishuCardTests(unittest.TestCase):
    def test_signature_matches_feishu_java_example_algorithm(self) -> None:
        self.assertEqual(
            _gen_sign("demo", 1599360473),
            "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8=",
        )

    def test_payload_is_signed_interactive_card_with_metadata_and_footer(self) -> None:
        with patch("backend.app.feishu.feishu.time.time", return_value=1599360473):
            payload = _build_payload(
                "盘前简报 | 2026-07-21",
                "**核心结论**\n今日关注风险与机会。",
                "demo",
                "schedule",
                metadata=[("任务", "盘前简报"), ("数字人", "情报官")],
                footer="SixRabbits Stock | 自动任务",
                template="wathet",
            )

        self.assertEqual(payload["msg_type"], "interactive")
        self.assertEqual(payload["timestamp"], "1599360473")
        self.assertEqual(payload["sign"], "l1N0gAcBjdwBvGm1xMjOF0XSyaLRpR7tuO5dHfhAYc8=")
        card = payload["card"]
        self.assertTrue(card["config"]["wide_screen_mode"])
        self.assertTrue(card["config"]["enable_forward"])
        self.assertEqual(card["header"]["template"], "wathet")
        self.assertEqual([element["tag"] for element in card["elements"]], ["div", "hr", "markdown", "hr", "note"])
        self.assertEqual(card["elements"][0]["fields"][0]["text"]["tag"], "lark_md")
        self.assertIn("核心结论", card["elements"][2]["content"])
        self.assertEqual(card["elements"][-1]["elements"][0]["content"], "SixRabbits Stock | 自动任务")

    def test_only_one_builtin_channel_is_exposed(self) -> None:
        self.assertEqual(BUILTIN_CHANNEL_KEYS, (SCHEDULE_CHANNEL_KEY,))


class FeishuChannelMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_legacy_channel_and_jobs_migrate_to_single_schedule_channel(self) -> None:
        self.db.add(
            NotifyChannel(
                channel_key="feishu-alert",
                name="旧告警群",
                purpose="alert",
                channel_type="feishu_webhook",
                webhook_url_cipher="encrypted-url",
                secret_cipher="encrypted-secret",
                status="enabled",
            )
        )
        self.db.add_all(
            [
                ScheduledJob(
                    job_key="legacy-job",
                    name="旧任务",
                    job_kind="agent_report",
                    schedule_type="daily",
                    push_channel_key="feishu-alert",
                ),
                ScheduledJob(
                    job_key="health-check-0815",
                    name="健康检查",
                    job_kind="health_check",
                    schedule_type="daily",
                    push_channel_key=None,
                ),
            ]
        )
        self.db.commit()

        seed_notify_channels(self.db)
        self.db.commit()

        channel = self.db.scalar(select(NotifyChannel).where(NotifyChannel.channel_key == SCHEDULE_CHANNEL_KEY))
        legacy = self.db.scalar(select(NotifyChannel).where(NotifyChannel.channel_key == "feishu-alert"))
        jobs = self.db.scalars(select(ScheduledJob).order_by(ScheduledJob.job_key)).all()
        self.assertEqual(channel.webhook_url_cipher, "encrypted-url")
        self.assertEqual(channel.secret_cipher, "encrypted-secret")
        self.assertEqual(channel.status, "enabled")
        self.assertEqual(legacy.status, "disabled")
        self.assertEqual({job.push_channel_key for job in jobs}, {SCHEDULE_CHANNEL_KEY})


class SchedulerDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine)
        with self.SessionFactory() as db:
            db.add(
                ScheduledJob(
                    job_key="report-job",
                    name="测试报告",
                    job_kind="agent_report",
                    schedule_type="daily",
                    digital_human_code="stock-pm",
                    push_channel_key=SCHEDULE_CHANNEL_KEY,
                )
            )
            db.add(
                JobRun(
                    run_id="run-1",
                    job_key="report-job",
                    fire_key="report-job@manual-run-1",
                    trigger_type="manual",
                    status="running",
                    started_at=datetime(2026, 7, 20, 20, 0, 0),
                )
            )
            db.commit()

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_push_failure_marks_run_failed_but_keeps_report_audit_ids(self) -> None:
        with (
            patch.object(rhythm_scheduler, "SessionLocal", self.SessionFactory),
            patch.object(
                rhythm_scheduler,
                "_run_agent_report",
                return_value=("report-1", 17, "报告正文预览", "签名校验失败"),
            ),
        ):
            rhythm_scheduler._execute_job("report-job", "run-1")

        with self.SessionFactory() as db:
            run = db.scalar(select(JobRun).where(JobRun.run_id == "run-1"))
            job = db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == "report-job"))
            self.assertEqual(run.status, "failed")
            self.assertEqual(run.report_id, "report-1")
            self.assertEqual(run.notification_id, 17)
            self.assertIn("签名校验失败", run.error)
            self.assertEqual(job.last_status, "failed")


class ScheduleAdminSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)

    def tearDown(self) -> None:
        self.db.close()
        self.engine.dispose()

    def test_detects_double_decoded_chinese_without_rejecting_valid_text(self) -> None:
        mojibake = "盘前简报".encode("utf-8").decode("latin-1")

        self.assertTrue(_looks_like_utf8_mojibake(mojibake))
        self.assertFalse(_looks_like_utf8_mojibake("盘前简报"))
        self.assertFalse(_looks_like_utf8_mojibake("Premarket brief"))

    def test_bulk_activation_does_not_round_trip_editable_text(self) -> None:
        report = ScheduledJob(
            job_key="report-job",
            name="用户自定义报告",
            description="保留这段说明",
            prompt_template="保留这段提示词",
            job_kind="agent_report",
            schedule_type="daily",
            enabled=False,
            push_channel_key=None,
        )
        system_job = ScheduledJob(
            job_key="system-job",
            name="系统任务",
            job_kind="health_check",
            schedule_type="daily",
            enabled=False,
            push_channel_key=None,
        )
        self.db.add_all([report, system_job])
        self.db.commit()

        updated = _activate_agent_report_jobs(self.db)

        self.assertEqual([job.job_key for job in updated], ["report-job"])
        self.assertTrue(report.enabled)
        self.assertEqual(report.push_channel_key, SCHEDULE_CHANNEL_KEY)
        self.assertEqual(report.name, "用户自定义报告")
        self.assertEqual(report.description, "保留这段说明")
        self.assertEqual(report.prompt_template, "保留这段提示词")
        self.assertFalse(system_job.enabled)
        self.assertIsNone(system_job.push_channel_key)


if __name__ == "__main__":
    unittest.main()
