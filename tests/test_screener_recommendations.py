from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from backend.app.quant import screener_ai, screener_service


class ScreenerProgressionTemplateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.facts = {
            "position_count": 9,
            "stats": {
                "price": {"min": 4.2, "median": 19.67, "max": 76.8},
                "total_mv_yi": {"min": 25.0, "median": 74.5, "max": 510.0},
                "turnover_rate": {"min": 0.4, "median": 2.1, "max": 6.2},
                "pe_ttm": {"min": 18.0, "median": 65.89, "max": 180.0},
                "pct_chg": {"min": -1.2, "median": 0.78, "max": 4.1},
                "above_ma20_count": 7,
            },
        }

    def test_builds_five_distinct_actionable_strategies(self) -> None:
        templates = screener_service.build_progression_templates(self.facts)

        self.assertEqual(len(templates), 5)
        self.assertEqual(len({item["stage"] for item in templates}), 5)
        for item in templates:
            query = item["query"]
            self.assertEqual(item["target_min"], 20)
            self.assertEqual(item["target_max"], 120)
            self.assertEqual(query["above_ma"], 20)
            self.assertGreaterEqual(
                len([value for value in query.values() if value is not None]),
                11,
            )
            self.assertTrue(item["objective"])
            self.assertTrue(item["rationale"])

    def test_profile_summary_uses_real_holding_facts(self) -> None:
        summary = screener_service.position_profile_summary(self.facts)

        self.assertIn("74.5", summary)
        self.assertIn("7/9", summary)
        self.assertIn("估值优化", summary)

    def test_refinement_tightens_an_oversized_candidate_pool(self) -> None:
        template = screener_service.build_progression_templates(self.facts)[0]

        def count_by_width(query: dict) -> int:
            width = float(query["total_mv_max"]) - float(query["total_mv_min"])
            return max(1, round(width * 3.5))

        refined = screener_service.refine_progression_template(template, count_by_width)

        self.assertGreaterEqual(refined["universe"], 20)
        self.assertLessEqual(refined["universe"], 120)
        self.assertTrue(refined["tightening_notes"])
        self.assertGreaterEqual(refined["condition_count"], 11)


class ScreenerParseLatencyTests(unittest.TestCase):
    @patch.object(screener_ai, "chat_completion")
    @patch.object(screener_ai, "active_llm_config")
    def test_parse_disables_thinking_and_uses_short_timeout(
        self,
        active_config,
        chat_completion,
    ) -> None:
        active_config.return_value = {"model": "qwen3.7-plus"}
        content = json.dumps({"total_mv_max": 100}, ensure_ascii=False)
        chat_completion.return_value = {
            "choices": [{"message": {"content": content}}],
        }

        result = screener_ai.parse_query("总市值100亿以下")

        self.assertFalse(result["fallback"])
        self.assertEqual(result["query"]["total_mv_max"], 100)
        self.assertFalse(chat_completion.call_args.kwargs["enable_thinking"])
        self.assertEqual(chat_completion.call_args.kwargs["timeout"], 10.0)


if __name__ == "__main__":
    unittest.main()
