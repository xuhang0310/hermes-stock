"""运营后台（oc-console）只读路由。

仅对持有 ``ops_console:view`` 权限的用户开放（生产仅 zhangsan；super_admin 的 "all" 自然放行）。
聚合跨用户视图：持仓、自选股、数字人对话历史。全部只读，零写操作。

设计说明：
- 持仓/自选的 repository mixin 输出不含 owner 字段，故按用户遍历 ``for_user(u.id).positions()/watchlist()``
  后给每行补 ``owner_username``/``owner_display_name``；mixin 不动，既有 ``/api/positions``、``/api/watchlist`` 字节不变。
- 会话跨用户查看：不复用 ``ai_api._session_visibility``（仅本人语义不符）与私有 ``_chat_messages_out``，
  在此直接 join 查 + 最小重塑，原端点不动。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.auth.auth import AuthContext, require_permission
from backend.app.core.database import get_db
from backend.app.core.models import (
    AiChatMessage,
    AiChatSession,
    DigitalHumanProfile,
    LoginLog,
    User,
)
from backend.app.core.response import ok
from backend.app.data.deps import get_repo
from backend.app.data.repository import StockRepository

router = APIRouter()


def _user_summary(user: User) -> dict:
    return {
        "id": user.id,
        "username": user.username,
        "display_name": user.display_name,
    }


@router.get("/api/ops-console/users")
def ops_console_users(
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("ops_console:view")),
) -> dict:
    """启用用户清单（前端筛选下拉）。"""
    users = db.scalars(
        select(User).where(User.is_enabled.is_(True)).order_by(User.id)
    ).all()
    return ok([_user_summary(u) for u in users])


@router.get("/api/ops-console/positions")
def ops_console_positions(
    user_id: int | None = Query(default=None),
    repository: StockRepository = Depends(get_repo),
    _context: AuthContext = Depends(require_permission("ops_console:view")),
) -> dict:
    """跨用户持仓快照。``user_id`` 非空时只查该用户。"""
    users = repository.db.scalars(
        select(User).where(User.is_enabled.is_(True)).order_by(User.id)
    ).all()
    if user_id is not None:
        users = [u for u in users if u.id == user_id]
    rows: list[dict] = []
    for u in users:
        for item in repository.for_user(u.id).positions():
            item["owner_user_id"] = u.id
            item["owner_username"] = u.username
            item["owner_display_name"] = u.display_name
            rows.append(item)
    return ok(rows)


@router.get("/api/ops-console/watchlist")
def ops_console_watchlist(
    user_id: int | None = Query(default=None),
    repository: StockRepository = Depends(get_repo),
    _context: AuthContext = Depends(require_permission("ops_console:view")),
) -> dict:
    """跨用户自选股。``user_id`` 非空时只查该用户。"""
    users = repository.db.scalars(
        select(User).where(User.is_enabled.is_(True)).order_by(User.id)
    ).all()
    if user_id is not None:
        users = [u for u in users if u.id == user_id]
    rows: list[dict] = []
    for u in users:
        for item in repository.for_user(u.id).watchlist():
            item["owner_user_id"] = u.id
            item["owner_username"] = u.username
            item["owner_display_name"] = u.display_name
            rows.append(item)
    return ok(rows)


@router.get("/api/ops-console/chat-sessions")
def ops_console_chat_sessions(
    user_id: int | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("ops_console:view")),
) -> dict:
    """跨用户数字人对话会话列表。"""
    stmt = (
        select(AiChatSession, User, DigitalHumanProfile)
        .join(User, User.id == AiChatSession.user_id)
        .outerjoin(DigitalHumanProfile, DigitalHumanProfile.id == AiChatSession.digital_human_id)
        .where(AiChatSession.status != "deleted")
        .order_by(AiChatSession.updated_at.desc())
        .limit(limit)
    )
    if user_id is not None:
        stmt = stmt.where(AiChatSession.user_id == user_id)
    rows = db.execute(stmt).all()

    session_ids = [session.id for session, _, _ in rows]
    message_counts: dict[int, int] = {}
    if session_ids:
        count_rows = db.execute(
            select(AiChatMessage.session_id.label("sid"))
            .where(
                AiChatMessage.session_id.in_(session_ids),
                AiChatMessage.role.in_(["user", "assistant"]),
            )
        ).all()
        for (sid,) in count_rows:
            message_counts[sid] = message_counts.get(sid, 0) + 1

    payload: list[dict] = []
    for session, user, dh in rows:
        payload.append({
            "id": session.id,
            "title": session.title,
            "chat_mode": session.chat_mode,
            "source_type": session.source_type,
            "user_id": session.user_id,
            "username": user.username,
            "digital_human_name": dh.name if dh else None,
            "message_count": message_counts.get(session.id, 0),
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "updated_at": session.updated_at.isoformat() if session.updated_at else None,
        })
    return ok(payload)


@router.get("/api/ops-console/chat-sessions/{session_id}")
def ops_console_chat_session_detail(
    session_id: int,
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("ops_console:view")),
) -> dict:
    """单会话消息明细（跨用户，只读回放）。"""
    row = db.execute(
        select(AiChatSession, User, DigitalHumanProfile)
        .join(User, User.id == AiChatSession.user_id)
        .outerjoin(DigitalHumanProfile, DigitalHumanProfile.id == AiChatSession.digital_human_id)
        .where(AiChatSession.id == session_id)
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    session, user, dh = row
    messages = db.scalars(
        select(AiChatMessage)
        .where(AiChatMessage.session_id == session_id)
        .order_by(AiChatMessage.id)
    ).all()
    message_payload = [
        {
            "id": m.id,
            "role": m.role,
            "content": m.content,
            "error": m.error,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in messages
        if m.role in {"user", "assistant"}
    ]
    return ok({
        "id": session.id,
        "title": session.title,
        "username": user.username,
        "digital_human_name": dh.name if dh else None,
        "chat_mode": session.chat_mode,
        "messages": message_payload,
    })


@router.get("/api/ops-console/login-logs")
def ops_console_login_logs(
    username: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
    _context: AuthContext = Depends(require_permission("ops_console:view")),
) -> dict:
    """跨用户登录审计：成功/失败/限流均记录。"""
    stmt = (
        select(LoginLog)
        .order_by(LoginLog.created_at.desc())
        .limit(limit)
    )
    if username:
        stmt = stmt.where(LoginLog.username == username)
    logs = db.scalars(stmt).all()
    return ok([
        {
            "id": row.id,
            "username": row.username,
            "user_id": row.user_id,
            "role": row.role,
            "success": row.success,
            "reason": row.reason,
            "ip": row.ip,
            "user_agent": row.user_agent,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in logs
    ])
