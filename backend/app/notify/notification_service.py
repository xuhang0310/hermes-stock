"""站内通知服务：通知的创建、查询、已读、删除（唯一写入通道）。

单一职责：只负责 notifications 表的持久化与整形输出；业务方（调度器/智能体/
报告等）通过 ``create_notification`` 投递消息，不直接操作 ORM。

所有函数都以 user_id 严格隔离，绝不跨用户读写。
"""
from __future__ import annotations

from datetime import datetime, timezone

from backend.app.core.database import SessionLocal
from backend.app.core.models import InboxNotification

_LIST_LIMIT_MAX = 100


# 时间统一按 UTC 存、带时区输出：生产库默认时间（server_default NOW()）就是 UTC，
# 不带时区输出时前端会按本地时间显示，早 8 小时。
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _utc_iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _to_dict(row: InboxNotification) -> dict:
    return {
        "id": row.id,
        "level": row.level,
        "category": row.category,
        "title": row.title,
        "content": row.content,
        "link": row.link,
        "is_read": bool(row.is_read),
        "created_at": _utc_iso(row.created_at),
        "read_at": _utc_iso(row.read_at),
    }


def create_notification(
    user_id: int,
    title: str,
    *,
    content: str | None = None,
    level: str = "info",
    category: str = "system",
    link: str | None = None,
) -> dict:
    """给指定用户投递一条通知，返回创建后的通知字典。"""
    with SessionLocal() as session:
        row = InboxNotification(
            user_id=user_id,
            title=title[:200],
            content=content,
            level=level if level in ("info", "success", "warning", "error") else "info",
            category=category[:32],
            link=link,
            created_at=_utcnow(),
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        return _to_dict(row)


def list_notifications(
    user_id: int,
    *,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """返回（通知列表, 未读总数）；列表按创建时间倒序。"""
    limit = max(1, min(int(limit or 50), _LIST_LIMIT_MAX))
    offset = max(0, int(offset or 0))
    with SessionLocal() as session:
        query = session.query(InboxNotification).filter(InboxNotification.user_id == user_id)
        if unread_only:
            query = query.filter(InboxNotification.is_read.is_(False))
        rows = (
            query.order_by(InboxNotification.id.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        unread = (
            session.query(InboxNotification)
            .filter(InboxNotification.user_id == user_id, InboxNotification.is_read.is_(False))
            .count()
        )
        return [_to_dict(row) for row in rows], int(unread or 0)


def mark_read(user_id: int, notification_id: int) -> bool:
    """标记单条为已读；幂等（已读再标不产生副作用）。不存在返回 False。"""
    with SessionLocal() as session:
        row = (
            session.query(InboxNotification)
            .filter(InboxNotification.id == notification_id, InboxNotification.user_id == user_id)
            .first()
        )
        if row is None:
            return False
        if not row.is_read:
            row.is_read = True
            row.read_at = _utcnow()
            session.commit()
        return True


def mark_all_read(user_id: int) -> int:
    """批量标记该用户全部未读为已读，返回本次标记条数。"""
    with SessionLocal() as session:
        updated = (
            session.query(InboxNotification)
            .filter(InboxNotification.user_id == user_id, InboxNotification.is_read.is_(False))
            .update({"is_read": True, "read_at": _utcnow()}, synchronize_session=False)
        )
        session.commit()
        return int(updated or 0)


def delete_notification(user_id: int, notification_id: int) -> bool:
    """删除单条通知；不存在返回 False。"""
    with SessionLocal() as session:
        deleted = (
            session.query(InboxNotification)
            .filter(InboxNotification.id == notification_id, InboxNotification.user_id == user_id)
            .delete(synchronize_session=False)
        )
        session.commit()
        return bool(deleted)
