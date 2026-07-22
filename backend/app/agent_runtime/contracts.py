from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


RunStatus = Literal[
    "queued",
    "running",
    "waiting_approval",
    "waiting_children",
    "completed",
    "failed",
    "cancelled",
]

TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "cancelled"})
RUNNABLE_RUN_STATUSES = frozenset({"queued"})


@dataclass
class EngineOutcome:
    status: RunStatus = "completed"
    final_answer: str = ""
    error: str | None = None
    checkpoint_json: str | None = None
    waiting: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class StoredRunEvent:
    run_id: str
    seq: int
    event_type: str
    payload: dict[str, Any]
    created_at: str | None = None
