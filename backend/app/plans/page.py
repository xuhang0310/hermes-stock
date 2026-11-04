"""计划页：每次打开都实时算，按账户列出每条计划进行到哪一步。没有脚本，打开时是什么样就是什么样。"""
from __future__ import annotations

import html
import logging
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.models import ReportShare, StockLatestPrice, TradePlan, TradePlanItem
from backend.app.data.share_render import kpi_strip, long_date, page_shell
from backend.app.data.share_service import expires_on_text, share_url
from backend.app.plans import rules
from backend.app.plans.service import (
    account_labels,
    beijing_time,
    plan_items,
    plan_share_id,
    reconcile_owner,
)

logger = logging.getLogger("hermes.plans")

_PLAN_CSS = """
.kpi{margin-top:20px;padding-top:14px;border-top:1px solid var(--line-strong);border-bottom-color:var(--line-strong)}
.plan-banner{margin:16px 0 0;padding:8px 12px;border:1px solid rgba(230,195,111,.4);border-radius:2px;background:rgba(230,195,111,.08);color:var(--stop);font-size:13px;line-height:1.55}
.plan-banner a{margin-left:4px;color:var(--gold-2)}
.plan-empty{margin:16px 0 0;color:var(--muted)}
.row{padding:14px 0;border-bottom:1px solid var(--line)}
.r1,.r2{display:flex;justify-content:space-between;align-items:baseline;gap:8px}
.r1 .nm{color:var(--text);font-size:15px;font-weight:500}
.r1 .cd{margin-left:6px}
.st{flex-shrink:0;font-size:13px;font-weight:500;white-space:nowrap}
.st.pending,.st.expired,.st.superseded,.st.quiet{color:var(--muted);font-weight:400}
.st.triggered{color:var(--stop)}
.st.partially_executed{color:var(--gold-2)}
.st.executed{color:var(--down)}
.st.unwatched{color:var(--up)}
.r2{margin-top:6px}
.do{color:var(--soft);font-weight:500}
.do.buy{color:var(--up)}
.do.sell{color:var(--down)}
.do.stop{color:var(--stop)}
.r2 .now{margin-left:0;flex-shrink:0}
.r2 .now .fig{color:var(--text);font-size:16px}
.bar{height:2px;margin-top:10px;background:var(--line)}
.bar div{height:2px;background:var(--gold)}
.dt{margin:6px 0 0;color:var(--muted);font-size:13px;line-height:1.55}
"""

_STATUS_LABELS = {
    "pending": "待触发",
    "triggered": "已触发",
    "partially_executed": "部分成交",
    "executed": "已成交",
    "expired": "已过期",
    "superseded": "已被替代",
}
_SUMMARY_ORDER = ("pending", "triggered", "partially_executed", "executed", "expired", "superseded")
_BAR_FULL_RANGE = 10.0  # 离计划价 10% 及以上时进度条接近空，贴上去时接近满


def _is_watched(item: TradePlanItem) -> bool:
    return rules.watch_direction(item.action, item.trigger_type) is not None


def _status(item: TradePlanItem) -> tuple[str, str]:
    """返回 (样式, 文字)。"""
    if not _is_watched(item):
        return "quiet", rules.ACTION_LABELS.get(item.action, "")
    if not item.text_matched and item.status == "pending":
        return "unwatched", "未盯价"
    label = _STATUS_LABELS.get(item.status, item.status)
    if item.status == "triggered":
        return item.status, f"{label} {beijing_time(item.triggered_at):%H:%M}"
    return item.status, label


def _do_class(item: TradePlanItem) -> str:
    if item.action == "stop_loss":
        return "stop"
    return rules.item_family(item.action) or ""


def _distance_percent(item: TradePlanItem, price: float | None) -> float | None:
    if price is None or item.status != "pending" or not item.trigger_price or not item.text_matched:
        return None
    return abs(item.trigger_price - price) / price * 100 if _is_watched(item) else None


def _bar_width(item: TradePlanItem, distance: float | None) -> int | None:
    if item.status == "partially_executed" and item.quantity:
        return round(float(item.executed_qty or 0) / float(item.quantity) * 100)
    if distance is None:
        return None
    return max(4, min(100, round((1 - distance / _BAR_FULL_RANGE) * 100)))


def _detail(item: TradePlanItem, distance: float | None) -> str:
    parts: list[str] = []
    if distance is not None:
        parts.append(f"离计划价还差 {distance:.1f}%")
    elif item.status == "triggered":
        when = beijing_time(item.triggered_at)
        parts.append(f"{when:%H:%M} 碰到 {rules.format_price(item.triggered_price)}，还没有登记成交")
    elif item.status == "partially_executed":
        parts.append(f"已登记 {rules.format_quantity(item.executed_qty)} / {rules.format_quantity(item.quantity)} 股")
    elif item.status == "executed":
        parts.append(f"已登记 {rules.format_quantity(item.executed_qty)} 股")
    elif _is_watched(item) and not item.text_matched:
        parts.append("和报告正文对不上，系统不替这条盯价")
    elif _is_watched(item) and item.status == "expired":
        parts.append("没有碰到计划价")
    elif _is_watched(item) and item.status == "superseded":
        parts.append("已被新计划替代，不再提醒")
    if item.funding_note:
        parts.append(f"钱从哪来：{item.funding_note}")
    return "。".join(parts)


