from __future__ import annotations

import asyncio
import time
from typing import Any

from backend.app.agent_runtime.redis_broker import RedisBroker, redis_broker
from backend.app.agent_runtime.tool_repository import (
    TERMINAL_TOOL_STATUSES,
    ToolExecutionRepository,
    tool_execution_repository,
)
from backend.app.agent_runtime.tool_specs import ToolSpec
from backend.app.core.settings import settings


class ExternalToolExecutor:
    def __init__(
        self,
        repository: ToolExecutionRepository = tool_execution_repository,
        broker: RedisBroker = redis_broker,
    ) -> None:
        self.repository = repository
        self.broker = broker

    async def execute(
        self,
        *,
        run_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        spec: ToolSpec,
    ) -> tuple[dict[str, Any], str]:
        execution, _created = await asyncio.to_thread(
            self.repository.submit,
            run_id=run_id,
            tool_name=tool_name,
            arguments=arguments,
            spec=spec,
        )
        execution_id = str(execution["execution_id"])
        if execution["status"] == "completed":
            return dict(execution.get("result") or {}), execution_id
        if execution["status"] in {"failed", "dead", "unknown", "cancelled"}:
            return self._error_result(execution), execution_id

        pubsub = await self.broker.subscribe_tool_result(execution_id)
        started = time.monotonic()
        timeout_seconds = max(
            int(spec.timeout_seconds) + 30,
            int(settings.tool_result_wait_seconds),
        )
        try:
            while time.monotonic() - started < timeout_seconds:
                current = await asyncio.to_thread(self.repository.get, execution_id)
                if current is None:
                    return {"error": "External tool execution disappeared."}, execution_id
                if current["status"] == "completed":
                    return dict(current.get("result") or {}), execution_id
                if current["status"] in TERMINAL_TOOL_STATUSES:
                    return self._error_result(current), execution_id
                if pubsub is not None:
                    await self.broker.wait_for_event(pubsub, timeout_seconds=1.0)
                else:
                    await asyncio.sleep(0.25)
            return {
                "error": "External tool result wait timed out; the durable execution remains inspectable.",
                "execution_id": execution_id,
            }, execution_id
        finally:
            await self.broker.close_subscription(pubsub)

    @staticmethod
    def _error_result(execution: dict[str, Any]) -> dict[str, Any]:
        status = str(execution.get("status") or "failed")
        if status == "unknown":
            message = (
                "The worker lease expired after a side-effecting tool started. "
                "The system refused to replay it automatically; verify the business result before retrying."
            )
        else:
            message = str(execution.get("error") or f"External tool ended with status {status}.")
        return {
            "error": message,
            "execution_id": execution.get("execution_id"),
            "execution_status": status,
        }


external_tool_executor = ExternalToolExecutor()
