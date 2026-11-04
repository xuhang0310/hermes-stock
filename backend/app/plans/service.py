"""挂单计划：保存与校验、生效与替代、和实际成交对账。

一份计划的一生：
- 数字人调 ``save_trade_plan`` 存成 draft（当场校验，不合格就把原因还给它，同一次任务重存会覆盖）。
- 报告写完后 ``activate_for_report`` 再和最终正文核对一遍，然后生效并替代旧计划。先存后生效是为了
  保证客户先看到报告才会收到提醒；任务中途失败的计划永远不会提醒。
- 条目的状态和成交结果都由 ``reconcile_owner`` 从成交流水推出来，可以反复算，删错单也会自动回退。

时间一律按 UTC 裸时间存（和 ``created_at`` 的 ``NOW()`` 一致），展示时再转北京时间。
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from backend.app.core.models import (
    AutomaticSelectionCandidate,
    AutomaticSelectionRun,
    Instrument,
    PositionAccount,
    PositionHoldingSnapshot,
    PositionTrade,
    ReportShare,
    StockLatestPrice,
    TradePlan,
    TradePlanItem,
    WatchlistItem,
)
from backend.app.data.position_write_mixin import PositionWriteMixin
from backend.app.data.share_service import ShareSpec, create_share_link
from backend.app.plans import rules

SAVE_TRADE_PLAN_TOOL = "save_trade_plan"
SAVE_TRADE_PLAN_SCHEMA = {
    "type": "function",
    "function": {
        "name": SAVE_TRADE_PLAN_TOOL,
        "description": (
            "把这份报告里的操作计划按字段存进系统，系统靠它盯价提醒、和实际成交对账。"
            "要存今天仍然有效的全部操作：新计划生效后，旧计划就不再提醒。"
            "价格和股数必须和报告正文一模一样。不合格时返回 issues，按原因改好正文和计划后重新调用；"
            "同一次任务重复调用会覆盖上一次。确实没有任何操作就传空列表。"
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "items": {
                    "type": "array",
                    "maxItems": 80,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "account_id": {"type": "integer", "description": "持仓账户 ID，用持仓工具查，不要猜。"},
                            "symbol": {"type": "string", "description": "股票代码，如 605286.SH。"},
                            "action": {
                                "type": "string",
                                "enum": sorted(rules.ACTION_LABELS),
                                "description": "buy 买入、add 加仓、sell 卖出、reduce 减仓、hold 拿着不动、stop_loss 止损、cancel 撤单。",
                            },
                            "trigger_type": {
                                "type": "string",
                                "enum": list(rules.TRIGGER_TYPES),
                                "description": "limit 挂单价、rise_to 涨到才做、fall_to 跌到才做；hold 和 cancel 用 none。",
                            },
                            "trigger_price": {"type": "number", "description": "计划价；hold 和 cancel 不传。"},
                            "quantity": {"type": "integer", "description": "这一笔的股数；hold 和 cancel 不传。"},
                            "funding_note": {
                                "type": "string",
                                "description": "买入要动用当天卖出款时必填，写明用哪笔卖出的钱。",
                            },
                            "valid_until": {
                                "type": "string",
                                "description": "YYYY-MM-DD，默认只在计划生效当天有效。周计划、月计划可以写更晚。",
                            },
                        },
                        "required": ["account_id", "symbol", "action"],
                    },
                }
            },
            "required": ["items"],
        },
    },
}

PLAN_SAVE_INSTRUCTION = (
    "这份报告是操作计划：写完正文后调用一次 save_trade_plan，把每个账户要做的操作按字段存下来"
    "（拿着不动的也存，action 用 hold），价格和股数要和正文一模一样；工具说哪条不合格，"
    "就按原因把正文和计划一起改好再存。没有任何操作就传空列表。系统靠它盯价和对账，正文不用提保存这件事。"
)

_BEIJING = ZoneInfo("Asia/Shanghai")
_SYMBOL = re.compile(r"\d{6}\.(SH|SZ|BJ)")
_SELECTION_TOP_N = 200
_LATE_RECORD_GRACE = timedelta(days=1, hours=9)  # 成交常在收盘后才登记，算到下一天 09:00


def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def to_utc(beijing_naive: datetime) -> datetime:
    return beijing_naive.replace(tzinfo=_BEIJING).astimezone(timezone.utc).replace(tzinfo=None)


def beijing_time(utc_naive: datetime | None) -> datetime | None:
    if utc_naive is None:
        return None
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(_BEIJING).replace(tzinfo=None)


def _day(value: str) -> date:
    return date.fromisoformat(value)


def _market_close_utc(valid_until: str) -> datetime:
    return to_utc(datetime.combine(_day(valid_until), rules.MARKET_CLOSE))


def _trade_window(plan: TradePlan, item: TradePlanItem) -> tuple[datetime, datetime]:
    day_start = to_utc(datetime.combine(_day(plan.plan_date), datetime.min.time()))
    start = max(plan.activated_at or day_start, day_start)
    end = to_utc(datetime.combine(_day(item.valid_until), datetime.min.time())) + _LATE_RECORD_GRACE
    return start, end


def open_holdings(db: Session, owner_user_id: int) -> dict[tuple[int | None, str], PositionHoldingSnapshot]:
    latest_ids = (
        select(func.max(PositionHoldingSnapshot.id))
        .where(PositionHoldingSnapshot.owner_user_id == owner_user_id)
        .group_by(PositionHoldingSnapshot.account_id, PositionHoldingSnapshot.symbol)
    )
    rows = db.scalars(select(PositionHoldingSnapshot).where(PositionHoldingSnapshot.id.in_(latest_ids))).all()
    return {
        (row.account_id, row.symbol): row
        for row in rows
        if (row.status or "open") == "open" and (row.position_quantity or 0) > 0
    }


def _selection_symbols(db: Session) -> set[str]:
    latest = db.scalar(
        select(func.max(AutomaticSelectionRun.target_trade_date)).where(AutomaticSelectionRun.status == "completed")
    )
    if not latest:
        return set()
    run_ids = select(AutomaticSelectionRun.run_id).where(
        AutomaticSelectionRun.target_trade_date == latest,
        AutomaticSelectionRun.status == "completed",
    )
    return set(
        db.scalars(
            select(AutomaticSelectionCandidate.symbol).where(
                AutomaticSelectionCandidate.run_id.in_(run_ids),
                AutomaticSelectionCandidate.rank.is_not(None),
                AutomaticSelectionCandidate.rank <= _SELECTION_TOP_N,
            )
        ).all()
    )


def _normalize_symbol(raw: str) -> str:
    from backend.app.market.stock_tools import normalize_a_share_symbol

    return normalize_a_share_symbol(raw)


def _reference_close(quote: StockLatestPrice | None, plan_date: str) -> float | None:
    """算涨跌停用的昨收：行情已经是生效当天的就用它的 pre_close，否则它的最新价就是生效日的昨收。"""
    if quote is None:
        return None
    return quote.pre_close if quote.trade_date == plan_date.replace("-", "") else quote.price


def _sellable(holding: PositionHoldingSnapshot | None, *, same_day: bool) -> float:
    if holding is None:
        return 0.0
    if same_day and holding.available_quantity:
        return float(holding.available_quantity)
    return float(holding.position_quantity or 0)


def _validate_items(
    db: Session,
    *,
    owner_user_id: int,
    source: str,
    plan_date: str,
    same_day: bool,
    raw_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    accounts = {
        row.id: row
        for row in db.scalars(
            select(PositionAccount).where(
                PositionAccount.owner_user_id == owner_user_id,
                PositionAccount.status == "active",
            )
        ).all()
    }
    holdings = open_holdings(db, owner_user_id)
    held_symbols = {symbol for _, symbol in holdings}
    watchlist = set(
        db.scalars(
            select(WatchlistItem.symbol).where(
                WatchlistItem.owner_user_id == owner_user_id,
                WatchlistItem.status == "active",
            )
        ).all()
    )
    selection: set[str] | None = None
    issues: list[str] = []
    cleaned: list[dict[str, Any]] = []
    seen: set[tuple] = set()

    for position, raw in enumerate(raw_items, start=1):
        symbol = _normalize_symbol(str(raw.get("symbol") or ""))
        action = str(raw.get("action") or "")
        label = f"第 {position} 条（{symbol or raw.get('symbol')}）"
        if not _SYMBOL.fullmatch(symbol):
            issues.append(f"{label}：股票代码不对")
            continue
        if action not in rules.ACTION_LABELS:
            issues.append(f"{label}：操作类型不对")
            continue
        account_id = raw.get("account_id")
        if account_id not in accounts:
            issues.append(f"{label}：账户 ID {account_id} 不存在，先用持仓工具查")
            continue
        instrument = db.get(Instrument, symbol)
        holding = holdings.get((account_id, symbol))
        name = (holding.symbol_name if holding else None) or (instrument.name if instrument else None)

        if symbol not in held_symbols and symbol not in watchlist:
            if selection is None:
                selection = _selection_symbols(db)
            if symbol not in selection:
                issues.append(f"{label}：不在持仓、自选或最新选股名单里")
                continue

        valid_until = str(raw.get("valid_until") or plan_date)
        try:
            valid_day = _day(valid_until)
        except ValueError:
            issues.append(f"{label}：有效期要写成 YYYY-MM-DD")
            continue
        if valid_day < _day(plan_date):
            issues.append(f"{label}：有效期不能早于计划生效日 {plan_date}")
            continue

        item = {
            "account_id": account_id,
            "symbol": symbol,
            "name": name,
            "action": action,
            "trigger_type": "none",
            "trigger_price": None,
            "quantity": None,
            "funding_note": (str(raw.get("funding_note") or "").strip() or None),
            "valid_until": valid_until,
        }
        if action in rules.NO_TRIGGER_ACTIONS:
            cleaned.append(item)
            continue

        trigger_type = str(raw.get("trigger_type") or ("fall_to" if action == "stop_loss" else "limit"))
        trigger_price = raw.get("trigger_price")
        quantity = raw.get("quantity")
        if trigger_type not in rules.TRIGGER_TYPES or trigger_type == "none":
            issues.append(f"{label}：{rules.ACTION_LABELS[action]}要写清楚触发方式")
            continue
        if not isinstance(trigger_price, (int, float)) or trigger_price <= 0:
            issues.append(f"{label}：缺计划价")
            continue
        if not isinstance(quantity, (int, float)):
            issues.append(f"{label}：缺股数")
            continue

        is_sell = rules.item_family(action) == "sell"
        if is_sell and holding is None:
            issues.append(f"{label}：账户 {account_id} 没有这只股票，不能卖")
            continue
        held = float(holding.position_quantity or 0) if holding else 0.0
        problem = rules.quantity_problem(symbol, action, float(quantity), held)
        if problem:
            issues.append(f"{label}：{problem}（现在写的是 {rules.format_quantity(quantity)} 股）")
            continue
        sellable = _sellable(holding, same_day=same_day)
        if is_sell and float(quantity) > sellable:
            issues.append(
                f"{label}：要卖 {rules.format_quantity(quantity)} 股，超过可卖的 {rules.format_quantity(sellable)} 股"
            )
            continue

        if not rules.is_multi_day(source) and valid_until == plan_date:
            reference = _reference_close(db.get(StockLatestPrice, symbol), plan_date)
            if reference:
                low, high = rules.price_limits(symbol, name, float(reference))
                if not low - 0.01 <= float(trigger_price) <= high + 0.01:
                    issues.append(
                        f"{label}：计划价 {rules.format_price(trigger_price)} 超出 {plan_date} 的涨跌停范围"
                        f" {rules.format_price(low)}–{rules.format_price(high)}"
                    )
                    continue

        key = (account_id, symbol, action, trigger_type, float(trigger_price))
        if key in seen:
            issues.append(f"{label}：和前面一条重复")
            continue
        seen.add(key)
        item.update(trigger_type=trigger_type, trigger_price=float(trigger_price), quantity=float(quantity))
        cleaned.append(item)

    issues.extend(_cross_item_issues(cleaned, accounts, holdings, same_day=same_day))
    return cleaned, issues


def _cross_item_issues(
    items: list[dict[str, Any]],
    accounts: dict[int, PositionAccount],
    holdings: dict[tuple[int | None, str], PositionHoldingSnapshot],
    *,
    same_day: bool,
) -> list[str]:
    """跨条目的账：卖出合计不超过可卖；买入的钱不超过现金加当天卖出款，同一笔钱只算一次。

    止损和挂单卖常是同一批股票的两个出口，二选一，所以止损不计入卖出合计，也不算资金来源。
    """
    issues: list[str] = []
    sells: dict[tuple[int, str], float] = {}
    for item in items:
        if item["action"] in rules.SELL_ACTIONS:
            key = (item["account_id"], item["symbol"])
            sells[key] = sells.get(key, 0.0) + item["quantity"]
    for (account_id, symbol), total in sells.items():
        sellable = _sellable(holdings.get((account_id, symbol)), same_day=same_day)
        if total > sellable:
            issues.append(
                f"账户 {account_id} 的 {symbol}：卖出合计 {rules.format_quantity(total)} 股，"
                f"超过可卖的 {rules.format_quantity(sellable)} 股"
            )

    for account_id, account in accounts.items():
        buys = [i for i in items if i["account_id"] == account_id and i["action"] in rules.BUY_ACTIONS]
        if not buys or account.available_cash is None:
            continue
        buy_total = sum(i["trigger_price"] * i["quantity"] for i in buys)
        cash = float(account.available_cash)
        if buy_total <= cash:
            continue
        proceeds = sum(
            i["trigger_price"] * i["quantity"]
            for i in items
            if i["account_id"] == account_id and i["action"] in rules.SELL_ACTIONS
        )
        amounts = f"买入合计 {buy_total:,.0f} 元，现金 {cash:,.0f} 元，当天卖出款 {proceeds:,.0f} 元"
        if buy_total > cash + proceeds:
            issues.append(f"账户 {account_id} 的钱不够：{amounts}")
        elif not any(i["funding_note"] for i in buys):
            issues.append(f"账户 {account_id} 要动用当天卖出款，买入那条要写明钱从哪来：{amounts}")
    return issues


def save_trade_plan(
    db: Session,
    *,
    owner_user_id: int,
    source: str,
    job_key: str | None,
    job_run_id: str,
    run_id: str | None,
    digital_human_code: str | None,
    push_channel_key: str | None,
    raw_items: list[dict[str, Any]],
    now: datetime,
    is_open: Callable[[date], bool],
) -> dict[str, Any]:
    """校验并保存成草稿。now 是北京时间。"""
    existing = db.scalar(select(TradePlan).where(TradePlan.job_run_id == job_run_id).limit(1))
    if existing is not None and existing.status != "draft":
        return {"saved": False, "error": "这份计划已经生效，不能再改。"}
    plan_day = rules.effective_plan_date(source, now, is_open)
    plan_date = plan_day.isoformat()
    cleaned, issues = _validate_items(
        db,
        owner_user_id=owner_user_id,
        source=source,
        plan_date=plan_date,
        same_day=plan_day == now.date(),
        raw_items=raw_items,
    )
    if issues:
        return {
            "saved": False,
            "error": "计划没有保存，下面这些要改好后重新调用。",
            "error_code": "trade_plan_validation_error",
            "issues": issues[:30],
        }

    plan = existing
    if plan is None:
        plan = TradePlan(
            plan_id=uuid.uuid4().hex,
            owner_user_id=owner_user_id,
            job_run_id=job_run_id,
            status="draft",
        )
        db.add(plan)
    plan.plan_date = plan_date
    plan.source = source
    plan.job_key = job_key
    plan.run_id = run_id
    plan.digital_human_code = digital_human_code
    plan.push_channel_key = push_channel_key
    db.flush()
    db.execute(delete(TradePlanItem).where(TradePlanItem.plan_id == plan.plan_id))
    for order, item in enumerate(cleaned):
        db.add(
            TradePlanItem(
                plan_id=plan.plan_id,
                owner_user_id=owner_user_id,
                sort_order=order,
                status="pending",
                **item,
            )
        )
    db.commit()
    return {
        "saved": True,
        "plan_date": plan_date,
        "item_count": len(cleaned),
        "note": f"计划已保存，从 {plan_date} 开始生效。",
    }


def execute_save_tool(args: dict[str, Any], request_payload: dict[str, Any]) -> dict[str, Any]:
    """Agent 工具入口。job_run_id 是这次定时任务的编号，同一次任务重存会覆盖。"""
    from backend.app.core.database import SessionLocal
    from backend.app.scheduler.rhythm_scheduler import is_trading_day, now_sh

    source = str(request_payload.get("trade_plan_source") or "")
    job_run_id = str(request_payload.get("source_ref") or "")
    if source not in rules.SOURCE_LABELS or not job_run_id:
        return {"saved": False, "error": "只有出操作计划的定时任务才能保存计划。"}
    context = request_payload.get("trade_plan_context") or {}
    with SessionLocal() as db:
        return save_trade_plan(
            db,
            owner_user_id=int(request_payload["user_id"]),
            source=source,
            job_key=context.get("job_key"),
            job_run_id=job_run_id,
            run_id=str(request_payload.get("run_id") or "") or None,
            digital_human_code=request_payload.get("digital_human_code"),
            push_channel_key=context.get("push_channel_key"),
            raw_items=list(args.get("items") or []),
            now=now_sh(),
            is_open=is_trading_day,
        )


# ------------------------------------------------------------------ 正文核对与生效
def plan_items(db: Session, plan_id: str) -> list[TradePlanItem]:
    return list(
        db.scalars(
            select(TradePlanItem).where(TradePlanItem.plan_id == plan_id).order_by(TradePlanItem.sort_order)
        ).all()
    )


def _item_label(item: TradePlanItem) -> str:
    return f"{item.name or item.symbol}（{rules.describe_plan(item.action, item.trigger_type, item.trigger_price, item.quantity)}）"


def _unmatched_items(items: list[TradePlanItem], text: str) -> list[tuple[TradePlanItem, str]]:
    numbers = rules.text_numbers(text)
    unmatched: list[tuple[TradePlanItem, str]] = []
    for item in items:
        if rules.watch_direction(item.action, item.trigger_type) is None:
            continue
        problem = rules.text_problem(
            symbol=item.symbol,
            name=item.name,
            price=item.trigger_price,
            quantity=item.quantity,
            text=text,
            numbers=numbers,
        )
        if problem:
            unmatched.append((item, problem))
    return unmatched


def repair_request(db: Session, job_run_id: str, text: str) -> str | None:
    """计划和最终正文对不上时，返回要交给数字人补救的说明；对得上返回 None。"""
    plan = db.scalar(select(TradePlan).where(TradePlan.job_run_id == job_run_id).limit(1))
    if plan is None:
        return "这份报告的计划还没有存进系统。"
    unmatched = _unmatched_items(plan_items(db, plan.plan_id), text)
    if not unmatched:
        return None
    lines = "\n".join(f"- {_item_label(item)}：{problem}" for item, problem in unmatched[:20])
    return f"已保存的计划有 {len(unmatched)} 条和正文对不上，要以正文为准重新保存：\n{lines}"


def repair_question(problem: str, task_name: str, report_text: str) -> str:
    return (
        f"下面是刚写好的《{task_name}》正文。{problem}\n\n"
        "现在只做一件事：调用 save_trade_plan，把正文里的操作按字段存下来，价格和股数必须和正文一模一样；"
        "账户 ID 可以用持仓工具查。不要改正文，不用重写报告，存好回一句“已保存”就行。\n\n"
        f"--- 正文开始 ---\n{report_text}\n--- 正文结束 ---"
    )


@dataclass(frozen=True)
class ActivationResult:
    note: str = ""  # 要追加到报告末尾的说明
    plan_url: str | None = None
    plan_id: str | None = None


def activate_for_report(
    db: Session,
    *,
    job_run_id: str,
    report_id: str,
    text: str,
    generated_at: str,
    today: str,
    session_live: bool,
) -> ActivationResult:
    """报告定稿后调用：标记对不上的条目（不盯价），让计划生效并替代旧计划。

    session_live 表示今天的交易还没结束：这时手动跑出一份明天才生效的计划，不能替代今天正在盯的计划。
    """
    plan = db.scalar(select(TradePlan).where(TradePlan.job_run_id == job_run_id).limit(1))
    if plan is None:
        return ActivationResult(note="这份清单没有存成结构化计划，这次不会有到价提醒。")
    now = utc_now()
    items = plan_items(db, plan.plan_id)
    unmatched = _unmatched_items(items, text)
    unmatched_ids = {item.id for item, _ in unmatched}
    for item in items:
        item.text_matched = item.id not in unmatched_ids

    since = plan.plan_date
    for older in db.scalars(
        select(TradePlan).where(
            TradePlan.owner_user_id == plan.owner_user_id,
            TradePlan.status == "active",
            TradePlan.plan_id != plan.plan_id,
            TradePlan.plan_date <= plan.plan_date,
        )
    ).all():
        if session_live and plan.plan_date > today and older.plan_date <= today:
            continue
        older.status = "superseded"
        older.superseded_at = now
        older.superseded_by = plan.plan_id
        since = min(since, older.plan_date)
    plan.status = "active"
    plan.activated_at = now
    plan.report_id = report_id
    db.flush()
    reconcile_owner(db, plan.owner_user_id, since=since)

    plan_url = None
    if items:
        label = rules.SOURCE_LABELS.get(plan.source, "操作计划")
        plan_url = create_share_link(
            db,
            ShareSpec(
                source_type="trade_plan",
                source_ref=plan.plan_id,
                owner_user_id=plan.owner_user_id,
                meta={"generated_at": generated_at},
            ),
            title=f"{label} | {plan.plan_date}",
            content="",
        )
    db.commit()

    note = ""
    if unmatched:
        names = "、".join(_item_label(item) for item, _ in unmatched[:5])
        more = f" 等 {len(unmatched)} 条" if len(unmatched) > 5 else ""
        note = f"有计划和正文对不上，系统不替这些盯价，请以正文为准：{names}{more}。"
    return ActivationResult(note=note, plan_url=plan_url, plan_id=plan.plan_id)


def plan_share_id(db: Session, plan_id: str) -> str | None:
    return db.scalar(
        select(ReportShare.share_id).where(
            ReportShare.source_type == "trade_plan",
            ReportShare.source_ref == plan_id,
            ReportShare.revoked_at.is_(None),
        ).limit(1)
    )


# ------------------------------------------------------------------ 成交对账
def _derive_status(item: TradePlanItem, plan: TradePlan, now: datetime) -> str:
    executed = float(item.executed_qty or 0)
    if item.quantity and executed >= float(item.quantity):
        return "executed"
    if executed > 0:
        return "partially_executed"
    if item.triggered_at is not None:
        return "triggered"
    closes_at = _market_close_utc(item.valid_until)
    if plan.superseded_at is not None and plan.superseded_at < closes_at:
        return "superseded"
    return "expired" if now >= closes_at else "pending"


def reconcile_owner(db: Session, owner_user_id: int, *, since: str, now: datetime | None = None) -> None:
    """按成交流水重算这位用户 since 之后所有计划的成交数量和状态。可以反复算；不提交事务。

    一笔成交按“账户 + 股票 + 方向 + 计划生效之后”分配：优先给最新的计划，再优先已触发的、
    价格最接近的条目。被替代之后才登记的成交不算到旧计划，除非那一条在被替代前就已经触发。
    """
    now = now or utc_now()
    plans = {
        plan.plan_id: plan
        for plan in db.scalars(
            select(TradePlan).where(
                TradePlan.owner_user_id == owner_user_id,
                TradePlan.status != "draft",
                TradePlan.plan_date >= since,
            )
        ).all()
    }
    if not plans:
        return
    items = list(db.scalars(select(TradePlanItem).where(TradePlanItem.plan_id.in_(plans))).all())
    candidates: dict[tuple[int | None, str, str], list[TradePlanItem]] = {}
    for item in items:
        family = rules.item_family(item.action)
        if family:
            candidates.setdefault((item.account_id, item.symbol, family), []).append(item)

    executed: dict[int, float] = {item.id: 0.0 for item in items}
    trade_ids: dict[int, list[int]] = {item.id: [] for item in items}
    if candidates:
        earliest = min(_trade_window(plans[i.plan_id], i)[0] for group in candidates.values() for i in group)
        trades = db.scalars(
            select(PositionTrade)
            .where(
                PositionTrade.owner_user_id == owner_user_id,
                PositionTrade.created_at >= earliest,
                PositionTrade.source_type.notin_(PositionWriteMixin.NON_TRADE_SOURCE_TYPES),
            )
            .order_by(PositionTrade.created_at, PositionTrade.id)
        ).all()
        for trade in trades:
            family = rules.trade_family(trade.action)
            group = candidates.get((trade.account_id, trade.symbol, family)) if family else None
            if not group or not trade.quantity or trade.created_at is None:
                continue
            eligible = []
            for item in group:
                plan = plans[item.plan_id]
                start, end = _trade_window(plan, item)
                if not start <= trade.created_at < end:
                    continue
                if plan.superseded_at and trade.created_at >= plan.superseded_at and item.triggered_at is None:
                    continue
                eligible.append(item)
            eligible.sort(
                key=lambda item: (
                    -(plans[item.plan_id].activated_at or datetime.min).timestamp()
                    if plans[item.plan_id].activated_at
                    else 0,
                    item.triggered_at is None,
                    abs(float(item.trigger_price or 0) - float(trade.price or item.trigger_price or 0)),
                    item.id,
                )
            )
            left = float(trade.quantity)
            for item in eligible:
                room = float(item.quantity or 0) - executed[item.id]
                if room <= 0:
                    continue
                take = min(room, left)
                executed[item.id] += take
                trade_ids[item.id].append(trade.id)
                left -= take
                if left <= 0:
                    break

    for item in items:
        new_qty = executed[item.id]
        new_ids = json.dumps(trade_ids[item.id]) if trade_ids[item.id] else None
        if float(item.executed_qty or 0) != new_qty:
            item.executed_qty = new_qty
        if item.trade_ids != new_ids:
            item.trade_ids = new_ids
        new_status = _derive_status(item, plans[item.plan_id], now)
        if item.status != new_status:
            item.status = new_status
    db.flush()


_PLACEHOLDER_BROKERS = frozenset({"manual", "ai_agent"})


def _account_label(account: PositionAccount | None, account_id: int | None) -> str:
    """券商名加账户号：同一家券商可能有两个账户，报告里也是按“账户3”称呼的。"""
    broker = account.broker_name if account is not None else None
    if not broker or broker in _PLACEHOLDER_BROKERS:
        return f"账户{account_id}"
    return f"{broker} 账户{account_id}"


def account_labels(db: Session, account_ids: set[int | None]) -> dict[int | None, str]:
    known = {i for i in account_ids if i is not None}
    rows = (
        {row.id: row for row in db.scalars(select(PositionAccount).where(PositionAccount.id.in_(known))).all()}
        if known
        else {}
    )
    return {account_id: _account_label(rows.get(account_id), account_id) for account_id in account_ids}


def reconciliation_text(db: Session, owner_user_id: int, plan_date: str) -> str:
    """当天计划和实际成交的对账结果，交给晚间日报直接用；当天没有计划就返回空串。"""
    reconcile_owner(db, owner_user_id, since=plan_date)
    db.commit()
    plans = list(
        db.scalars(
            select(TradePlan)
            .where(
                TradePlan.owner_user_id == owner_user_id,
                TradePlan.status != "draft",
                TradePlan.plan_date == plan_date,
            )
            .order_by(TradePlan.activated_at)
        ).all()
    )
    if not plans:
        return ""
    lines: list[str] = []
    for plan in plans:
        items = [i for i in plan_items(db, plan.plan_id) if rules.item_family(i.action)]
        shown = [i for i in items if not (plan.status == "superseded" and i.status == "superseded")]
        if not shown:
            continue
        labels = account_labels(db, {i.account_id for i in shown})
        lines.append(f"{rules.SOURCE_LABELS.get(plan.source, plan.source)}：")
        for item in shown:
            lines.append(f"- {labels[item.account_id]}｜{item.name or item.symbol} {item.symbol}：{_outcome_text(item)}")
    if not lines:
        return ""
    return (
        "今天的计划和实际成交，系统已经按登记的成交算好（只算系统里记过的成交，价格碰到不算成交，"
        "持仓修正和截图导入也不算）。“计划和实际对照”这一段直接用下面的结果，不用再翻文件比对：\n"
        + "\n".join(lines)
    )


def _outcome_text(item: TradePlanItem) -> str:
    plan_text = rules.describe_plan(item.action, item.trigger_type, item.trigger_price, item.quantity)
    parts = [f"计划{plan_text}"]
    triggered = beijing_time(item.triggered_at)
    if triggered is not None:
        parts.append(f"{triggered:%H:%M} 碰到过 {rules.format_price(item.triggered_price)}")
    executed = float(item.executed_qty or 0)
    if executed > 0:
        parts.append(f"已登记成交 {rules.format_quantity(executed)} 股")
    elif triggered is not None:
        parts.append("没有登记成交")
    else:
        parts.append({"expired": "没碰到计划价", "pending": "还没碰到计划价", "superseded": "已被新计划替代"}.get(item.status, item.status))
    return "，".join(parts)
