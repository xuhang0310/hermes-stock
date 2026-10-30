"""Health-check windows use UTC rows while heartbeat/card times stay in Beijing."""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker

from backend.app.core.models import Base, Heartbeat, JobRun, SystemLog
from backend.app.scheduler import rhythm_scheduler


SHANGHAI = ZoneInfo("Asia/Shanghai")
BEIJING_NOW = datetime(2026, 10, 30, 8, 15, 5)
UTC_NOW = datetime(2026, 10, 30, 0, 15, 5)


class HealthCheckTimeWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(
            patch("pymysql.connections.Connection.connect", side_effect=AssertionError("unexpected MySQL connection"))
        )
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine)
        self.enterContext(patch.object(rhythm_scheduler, "SessionLocal", self.SessionFactory))
        self.send = self.enterContext(
            patch.object(
                rhythm_scheduler,
                "send_feishu",
                return_value=SimpleNamespace(id=42, status="sent", error=None),
            )
        )
        self.prune = self.enterContext(patch("backend.app.core.applog.prune_system_logs"))

    def _seed_rows(self, log_at: datetime, run_at: datetime) -> None:
        with self.SessionFactory() as db:
            db.execute(delete(SystemLog))
            db.execute(delete(JobRun))
            db.add(SystemLog(level="ERROR", module="test", message="test error", created_at=log_at))
            db.add(
                JobRun(
                    run_id="failed-test-run",
                    job_key="test-job",
                    fire_key="test-fire",
                    trigger_type="schedule",
                    status="failed",
                    started_at=run_at + timedelta(hours=8),
                    created_at=run_at,
                )
            )
            db.commit()

    def _check(self, now: datetime = BEIJING_NOW) -> str:
        self.send.reset_mock()
        self.prune.reset_mock()
        result = rhythm_scheduler._run_health_check(now, "test-channel", "health-test-run")
        self.prune.assert_called_once_with()
        if not self.send.called:
            self.assertEqual(result, ("健康检查通过，无异常", None, None))
            return ""
        self.send.assert_called_once()
        self.assertEqual(result[1:], (42, None))
        return self.send.call_args.args[3]

    def _assert_counts(self, body: str, count: int = 1) -> None:
        self.assertIn(f"近 12 小时有 {count} 条 ERROR 日志", body)
        self.assertIn(f"近 24 小时有 {count} 个定时任务执行失败", body)

    def test_oldest_eight_hours_of_both_windows_are_included(self) -> None:
        # 08:15 北京检查时，这两行在真实窗口内，但旧逻辑会把 11h/23h 前的数据漏掉。
        self._seed_rows(datetime(2026, 10, 29, 13, 15, 5), datetime(2026, 10, 29, 1, 15, 5))
        with self.SessionFactory() as db:
            db.add(SystemLog(level="INFO", module="test", message="not an error", created_at=UTC_NOW))
            db.add(
                JobRun(
                    run_id="successful-test-run",
                    job_key="test-job",
                    fire_key="successful-test-fire",
                    status="success",
                    started_at=BEIJING_NOW,
                    created_at=UTC_NOW,
                )
            )
            db.commit()

        self._assert_counts(self._check())

    def test_window_boundaries_and_future_rows(self) -> None:
        tick = timedelta(microseconds=1)
        for name, log_at, run_at, included in (
            ("before cutoff", UTC_NOW - timedelta(hours=12) - tick, UTC_NOW - timedelta(hours=24) - tick, False),
            ("at cutoff", UTC_NOW - timedelta(hours=12), UTC_NOW - timedelta(hours=24), True),
            ("after cutoff", UTC_NOW - timedelta(hours=12) + tick, UTC_NOW - timedelta(hours=24) + tick, True),
            ("at check time", UTC_NOW, UTC_NOW, True),
            ("after check time", UTC_NOW + tick, UTC_NOW + tick, False),
        ):
            with self.subTest(boundary=name):
                self._seed_rows(log_at, run_at)
                body = self._check()
                if included:
                    self._assert_counts(body)
                else:
                    self.assertEqual(body, "")

    def test_beijing_midnight_windows_cross_the_previous_utc_date(self) -> None:
        # 北京 11 月 1 日 00:15 = UTC 10 月 31 日 16:15，两个下界也跨日期。
        self._seed_rows(datetime(2026, 10, 31, 4, 15, 5), datetime(2026, 10, 30, 16, 15, 5))

        self._assert_counts(self._check(datetime(2026, 11, 1, 0, 15, 5)))
        self.assertIn(("检查时间", "2026-11-01 00:15:05"), self.send.call_args.kwargs["card_metadata"])

    def test_naive_beijing_and_aware_inputs_keep_identical_windows_and_local_times(self) -> None:
        self._seed_rows(UTC_NOW - timedelta(hours=11), UTC_NOW - timedelta(hours=23))
        with self.SessionFactory() as db:
            db.add_all(
                [
                    Heartbeat(component="fresh-worker", status="ok", last_seen_at=BEIJING_NOW - timedelta(minutes=1)),
                    Heartbeat(component="boundary-worker", status="ok", last_seen_at=BEIJING_NOW - timedelta(minutes=30)),
                    Heartbeat(component="stale-worker", status="ok", last_seen_at=BEIJING_NOW - timedelta(minutes=30, seconds=1)),
                ]
            )
            db.commit()

        bodies = []
        for now in (
            BEIJING_NOW,
            BEIJING_NOW.replace(tzinfo=SHANGHAI),
            UTC_NOW.replace(tzinfo=timezone.utc),
            datetime(2026, 10, 29, 19, 15, 5, tzinfo=timezone(timedelta(hours=-5))),
        ):
            with self.subTest(now=now.isoformat()):
                body = self._check(now)
                bodies.append(body)
                self._assert_counts(body)
                self.assertIn("组件心跳超时: stale-worker", body)
                self.assertNotIn("fresh-worker", body)
                self.assertNotIn("boundary-worker", body)
                self.assertIn(("检查时间", "2026-10-30 08:15:05"), self.send.call_args.kwargs["card_metadata"])
                with self.SessionFactory() as db:
                    heartbeat = db.get(Heartbeat, rhythm_scheduler.HEARTBEAT_COMPONENT)
                    self.assertEqual(heartbeat.last_seen_at, BEIJING_NOW)
                    self.assertEqual(json.loads(heartbeat.detail_json)["checked_at"], BEIJING_NOW.isoformat())
        self.assertTrue(all(body == bodies[0] for body in bodies))


if __name__ == "__main__":
    unittest.main()
