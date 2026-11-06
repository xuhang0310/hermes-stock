"""盯价：挂在每分钟持仓价格同步后面，不调模型，没有额外成本。

条目先落状态再推送：推送失败时 notified_at 还是空，下一分钟会重试（最多重试 30 分钟）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.models import TradePlan, TradePlanItem
from backend.app.data.share_service import ShareSpec
from backend.app.events.service import flag_text, open_flags
from backend.app.feishu.feishu import SKIPPED, send_feishu
from backend.app.plans import rules
from backend.app.plans.service import (
    account_labels,
    beijing_time,
    open_holdings,
    reconcile_owner,
    to_utc,
)

logger = logging.getLogger("hermes.plans")

_NOTIFY_RETRY_WINDOW = timedelta(minutes=30)
_ALERT_FOOTER = "SixRabbits Stock | 按计划价自动提醒，不下单，仅供研究与复盘"


def _current_plans(db: Session, today: str) -> dict[int, TradePlan]:
    """每位用户今天说了算的计划：已生效里最新的一份。明天才生效的计划今天不盯。"""
    current: dict[int, TradePlan] = {}
    for plan in db.scalars(
        select(TradePlan).where(TradePlan.status == "active", TradePlan.plan_date <= today)
    ).all():
        held = current.get(plan.owner_user_id)
        if held is None or (plan.plan_date, plan.activated_at or datetime.min) > (
            held.plan_date,
            held.activated_at or datetime.min,
        ):
            current[plan.owner_user_id] = plan
    return current


def _watchable(db: Session, today: str) -> list[tuple[TradePlanItem, TradePlan]]:
    plans = {plan.plan_id: plan for plan in _current_plans(db, today).values()}
    if not plans:
        return []
    items = db.scalars(
        select(TradePlanItem).where(
            TradePlanItem.plan_id.in_(plans),
            TradePlanItem.valid_until >= today,
            TradePlanItem.status == "pending",
            TradePlanItem.text_matched.is_(True),
            TradePlanItem.trigger_price.is_not(None),
        )
    ).all()
    return [
        (item, plans[item.plan_id])
        for item in items
        if rules.watch_direction(item.action, item.trigger_type)
    ]


def watch_symbols(today: str, *, session_factory=SessionLocal) -> set[str]:
    """计划里要盯的代码。买入的自选股可能不在持仓里，要一起放进每分钟刷新的列表。"""
    with session_factory() as db:
        return {item.symbol for item, _ in _watchable(db, today)}


def check_plan_triggers(
    quotes: dict[str, dict[str, Any]],
    now: datetime,
    *,
    session_factory=SessionLocal,
) -> dict[str, int]:
    """用这一轮的行情判断有没有碰到计划价，并推送提醒。now 是北京时间。"""
    today = now.date().isoformat()
    compact = today.replace("-", "")
    now_utc = to_utc(now)
    triggered = 0
    with session_factory() as db:
        current = _current_plans(db, today)
        for owner, plan in current.items():
            # 先对账：已经登记成交的条目就不用再提醒了
            reconcile_owner(db, owner, since=plan.plan_date, now=now_utc)
        holdings = {owner: open_holdings(db, owner) for owner in current}
        for item, _plan in _watchable(db, today):
            quote = quotes.get(item.symbol)
            if not quote or str(quote.get("trade_date") or "") != compact:
                continue
            # 止损和挂单卖常是同一批股票的两个出口：这批已经卖光了，另一个就不用再提醒
            if rules.item_family(item.action) == "sell" and (item.account_id, item.symbol) not in holdings[item.owner_user_id]:
                continue
            high, low = quote.get("high"), quote.get("low")
            hit = rules.evaluate_trigger(
                direction=rules.watch_direction(item.action, item.trigger_type),
                trigger_price=float(item.trigger_price),
                price=float(quote["price"]),
                high=high,
                low=low,
                baseline_high=item.baseline_high,
                baseline_low=item.baseline_low,
            )
            if item.baseline_high is None and high is not None:
                item.baseline_high = high
            if item.baseline_low is None and low is not None:
                item.baseline_low = low
            if hit is None:
                continue
            item.status = "triggered"
            item.triggered_at = now_utc
            item.triggered_price = hit.price
            item.trigger_via = hit.via
            triggered += 1
        db.commit()

    sent = _send_due_alerts(quotes, now, session_factory=session_factory)
    return {"triggered": triggered, "alerts_sent": sent}


def _send_due_alerts(quotes: dict[str, dict[str, Any]], now: datetime, *, session_factory=SessionLocal) -> int:
    now_utc = to_utc(now)
    sent = 0
    with session_factory() as db:
        rows = db.execute(
            select(TradePlanItem, TradePlan)
            .join(TradePlan, TradePlan.plan_id == TradePlanItem.plan_id)
            .where(
                TradePlanItem.triggered_at.is_not(None),
                TradePlanItem.triggered_at >= now_utc - _NOTIFY_RETRY_WINDOW,
                TradePlanItem.notified_at.is_(None),
            )
            .order_by(TradePlanItem.triggered_at, TradePlanItem.id)
        ).all()
        groups: dict[tuple[int, str | None], list[tuple[TradePlanItem, TradePlan]]] = {}
        for item, plan in rows:
            groups.setdefault((item.owner_user_id, plan.push_channel_key), []).append((item, plan))
        for (owner, channel), group in groups.items():
            if not channel:
                # 这份计划的任务没配推送通道：没地方发，也不再重试
                for item, _ in group:
                    item.notified_at = now_utc
                db.commit()
                continue
            items = [item for item, _ in group]
            newest_plan = max((plan for _, plan in group), key=lambda plan: plan.activated_at or datetime.min)
            notification = send_feishu(
                db,
                channel,
                f"⚡ 触价提醒 · {now:%H:%M}",
                alert_content(db, group, quotes),
                event_id=f"plan-alert:{owner}:{now:%Y%m%d%H%M}",
                card_template="red" if any(item.action == "stop_loss" for item in items) else "orange",
                card_footer=_ALERT_FOOTER,
                owner_user_id=owner,
                share=ShareSpec(
                    source_type="trade_plan",
                    source_ref=newest_plan.plan_id,
                    owner_user_id=owner,
                    button_text="📱 打开今天的计划",
                ),
            )
            if notification.status in ("sent", SKIPPED):
                for item in items:
                    item.notified_at = now_utc
                sent += notification.status == "sent"
            else:
                logger.warning("触价提醒发送失败，下一分钟重试: %s", (notification.error or "")[:200])
            db.commit()
    return sent


def alert_content(
    db: Session,
    group: list[tuple[TradePlanItem, TradePlan]],
    quotes: dict[str, dict[str, Any]],
) -> str:
    labels = account_labels(db, {item.account_id for item, _ in group})
    blocks: list[str] = []
    for item, plan in group:
        quote = quotes.get(item.symbol) or {}
        current = quote.get("price")
        heading = f"**{labels[item.account_id]}｜{item.name or item.symbol} {item.symbol.split('.')[0]}**"
        if item.trigger_via == "spike":
            when = beijing_time(item.triggered_at)
            first = f"{heading} 盘中碰到过 {rules.format_price(item.triggered_price)}（{when:%H:%M} 前后），现价 {rules.format_price(current)}"
        else:
            first = f"{heading} 现价 {rules.format_price(current if current is not None else item.triggered_price)}"
        source = rules.SOURCE_LABELS.get(plan.source, "计划")
        plan_text = rules.describe_plan(item.action, item.trigger_type, item.trigger_price, item.quantity)
        block = f"{first}\n碰到{source}：{plan_text}"
        flag = open_flags(db, item.owner_user_id, {item.symbol}, plan.activated_at).get(item.symbol)
        if flag is not None:
            block += f"\n⚠ 计划之后出了消息，计划还没考虑：{flag_text(flag)}"
        blocks.append(block)
    blocks.append("价格碰到不等于成交，以券商为准。成交后记得在系统里记一笔（持仓页或持仓录入 Agent），计划页才会显示已成交。")
    return "\n\n".join(blocks)
