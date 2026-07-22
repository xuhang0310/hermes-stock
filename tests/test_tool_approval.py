from __future__ import annotations

import unittest

from backend.app.agents.tool_approval import (
    classify_tool_risk,
    normalize_approval_policy,
    requires_approval,
)


class ToolApprovalPolicyTests(unittest.TestCase):
    def test_policy_normalization_keeps_legacy_clients_compatible(self) -> None:
        self.assertEqual(normalize_approval_policy("ask"), "ask")
        self.assertEqual(normalize_approval_policy("smart"), "smart")
        self.assertEqual(normalize_approval_policy("full"), "full")
        self.assertEqual(normalize_approval_policy(None), "ask")
        self.assertEqual(normalize_approval_policy(None, legacy_allow_code_execution=True), "full")

    def test_tool_risk_classification(self) -> None:
        write_tools = {"create_position_snapshot"}

        self.assertEqual(classify_tool_risk("search_stock", {}, write_tools), "low")
        self.assertEqual(classify_tool_risk("ws_write_file", {"path": "notes/a.md"}, write_tools), "medium")
        self.assertEqual(classify_tool_risk("ws_edit_file", {"path": "notes/a.md"}, write_tools), "medium")
        self.assertEqual(classify_tool_risk("ws_run_python", {"script_path": "scripts/a.py"}, write_tools), "high")
        self.assertEqual(classify_tool_risk("create_position_snapshot", {"confirmed": False}, write_tools), "low")
        self.assertEqual(classify_tool_risk("create_position_snapshot", {"confirmed": True}, write_tools), "high")

    def test_policy_matrix(self) -> None:
        self.assertFalse(requires_approval("ask", "low"))
        self.assertTrue(requires_approval("ask", "medium"))
        self.assertTrue(requires_approval("ask", "high"))

        self.assertFalse(requires_approval("smart", "low"))
        self.assertFalse(requires_approval("smart", "medium"))
        self.assertTrue(requires_approval("smart", "high"))

        self.assertFalse(requires_approval("full", "low"))
        self.assertFalse(requires_approval("full", "medium"))
        self.assertFalse(requires_approval("full", "high"))


if __name__ == "__main__":
    unittest.main()
