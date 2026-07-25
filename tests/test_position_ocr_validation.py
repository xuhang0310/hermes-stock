from __future__ import annotations

import unittest
from unittest.mock import patch

from backend.app.agents import position_agent


class _NoWriteRepository:
    def create_position_import_batch(self, **_kwargs):
        raise AssertionError("validation flow must not create an import batch")


class _CaptureWriteRepository:
    def __init__(self) -> None:
        self.merged: list[dict] = []
        self.merge_options: list[dict] = []

    def create_position_import_batch(self, **_kwargs):
        return type("Batch", (), {"id": 41, "status": "parsing"})()

    def merge_position_holding(self, payload, **kwargs):
        self.merged.append(dict(payload))
        self.merge_options.append(dict(kwargs))
        return {**payload, "id": len(self.merged)}

    def finish_position_import_batch(self, batch, _rows):
        batch.status = "completed"


class PositionOcrValidationTests(unittest.TestCase):
    def test_position_prompt_requires_incremental_rows_only(self) -> None:
        prompt = position_agent._position_agent_prompt()

        self.assertIn("rows 只允许包含用户本轮明确要求变更的标的", prompt)
        self.assertIn("绝不能附带其他未变化的已有持仓", prompt)
        self.assertNotIn("必须同时包含该新标的行和所有已有持仓行", prompt)

    def test_valid_symbol_uses_authoritative_name(self) -> None:
        with patch.object(
            position_agent,
            "lookup_a_share_instrument",
            return_value={
                "available": True,
                "instrument": {"ts_code": "600519.SH", "name": "贵州茅台"},
            },
        ):
            result = position_agent.validate_position_symbol("600519", "贵州毛台")

        self.assertTrue(result["valid"])
        self.assertEqual(result["normalized_symbol"], "600519.SH")
        self.assertEqual(result["canonical_name"], "贵州茅台")
        self.assertEqual(result["status"], "name_corrected")

    def test_invalid_ocr_code_can_offer_name_based_suggestion(self) -> None:
        def fake_resolve(query: str) -> dict:
            if query == "贵州茅台":
                return {"candidates": [{"ts_code": "600519.SH", "name": "贵州茅台"}]}
            return {"candidates": []}

        with (
            patch.object(
                position_agent,
                "lookup_a_share_instrument",
                return_value={"available": True, "instrument": None},
            ),
            patch.object(position_agent, "resolve_stock", side_effect=fake_resolve),
        ):
            result = position_agent.validate_position_symbol("600579", "贵州茅台")

        self.assertFalse(result["valid"])
        self.assertEqual(result["suggested_symbol"], "600519.SH")
        self.assertEqual(result["suggested_name"], "贵州茅台")

    def test_ocr_rows_require_review_before_database_write(self) -> None:
        with patch.object(
            position_agent,
            "lookup_a_share_instrument",
            return_value={
                "available": True,
                "instrument": {"ts_code": "000001.SZ", "name": "平安银行"},
            },
        ):
            result = position_agent._execute_position_tool(
                repository=_NoWriteRepository(),
                name="save_position_holdings",
                args={
                    "rows": [
                        {
                            "symbol": "000001",
                            "name": "平安银行",
                            "position_quantity": 1000,
                            "cost_price": 10.2,
                        }
                    ]
                },
                created_by="tester",
                source_name="vision",
                image_mime_type="image/png",
                image_sha256="ocr-image-hash",
                account_id=1,
            )

        self.assertTrue(result["need_review"])
        self.assertEqual(result["saved_count"], 0)
        self.assertEqual(result["rows"][0]["symbol"], "000001.SZ")
        self.assertTrue(result["validations"][0]["valid"])

        pending = position_agent._pending_from_tool_result(
            "save_position_holdings",
            {"rows": result["rows"]},
            result,
        )
        self.assertTrue(pending["review_required"])
        self.assertEqual(pending["arguments"]["rows"][0]["symbol"], "000001.SZ")

    def test_named_broker_account_reaches_write_tool_without_default_override(self) -> None:
        repository = _CaptureWriteRepository()
        with patch.object(
            position_agent,
            "lookup_a_share_instrument",
            return_value={
                "available": True,
                "instrument": {"ts_code": "000001.SZ", "name": "平安银行"},
            },
        ):
            result = position_agent._execute_position_tool(
                repository=repository,
                name="save_position_holdings",
                args={
                    "account": {
                        "broker_name": "广发证券",
                        "account_name": "账户#4",
                    },
                    "rows": [
                        {
                            "symbol": "000001.SZ",
                            "name": "平安银行",
                            "position_quantity": 1000,
                            "cost_price": 10.2,
                        }
                    ],
                },
                created_by="tester",
                source_name="AgentScope",
                image_mime_type="text/plain",
                image_sha256=None,
                account_id=None,
            )

        self.assertEqual(result["saved_count"], 1)
        self.assertEqual(repository.merged[0]["broker_name"], "广发证券")
        self.assertEqual(repository.merged[0]["account_name"], "账户#4")
        self.assertNotIn("account_id", repository.merged[0])

    def test_single_symbol_request_drops_echoed_existing_positions(self) -> None:
        repository = _CaptureWriteRepository()

        def lookup(symbol: str) -> dict:
            names = {
                "002594.SZ": "比亚迪",
                "600984.SH": "建设机械",
            }
            return {
                "available": True,
                "instrument": {"ts_code": symbol, "name": names[symbol]},
            }

        with patch.object(position_agent, "lookup_a_share_instrument", side_effect=lookup):
            result = position_agent._execute_position_tool(
                repository=repository,
                name="save_position_holdings",
                args={
                    "rows": [
                        {
                            "symbol": "002594.SZ",
                            "name": "比亚迪",
                            "position_quantity": 100,
                            "cost_price": 90,
                            "action": "buy",
                        },
                        {
                            "symbol": "600984.SH",
                            "name": "建设机械",
                            "position_quantity": 1000,
                            "cost_price": 3.285,
                            "action": "replace",
                        },
                    ]
                },
                created_by="tester",
                source_name="AgentScope",
                image_mime_type="text/plain",
                image_sha256=None,
                account_id=3,
                instruction="请在方正证券账户录入比亚迪 100 股，成本价 90 元",
            )

        self.assertEqual(result["saved_count"], 1)
        self.assertEqual(result["ignored_unchanged_count"], 1)
        self.assertEqual(result["ignored_unchanged_rows"], ["建设机械"])
        self.assertEqual([row["symbol"] for row in repository.merged], ["002594.SZ"])
        self.assertEqual(repository.merge_options[0]["action"], "buy")
        self.assertEqual(repository.merge_options[0]["import_batch"].id, 41)


if __name__ == "__main__":
    unittest.main()
