from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from backend.app.core.models import JobRun, ScheduledJob
from backend.app.scheduler import rhythm_scheduler


class AutomaticSelectionSchedulerTests(unittest.TestCase):
    @staticmethod
    def _job(
        job_key: str,
        job_kind: str,
        run_time: str,
        *,
        owner_user_id: int = 7,
    ) -> ScheduledJob:
        return ScheduledJob(
            job_key=job_key,
            name=job_key,
            job_kind=job_kind,
            schedule_type="daily",
            run_time=run_time,
            weekday_mask="1,2,3,4,5",
            trading_day_only=True,
            owner_user_id=owner_user_id,
            enabled=True,
        )

    def test_built_in_jobs_have_ordered_post_close_times(self) -> None:
        configs = {
            item["job_key"]: item
            for item in rhythm_scheduler.DEFAULT_JOBS
            if item["job_key"]
            in {
                rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
                rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
            }
        }

        data = configs[rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY]
        ranking = configs[rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY]
        # TuShare 当天数据 17:00 前后才发齐：入库在其后，排名再其后。
        self.assertEqual(data["run_time"], "17:30")
        self.assertEqual(data["job_kind"], "automatic_selection_data")
        self.assertEqual(ranking["run_time"], "17:50")
        self.assertLess(data["run_time"], ranking["run_time"])
        self.assertEqual(ranking["job_kind"], "automatic_selection")
        for config in configs.values():
            self.assertEqual(config["schedule_type"], "daily")
            self.assertEqual(config["weekday_mask"], "1,2,3,4,5")
            self.assertTrue(config["trading_day_only"])
            self.assertTrue(config["enabled"])

    def test_only_automatic_selection_jobs_get_same_day_catchup(self) -> None:
        ranking = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
            "automatic_selection",
            "16:10",
        )
        ordinary = self._job("ordinary-daily", "agent_report", "16:10")

        with patch.object(
            rhythm_scheduler,
            "is_trading_day",
            return_value=True,
        ):
            self.assertFalse(
                rhythm_scheduler._is_due(
                    ranking,
                    datetime(2026, 9, 7, 16, 9),
                )
            )
            self.assertTrue(
                rhythm_scheduler._is_due(
                    ranking,
                    datetime(2026, 9, 7, 16, 10),
                )
            )
            self.assertTrue(
                rhythm_scheduler._is_due(
                    ranking,
                    datetime(2026, 9, 7, 18, 30),
                )
            )
            self.assertFalse(
                rhythm_scheduler._is_due(
                    ordinary,
                    datetime(2026, 9, 7, 18, 30),
                )
            )

    def test_catchup_fire_key_uses_canonical_configured_time(self) -> None:
        ranking_job = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
            "automatic_selection",
            "17:50",
        )
        fire_key = rhythm_scheduler._scheduled_fire_key(
            ranking_job,
            datetime(2026, 9, 7, 20, 30),
        )

        self.assertEqual(
            fire_key,
            f"{rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY}"
            "@2026-09-07T17:50",
        )

    def test_data_job_gets_one_retry_slot_every_thirty_minutes_until_cutoff(self) -> None:
        data_job = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
            "automatic_selection_data",
            "17:30",
        )
        prefix = f"{data_job.job_key}@2026-09-07T17:30"
        expectations = {
            (17, 30): prefix,
            (17, 59): prefix,
            (18, 0): f"{prefix}~retry1",
            (21, 59): f"{prefix}~retry8",
            (22, 0): f"{prefix}~retry9",
            (23, 40): f"{prefix}~retry9",
        }
        for (hour, minute), expected in expectations.items():
            with self.subTest(at=f"{hour}:{minute}"):
                self.assertEqual(
                    rhythm_scheduler._scheduled_fire_key(data_job, datetime(2026, 9, 7, hour, minute)),
                    expected,
                )

    def test_data_retry_stops_once_current_target_is_ingested(self) -> None:
        data_job = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
            "automatic_selection_data",
            "17:30",
        )
        session = MagicMock()
        with (
            patch.object(rhythm_scheduler, "_lock_claim_group", return_value={data_job.job_key: data_job}),
            patch.object(rhythm_scheduler, "_find_running_claim", return_value=None),
            patch.object(rhythm_scheduler, "is_trading_day", return_value=True),
            patch.object(rhythm_scheduler, "_scheduled_automatic_selection_data_ready", return_value=True),
        ):
            result = rhythm_scheduler._claim_job_run(
                session,
                data_job.job_key,
                trigger_type="schedule",
                ran_at=datetime(2026, 9, 7, 18, 5),
            )

        self.assertEqual(result, {"status": "not_due", "run_id": None})
        session.add.assert_not_called()

    def test_both_automatic_jobs_use_one_deterministically_locked_group(self) -> None:
        expected = tuple(
            sorted(
                (
                    rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
                    rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
                )
            )
        )
        self.assertEqual(
            rhythm_scheduler._claim_group_job_keys(
                rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY
            ),
            expected,
        )
        self.assertEqual(
            rhythm_scheduler._claim_group_job_keys(
                rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY
            ),
            expected,
        )

        session = MagicMock()
        scalar_result = MagicMock()
        scalar_result.all.return_value = [
            self._job(
                rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
                "automatic_selection_data",
                "15:45",
            ),
            self._job(
                rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
                "automatic_selection",
                "16:10",
            ),
        ]
        session.scalars.return_value = scalar_result
        rhythm_scheduler._lock_claim_group(
            session,
            rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
        )
        statement = session.scalars.call_args.args[0]
        sql = str(
            statement.compile(compile_kwargs={"literal_binds": True})
        )
        self.assertIn("ORDER BY scheduled_jobs.job_key", sql)
        self.assertIn(
            rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
            sql,
        )
        self.assertIn(rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY, sql)

    def test_group_running_check_blocks_manual_ranking_claim(self) -> None:
        data_job = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
            "automatic_selection_data",
            "15:45",
        )
        ranking_job = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
            "automatic_selection",
            "16:10",
        )
        session = MagicMock()
        running_data = SimpleNamespace(run_id="data-run")
        with (
            patch.object(
                rhythm_scheduler,
                "_lock_claim_group",
                return_value={
                    data_job.job_key: data_job,
                    ranking_job.job_key: ranking_job,
                },
            ) as lock_group,
            patch.object(
                rhythm_scheduler,
                "_find_running_claim",
                return_value=running_data,
            ) as find_running,
        ):
            result = rhythm_scheduler._claim_job_run(
                session,
                ranking_job.job_key,
                trigger_type="manual",
                ran_at=datetime(2026, 9, 7, 16, 11),
                actor_user_id=17,
                expected_job_kind="automatic_selection",
            )

        self.assertEqual(
            result,
            {"status": "already_running", "run_id": "data-run"},
        )
        lock_group.assert_called_once_with(session, ranking_job.job_key)
        find_running.assert_called_once_with(
            session,
            ranking_job.job_key,
            started_after=None,
        )
        session.add.assert_not_called()
        session.rollback.assert_called_once_with()

    def test_scheduled_catchup_claim_uses_canonical_fire_and_job_owner(self) -> None:
        data_job = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
            "automatic_selection_data",
            "17:30",
            owner_user_id=31,
        )
        ranking_job = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
            "automatic_selection",
            "17:50",
            owner_user_id=31,
        )
        session = MagicMock()
        ran_at = datetime(2026, 9, 7, 17, 40)
        with (
            patch.object(
                rhythm_scheduler,
                "_lock_claim_group",
                return_value={
                    data_job.job_key: data_job,
                    ranking_job.job_key: ranking_job,
                },
            ),
            patch.object(
                rhythm_scheduler,
                "_find_running_claim",
                return_value=None,
            ),
            patch.object(
                rhythm_scheduler,
                "is_trading_day",
                return_value=True,
            ),
        ):
            result = rhythm_scheduler._claim_job_run(
                session,
                data_job.job_key,
                trigger_type="schedule",
                ran_at=ran_at,
            )

        self.assertEqual(result["status"], "claimed")
        added = session.add.call_args.args[0]
        self.assertIsInstance(added, JobRun)
        self.assertEqual(added.owner_user_id, 31)
        self.assertEqual(
            added.fire_key,
            f"{data_job.job_key}@2026-09-07T17:30",
        )
        session.commit.assert_called_once_with()

    def test_scheduled_ranking_waits_for_matching_ingestion_evidence(self) -> None:
        data_job = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
            "automatic_selection_data",
            "15:45",
        )
        ranking_job = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
            "automatic_selection",
            "16:10",
        )
        session = MagicMock()
        with (
            patch.object(
                rhythm_scheduler,
                "_lock_claim_group",
                return_value={
                    data_job.job_key: data_job,
                    ranking_job.job_key: ranking_job,
                },
            ),
            patch.object(
                rhythm_scheduler,
                "_find_running_claim",
                return_value=None,
            ),
            patch.object(
                rhythm_scheduler,
                "_is_due",
                return_value=True,
            ),
            patch.object(
                rhythm_scheduler,
                "_scheduled_automatic_selection_data_ready",
                return_value=False,
            ) as data_ready,
        ):
            result = rhythm_scheduler._claim_job_run(
                session,
                ranking_job.job_key,
                trigger_type="schedule",
                ran_at=datetime(2026, 9, 7, 16, 10),
            )

        self.assertEqual(
            result,
            {"status": "waiting_dependency", "run_id": None},
        )
        data_ready.assert_called_once_with(
            session,
            datetime(2026, 9, 7, 16, 10),
        )
        session.add.assert_not_called()
        session.rollback.assert_called_once_with()

    def test_ranking_gate_uses_latest_success_and_local_same_day_calendar(self) -> None:
        session = MagicMock()
        latest_success = SimpleNamespace(
            status="success",
            output_content=json.dumps(
                {
                    "ready": True,
                    "target_trade_date": "20260907",
                }
            )
        )
        session.scalar.side_effect = ["20260907", latest_success]

        with patch.object(
            rhythm_scheduler,
            "is_trading_day",
            side_effect=AssertionError("不得在依赖门禁中外呼交易日历"),
        ):
            ready = rhythm_scheduler._scheduled_automatic_selection_data_ready(
                session,
                datetime(2026, 9, 7, 17, 50),
            )

        self.assertTrue(ready)
        success_statement = session.scalar.call_args_list[1].args[0]
        success_sql = str(
            success_statement.compile(
                compile_kwargs={"literal_binds": True}
            )
        )
        self.assertNotIn("job_runs.status = 'success'", success_sql)
        self.assertIn("job_runs.started_at DESC", success_sql)
        self.assertIn("job_runs.id DESC", success_sql)

        stale_session = MagicMock()
        stale_session.scalar.side_effect = [
            "20260907",
            SimpleNamespace(
                status="success",
                output_content=json.dumps(
                    {"ready": True, "target_trade_date": "20260904"}
                )
            ),
        ]
        self.assertFalse(
            rhythm_scheduler._scheduled_automatic_selection_data_ready(
                stale_session,
                datetime(2026, 9, 7, 17, 50),
            )
        )

    def test_before_data_release_the_gate_targets_the_previous_open_day(self) -> None:
        session = MagicMock()
        session.scalar.side_effect = [
            "20260907",
            "20260904",
            SimpleNamespace(
                status="success",
                output_content=json.dumps({"ready": True, "target_trade_date": "20260904"}),
            ),
        ]
        self.assertTrue(
            rhythm_scheduler._scheduled_automatic_selection_data_ready(session, datetime(2026, 9, 7, 16, 59))
        )
        previous_sql = str(session.scalar.call_args_list[1].args[0].compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("max(market_trading_calendar.cal_date)", previous_sql)
        self.assertIn("market_trading_calendar.cal_date <= '20260906'", previous_sql)

        holiday = MagicMock()
        holiday.scalar.side_effect = [None]
        self.assertFalse(
            rhythm_scheduler._scheduled_automatic_selection_data_ready(holiday, datetime(2026, 10, 1, 18, 0))
        )

    def test_scheduled_ranking_due_check_does_not_call_external_calendar(self) -> None:
        job = self._job(rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY, "automatic_selection", "16:10")
        with patch.object(rhythm_scheduler, "is_trading_day", side_effect=AssertionError("不得外呼")):
            self.assertTrue(rhythm_scheduler._is_due(job, datetime(2026, 9, 7, 16, 11)))

    def test_newer_failed_ingestion_does_not_reuse_older_success(self) -> None:
        session = MagicMock()
        session.scalar.side_effect = ["20260907", SimpleNamespace(status="failed", output_content='{"ready":true,"target_trade_date":"20260907"}')]
        self.assertFalse(rhythm_scheduler._scheduled_automatic_selection_data_ready(session, datetime(2026, 9, 7, 17, 51)))

    def test_running_claim_check_is_a_current_locking_read(self) -> None:
        session = MagicMock()
        rhythm_scheduler._find_running_claim(session, rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY)
        sql = str(session.scalar.call_args.args[0].compile(compile_kwargs={"literal_binds": True}))
        self.assertIn("FOR UPDATE", sql)
        self.assertIn(rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY, sql)

    def test_manual_claim_preserves_authenticated_actor(self) -> None:
        ranking_job = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
            "automatic_selection",
            "16:10",
            owner_user_id=17,
        )
        data_job = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
            "automatic_selection_data",
            "15:45",
            owner_user_id=17,
        )
        session = MagicMock()
        with (
            patch.object(
                rhythm_scheduler,
                "_lock_claim_group",
                return_value={
                    data_job.job_key: data_job,
                    ranking_job.job_key: ranking_job,
                },
            ),
            patch.object(
                rhythm_scheduler,
                "_find_running_claim",
                return_value=None,
            ),
        ):
            result = rhythm_scheduler._claim_job_run(
                session,
                ranking_job.job_key,
                trigger_type="manual",
                ran_at=datetime(2026, 9, 7, 16, 10),
                actor_user_id=41,
                expected_job_kind="automatic_selection",
            )

        self.assertEqual(result["status"], "claimed")
        added = session.add.call_args.args[0]
        self.assertEqual(added.owner_user_id, 41)
        self.assertEqual(added.trigger_type, "manual")

    def test_manual_pipeline_claim_persists_actor_and_pipeline_marker(self) -> None:
        data_job = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
            "automatic_selection_data",
            "15:45",
            owner_user_id=17,
        )
        ranking_job = self._job(
            rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
            "automatic_selection",
            "16:10",
            owner_user_id=17,
        )
        session = MagicMock()
        with (
            patch.object(
                rhythm_scheduler,
                "_lock_claim_group",
                return_value={
                    data_job.job_key: data_job,
                    ranking_job.job_key: ranking_job,
                },
            ),
            patch.object(
                rhythm_scheduler,
                "_find_running_claim",
                return_value=None,
            ),
        ):
            result = rhythm_scheduler._claim_job_run(
                session,
                data_job.job_key,
                trigger_type="manual_pipeline",
                ran_at=datetime(2026, 9, 7, 13, 0),
                actor_user_id=41,
                expected_job_kind="automatic_selection_data",
            )

        self.assertEqual(result["status"], "claimed")
        added = session.add.call_args.args[0]
        self.assertEqual(added.owner_user_id, 41)
        self.assertEqual(added.trigger_type, "manual_pipeline")
        self.assertTrue(added.fire_key.startswith(f"{data_job.job_key}@manual-"))

    def test_manual_button_dispatches_data_then_ranking_pipeline(self) -> None:
        with (
            patch.object(
                rhythm_scheduler,
                "_claim_manual_run",
                return_value={"status": "claimed", "run_id": "data-run"},
            ) as claim,
            patch.object(rhythm_scheduler, "_dispatch") as dispatch,
        ):
            result = asyncio.run(
                rhythm_scheduler.dispatch_automatic_selection_pipeline_result(
                    actor_user_id=41,
                )
            )

        claim.assert_called_once_with(
            rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
            actor_user_id=41,
            expected_job_kind="automatic_selection_data",
            trigger_type="manual_pipeline",
        )
        dispatch.assert_called_once_with(
            rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
            "data-run",
        )
        self.assertEqual(result["pipeline_stage"], "data_ingestion")
        self.assertEqual(
            result["job_key"],
            rhythm_scheduler.AUTOMATIC_SELECTION_DATA_JOB_KEY,
        )

    def test_successful_manual_ingestion_runs_ranking_as_followup(self) -> None:
        snapshot = {
            "job_kind": "automatic_selection_data",
            "trigger_type": "manual_pipeline",
            "owner_user_id": 41,
        }
        with (
            patch.object(
                rhythm_scheduler,
                "_claim_manual_run",
                return_value={"status": "claimed", "run_id": "ranking-run"},
            ) as claim,
            patch.object(rhythm_scheduler, "_execute_job") as execute,
        ):
            result = rhythm_scheduler._run_manual_automatic_selection_followup(
                snapshot,
                "success",
            )

        self.assertEqual(result, {"status": "claimed", "run_id": "ranking-run"})
        claim.assert_called_once_with(
            rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
            actor_user_id=41,
            expected_job_kind="automatic_selection",
        )
        execute.assert_called_once_with(
            rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
            "ranking-run",
        )

    def test_failed_manual_ingestion_never_runs_ranking(self) -> None:
        snapshot = {
            "job_kind": "automatic_selection_data",
            "trigger_type": "manual_pipeline",
            "owner_user_id": 41,
        }
        with (
            patch.object(rhythm_scheduler, "_claim_manual_run") as claim,
            patch.object(rhythm_scheduler, "_execute_job") as execute,
        ):
            result = rhythm_scheduler._run_manual_automatic_selection_followup(
                snapshot,
                "failed",
            )

        self.assertIsNone(result)
        claim.assert_not_called()
        execute.assert_not_called()

    def test_position_price_sync_handles_structured_claim_statuses(self) -> None:
        ran_at = datetime(2026, 9, 7, 10, 0)
        cases = [
            (
                {"status": "claimed", "run_id": "price-run"},
                "price-run",
                False,
                "success",
            ),
            (
                {"status": "missing", "run_id": None},
                None,
                False,
                "success",
            ),
            (
                {"status": "already_running", "run_id": "scheduled-run"},
                None,
                True,
                "skipped",
            ),
            (
                {"status": "invalid_kind", "run_id": None},
                None,
                True,
                "failed",
            ),
        ]
        for claim, expected_run_id, skipped, status in cases:
            with (
                self.subTest(claim=claim),
                patch.object(
                    rhythm_scheduler,
                    "_claim_manual_run",
                    return_value=claim,
                ) as claim_manual,
                patch.object(
                    rhythm_scheduler,
                    "_sync_prices_and_finalize",
                    return_value={
                        "skipped": False,
                        "status": "success",
                        "message": "ok",
                    },
                ) as finalize,
            ):
                result = rhythm_scheduler.sync_position_prices(ran_at)
                self.assertEqual(result["skipped"], skipped)
                self.assertEqual(result["status"], status)
                claim_manual.assert_called_once_with(
                    rhythm_scheduler.POSITION_PRICE_SYNC_JOB_KEY,
                    expected_job_kind="position_price_sync",
                )
                if status == "success":
                    finalize.assert_called_once_with(
                        ran_at,
                        run_id=expected_run_id,
                    )
                else:
                    finalize.assert_not_called()

    def test_ingestion_summary_maps_current_source_stats_to_stable_output(self) -> None:
        source_result = {
            "target_trade_date": "20260907",
            "ready": True,
            "strategy_count": 3,
            "market_sync": {
                "bars_upserted": 1234,
                "basic_upserted": 567,
                "calendar_rows_upserted": 90,
            },
            "factor_sync": {"stored_rows": 44},
            "errors": [],
        }
        with patch(
            "backend.app.quant.automatic_selection_service."
            "sync_automatic_selection_data",
            return_value=source_result,
        ):
            output, notification_id, status, error = (
                rhythm_scheduler._run_automatic_selection_data(
                    {"owner_user_id": 17},
                    "data-run",
                )
            )

        summary = json.loads(output)
        self.assertEqual(summary["bar_rows_upserted"], 1234)
        self.assertEqual(summary["basic_rows_upserted"], 567)
        self.assertNotIn("bars_upserted", summary)
        self.assertNotIn("basic_upserted", summary)
        self.assertIsNone(notification_id)
        self.assertEqual(status, "success")
        self.assertIsNone(error)


