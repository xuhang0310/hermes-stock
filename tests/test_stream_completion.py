from __future__ import annotations

import asyncio
import threading
import time
import unittest
from unittest.mock import patch

from backend.app.ai import prompt_recommendations


class PromptRefreshSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_prompt_refresh_does_not_block_terminal_response_path(self) -> None:
        release = threading.Event()

        def slow_refresh(_user_id: int, _profile_id: int, _chat_mode: str) -> None:
            release.wait(timeout=1)

        try:
            with patch.object(prompt_recommendations, "_refresh_one_in_new_session", side_effect=slow_refresh):
                started = time.monotonic()
                task = prompt_recommendations.schedule_recommendation_refresh(
                    user_id=1,
                    profile_id=2,
                    chat_mode="research",
                )
                elapsed = time.monotonic() - started

                self.assertLess(elapsed, 0.05)
                await asyncio.sleep(0.02)
                self.assertFalse(task.done())
                release.set()
                await asyncio.wait_for(task, timeout=1)
        finally:
            release.set()


if __name__ == "__main__":
    unittest.main()
