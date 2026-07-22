from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from backend.app.ai.ai_api import _chat_messages_out
from backend.app.agent_runtime.duration import elapsed_wall_clock_ms


class ChatMessageDurationTests(unittest.TestCase):
    def message(
        self,
        message_id: int,
        role: str,
        created_at: datetime,
        duration_ms: int | None = None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            id=message_id,
            role=role,
            content=f"message-{message_id}",
            attachments_json=None,
            error=None,
            duration_ms=duration_ms,
            created_at=created_at,
        )

    def test_keeps_persisted_duration(self) -> None:
        started = datetime(2026, 7, 21, 10, 0, 0)
        rows = _chat_messages_out([
            self.message(1, "user", started),
            self.message(2, "assistant", started + timedelta(seconds=8), 4321),
        ])

        self.assertEqual(rows[1]["duration_ms"], 4321)

    def test_run_duration_corrects_a_resumed_team_message(self) -> None:
        started = datetime(2026, 7, 22, 15, 40, 47)
        rows = _chat_messages_out(
            [
                self.message(1, "user", started),
                self.message(2, "assistant", started + timedelta(minutes=8, seconds=28), 62401),
            ],
            {2: 508000},
        )

        self.assertEqual(rows[1]["duration_ms"], 508000)

    def test_wall_clock_duration_includes_waiting_children(self) -> None:
        self.assertEqual(
            elapsed_wall_clock_ms(
                datetime(2026, 7, 22, 15, 40, 47),
                datetime(2026, 7, 22, 15, 49, 15),
            ),
            508000,
        )

    def test_infers_legacy_duration_from_previous_user_message(self) -> None:
        started = datetime(2026, 7, 21, 10, 0, 0)
        rows = _chat_messages_out([
            self.message(1, "user", started),
            self.message(2, "assistant", started + timedelta(seconds=12, milliseconds=345)),
        ])

        self.assertEqual(rows[1]["duration_ms"], 12345)


if __name__ == "__main__":
    unittest.main()
