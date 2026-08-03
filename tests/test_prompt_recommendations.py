from __future__ import annotations

import json
import unittest
from datetime import timedelta
from unittest.mock import call, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.ai import prompt_recommendations
from backend.app.core.database import Base
from backend.app.core.models import AiPromptRecommendation, DigitalHumanProfile, User


class PromptRecommendationModelRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = DigitalHumanProfile(
            id=1,
            code="stock-pm",
            name="Stock research coordinator",
            avatar_tone="calm",
            target_roles="*",
            description="Break down questions and organize conclusions.",
            system_prompt="Collect facts before drawing conclusions.",
            quick_prompts_json="[]",
            status="enabled",
            sort_order=0,
        )
        self.context = {
            "messages": [{"role": "user", "content": "Analyze portfolio risk."}],
            "positions": [],
            "watchlist": [],
            "events": [],
        }

    @staticmethod
    def _config(model: str) -> dict:
        return {
            "id": 1,
            "display_name": model,
            "provider_name": "test",
            "base_url": "https://example.invalid/v1",
            "api_key": "secret",
            "model": model,
            "temperature": 0.2,
            "max_steps": 10,
        }

    @staticmethod
    def _response(text: str = "Review current portfolio risk") -> dict:
        content = json.dumps(
            {
                "recommendations": [
                    {
                        "text": text,
                        "reason": "Recent risk has priority",
                        "score": 90,
                        "source_type": "llm",
                    }
                ]
            },
            ensure_ascii=False,
        )
        return {"choices": [{"message": {"content": content}}]}

    @patch.object(prompt_recommendations, "log_event")
    @patch.object(prompt_recommendations, "chat_completion")
    @patch.object(prompt_recommendations, "active_llm_config")
    @patch.object(prompt_recommendations, "resolve_model_config_ids_by_priority")
    def test_primary_model_success_does_not_call_fallback(
        self,
        resolve_ids,
        active_config,
        chat_completion,
        _log_event,
    ) -> None:
        resolve_ids.return_value = [11, 22]
        active_config.side_effect = lambda model_id: self._config(
            "deepseek-v4-flash-0731" if model_id == 11 else "deepseek-v4-pro"
        )
        chat_completion.return_value = self._response()

        result = prompt_recommendations._call_llm_for_recommendations(
            self.profile,
            self.context,
        )

        self.assertEqual(
            [item.text for item in result],
            ["Review current portfolio risk"],
        )
        active_config.assert_called_once_with(11)
        self.assertEqual(chat_completion.call_count, 1)
        self.assertEqual(chat_completion.call_args.kwargs["timeout"], 18.0)

    @patch.object(prompt_recommendations, "log_event")
    @patch.object(prompt_recommendations, "chat_completion")
    @patch.object(prompt_recommendations, "active_llm_config")
    @patch.object(prompt_recommendations, "resolve_model_config_ids_by_priority")
    def test_primary_failure_uses_pro_fallback(
        self,
        resolve_ids,
        active_config,
        chat_completion,
        _log_event,
    ) -> None:
        resolve_ids.return_value = [11, 22]
        active_config.side_effect = lambda model_id: self._config(
            "deepseek-v4-flash-0731" if model_id == 11 else "deepseek-v4-pro"
        )
        chat_completion.side_effect = [
            RuntimeError("primary unavailable"),
            self._response("Recommendation from fallback model"),
        ]

        result = prompt_recommendations._call_llm_for_recommendations(
            self.profile,
            self.context,
        )

        self.assertEqual(
            [item.text for item in result],
            ["Recommendation from fallback model"],
        )
        self.assertEqual(active_config.call_args_list, [call(11), call(22)])
        self.assertEqual(chat_completion.call_count, 2)
        self.assertEqual(chat_completion.call_args_list[0].kwargs["timeout"], 18.0)
        self.assertEqual(chat_completion.call_args_list[1].kwargs["timeout"], 45.0)

    @patch.object(prompt_recommendations, "log_event")
    @patch.object(prompt_recommendations, "chat_completion")
    @patch.object(prompt_recommendations, "active_llm_config")
    @patch.object(prompt_recommendations, "resolve_model_config_ids_by_priority")
    def test_invalid_primary_output_uses_pro_fallback(
        self,
        resolve_ids,
        active_config,
        chat_completion,
        _log_event,
    ) -> None:
        resolve_ids.return_value = [11, 22]
        active_config.side_effect = lambda model_id: self._config(
            "deepseek-v4-flash-0731" if model_id == 11 else "deepseek-v4-pro"
        )
        chat_completion.side_effect = [
            {"choices": [{"message": {"content": "not-json"}}]},
            self._response("Fallback after invalid primary response"),
        ]

        result = prompt_recommendations._call_llm_for_recommendations(
            self.profile,
            self.context,
        )

        self.assertEqual(
            [item.text for item in result],
            ["Fallback after invalid primary response"],
        )
        self.assertEqual(chat_completion.call_count, 2)


class PromptRecommendationCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        with prompt_recommendations._refresh_failure_lock:
            prompt_recommendations._refresh_failure_state.clear()
        self.engine = create_engine(
            "sqlite+pysqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.SessionFactory = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.SessionFactory() as db:
            user = User(
                username="recommendation-user",
                display_name="Recommendation User",
                role="admin",
                permission_scope="all",
                password_hash="test",
            )
            profile = DigitalHumanProfile(
                code="stock-pm",
                name="Stock research coordinator",
                avatar_tone="calm",
                target_roles="*",
                description="Break down questions and organize conclusions.",
                system_prompt="Collect facts before drawing conclusions.",
                quick_prompts_json=json.dumps(["Generic fallback prompt"]),
                status="enabled",
                sort_order=0,
            )
            db.add_all([user, profile])
            db.commit()
            self.user_id = user.id
            self.profile_id = profile.id

    def tearDown(self) -> None:
        with prompt_recommendations._refresh_failure_lock:
            prompt_recommendations._refresh_failure_state.clear()
        self.engine.dispose()

    @staticmethod
    def _context() -> dict:
        return {
            "messages": [{"role": "user", "content": "Analyze portfolio risk."}],
            "positions": [],
            "watchlist": [],
            "risks": [],
            "events": [],
        }

    def test_failed_refresh_preserves_last_personalized_batch(self) -> None:
        expired_at = prompt_recommendations._utcnow() - timedelta(minutes=1)
        with self.SessionFactory() as db:
            db.add(
                AiPromptRecommendation(
                    user_id=self.user_id,
                    digital_human_id=self.profile_id,
                    digital_human_code="stock-pm",
                    chat_mode="research",
                    prompt_text="Personalized portfolio follow-up",
                    reason="Based on the last conversation",
                    source_type="llm",
                    score=92,
                    status="active",
                    expires_at=expired_at,
                )
            )
            db.commit()

        with self.SessionFactory() as db:
            profile = db.get(DigitalHumanProfile, self.profile_id)
            with (
                patch.object(prompt_recommendations, "_collect_context", return_value=self._context()),
                patch.object(prompt_recommendations, "_build_candidates", return_value=[]),
                patch.object(prompt_recommendations, "log_event"),
                patch.object(prompt_recommendations, "_recommendation_redis_client", return_value=None),
            ):
                rows = prompt_recommendations._refresh_recommendations(
                    db,
                    user_id=self.user_id,
                    profile=profile,
                    chat_mode="research",
                    context_digital_human_id=self.profile_id,
                )

        self.assertEqual([row.prompt_text for row in rows], ["Personalized portfolio follow-up"])
        self.assertEqual(rows[0].source_type, "llm")
        self.assertEqual(rows[0].expires_at, expired_at)
        with self.SessionFactory() as db:
            stored = db.scalars(
                select(AiPromptRecommendation).where(
                    AiPromptRecommendation.user_id == self.user_id,
                    AiPromptRecommendation.digital_human_id == self.profile_id,
                )
            ).all()
        self.assertEqual(len(stored), 1)
        self.assertEqual(stored[0].status, "active")
        self.assertEqual(stored[0].source_type, "llm")

    def test_cold_start_still_uses_role_fallback_when_models_fail(self) -> None:
        with self.SessionFactory() as db:
            profile = db.get(DigitalHumanProfile, self.profile_id)
            with (
                patch.object(prompt_recommendations, "_collect_context", return_value=self._context()),
                patch.object(prompt_recommendations, "_build_candidates", return_value=[]),
                patch.object(prompt_recommendations, "_recommendation_redis_client", return_value=None),
            ):
                rows = prompt_recommendations._refresh_recommendations(
                    db,
                    user_id=self.user_id,
                    profile=profile,
                    chat_mode="research",
                    context_digital_human_id=self.profile_id,
                )

        self.assertEqual([row.prompt_text for row in rows], ["Generic fallback prompt"])
        self.assertEqual(rows[0].source_type, "fallback")

    def test_failed_refresh_sets_and_success_clears_backoff(self) -> None:
        with patch.object(prompt_recommendations, "_recommendation_redis_client", return_value=None):
            prompt_recommendations._record_refresh_failure(
                self.user_id,
                self.profile_id,
                "research",
            )
            self.assertTrue(
                prompt_recommendations._refresh_backoff_active(
                    self.user_id,
                    self.profile_id,
                    "research",
                )
            )
            prompt_recommendations._clear_refresh_failure(
                self.user_id,
                self.profile_id,
                "research",
            )
            self.assertFalse(
                prompt_recommendations._refresh_backoff_active(
                    self.user_id,
                    self.profile_id,
                    "research",
                )
            )


if __name__ == "__main__":
    unittest.main()
