from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.app.auth.auth import (
    AuthContext,
    access_token_ttl_seconds,
    authenticate_user,
    create_access_token,
    clear_login_failures,
    get_current_user,
    hash_password,
    login_retry_after,
    permissions_for,
    record_login_failure,
    user_payload,
    verify_password,
)
from backend.app.core.applog import log_event
from backend.app.core.database import get_db
from backend.app.core.models import LoginLog
from backend.app.core.request_context import get_trace_id


router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=120)
    remember_me: bool = True


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=120)
    new_password: str = Field(min_length=8, max_length=120)


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip() or "unknown"
    return request.client.host if request.client else "unknown"


def _record_login(
    db: Session,
    *,
    username: str,
    user,
    success: bool,
    reason: str | None,
    request: Request,
) -> None:
    """落一条登录审计到 login_logs（只增；失败也记，user_id 可空）。"""
    db.add(
        LoginLog(
            username=username[:80],
            user_id=user.id if user is not None else None,
            role=user.role if user is not None else None,
            success=success,
            reason=reason,
            ip=_client_ip(request),
            user_agent=(request.headers.get("user-agent") or "")[:255] or None,
            trace_id=get_trace_id(),
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
    )
    db.commit()


@router.post("/login")
def login(payload: LoginIn, request: Request, db: Annotated[Session, Depends(get_db)]) -> dict:
    username = payload.username.strip()
    client_ip = _client_ip(request)
    retry_after = login_retry_after(username=username, client_ip=client_ip)
    if retry_after:
        _record_login(
            db, username=username, user=None, success=False, reason="rate_limited", request=request
        )
        raise HTTPException(
            status_code=429,
            detail=f"登录失败次数过多，请在 {retry_after} 秒后重试",
            headers={"Retry-After": str(retry_after)},
        )
    user = authenticate_user(db, username, payload.password)
    if user is None:
        record_login_failure(username=username, client_ip=client_ip)
        log_event("auth", "WARNING", "登录失败", username=username)
        _record_login(
            db, username=username, user=None, success=False, reason="bad_credentials", request=request
        )
        raise HTTPException(status_code=401, detail="账号或密码错误，或账号未启用")
    clear_login_failures(username=username)
    context = AuthContext(user=user, permissions=permissions_for(user))
    log_event("auth", "INFO", "登录成功", username=username, role=user.role)
    _record_login(
        db, username=username, user=user, success=True, reason=None, request=request
    )
    expires_in = access_token_ttl_seconds(remember_me=payload.remember_me)
    return {
        "access_token": create_access_token(user, remember_me=payload.remember_me),
        "token_type": "bearer",
        "expires_in": expires_in,
        "remember_me": payload.remember_me,
        "user": user_payload(context),
    }


@router.get("/me")
def me(context: Annotated[AuthContext, Depends(get_current_user)]) -> dict:
    return user_payload(context)


@router.post("/change-password")
def change_password(
    payload: ChangePasswordIn,
    context: Annotated[AuthContext, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    user = context.user
    if not verify_password(payload.old_password, user.password_hash):
        raise HTTPException(status_code=400, detail="原密码不正确")
    user.password_hash = hash_password(payload.new_password)
    user.token_version = int(user.token_version or 1) + 1
    db.commit()
    return {"ok": True, "reauth_required": True}


@router.post("/logout-all")
def logout_all(
    context: Annotated[AuthContext, Depends(get_current_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    context.user.token_version = int(context.user.token_version or 1) + 1
    db.commit()
    return {"ok": True}
