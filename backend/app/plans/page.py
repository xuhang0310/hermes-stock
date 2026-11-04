"""计划页：每次打开都实时算，按账户列出每条计划进行到哪一步。没有脚本，打开时是什么样就是什么样。"""
from __future__ import annotations

import html
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.models import ReportShare, StockLatestPrice, TradePlan, TradePlanItem
from backend.app.data.share_render import page_shell
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
.plan-banner{margin:12px 0 0;padding:8px 12px;border:1px solid rgba(213,166,66,.4);border-radius:var(--r-panel);background:rgba(213,166,66,.1);color:#e6c36f;font-size:13px}
.plan-banner a{color:var(--gold-2)}
.kpi{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:1px;margin-top:12px;overflow:hidden;border:1px solid var(--line);border-radius:var(--r-panel);background:var(--line)}
.kpi>div{display:grid;gap:2px;padding:8px 12px;background:var(--surface)}
.kpi span{color:var(--muted);font-size:12px;white-space:nowrap}
.kpi strong{color:var(--text);font-size:20px;font-weight:500;line-height:1.2}
.plan-empty{margin:16px 0 0;color:var(--muted)}
.plan-group{margin-top:24px}
.plan-group h2{margin:0 0 8px;padding-bottom:5px;border-bottom:1px solid rgba(213,180,106,.18);color:var(--gold-2);font-size:17px;font-weight:600;line-height:1.35}
.plan-card{margin-bottom:8px;padding:12px;border:1px solid var(--line);border-radius:var(--r-panel);background:var(--surface);overflow-wrap:anywhere}
.plan-top{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px}
.plan-name{color:var(--text);font-weight:500}
.plan-code{color:var(--muted);font-size:12px}
.badge{margin-left:auto;padding:1px 8px;border:1px solid var(--line);border-radius:var(--r-pill);background:var(--surface-2);color:var(--muted);font-size:12px;white-space:nowrap}
.badge.triggered,.badge.partially_executed{border-color:rgba(213,166,66,.4);background:rgba(213,166,66,.1);color:#e6c36f}
.badge.executed{border-color:rgba(39,197,138,.35);background:rgba(39,197,138,.08);color:#6fd8b2}
.badge.unwatched{border-color:rgba(255,93,93,.4);background:rgba(255,93,93,.08);color:#ff8b8b}
.plan-do{margin:8px 0 0;color:var(--text);font-weight:600}
.plan-do.buy{color:var(--up)}
.plan-do.sell{color:var(--down)}
.plan-do.stop{color:#e6c36f}
.plan-line{margin:2px 0 0;color:var(--muted);font-size:13px;line-height:1.55}
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


def _badge_text(item: TradePlanItem) -> str:
    if rules.watch_direction(item.action, item.trigger_type) is None:
        return rules.ACTION_LABELS.get(item.action, "")
    if not item.text_matched and item.status == "pending":
        return "未盯价"
    label = _STATUS_LABELS.get(item.status, item.status)
    if item.status == "triggered":
        when = beijing_time(item.triggered_at)
        return f"{label} {when:%H:%M} @{rules.format_price(item.triggered_price)}"
    if item.status in ("executed", "partially_executed"):
        return f"{label} {rules.format_quantity(item.executed_qty)} 股"
    return label


def _badge_class(item: TradePlanItem) -> str:
    if rules.watch_direction(item.action, item.trigger_type) is None:
        return "pending"
    return "unwatched" if not item.text_matched and item.status == "pending" else item.status


def _do_class(item: TradePlanItem) -> str:
    if item.action == "stop_loss":
        return "stop"
    return rules.item_family(item.action) or ""


def _distance_text(item: TradePlanItem, price: float | None) -> str:
    if price is None or item.status != "pending" or not item.trigger_price or not item.text_matched:
        return ""
    if rules.watch_direction(item.action, item.trigger_type) is None:
        return ""
    return f"，离计划价还差 {abs(item.trigger_price - price) / price * 100:.1f}%"


def _card(item: TradePlanItem, quote: StockLatestPrice | None) -> str:
    price = float(quote.price) if quote is not None and quote.price is not None else None
    plan_text = rules.describe_plan(item.action, item.trigger_type, item.trigger_price, item.quantity)
    lines = [f"现价 {rules.format_price(price)}{_distance_text(item, price)}"]
    if item.funding_note:
        lines.append(f"钱从哪来：{item.funding_note}")
    if item.status == "partially_executed":
        lines.append(f"计划 {rules.format_quantity(item.quantity)} 股，已登记 {rules.format_quantity(item.executed_qty)} 股")
    line_html = "".join(f'<p class="plan-line">{html.escape(line)}</p>' for line in lines)
    return (
        '<section class="plan-card"><div class="plan-top">'
        f'<span class="plan-name">{html.escape(item.name or item.symbol)}</span>'
        f'<span class="plan-code">{html.escape(item.symbol.split(".")[0])}</span>'
        f'<span class="badge {_badge_class(item)}">{html.escape(_badge_text(item))}</span></div>'
        f'<p class="plan-do {_do_class(item)}">{html.escape(plan_text)}</p>{line_html}</section>'
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
        f'<div class="plan-group"><h2>{html.escape(labels[account_id])}</h2>'
        + "".join(_card(item, quotes.get(item.symbol)) for item in group)
        + "</div>"
        for account_id, group in groups.items()
    )
    if not items:
        body_groups = '<p class="plan-empty">这份计划没有操作。</p>'

    watched = [item for item in items if rules.watch_direction(item.action, item.trigger_type)]
    counts = {key: sum(1 for item in watched if item.status == key) for key in _SUMMARY_ORDER}
    cells = "".join(
        f"<div><span>{_STATUS_LABELS[key]}</span><strong>{counts[key]}</strong></div>"
        for key in _SUMMARY_ORDER
        if counts[key]
    )
    summary_html = f'<div class="kpi">{cells}</div>' if cells else ""

    label = rules.SOURCE_LABELS.get(plan.source, "操作计划")
    activated = beijing_time(plan.activated_at)
    meta = " · ".join(part for part in (plan.plan_date, f"{activated:%H:%M} 生成" if activated else "") if part)
    fetched = [q.fetched_at for q in quotes.values() if q.fetched_at is not None]
    # 行情缓存的 fetched_at 是北京时间
    updated = f"现价更新于 {max(fetched):%m-%d %H:%M} · " if fetched else ""
    foot = f"{updated}链接有效期至 {expires_on_text(row)}"
    return page_shell(
        f"{label} | {plan.plan_date}",
        '<main class="page"><header class="page-head">'
        f'<h1 class="page-title">{html.escape(label)}</h1><p class="page-meta">{html.escape(meta)}</p></header>'
        f"{_superseded_banner(db, plan)}{summary_html}{body_groups}"
        f'<footer class="page-foot">{html.escape(foot)}<br>价格碰到不等于成交，以券商为准</footer></main>',
        extra_css=_PLAN_CSS,
    )
