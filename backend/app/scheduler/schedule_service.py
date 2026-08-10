from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from backend.app.core.database import SessionLocal
from backend.app.core.models import DigitalHumanProfile, ScheduledJob, User
from backend.app.agent_runtime.profile_access import target_roles_allow
from backend.app.feishu.feishu import SCHEDULE_CHANNEL_KEY
from backend.app.scheduler.rhythm_scheduler import DH_TO_AGENT


_TIME_PATTERN = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_SCHEDULE_TYPES = frozenset({"once", "daily", "weekly", "monthly_last", "interval"})
_FORBIDDEN_TARGETS = frozenset(
    {"ask_stock", "position-entry-agent", "watchlist-entry-agent"}
)
_SCHEDULE_AGENT_ALIASES = {
    "orchestrator": "stock-pm",
    "pm": "stock-pm",
    "stock_pm": "stock-pm",
    "总调度": "stock-pm",
    "总调度官": "stock-pm",
}


class AgentScheduleService:
    def __init__(
        self,
        session_factory: Callable[[], Session] | sessionmaker[Session] = SessionLocal,
    ) -> None:
        self.session_factory = session_factory

    def execute(self, name: str, args: dict[str, Any], request_payload: dict[str, Any]) -> dict[str, Any]:
        user_id = int(request_payload["user_id"])
        run_id = str(request_payload.get("run_id") or "") or None
        if (
            name in {"create_agent_schedule", "update_agent_schedule"}
            and bool(args.get("allow_code_execution"))
            and not bool(request_payload.get("allow_code_execution"))
        ):
            raise PermissionError("请先为本次总调度会话显式开启 Python 执行权限。")
        if name == "list_agent_schedules":
            jobs = self.list_jobs(user_id, bool(args.get("include_disabled", True)))
            return {
                "jobs": jobs,
                "total": len(jobs),
                "enabled_total": sum(1 for job in jobs if job["enabled"]),
            }
        if name == "get_agent_schedule":
            return {"job": self.get_job(user_id, str(args.get("job_key") or ""))}
        if name == "create_agent_schedule":
            return self.create_job(user_id=user_id, run_id=run_id, args=args)
        if name == "update_agent_schedule":
            return self.update_job(user_id=user_id, run_id=run_id, args=args)
        if name == "pause_agent_schedule":
            return self.set_enabled(
                user_id=user_id,
                job_key=str(args.get("job_key") or ""),
                enabled=bool(args.get("enabled")),
            )
        if name == "delete_agent_schedule":
            return self.delete_job(user_id=user_id, job_key=str(args.get("job_key") or ""))
        return {"error": f"Unknown schedule tool: {name}"}

    def list_jobs(self, user_id: int, include_disabled: bool = True) -> list[dict[str, Any]]:
        with self.session_factory() as db:
            actor = self._actor(db, user_id)
            query = select(ScheduledJob).where(ScheduledJob.owner_user_id == user_id)
            if not include_disabled:
                query = query.where(ScheduledJob.enabled.is_(True))
            rows = db.scalars(query.order_by(ScheduledJob.sort_order, ScheduledJob.id)).all()
            return [self._out(row, actor=actor) for row in rows]

    def get_job(self, user_id: int, job_key: str) -> dict[str, Any]:
        with self.session_factory() as db:
            actor = self._actor(db, user_id)
            job = self._visible_job(db, actor, job_key)
            return self._out(job, actor=actor)

    def create_job(self, *, user_id: int, run_id: str | None, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("name") or "").strip()
        instruction = str(args.get("instruction") or "").strip()
        target_code = str(args.get("target_agent_code") or "").strip()
        if not name or not instruction or not target_code:
            raise ValueError("名称、执行数字人和任务指令不能为空。")
        if len(name) > 160 or len(instruction) > 16000:
            raise ValueError("任务名称或指令过长。")
        schedule = self._normalize_schedule(args.get("schedule"))
        with self.session_factory() as db:
            actor = self._actor(db, user_id)
            profile = self._target_profile(db, target_code, user_id)
            expected = {
                "name": name,
                "instruction": instruction,
                "target_agent_code": profile.code,
                "trading_day_only": bool(args.get("trading_day_only", False)),
                "push_to_feishu": bool(args.get("push_to_feishu", False)),
                "allow_code_execution": bool(args.get("allow_code_execution", False)),
                "enabled": True,
                **schedule,
            }
            existing = db.scalar(
                select(ScheduledJob)
                .where(
                    ScheduledJob.owner_user_id == user_id,
                    ScheduledJob.created_via == "agent",
                    ScheduledJob.name == name,
                )
                .order_by(ScheduledJob.id.desc())
                .limit(1)
            )
            if existing is not None:
                if not self._matches_create(existing, expected):
                    raise ValueError(
                        f"已存在同名定时任务 {existing.job_key}，但配置不同。"
                        "请先查询并修改该任务，或使用不同的任务名称。"
                    )
                return {
                    "created": False,
                    "already_exists": True,
                    "verified": True,
                    "job": self._out(existing, actor=actor),
                }
            job = ScheduledJob(
                job_key=f"agent-{user_id}-{uuid.uuid4().hex[:12]}",
                name=name,
                description=f"由总调度创建，执行者：{profile.name}",
                job_kind="agent_task",
                digital_human_code=profile.code,
                agent_name=DH_TO_AGENT.get(profile.code),
                prompt_template=instruction,
                trading_day_only=bool(args.get("trading_day_only", False)),
                push_channel_key=SCHEDULE_CHANNEL_KEY if args.get("push_to_feishu") else None,
                owner_user_id=user_id,
                created_by_run_id=run_id,
                created_via="agent",
                allow_code_execution=bool(args.get("allow_code_execution", False)),
                approval_policy="full",
                enabled=True,
                timezone="Asia/Shanghai",
                **schedule,
            )
            db.add(job)
            db.commit()
            db.refresh(job)
            if not self._matches_create(job, expected):
                raise RuntimeError("定时任务已写入，但数据库回读校验未通过，请按任务名称核查。")
            return {
                "created": True,
                "already_exists": False,
                "verified": True,
                "job": self._out(job, actor=actor),
            }

    def update_job(self, *, user_id: int, run_id: str | None, args: dict[str, Any]) -> dict[str, Any]:
        job_key = str(args.get("job_key") or "").strip()
        with self.session_factory() as db:
            actor = self._actor(db, user_id)
            job = self._editable_job(db, actor, job_key)
            expected: dict[str, Any] = {}
            if "name" in args:
                value = str(args.get("name") or "").strip()
                if not value:
                    raise ValueError("任务名称不能为空。")
                job.name = value[:160]
                expected["name"] = job.name
            if "instruction" in args:
                value = str(args.get("instruction") or "").strip()
                if not value:
                    raise ValueError("任务指令不能为空。")
                job.prompt_template = value[:16000]
                expected["prompt_template"] = job.prompt_template
            if "target_agent_code" in args:
                profile = self._target_profile(
                    db,
                    str(args.get("target_agent_code") or ""),
                    user_id,
                )
                job.digital_human_code = profile.code
                job.agent_name = DH_TO_AGENT.get(profile.code)
                expected["digital_human_code"] = profile.code
            if "schedule" in args:
                schedule = self._normalize_schedule(args.get("schedule"))
                for key, value in schedule.items():
                    setattr(job, key, value)
                    expected[key] = value
            if "trading_day_only" in args:
                job.trading_day_only = bool(args.get("trading_day_only"))
                expected["trading_day_only"] = job.trading_day_only
            if "push_to_feishu" in args:
                job.push_channel_key = SCHEDULE_CHANNEL_KEY if args.get("push_to_feishu") else None
                expected["push_channel_key"] = job.push_channel_key
            if "allow_code_execution" in args:
                job.allow_code_execution = bool(args.get("allow_code_execution"))
                expected["allow_code_execution"] = job.allow_code_execution
            if "enabled" in args:
                job.enabled = bool(args.get("enabled"))
                expected["enabled"] = job.enabled
            job.created_by_run_id = run_id or job.created_by_run_id
            db.commit()
            db.refresh(job)
            if any(getattr(job, key) != value for key, value in expected.items()):
                raise RuntimeError("定时任务修改后的数据库回读校验未通过。")
            return {"updated": True, "verified": True, "job": self._out(job, actor=actor)}

    def set_enabled(self, *, user_id: int, job_key: str, enabled: bool) -> dict[str, Any]:
        with self.session_factory() as db:
            actor = self._actor(db, user_id)
            job = self._editable_job(db, actor, job_key)
            job.enabled = enabled
            db.commit()
            db.refresh(job)
            if bool(job.enabled) != enabled:
                raise RuntimeError("定时任务启停状态的数据库回读校验未通过。")
            return {"updated": True, "verified": True, "job": self._out(job, actor=actor)}

    def delete_job(self, *, user_id: int, job_key: str) -> dict[str, Any]:
        with self.session_factory() as db:
            actor = self._actor(db, user_id)
            job = self._editable_job(db, actor, job_key)
            db.delete(job)
            db.commit()
            remaining = db.scalar(
                select(ScheduledJob.id).where(ScheduledJob.job_key == job_key).limit(1)
            )
            if remaining is not None:
                raise RuntimeError("定时任务删除后的数据库回读校验未通过。")
            return {"deleted": True, "verified": True, "job_key": job_key}

    @staticmethod
    def _actor(db: Session, user_id: int) -> User:
        actor = db.get(User, user_id)
        if actor is None or not actor.is_enabled:
            raise PermissionError("当前用户不存在或已停用。")
        return actor

    @classmethod
    def _can_edit(cls, actor: User, job: ScheduledJob) -> bool:
        return job.owner_user_id == actor.id

    @classmethod
    def _visible_job(cls, db: Session, actor: User, job_key: str) -> ScheduledJob:
        job = db.scalar(
            select(ScheduledJob).where(
                ScheduledJob.job_key == job_key,
                ScheduledJob.owner_user_id == actor.id,
            ).limit(1)
        )
        if job is None:
            raise ValueError("定时任务不存在。")
        return job

    @classmethod
    def _editable_job(cls, db: Session, actor: User, job_key: str) -> ScheduledJob:
        job = cls._visible_job(db, actor, job_key)
        if not cls._can_edit(actor, job):
            raise PermissionError("当前用户无权修改该定时任务。")
        return job

    @staticmethod
    def _target_profile(db: Session, code: str, user_id: int) -> DigitalHumanProfile:
        requested = code.strip()
        normalized = _SCHEDULE_AGENT_ALIASES.get(requested.casefold(), requested)
        profile = db.scalar(
            select(DigitalHumanProfile).where(
                DigitalHumanProfile.code == normalized,
                DigitalHumanProfile.status == "enabled",
            ).limit(1)
        )
        if profile is None and requested:
            profile = db.scalar(
                select(DigitalHumanProfile).where(
                    DigitalHumanProfile.name == requested,
                    DigitalHumanProfile.status == "enabled",
                ).limit(1)
            )
            if profile is not None:
                normalized = profile.code
        if profile is None or normalized in _FORBIDDEN_TARGETS:
            raise ValueError(f"数字人 {normalized or '-'} 不可用于定时任务。")
        owner = db.get(User, user_id)
        if owner is None or not owner.is_enabled:
            raise PermissionError("定时任务所有者不存在或已停用。")
        if not target_roles_allow(profile.target_roles, owner.role):
            raise PermissionError(f"当前用户角色无权调用数字人 {normalized}。")
        return profile

    @staticmethod
    def _matches_create(job: ScheduledJob, expected: dict[str, Any]) -> bool:
        actual = {
            "name": job.name,
            "instruction": job.prompt_template or "",
            "target_agent_code": job.digital_human_code or "",
            "trading_day_only": bool(job.trading_day_only),
            "push_to_feishu": bool(job.push_channel_key),
            "allow_code_execution": bool(job.allow_code_execution),
            "enabled": bool(job.enabled),
            "schedule_type": job.schedule_type,
            "run_time": job.run_time,
            "weekday_mask": job.weekday_mask,
            "interval_minutes": job.interval_minutes,
            "window_start": job.window_start,
            "window_end": job.window_end,
            "run_once_at": job.run_once_at,
        }
        return actual == expected

    @staticmethod
    def _normalize_schedule(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("schedule 必须是结构化对象。")
        schedule_type = str(value.get("type") or "").strip()
        if schedule_type not in _SCHEDULE_TYPES:
            raise ValueError("不支持的调度类型。")
        result: dict[str, Any] = {
            "schedule_type": schedule_type,
            "run_time": None,
            "weekday_mask": None,
            "interval_minutes": None,
            "window_start": None,
            "window_end": None,
            "run_once_at": None,
        }
        if schedule_type == "once":
            raw = str(value.get("run_at") or "").strip()
            try:
                run_once_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("一次任务的 run_at 必须是 ISO 8601 时间。") from exc
            if run_once_at.tzinfo is not None:
                run_once_at = run_once_at.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
            if run_once_at <= datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None):
                raise ValueError("一次任务的执行时间必须晚于当前时间。")
            result["run_once_at"] = run_once_at
        elif schedule_type in {"daily", "weekly", "monthly_last"}:
            run_time = str(value.get("run_time") or "").strip()
            AgentScheduleService._validate_time(run_time, "run_time")
            result["run_time"] = run_time
            if schedule_type == "weekly":
                weekdays = value.get("weekdays")
                if not isinstance(weekdays, list) or not weekdays:
                    raise ValueError("每周任务必须提供 weekdays。")
                normalized = sorted({int(day) for day in weekdays})
                if any(day < 1 or day > 7 for day in normalized):
                    raise ValueError("weekdays 只能是 1 到 7。")
                result["weekday_mask"] = ",".join(str(day) for day in normalized)
        else:
            interval = int(value.get("interval_minutes") or 0)
            if interval < 1 or interval > 1440:
                raise ValueError("interval_minutes 必须在 1 到 1440 之间。")
            result["interval_minutes"] = interval
            start = str(value.get("window_start") or "00:00").strip()
            end = str(value.get("window_end") or "23:59").strip()
            AgentScheduleService._validate_time(start, "window_start")
            AgentScheduleService._validate_time(end, "window_end")
            if start > end:
                raise ValueError("window_start 不能晚于 window_end。")
            result["window_start"] = start
            result["window_end"] = end
        return result

    @staticmethod
    def _validate_time(value: str, field: str) -> None:
        if not _TIME_PATTERN.fullmatch(value):
            raise ValueError(f"{field} 必须是 HH:MM。")

    @classmethod
    def _out(cls, job: ScheduledJob, *, actor: User) -> dict[str, Any]:
        if job.owner_user_id == actor.id:
            scope = "own"
        elif job.owner_user_id is None:
            scope = "system"
        else:
            scope = "managed"
        return {
            "job_key": job.job_key,
            "name": job.name,
            "job_kind": job.job_kind,
            "schedule_type": job.schedule_type,
            "run_time": job.run_time,
            "run_once_at": job.run_once_at.isoformat() if job.run_once_at else None,
            "weekday_mask": job.weekday_mask,
            "interval_minutes": job.interval_minutes,
            "window_start": job.window_start,
            "window_end": job.window_end,
            "trading_day_only": bool(job.trading_day_only),
            "target_agent_code": job.digital_human_code,
            "instruction": job.prompt_template,
            "push_to_feishu": bool(job.push_channel_key),
            "allow_code_execution": bool(job.allow_code_execution),
            "enabled": bool(job.enabled),
            "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
            "last_status": job.last_status,
            "last_error": job.last_error,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "updated_at": job.updated_at.isoformat() if job.updated_at else None,
            "created_via": job.created_via,
            "scope": scope,
            "editable": cls._can_edit(actor, job),
        }


agent_schedule_service = AgentScheduleService()
