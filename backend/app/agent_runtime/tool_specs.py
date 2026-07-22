from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


SideEffect = Literal["none", "workspace", "business", "external"]
Idempotency = Literal["pure", "key_required", "non_retryable"]
ExecutionMode = Literal["inline", "worker", "sandbox"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    version: str = "v1"
    side_effect: SideEffect = "none"
    idempotency: Idempotency = "pure"
    execution_mode: ExecutionMode = "inline"
    timeout_seconds: int = 30
    max_attempts: int = 2
    is_read_only: bool = True
    is_concurrency_safe: bool = True

    @property
    def retry_safe(self) -> bool:
        return self.idempotency in {"pure", "key_required"}


_EXTERNAL_READ_TOOLS = {
    "get_intelligence_digest",
    "rank_intelligence_sources",
    "get_macro_policy_digest",
    "get_hotspot_momentum",
}
_BUSINESS_WRITE_TOOLS = {
    "save_position_holdings",
    "add_watchlist_items",
    "remove_watchlist_items",
    "update_my_positions",
    "update_position_field",
    "rollback_last_position_batch",
}


def tool_spec(tool_name: str, *, write_names: set[str] | None = None) -> ToolSpec:
    """Return the hard execution policy for one tool.

    Model text never controls these values. Unknown tools remain short inline
    reads, while every known side effect receives an explicit replay policy.
    """

    if tool_name == "ws_run_python":
        return ToolSpec(
            name=tool_name,
            side_effect="workspace",
            idempotency="non_retryable",
            execution_mode="sandbox",
            timeout_seconds=120,
            max_attempts=1,
            is_read_only=False,
            is_concurrency_safe=False,
        )
    if tool_name == "ws_write_file":
        return ToolSpec(
            name=tool_name,
            side_effect="workspace",
            idempotency="key_required",
            execution_mode="worker",
            timeout_seconds=60,
            max_attempts=2,
            is_read_only=False,
            is_concurrency_safe=False,
        )
    if tool_name == "ws_edit_file":
        return ToolSpec(
            name=tool_name,
            side_effect="workspace",
            idempotency="non_retryable",
            execution_mode="worker",
            timeout_seconds=60,
            max_attempts=1,
            is_read_only=False,
            is_concurrency_safe=False,
        )
    if tool_name in _BUSINESS_WRITE_TOOLS or tool_name in (write_names or set()):
        return ToolSpec(
            name=tool_name,
            side_effect="business",
            idempotency="non_retryable",
            execution_mode="worker",
            timeout_seconds=45,
            max_attempts=1,
            is_read_only=False,
            is_concurrency_safe=False,
        )
    if tool_name in _EXTERNAL_READ_TOOLS:
        return ToolSpec(
            name=tool_name,
            side_effect="none",
            idempotency="pure",
            execution_mode="worker",
            timeout_seconds=60,
            max_attempts=3,
        )
    return ToolSpec(name=tool_name)


def should_externalize(spec: ToolSpec, *, enabled: bool) -> bool:
    return enabled and spec.execution_mode in {"worker", "sandbox"}
