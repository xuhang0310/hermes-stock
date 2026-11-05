"""影子账户对比页：每次打开实时读库。样式和计划页同一套，token 来自 share_render。"""
from __future__ import annotations

import html
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.models import ReportShare, ShadowAccount, ShadowOrder
from backend.app.data.share_render import long_date, page_shell
from backend.app.data.share_service import expires_on_text
from backend.app.plans.rules import format_price, format_quantity
from backend.app.shadow import rules
from backend.app.shadow.service import FILLED, PENDING, STRATEGY_LABELS, compare, nav_series, owner_summary

_CSS = """
.cmp{width:100%;border-collapse:collapse;margin:8px 0 4px;font-size:13px}
.cmp th,.cmp td{white-space:nowrap;min-width:0}
.cmp td.fig{font-size:13px}
.cmp th+th,.cmp td+td{padding-left:6px}
.cmp th{color:var(--muted);font-weight:400;font-size:13px;text-align:right;padding:6px 0;border-bottom:1px solid var(--line)}
.cmp th:first-child,.cmp td:first-child{text-align:left}
.cmp td{padding:8px 0;border-bottom:1px solid var(--line);text-align:right}
.cmp .up{color:var(--up)}.cmp .down{color:var(--down)}
.ord{padding:12px 0;border-bottom:1px solid var(--line)}
.ord .r1{display:flex;justify-content:space-between;gap:12px}
.ord .nm{font-weight:500}
.ord .cd{color:var(--muted);font-size:13px}
.ord .do{color:var(--gold-2);white-space:nowrap}
.ord .why{margin:4px 0 0;color:var(--muted);font-size:13px;line-height:1.55}
.plan-empty{color:var(--muted);font-size:14px}
"""


def _pct_cell(value: float | None) -> str:
    if value is None:
        return "<td>—</td>"
    if value == 0:
        return '<td class="fig">0.00%</td>'
    tone = "up" if value > 0 else "down"
    return f'<td class="{tone} fig">{value * 100:+.2f}%</td>'


def _money(value: float) -> str:
    return f"{value:,.0f}" if value < 100_000 else f"{value / 10_000:,.1f}万"


def _table(series: dict[str, list[float]], real: list[float]) -> str:
    """每个账户一行，手机上才放得下三个影子和真实账户。金额用万，小账户保留原数。"""
    accounts = [(STRATEGY_LABELS.get(strategy, strategy), values) for strategy, values in series.items()] + [("你的账户", real)]
    days = max(len(real) - 1, 0)
    body = "".join(
        f'<tr><td>{html.escape(label)}</td><td class="fig">{_money(values[-1])}</td>'
        f"{_pct_cell(rules.total_return(values[0], values[-1]))}"
        f"{_pct_cell(rules.annualized(values[0], values[-1], days))}"
        f"{_pct_cell(rules.max_drawdown(values))}</tr>"
        for label, values in accounts
    )
    return f'<table class="cmp"><tr><th></th><th>总资产</th><th>累计</th><th>年化</th><th>回撤</th></tr>{body}</table>'


def _order_row(order: ShadowOrder) -> str:
    side = "卖" if order.side == "sell" else "买"
    if order.status == FILLED:
        action = f"已{side} {format_quantity(order.quantity)} 股 @ {format_price(order.fill_price)}"
    else:
        action = f"{side} {format_quantity(order.quantity)} 股 @ {format_price(order.price)}"
    return (
        '<section class="ord"><div class="r1">'
        f'<span class="nm">{html.escape(order.stock_name)}<span class="cd fig"> {html.escape(order.symbol.split(".")[0])}</span></span>'
        f'<span class="do fig">{html.escape(action)}</span></div>'
        f'<p class="why">{html.escape(order.basis or "")}</p></section>'
    )


def render_shadow_page(db: Session, row: ReportShare) -> str | None:
    if row.owner_user_id is None:
        return None
    summary = owner_summary(db, row.owner_user_id)
    accounts: list[ShadowAccount] = summary["accounts"]
    if not accounts:
        return None
    parts: list[str] = []
    if summary["days"]:
        parts.append("<h2>全部账户</h2>" + _table(summary["series"], summary["real"]))
    by_real: dict[int, list[ShadowAccount]] = {}
    for account in accounts:
        by_real.setdefault(account.real_account_id, []).append(account)
    for group in by_real.values():
        aligned = compare({account.strategy: nav_series(db, account.id) for account in group})
        if not aligned["days"]:
            continue
        parts.append(f"<h2>{html.escape(group[0].account_label)}</h2>" + _table(aligned["series"], aligned["real"]))
        for account in group:
            label = STRATEGY_LABELS.get(account.strategy, account.strategy)
            pending = db.scalars(
                select(ShadowOrder).where(ShadowOrder.shadow_account_id == account.id, ShadowOrder.status == PENDING).order_by(ShadowOrder.id)
            ).all()
            filled = db.scalars(
                select(ShadowOrder).where(ShadowOrder.shadow_account_id == account.id, ShadowOrder.status == FILLED).order_by(ShadowOrder.id.desc()).limit(10)
            ).all()
            parts.append(f"<h3>{label}：下一个交易日挂单</h3>" + ("".join(_order_row(o) for o in pending) or '<p class="plan-empty">不挂单。</p>'))
            if filled:
                parts.append(f"<h3>{label}：最近成交</h3>" + "".join(_order_row(o) for o in filled))
    days = summary["days"]
    subtitle = ""
    if days:
        start = datetime.strptime(days[0], "%Y%m%d").date()
        subtitle = f"{long_date(start)}开始，已运行 {max(len(days) - 1, 0)} 个交易日"
    foot = (
        f"影子账户由代码自动挂单、按当天 K 线模拟成交，不下真实单。影子 A 只按风险预算，影子 B 再按模型排名微调，影子 C 在 B 的基础上，买回的价和买不买听买入模型。碰到价就算成交，实盘可能排不到队。"
        f"满 {rules.ANNUALIZE_MIN_DAYS} 个交易日才算年化。你的账户按系统登记的持仓和现金算，入金、出金和没登记的成交都会影响对比。"
        f"链接有效期至 {expires_on_text(row)}。"
    )
    return page_shell(
        "影子账户对比",
        f'<main class="page"><h1 class="page-title">影子账户对比</h1><p class="page-meta">{html.escape(subtitle)}</p>'
        f'<article class="report">{"".join(parts)}</article>'
        f'<footer class="page-foot">{html.escape(foot)}</footer></main>',
        extra_css=_CSS,
    )
