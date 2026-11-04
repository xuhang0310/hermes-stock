"""分享页接口。

- GET  /api/share/{share_id}         公开页面：链接即凭证，不需要登录，返回自包含 HTML
- GET  /api/shares                   我的分享列表（ok 信封，不含正文）
- POST /api/shares/{share_id}/revoke 作废一条链接（ok 信封）

有 ``user:manage`` 权限的管理员可以看和作废所有人的链接（内置任务生成的链接没有归属用户）。
"""
from __future__ import annotations

import logging
import re

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from backend.app.auth.auth import AuthContext, has_permission, require_permission
from backend.app.core.database import get_db
from backend.app.core.response import fail, ok
from backend.app.data.share_render import render_notice_page, render_share_page
from backend.app.data.share_service import (
    expires_on_text,
    get_share,
    list_shares,
    record_view,
    revoke_share,
    share_meta,
    share_out,
    share_status,
)

logger = logging.getLogger("hermes.share")

router = APIRouter(tags=["shares"])

_SHARE_ID = re.compile(r"[0-9a-f]{32}")

# 页面里有持仓、股数和金额：不进搜索引擎、不留缓存、不带来源、不执行任何脚本。
_PAGE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Robots-Tag": "noindex, nofollow",
}

_NOTICES = {
    "missing": (404, "链接不存在", "请回到飞书，从最新的推送重新打开。"),
    "expired": (410, "链接已过期", "这份内容的查看期限已过，可以在系统里查看历史记录。"),
    "revoked": (410, "链接已作废", "这条链接已被作废，可以在系统里查看历史记录。"),
}


def _html_response(body: str, status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(body, status_code=status_code, headers=_PAGE_HEADERS)


def _notice(kind: str) -> HTMLResponse:
    status_code, heading, detail = _NOTICES[kind]
    return _html_response(render_notice_page(heading, detail), status_code)


@router.get("/api/share/{share_id}", include_in_schema=False)
def share_page(share_id: str, db: Session = Depends(get_db)) -> HTMLResponse:
    row = get_share(db, share_id) if _SHARE_ID.fullmatch(share_id) else None
    if row is None:
        return _notice("missing")
    status = share_status(row)
    if status != "active":
        return _notice(status)
    page = render_share_page(
        title=row.title,
        meta=share_meta(row),
        content_md=row.content_md,
        expires_on=expires_on_text(row),
    )
    try:
        record_view(db, share_id)
    except Exception as exc:  # noqa: BLE001 - 打开次数只是参考，记不上也要让客户看到内容
        db.rollback()
        logger.warning("分享页打开次数写入失败: %s", str(exc)[:200])
    return _html_response(page)


def _visible_owner(context: AuthContext) -> int | None:
    return None if has_permission(context, "user:manage") else context.user.id


@router.get("/api/shares")
def shares(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("data:read")),
) -> dict:
    rows = list_shares(db, owner_user_id=_visible_owner(context), limit=limit, offset=offset)
    return ok([share_out(row) for row in rows])


@router.post("/api/shares/{share_id}/revoke")
def revoke(
    share_id: str,
    db: Session = Depends(get_db),
    context: AuthContext = Depends(require_permission("data:write")),
) -> dict:
    row = get_share(db, share_id) if _SHARE_ID.fullmatch(share_id) else None
    owner = _visible_owner(context)
    if row is None or (owner is not None and row.owner_user_id != owner):
        return JSONResponse(status_code=404, content=fail("链接不存在", code="not_found"))
    revoke_share(db, row)
    return ok(share_out(row), message="链接已作废")
