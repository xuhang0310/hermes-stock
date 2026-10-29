from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.auth.auth import BEIJING_TZ, AuthContext, hash_password, require_permission
from backend.app.core.applog import log_event
from backend.app.core.database import get_db
from backend.app.core.models import LoginLog, User


router = APIRouter(prefix="/api/users", tags=["users"])
UserRole = Literal["super_admin", "admin", "researcher", "viewer"]
_USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,79}$")


class UserCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=80)
    display_name: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=8, max_length=120)
    role: UserRole = "researcher"


class UserUpdateIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    role: UserRole | None = None
    is_enabled: bool | None = None


class PasswordResetIn(BaseModel):
    new_password: str = Field(min_length=8, max_length=120)


def _out(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
        "role": user.role,
        "is_enabled": bool(user.is_enabled),
        "last_login_at": user.last_login_at.replace(tzinfo=BEIJING_TZ).isoformat()
        if user.last_login_at else None,
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "updated_at": user.updated_at.isoformat() if user.updated_at else None,
    }


def _target_or_404(db: Session, user_id: int) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    return user


def _assert_role_authority(actor: User, role: str) -> None:
    if actor.is_super_admin:
        return
    if role in {"super_admin", "admin"}:
        raise HTTPException(status_code=403, detail="只有超级管理员可以授予管理员角色")


def _assert_target_authority(actor: User, target: User) -> None:
    if not actor.is_super_admin and target.role in {"super_admin", "admin"} and target.id != actor.id:
        raise HTTPException(status_code=403, detail="只有超级管理员可以管理管理员账号")


def _assert_not_last_super_admin(db: Session, target: User) -> None:
    if not target.is_super_admin or not target.is_enabled:
        return
    enabled_super_admins = int(
        db.scalar(
            select(func.count()).select_from(User).where(
                User.role == "super_admin",
                User.is_enabled.is_(True),
            )
        )
        or 0
    )
    if enabled_super_admins <= 1:
        raise HTTPException(status_code=409, detail="不能停用或降级最后一个超级管理员")


@router.get("")
def list_users(
    db: Annotated[Session, Depends(get_db)],
    _context: Annotated[AuthContext, Depends(require_permission("user:manage"))],
) -> list[dict]:
    rows = db.scalars(select(User).order_by(User.created_at, User.id)).all()
    return [_out(row) for row in rows]


@router.post("", status_code=201)
def create_user(
    payload: UserCreateIn,
    db: Annotated[Session, Depends(get_db)],
    context: Annotated[AuthContext, Depends(require_permission("user:manage"))],
) -> dict:
    username = payload.username.strip()
    display_name = payload.display_name.strip()
    if not _USERNAME.fullmatch(username):
        raise HTTPException(status_code=422, detail="用户名只能包含字母、数字、点、横线和下划线")
    _assert_role_authority(context.user, payload.role)
    duplicate = db.scalar(
        select(User.id).where(func.lower(User.username) == username.lower()).limit(1)
    )
    if duplicate is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")

    user = User(
        username=username,
        display_name=display_name,
        role=payload.role,
        permission_scope="",
        password_hash=hash_password(payload.password),
        token_version=1,
        is_enabled=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    log_event(
        "auth",
        "INFO",
        "创建用户",
        actor_user_id=context.user.id,
        target_user_id=user.id,
        target_username=user.username,
        role=user.role,
    )
    return _out(user)


@router.get("/login-logs")
def list_login_logs(
    db: Annotated[Session, Depends(get_db)],
    _context: Annotated[AuthContext, Depends(require_permission("user:manage"))],
    username: Annotated[str | None, Query(max_length=80)] = None,
    success: bool | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict:
    """管理员登录审计；日期筛选按北京时间，持久化时间按 UTC。

    按日志中原始账号筛选，不关联当前 users 表，保留未知账号和历史账号的失败记录。
    """
    if start_date and end_date and start_date > end_date:
        raise HTTPException(status_code=422, detail="开始日期不能晚于结束日期")
    if end_date == date.max:
        raise HTTPException(status_code=422, detail="结束日期超出支持范围")
    if start_date == date.min:
        raise HTTPException(status_code=422, detail="开始日期超出支持范围")

    filters = []
    if username and username.strip():
        filters.append(func.lower(LoginLog.username) == username.strip().lower())
    if success is not None:
        filters.append(LoginLog.success.is_(success))
    if start_date:
        start = datetime.combine(start_date, time.min, tzinfo=BEIJING_TZ)
        filters.append(LoginLog.created_at >= start.astimezone(timezone.utc).replace(tzinfo=None))
    if end_date:
        end = datetime.combine(end_date + timedelta(days=1), time.min, tzinfo=BEIJING_TZ)
        filters.append(LoginLog.created_at < end.astimezone(timezone.utc).replace(tzinfo=None))

    total = int(db.scalar(select(func.count()).select_from(LoginLog).where(*filters)) or 0)
    rows = db.scalars(
        select(LoginLog)
        .where(*filters)
        .order_by(LoginLog.created_at.desc(), LoginLog.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "username": row.username,
                "user_id": row.user_id,
                "role": row.role,
                "success": bool(row.success),
                "reason": row.reason,
                "ip": row.ip,
                "user_agent": row.user_agent,
                "trace_id": row.trace_id,
                "created_at": row.created_at.replace(tzinfo=timezone.utc).isoformat()
                if row.created_at else None,
            }
            for row in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.patch("/{user_id}")
def update_user(
    user_id: int,
    payload: UserUpdateIn,
    db: Annotated[Session, Depends(get_db)],
    context: Annotated[AuthContext, Depends(require_permission("user:manage"))],
) -> dict:
    target = _target_or_404(db, user_id)
    _assert_target_authority(context.user, target)

    if payload.role is not None and payload.role != target.role:
        if target.id == context.user.id:
            raise HTTPException(status_code=409, detail="不能修改自己的角色")
        _assert_role_authority(context.user, payload.role)
        if target.is_super_admin:
            _assert_not_last_super_admin(db, target)
        target.role = payload.role
        target.permission_scope = ""
        target.token_version = int(target.token_version or 1) + 1

    if payload.is_enabled is not None and payload.is_enabled != target.is_enabled:
        if target.id == context.user.id:
            raise HTTPException(status_code=409, detail="不能停用当前登录账号")
        if not payload.is_enabled:
            _assert_not_last_super_admin(db, target)
        target.is_enabled = payload.is_enabled
        target.token_version = int(target.token_version or 1) + 1

    if payload.display_name is not None:
        target.display_name = payload.display_name.strip()

    target.updated_at = datetime.now()
    db.commit()
    db.refresh(target)
    log_event(
        "auth",
        "INFO",
        "更新用户",
        actor_user_id=context.user.id,
        target_user_id=target.id,
        role=target.role,
        enabled=target.is_enabled,
    )
    return _out(target)


@router.post("/{user_id}/reset-password")
def reset_password(
    user_id: int,
    payload: PasswordResetIn,
    db: Annotated[Session, Depends(get_db)],
    context: Annotated[AuthContext, Depends(require_permission("user:manage"))],
) -> dict:
    target = _target_or_404(db, user_id)
    _assert_target_authority(context.user, target)
    target.password_hash = hash_password(payload.new_password)
    target.token_version = int(target.token_version or 1) + 1
    db.commit()
    log_event(
        "auth",
        "INFO",
        "重置用户密码",
        actor_user_id=context.user.id,
        target_user_id=target.id,
    )
    return {"ok": True, "reauth_required": target.id == context.user.id}