class AutomaticSelectionDigestDeliveryTests(unittest.TestCase):
    CYCLE = {
        "target_trade_date": "20261021",
        "strategy_count": 1,
        "completed_count": 1,
        "blocked_count": 0,
        "failed_count": 0,
        "selected_count": 30,
        "evaluation_updates": {"completed": 0, "insufficient_data": 0, "pending": 3, "failed": 0},
    }
    DIGEST = {
        "run_id": "selection-run-1",
        "trade_date": "20261021",
        "title": "每日选股 | 2026-10-21",
        "inbox_title": "每日选股（10 月 21 日）：甲、乙、丙等 30 只",
        "content": "**10 月 21 日收盘后，按固定规则选出 30 只，下面是排名前 5 的。**",
    }

    def _run(self, *, cycle=None, digest=None, feishu_status="sent", push_channel_key="feishu-daily"):
        from backend.app.notify import notification_service
        from backend.app.quant import automatic_selection_presentation, automatic_selection_service

        created: list[dict] = []

        def create_notification(user_id, title, **kwargs):
            created.append({"user_id": user_id, "title": title, **kwargs})
            return {"id": len(created)}

        feishu = MagicMock(
            return_value=SimpleNamespace(
                status=feishu_status,
                error=None if feishu_status == "sent" else "签名校验失败",
            )
        )
        session = MagicMock()
        session.__enter__.return_value = "db-session"
        session.__exit__.return_value = False
        digest_patch = (
            patch.object(automatic_selection_presentation, "daily_digest", side_effect=digest)
            if isinstance(digest, Exception)
            else patch.object(automatic_selection_presentation, "daily_digest", return_value=digest)
        )
        with (
            patch.object(
                automatic_selection_service,
                "run_automatic_selection_cycle",
                return_value=cycle or self.CYCLE,
            ),
            digest_patch as daily_digest,
            patch.object(notification_service, "create_notification", side_effect=create_notification),
            patch.object(rhythm_scheduler, "send_feishu", feishu),
            patch.object(rhythm_scheduler, "SessionLocal", return_value=session),
        ):
            result = rhythm_scheduler._run_automatic_selection(
                {
                    "job_key": rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
                    "name": "全自动选股",
                    "owner_user_id": 7,
                    "push_channel_key": push_channel_key,
                    "trigger_type": "schedule",
                },
                "job-run-1",
            )
        return result, created, feishu, daily_digest

    def test_completed_selection_pushes_top_picks_to_feishu_and_inbox(self) -> None:
        (_, notification_id, status, error), created, feishu, daily_digest = self._run(digest=self.DIGEST)

        self.assertEqual((status, error), ("success", None))
        daily_digest.assert_called_once_with(trade_date="20261021")
        args, kwargs = feishu.call_args
        self.assertEqual(args, ("db-session", "feishu-daily", self.DIGEST["title"], self.DIGEST["content"]))
        self.assertEqual(kwargs["event_id"], "automatic-selection:selection-run-1")
        self.assertEqual(kwargs["card_template"], "green")
        self.assertIn(("触发方式", "自动调度"), kwargs["card_metadata"])
        share = kwargs["share"]
        self.assertEqual(
            (share.source_type, share.source_ref, share.owner_user_id),
            ("selection_digest", "selection-run-1", 7),
        )
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["title"], self.DIGEST["inbox_title"])
        self.assertEqual(created[0]["content"], self.DIGEST["content"])
        self.assertEqual(created[0]["level"], "success")
        self.assertEqual(created[0]["link"], "/automatic-selection")
        self.assertEqual(notification_id, 1)

    def test_feishu_failure_marks_run_failed_but_keeps_inbox_digest(self) -> None:
        (_, _, status, error), created, _, _ = self._run(digest=self.DIGEST, feishu_status="failed")

        self.assertEqual(status, "failed")
        self.assertIn("飞书任务群推送失败", error)
        self.assertIn("签名校验失败", error)
        self.assertEqual([item["title"] for item in created], [self.DIGEST["inbox_title"]])

    def test_blocked_run_sends_a_plain_status_notice_only(self) -> None:
        blocked = {**self.CYCLE, "completed_count": 0, "blocked_count": 1, "selected_count": 0}
        (_, _, status, _), created, feishu, daily_digest = self._run(cycle=blocked)

        self.assertEqual(status, "skipped")
        daily_digest.assert_not_called()
        feishu.assert_not_called()
        self.assertEqual(created[0]["title"], "自动选股需要关注")
        self.assertIn("因数据不全暂停", created[0]["content"])
        self.assertNotIn("阻断", created[0]["content"])
        self.assertNotIn("入选证据", created[0]["content"])

    def test_job_without_channel_only_writes_the_inbox(self) -> None:
        (_, _, status, _), created, feishu, _ = self._run(digest=self.DIGEST, push_channel_key=None)

        self.assertEqual(status, "success")
        feishu.assert_not_called()
        self.assertEqual(created[0]["title"], self.DIGEST["inbox_title"])

    def test_digest_failure_falls_back_to_status_notice(self) -> None:
        (_, _, status, _), created, feishu, _ = self._run(digest=RuntimeError("库连不上"))

        self.assertEqual(status, "success")
        feishu.assert_not_called()
        self.assertEqual(created[0]["title"], "自动选股运行完成")
        self.assertIn("1 个策略出了结果", created[0]["content"])


class AutomaticSelectionPushMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from backend.app.core.models import Base, User

        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.addCleanup(self.engine.dispose)
        self.SessionFactory = sessionmaker(bind=self.engine)
        with self.SessionFactory() as db:
            admin = User(username="admin", display_name="管理员", role="super_admin", permission_scope="", password_hash="x")
            db.add(admin)
            db.commit()
            self.admin_id = admin.id

    def _selection_job(self, db, description: str) -> None:
        job = self._job_row(db)
        if job is None:
            db.add(
                ScheduledJob(
                    job_key=rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY,
                    name="全自动选股",
                    description=description,
                    job_kind="automatic_selection",
                    schedule_type="daily",
                    run_time="17:50",
                    weekday_mask="1,2,3,4,5",
                    trading_day_only=True,
                    owner_user_id=self.admin_id,
                    push_channel_key=None,
                    enabled=True,
                )
            )
            db.commit()

    @staticmethod
    def _job_row(db):
        from sqlalchemy import select

        return db.scalar(select(ScheduledJob).where(ScheduledJob.job_key == rhythm_scheduler.AUTOMATIC_SELECTION_JOB_KEY))

    def test_untouched_selection_job_gets_feishu_push_once(self) -> None:
        with self.SessionFactory() as db:
            self._selection_job(db, rhythm_scheduler._LEGACY_AUTOMATIC_SELECTION_DESCRIPTION)
            rhythm_scheduler.ensure_default_jobs(db)
            db.commit()
            job = self._job_row(db)
            self.assertEqual(job.push_channel_key, "feishu-daily")
            self.assertIn("推送任务群", job.description)

            job.push_channel_key = None
            db.commit()
            rhythm_scheduler.ensure_default_jobs(db)
            db.commit()
            self.assertIsNone(self._job_row(db).push_channel_key)

    def test_customized_selection_job_keeps_its_channel_choice(self) -> None:
        with self.SessionFactory() as db:
            self._selection_job(db, "管理员自己写的说明")
            rhythm_scheduler.ensure_default_jobs(db)
            db.commit()
            job = self._job_row(db)
            self.assertIsNone(job.push_channel_key)
            self.assertEqual(job.description, "管理员自己写的说明")


if __name__ == "__main__":
    unittest.main()
