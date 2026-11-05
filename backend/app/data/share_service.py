"""分享链接：推送时存一份正文快照，手机上点链接就能看完整版。

单一职责：只管 ``report_shares`` 的增删查和状态判断，不管渲染（share_render）和路由（share_api）。

链接本身就是凭证（128 位随机 ID），所以这张表不登记进 ``sql_sandbox``，数字人查不到分享 ID。
时间一律按 UTC 裸时间存，和 ``created_at`` 的 ``NOW()`` 口径一致。
"""
from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.app.core.models import ReportShare
from backend.app.core.settings import settings

_BEIJING = timezone(timedelta(hours=8))
# 这些来源的页面每次打开都实时算，不靠正文快照，所以后续推送不要改动已有记录。
LIVE_SHARE_SOURCES = frozenset({"trade_plan", "shadow_compare"})

# 设置页“测试”按钮用的示例，全部是虚构内容，点开就能验收手机版的排版。
SAMPLE_SHARE_MARKDOWN = """\
这是手机版网页的**示例页面**，内容全部虚构，不是你的持仓和计划。正式推送打开后就是这个排版。

## 今天的计划

| 股票（最新价） | 今天怎么做 | 价格 | 股数（股） |
| --- | --- | --- | --- |
| 示例科技 22.35 | 反弹卖一部分 | 限价 24.13 元 | 卖 500，留 1,000 |
| 示例制造 16.80 | 跌破就止损 | 15.98 元 | 卖 1,000，留 0 |
| 示例银行 8.48 | 准备买 | 限价 8.30 元 | 买 2,000 |
| 示例能源 13.76 | 拿着 | — | 持有 3,000 |

## 普通表格

| 账户 | 持股 | 成本价 | 浮亏及亏幅 |
| --- | --- | ---: | ---: |
| 示例账户甲 | 示例科技 1,500 股 | 25.10 | -4,125 元（-10.9%） |
| 示例账户乙 | 示例制造 1,000 股 | 18.20 | -1,400 元（-7.7%） |

## 窄屏说明

手机上计划表一行一张卡片，普通表格可以左右滑动，首列固定。
"""


@dataclass(frozen=True)
class ShareSpec:
    """一次推送要附带什么样的分享页；标题和正文取自推送本身，保证和卡片是同一份。"""

    source_type: str  # agent_report / selection_digest / topnews_brief / channel_test ...
    source_ref: str | None = None  # 同一来源重发时复用同一条链接
    owner_user_id: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)  # digital_human、generated_at、plan_url、plan_stats
    ttl_days: int | None = None
    button_text: str | None = None  # 卡片按钮文字，默认“手机查看完整版”


def _utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def share_url(share_id: str) -> str:
    return f"{settings.public_base_url}/api/share/{share_id}"


def create_share_link(db: Session, spec: ShareSpec, *, title: str, content: str) -> str | None:
    """建（或刷新）分享记录并返回链接；没配对外地址就不建，返回 None。不提交事务，由调用方提交。"""
    if not settings.public_base_url:
        return None
    expires_at = _utc_now() + timedelta(days=max(1, spec.ttl_days or settings.share_ttl_days))
    meta_json = json.dumps(spec.meta, ensure_ascii=False)
    row = None
    if spec.source_ref:
        row = db.scalar(
            select(ReportShare)
            .where(
                ReportShare.source_type == spec.source_type,
                ReportShare.source_ref == spec.source_ref,
                ReportShare.revoked_at.is_(None),
            )
            .limit(1)
        )
    if row is not None and spec.source_type in LIVE_SHARE_SOURCES:
        return share_url(row.share_id)
    if row is None:
        # 用十六进制而不是 base64url：MySQL 默认排序规则不分大小写，全小写才不会有两个 ID 对上同一行
        row = ReportShare(
            share_id=secrets.token_hex(16),
            source_type=spec.source_type,
            source_ref=spec.source_ref,
        )
        db.add(row)
    row.owner_user_id = spec.owner_user_id
    row.title = title[:300]
    row.meta_json = meta_json
    row.content_md = content
    row.expires_at = expires_at
    db.flush()
    return share_url(row.share_id)


def get_share(db: Session, share_id: str) -> ReportShare | None:
    return db.get(ReportShare, share_id)


def share_status(row: ReportShare, *, now: datetime | None = None) -> str:
    """active / revoked / expired。"""
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at <= (now or _utc_now()):
        return "expired"
    return "active"


def share_meta(row: ReportShare) -> dict:
    try:
        meta = json.loads(row.meta_json or "{}")
    except ValueError:
        return {}
    return meta if isinstance(meta, dict) else {}


def expires_on_text(row: ReportShare) -> str:
    return row.expires_at.replace(tzinfo=timezone.utc).astimezone(_BEIJING).strftime("%Y-%m-%d")


def record_view(db: Session, share_id: str) -> None:
    db.execute(
        update(ReportShare)
        .where(ReportShare.share_id == share_id)
        .values(view_count=ReportShare.view_count + 1, last_viewed_at=_utc_now())
    )
    db.commit()


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def share_out(row: ReportShare) -> dict:
    """管理接口的输出；不含正文。链接是凭证，只给有权查看这条记录的人。"""
    return {
        "share_id": row.share_id,
        "url": share_url(row.share_id) if settings.public_base_url else None,
        "title": row.title,
        "source_type": row.source_type,
        "source_ref": row.source_ref,
        "owner_user_id": row.owner_user_id,
        "status": share_status(row),
        "created_at": _iso(row.created_at),
        "expires_at": _iso(row.expires_at),
        "revoked_at": _iso(row.revoked_at),
        "view_count": row.view_count,
        "last_viewed_at": _iso(row.last_viewed_at),
    }


def list_shares(
    db: Session,
    *,
    owner_user_id: int | None,
    limit: int = 50,
    offset: int = 0,
) -> list[ReportShare]:
    """owner_user_id 为 None 表示不按用户过滤（管理员）。"""
    query = select(ReportShare)
    if owner_user_id is not None:
        query = query.where(ReportShare.owner_user_id == owner_user_id)
    query = query.order_by(ReportShare.created_at.desc(), ReportShare.share_id).limit(limit).offset(offset)
    return list(db.scalars(query).all())


def revoke_share(db: Session, row: ReportShare) -> None:
    if row.revoked_at is None:
        row.revoked_at = _utc_now()
    db.commit()