def _row(item: TradePlanItem, quote: StockLatestPrice | None) -> str:
    price = float(quote.price) if quote is not None and quote.price is not None else None
    plan_text = rules.describe_plan(item.action, item.trigger_type, item.trigger_price, item.quantity)
    status_class, status_text = _status(item)
    distance = _distance_percent(item, price)
    width = _bar_width(item, distance)
    bar = f'<div class="bar"><div style="width:{width}%"></div></div>' if width is not None else ""
    detail = _detail(item, distance)
    detail_html = f'<p class="dt">{html.escape(detail)}</p>' if detail else ""
    return (
        '<section class="row"><div class="r1"><span>'
        f'<span class="nm">{html.escape(item.name or item.symbol)}</span>'
        f'<span class="cd fig">{html.escape(item.symbol.split(".")[0])}</span></span>'
        f'<span class="st {status_class}">{html.escape(status_text)}</span></div>'
        f'<div class="r2"><span class="do {_do_class(item)}">{html.escape(plan_text)}</span>'
        f'<span class="now">现价 <span class="fig">{rules.format_price(price)}</span></span></div>'
        f"{bar}{detail_html}</section>"
    )


def _superseded_banner(db: Session, plan: TradePlan) -> str:
    if plan.status != "superseded":
        return ""
    when = beijing_time(plan.superseded_at)
    newer = db.get(TradePlan, plan.superseded_by) if plan.superseded_by else None
    text = f"这份计划在 {when:%m-%d %H:%M} 已被新计划替代，下面的待触发条目不再提醒。"
    if newer is not None:
        share_id = plan_share_id(db, newer.plan_id)
        if share_id:
            return f'<p class="plan-banner">{html.escape(text)}<a href="{html.escape(share_url(share_id))}">看最新计划</a></p>'
    return f'<p class="plan-banner">{html.escape(text)}</p>'


def render_plan_page(db: Session, row: ReportShare) -> str | None:
    """row 是 source_type=trade_plan 的分享记录；计划不存在时返回 None。"""
    plan = db.get(TradePlan, row.source_ref) if row.source_ref else None
    if plan is None:
        return None
    try:
        reconcile_owner(db, plan.owner_user_id, since=plan.plan_date)
        db.commit()
    except Exception as exc:  # noqa: BLE001 - 对账失败也要让客户看到计划，只是状态可能不是最新
        db.rollback()
        logger.warning("打开计划页时对账失败: %s", str(exc)[:200])

    items = plan_items(db, plan.plan_id)
    symbols = {item.symbol for item in items}
    quotes = (
        {q.symbol: q for q in db.scalars(select(StockLatestPrice).where(StockLatestPrice.symbol.in_(symbols))).all()}
        if symbols
        else {}
    )
    labels = account_labels(db, {item.account_id for item in items})
    groups: dict[int | None, list[TradePlanItem]] = {}
    for item in items:
        groups.setdefault(item.account_id, []).append(item)
    body_groups = "".join(
        f"<h2>{html.escape(labels[account_id])}</h2>"
        + "".join(_row(item, quotes.get(item.symbol)) for item in group)
        for account_id, group in groups.items()
    )
    if not items:
        body_groups = '<p class="plan-empty">这份计划没有操作。</p>'

    watched = [item for item in items if _is_watched(item)]
    counts = [(sum(1 for item in watched if item.status == key), key) for key in _SUMMARY_ORDER]
    cells = [(count, _STATUS_LABELS[key]) for count, key in counts if count]
    summary_html = kpi_strip(cells) if cells else ""

    label = rules.SOURCE_LABELS.get(plan.source, "操作计划")
    activated = beijing_time(plan.activated_at)
    plan_day = date.fromisoformat(plan.plan_date)
    made = ""
    if activated is not None:
        made = f"{activated:%H:%M} 生成" if activated.date() == plan_day else f"{activated:%m-%d %H:%M} 生成"
    subtitle = "　".join(part for part in (long_date(plan_day), made) if part)
    fetched = [q.fetched_at for q in quotes.values() if q.fetched_at is not None]
    # 行情缓存的 fetched_at 是北京时间
    updated = f"{max(fetched):%m-%d %H:%M} 更新" if fetched else ""
    foot = f"价格碰到不等于成交，以券商为准。链接有效期至 {expires_on_text(row)}。"
    return page_shell(
        f"{label} | {plan.plan_date}",
        f'<main class="page"><h1 class="page-title">{html.escape(label)}进度</h1>'
        f'<p class="page-meta">{html.escape(subtitle)}</p>'
        f"{_superseded_banner(db, plan)}{summary_html}"
        f'<article class="report">{body_groups}</article>'
        f'<footer class="page-foot">{html.escape(foot)}</footer></main>',
        mast_note=updated,
        extra_css=_PLAN_CSS,
    )
