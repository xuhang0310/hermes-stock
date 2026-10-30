"""User-owned research schedules exposed to authenticated MCP callers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator
from sqlalchemy import select

from backend.app.core.database import SessionLocal
from backend.app.core.models import DigitalHumanProfile
from backend.app.mcp_gateway.mutations import run_idempotent_mutation
from backend.app.scheduler.schedule_service import AgentScheduleService


SHANGHAI = ZoneInfo("Asia/Shanghai")
ClockTime = Annotated[str, Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]
Weekday = Annotated[int, Field(strict=True, ge=1, le=7)]


class _StrictSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OnceSchedule(_StrictSchedule):
    type: Literal["once"]
    run_at: datetime = Field(description="ISO 8601 时间；无时区时按北京时间解释。")


class DailySchedule(_StrictSchedule):
    type: Literal["daily"]
    run_time: ClockTime


class WeeklySchedule(_StrictSchedule):
    type: Literal["weekly"]
    run_time: ClockTime
    weekdays: list[Weekday] = Field(min_length=1, max_length=7, description="周一为 1，周日为 7。")

    @field_validator("weekdays")
    @classmethod
    def normalize_weekdays(cls, value: list[int]) -> list[int]:
        return sorted(set(value))


class MonthlyLastSchedule(_StrictSchedule):
    type: Literal["monthly_last"]
    run_time: ClockTime


class IntervalSchedule(_StrictSchedule):
    type: Literal["interval"]
    interval_minutes: Annotated[int, Field(strict=True, ge=1, le=1440)]
    window_start: ClockTime = "00:00"
    window_end: ClockTime = "23:59"

    @model_validator(mode="after")
    def validate_window(self) -> IntervalSchedule:
        if self.window_start > self.window_end:
            raise ValueError("window_start 不能晚于 window_end。")
        return self


ScheduleSpec = Annotated[
    OnceSchedule | DailySchedule | WeeklySchedule | MonthlyLastSchedule | IntervalSchedule,
    Field(discriminator="type"),
]
_SCHEDULE_ADAPTER = TypeAdapter(ScheduleSpec)


class _CreateSchedule(_StrictSchedule):
    name: str = Field(min_length=1, max_length=160)
    instruction: str = Field(min_length=1, max_length=16000)
    target_agent_code: str = Field(min_length=1, max_length=80)
    schedule: ScheduleSpec
    trading_day_only: bool = Field(default=False, strict=True)

    @field_validator("name", "instruction", "target_agent_code")
    @classmethod
    def nonempty_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("名称、指令和数字人代码不能为空。")
        return value.strip()


def _dated_job(job: dict[str, Any]) -> dict[str, Any]:
    result = dict(job)
    result["timezone"] = "Asia/Shanghai"
    # The scheduler stores run times in local wall clock, DB defaults in UTC.
    for key, source_zone in (
        ("run_once_at", SHANGHAI),
        ("last_run_at", SHANGHAI),
        ("created_at", timezone.utc),
        ("updated_at", timezone.utc),
    ):
        if result.get(key):
            value = datetime.fromisoformat(result[key])
            if value.tzinfo is None:
                value = value.replace(tzinfo=source_zone)
            result[key] = value.astimezone(SHANGHAI).isoformat()
    return result


def list_schedules(user_id: int, include_disabled: bool = True) -> dict[str, Any]:
    """List only this user's schedules; never expose system/other-user jobs."""
    service = AgentScheduleService(SessionLocal)
    jobs = [_dated_job(job) for job in service.list_jobs(user_id, include_disabled)]
    return {"jobs": jobs, "total": len(jobs), "timezone": "Asia/Shanghai"}


def get_schedule(user_id: int, job_key: str) -> dict[str, Any]:
    return {"job": _dated_job(AgentScheduleService(SessionLocal).get_job(user_id, job_key))}


def list_research_profiles(user_id: int) -> dict[str, Any]:
    """Return only enabled, role-accessible profiles allowed for research schedules."""
    service = AgentScheduleService(SessionLocal)
    with SessionLocal() as db:
        service._actor(db, user_id)
        candidates = db.scalars(
            select(DigitalHumanProfile)
            .where(DigitalHumanProfile.status == "enabled")
            .order_by(DigitalHumanProfile.sort_order, DigitalHumanProfile.id)
        ).all()
        profiles = []
        for candidate in candidates:
            try:
                profile = service._target_profile(db, candidate.code, user_id)
            except (ValueError, PermissionError):
                continue
            profiles.append({"code": profile.code, "name": profile.name})
    return {"profiles": profiles, "total": len(profiles)}


def create_schedule(
    user_id: int,
    name: str,
    instruction: str,
    target_agent_code: str,
    schedule: dict[str, Any] | _StrictSchedule,
    idempotency_key: str,
    trading_day_only: bool = False,
) -> dict[str, Any]:
    """Create an enabled research task with retries deduplicated in one transaction.

    This entry point never enables Python execution or pushes to a shared chat.
    It records a future task; it does not execute it immediately.
    """
    validated_schedule = _SCHEDULE_ADAPTER.validate_python(schedule)
    payload = _CreateSchedule(
        name=name,
        instruction=instruction,
        target_agent_code=target_agent_code,
        schedule=validated_schedule,
        trading_day_only=trading_day_only,
    ).model_dump(mode="json")
    service = AgentScheduleService(SessionLocal)
    with SessionLocal() as db:
        def operation() -> dict[str, Any]:
            result = service.create_job(
                user_id=user_id,
                run_id=None,
                args={**payload, "push_to_feishu": False, "allow_code_execution": False},
                db=db,
                commit=False,
            )
            return {**result, "job": _dated_job(result["job"])}

        return run_idempotent_mutation(
            db, user_id, "create_schedule", idempotency_key, payload, operation,
        )
