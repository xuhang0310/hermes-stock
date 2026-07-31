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
    _build_payloads,
    encrypt_value,
    _gen_sign,
    send_feishu,
)
from backend.app.scheduler import rhythm_scheduler
from backend.app.scheduler.rhythm_api import (
    _activate_agent_report_jobs,
    clear_schedule_runs,
    _looks_like_utf8_mojibake,
    schedule_jobs_paged,
    schedule_runs_paged,
)
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
        self.assertEqual(card["schema"], "2.0")
        self.assertTrue(card["config"]["wide_screen_mode"])
        self.assertTrue(card["config"]["enable_forward"])
        self.assertEqual(card["header"]["template"], "wathet")
        elements = card["body"]["elements"]
        self.assertEqual(
            [element["tag"] for element in elements],
            ["markdown", "hr", "markdown", "hr", "markdown"],
        )
        self.assertIn("| 项目 | 内容 |", elements[0]["content"])
        self.assertIn("核心结论", elements[2]["content"])
        self.assertEqual(elements[-1]["content"], "SixRabbits Stock | 自动任务")

    def test_json_2_card_linearizes_tables_and_continues_long_reports(self) -> None:
        table = "| 指标 | 数值 |\n| --- | --- |\n| 总收益 | +12.3% |"
        section = "## 分析章节\n\n" + ("这是完整报告内容。" * 500)
        content = table + "\n\n" + "\n\n".join(section for _ in range(12)) + "\n\nEND-OF-REPORT"

        payloads = _build_payloads("长报告", content, None, "schedule")

        self.assertGreater(len(payloads), 1)
        self.assertTrue(all(payload["card"]["schema"] == "2.0" for payload in payloads))
        rendered = "\n".join(
            element["content"]
            for payload in payloads
            for element in payload["card"]["body"]["elements"]
            if element["tag"] == "markdown" and element["element_id"].startswith("content_")
        )
        self.assertNotIn(table, rendered)
        self.assertIn("**总收益**", rendered)
        self.assertIn("+12.3%", rendered)
        self.assertIn("END-OF-REPORT", rendered)
        self.assertNotIn("内容过长，已截断", rendered)

    def test_table_dense_report_is_mobile_safe_and_keeps_every_row(self) -> None:
        tables = [
            f"## 表格 {index}\n\n| 指标 | 数值 |\n| --- | --- |\n| 项目 {index} | {index} |"
            for index in range(11)
        ]

        payloads = _build_payloads(
            "多表格报告",
            "\n\n".join(tables),
            None,
            "schedule",
            metadata=[("任务", "盘前简报"), ("数字人", "情报官")],
        )

        rendered = []
        for payload in payloads:
            markdown = [
                element["content"]
                for element in payload["card"]["body"]["elements"]
                if element["tag"] == "markdown"
            ]
            table_count = sum(
                1
                for content in markdown
                for line in content.splitlines()
                if line.strip().startswith("| ---")
            )
            self.assertLessEqual(table_count, 5)
            rendered.extend(markdown)

        joined = "\n".join(rendered)
        content_joined = "\n".join(
            element["content"]
            for payload in payloads
            for element in payload["card"]["body"]["elements"]
            if element["tag"] == "markdown" and element["element_id"].startswith("content_")
        )
        self.assertNotIn("| --- | --- |", content_joined)
        for index in range(11):
            self.assertIn(f"项目 {index}", joined)

    def test_long_table_cell_is_not_clipped_or_discarded(self) -> None:
        detail = "放量反弹是积极信号，但需要继续观察量能、公告与关键支撑位。" * 12
        content = f"| 维度 | 内容 |\n| --- | --- |\n| 推断 | {detail} |"

        payloads = _build_payloads("移动端长单元格", content, None, "schedule")
        rendered = "\n".join(
            element["content"]
            for payload in payloads
            for element in payload["card"]["body"]["elements"]
            if element["tag"] == "markdown" and element["element_id"].startswith("content_")
        )

        self.assertIn("**推断**", rendered)
        self.assertIn(detail, rendered)
        self.assertNotIn("| --- |", rendered)

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

    def test_schedule_jobs_and_runs_support_stable_pagination(self) -> None:
        for index in range(11):
            self.db.add(
                ScheduledJob(
                    job_key=f"job-{index:02d}",
                    name=f"任务 {index:02d}",
                    job_kind="agent_task",
                    schedule_type="daily",
                    enabled=index % 2 == 0,
                    sort_order=index,
                )
            )
        for index in range(25):
            self.db.add(
                JobRun(
                    run_id=f"run-{index:02d}",
                    job_key=f"job-{index % 3:02d}",
                    fire_key=f"fire-{index:02d}",
                    trigger_type="schedule",
                    status="success" if index % 2 == 0 else "failed",
                    started_at=datetime(2026, 7, 23, 8, index),
                )
            )
        self.db.commit()

        jobs_page = schedule_jobs_paged(page=2, page_size=4, enabled=None, keyword=None, db=self.db)
        self.assertEqual(jobs_page["total"], 11)
        self.assertEqual(jobs_page["pages"], 3)
        self.assertEqual(jobs_page["enabled_total"], 6)
        self.assertEqual([item["job_key"] for item in jobs_page["items"]], ["job-04", "job-05", "job-06", "job-07"])

        runs_page = schedule_runs_paged(
            page=2,
            page_size=5,
            job_key="job-00",
            status=None,
            db=self.db,
        )
        self.assertEqual(runs_page["total"], 9)
        self.assertEqual(runs_page["pages"], 2)
        self.assertEqual(len(runs_page["items"]), 4)
        self.assertTrue(all(item["job_name"] == "任务 00" for item in runs_page["items"]))

        cleared = clear_schedule_runs(job_key="job-00", status=None, db=self.db)
        self.assertEqual(cleared["deleted"], 9)
        self.assertEqual(cleared["skipped_running"], 0)

        self.db.add(
            JobRun(
                run_id="run-active",
                job_key="job-01",
                fire_key="fire-active",
                trigger_type="manual",
                status="running",
                started_at=datetime(2026, 7, 23, 9, 0),
            )
        )
        self.db.commit()
        cleared_all = clear_schedule_runs(job_key=None, status=None, db=self.db)
        self.assertEqual(cleared_all["deleted"], 16)
        self.assertEqual(cleared_all["skipped_running"], 1)
        self.assertIsNotNone(
            self.db.scalar(select(JobRun).where(JobRun.run_id == "run-active"))
        )

    def test_long_delivery_posts_every_card_before_marking_notification_sent(self) -> None:
        self.db.add(
            NotifyChannel(
                channel_key="long-report",
                name="长报告",
                purpose="schedule",
                channel_type="feishu_webhook",
                webhook_url_cipher=encrypt_value(
                    "https://open.feishu.cn/open-apis/bot/v2/hook/test-long-report"
                ),
                status="enabled",
            )
        )
        self.db.commit()
        content = "\n\n".join("## 章节\n" + ("完整正文" * 900) for _ in range(8))
        expected_pages = len(_build_payloads("长报告", content, None, "schedule"))

        with (
            patch("backend.app.feishu.feishu._acquire_rate_slots", return_value=True),
            patch("backend.app.feishu.feishu._post_with_retry", return_value=(True, 200, "")) as post,
        ):
            notification = send_feishu(self.db, "long-report", "长报告", content)

        self.assertGreater(expected_pages, 1)
        self.assertEqual(post.call_count, expected_pages)
        self.assertEqual(notification.status, "sent")

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

    def test_due_job_claiming_never_exceeds_available_capacity(self) -> None:
        with self.SessionFactory() as db:
            db.add_all(
                [
                    ScheduledJob(
                        job_key=f"due-job-{index}",
                        name=f"到期任务 {index}",
                        job_kind="agent_report",
                        schedule_type="daily",
                        run_time="08:30",
                        enabled=True,
                        sort_order=index,
                    )
                    for index in range(3)
                ]
            )
            db.commit()

        with patch.object(rhythm_scheduler, "SessionLocal", self.SessionFactory):
            claimed = rhythm_scheduler._claim_due_jobs(datetime(2026, 7, 31, 8, 30), limit=1)

        self.assertEqual(len(claimed), 1)
        with self.SessionFactory() as db:
            running = db.scalars(select(JobRun).where(JobRun.status == "running")).all()
            # One fixture run already exists; only one due job may be claimed.
            self.assertEqual(len(running), 2)


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
