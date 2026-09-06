from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, call, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from backend.app.auth.auth import AuthContext, get_current_user
from backend.app.core.models import User
from backend.app.quant import automatic_selection_api as api
from backend.app.quant import automatic_selection_repository as repository


class AutomaticSelectionApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.user = User(
            id=17,
            username="automatic-selection-admin",
            display_name="自动选股管理员",
            role="admin",
            password_hash="test",
        )
        self.permissions = {"data:read", "schedule:manage", "model:manage"}
        self.db = object()
        app = FastAPI()
        app.include_router(api.router)
        app.dependency_overrides[get_current_user] = lambda: AuthContext(
            user=self.user,
            permissions=set(self.permissions),
        )
        app.dependency_overrides[api.get_db] = lambda: self.db
        self.client = TestClient(app)
        self.owned_job_patch = patch.object(
            api,
            "_owned_job",
            return_value=SimpleNamespace(
                job_key=api.AUTOMATIC_SELECTION_JOB_KEY,
                owner_user_id=self.user.id,
            ),
        )
        self.owned_job = self.owned_job_patch.start()
        self.addCleanup(self.owned_job_patch.stop)
        self.addCleanup(self.client.close)

    def test_read_endpoints_use_envelope_and_research_limitations(self) -> None:
        official = {
            "id": 1,
            "strategy_key": "forward-multifactor-v1",
            "lifecycle_state": "official",
            "is_official": True,
        }
        with (
            patch.object(repository, "list_strategies", return_value=[official]),
            patch.object(repository, "list_runs", return_value=[]),
        ):
            response = self.client.get("/api/automatic-selection/overview")
            strategies = self.client.get(
                "/api/automatic-selection/strategies"
            ).json()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertIsNone(body["data"]["latest_run"])
        self.assertIn("不构成投资建议", body["data"]["limitations"])
        self.assertTrue(strategies["data"][0]["is_official"])

    def test_permissions_separate_read_dispatch_and_strategy_management(self) -> None:
        self.permissions.clear()
        self.permissions.add("data:read")

        with patch.object(repository, "list_strategies", return_value=[]):
            self.assertEqual(
                self.client.get(
                    "/api/automatic-selection/strategies"
                ).status_code,
                200,
            )
        self.assertEqual(
            self.client.post(
                "/api/automatic-selection/strategies",
                json={
                    "strategy_key": "quality-growth",
                    "name": "质量成长",
                    "config": {},
                    "lifecycle_state": "challenger",
                },
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/api/automatic-selection/runs/dispatch"
            ).status_code,
            403,
        )
        self.owned_job.assert_not_called()

    def test_lifecycle_endpoints_forward_authenticated_actor(self) -> None:
        created = {
            "id": 23,
            "strategy_key": "quality-growth",
            "lifecycle_state": "challenger",
            "is_official": False,
        }
        promoted = {
            **created,
            "lifecycle_state": "official",
            "is_official": True,
        }
        retired = {
            **created,
            "lifecycle_state": "retired",
            "is_official": False,
        }
        with patch.object(
            repository,
            "create_strategy",
            return_value=created,
        ) as create_strategy:
            create_response = self.client.post(
                "/api/automatic-selection/strategies",
                json={
                    "strategy_key": "quality-growth",
                    "name": "质量成长",
                    "config": {"top_n": 30},
                    "lifecycle_state": "challenger",
                    "parent_strategy_id": 1,
                },
            )
        with patch.object(
            repository,
            "promote_strategy",
            return_value=promoted,
        ) as promote_strategy:
            promote_response = self.client.post(
                f"/api/automatic-selection/strategies/{created['id']}/promote",
                json={"confirmation_note": "已人工核验五日前瞻证据"},
            )
        with patch.object(
            repository,
            "retire_strategy",
            return_value=retired,
        ) as retire_strategy:
            retire_response = self.client.post(
                f"/api/automatic-selection/strategies/{created['id']}/retire",
                json={"note": "由新版本替代"},
            )

        self.assertEqual(create_response.status_code, 200)
        self.assertFalse(create_response.json()["data"]["is_official"])
        create_strategy.assert_called_once_with(
            strategy_key="quality-growth",
            name="质量成长",
            config={"top_n": 30},
            lifecycle_state="challenger",
            actor_user_id=self.user.id,
            parent_strategy_id=1,
        )
        self.assertEqual(promote_response.status_code, 200)
        self.assertTrue(promote_response.json()["data"]["is_official"])
        promote_strategy.assert_called_once_with(
            created["id"],
            actor_user_id=self.user.id,
            confirmation_note="已人工核验五日前瞻证据",
        )
        self.assertEqual(retire_response.status_code, 200)
        retire_strategy.assert_called_once_with(
            created["id"],
            actor_user_id=self.user.id,
            note="由新版本替代",
        )

    def test_promotion_without_mature_five_day_evidence_returns_400(self) -> None:
        with patch.object(
            repository,
            "promote_strategy",
            side_effect=ValueError(
                "尚未满足人工晋升的最低启动证据门槛：至少需要完成 5 日前瞻评估"
            ),
        ):
            response = self.client.post(
                "/api/automatic-selection/strategies/23/promote",
                json={"confirmation_note": "已完成人工复核"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["success"])
        self.assertEqual(response.json()["code"], "bad_request")
        self.assertIn("5 日前瞻评估", response.json()["message"])

    def test_invalid_strategy_config_returns_chinese_business_error(self) -> None:
        with patch.object(
            repository,
            "create_strategy",
            side_effect=ValueError("Top-N 必须大于 0"),
        ):
            response = self.client.post(
                "/api/automatic-selection/strategies",
                json={
                    "strategy_key": "invalid-strategy",
                    "name": "非法配置",
                    "config": {"top_n": 0},
                    "lifecycle_state": "shadow",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Top-N", response.json()["message"])

    def test_dispatch_checks_ownership_and_preserves_true_actor(self) -> None:
        with patch.object(
            api,
            "dispatch_automatic_selection_pipeline_result",
            new=AsyncMock(
                return_value={"status": "claimed", "run_id": "job-run-1"}
            ),
        ) as dispatch:
            response = self.client.post(
                "/api/automatic-selection/runs/dispatch"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["data"],
            {
                "job_run_id": "job-run-1",
                "job_key": api.AUTOMATIC_SELECTION_DATA_JOB_KEY,
                "pipeline_stage": "data_ingestion",
            },
        )
        self.owned_job.assert_has_calls(
            [
                call(
                    self.db,
                    user_id=self.user.id,
                    job_key=api.AUTOMATIC_SELECTION_DATA_JOB_KEY,
                ),
                call(
                    self.db,
                    user_id=self.user.id,
                    job_key=api.AUTOMATIC_SELECTION_JOB_KEY,
                ),
            ]
        )
        self.assertEqual(self.owned_job.call_count, 2)
        dispatch.assert_awaited_once_with(actor_user_id=self.user.id)

    def test_dispatch_hides_foreign_job_as_404_without_claiming(self) -> None:
        self.owned_job.side_effect = HTTPException(
            status_code=404,
            detail="任务不存在",
        )
        dispatch = AsyncMock()
        with patch.object(
            api,
            "dispatch_automatic_selection_pipeline_result",
            new=dispatch,
        ):
            response = self.client.post(
                "/api/automatic-selection/runs/dispatch"
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "任务不存在")
        dispatch.assert_not_awaited()

    def test_dispatch_maps_claim_failures_to_specific_envelopes(self) -> None:
        cases = [
            ({"status": "missing", "run_id": None}, 404, "not_found"),
            ({"status": "invalid_kind", "run_id": None}, 400, "job_invalid"),
            (
                {"status": "already_running", "run_id": "existing"},
                400,
                "run_conflict",
            ),
        ]
        for claim, expected_status, expected_code in cases:
            with self.subTest(claim=claim), patch.object(
                api,
                "dispatch_automatic_selection_pipeline_result",
                new=AsyncMock(return_value=claim),
            ):
                response = self.client.post(
                    "/api/automatic-selection/runs/dispatch"
                )
                self.assertEqual(response.status_code, expected_status)
                self.assertFalse(response.json()["success"])
                self.assertEqual(response.json()["code"], expected_code)

    def test_run_detail_forwards_scope_and_server_pagination(self) -> None:
        detail = {
            "run_id": "run-1",
            "candidates": [],
            "candidate_total": 125,
            "candidate_limit": 25,
            "candidate_offset": 50,
            "evaluations": [],
        }
        with patch.object(
            repository,
            "run_detail",
            return_value=detail,
        ) as run_detail:
            response = self.client.get(
                "/api/automatic-selection/runs/run-1"
                "?candidate_scope=excluded&candidate_limit=25&candidate_offset=50"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["candidate_total"], 125)
        run_detail.assert_called_once_with(
            "run-1",
            candidate_scope="excluded",
            candidate_limit=25,
            candidate_offset=50,
        )

    def test_evaluation_endpoint_uses_narrow_repository_query(self) -> None:
        with (
            patch.object(
                repository,
                "run_evaluations",
                return_value=[],
            ) as evaluations,
            patch.object(repository, "run_detail") as run_detail,
        ):
            response = self.client.get(
                "/api/automatic-selection/runs/run-1/evaluations"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], [])
        evaluations.assert_called_once_with("run-1")
        run_detail.assert_not_called()

    def test_run_detail_not_found_and_no_delete_route(self) -> None:
        with patch.object(repository, "run_detail", return_value=None):
            response = self.client.get(
                "/api/automatic-selection/runs/missing"
            )
        self.assertEqual(response.status_code, 404)
        self.assertFalse(response.json()["success"])
        self.assertIn(
            self.client.delete(
                "/api/automatic-selection/strategies/1"
            ).status_code,
            {404, 405},
        )


if __name__ == "__main__":
    unittest.main()
