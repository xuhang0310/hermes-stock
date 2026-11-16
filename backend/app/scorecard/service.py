"""建议成绩单：把计划里的每条建议记下来，到期后和全市场平均比，算出对错。

每天收盘数据入库后跑一次（挂在影子账户结算后面），可以反复跑：
1. 补全市场当天的平均涨跌（基准）。
2. 把已经给客户看过的计划（生效或被替代的）里的建议登记进来。
3. 到期的建议算结果。

只读计划和行情，不改计划，也不影响提醒。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.applog import get_logger
from backend.app.core.database import SessionLocal
from backend.app.core.models import AdviceOutcome, DailyBar, Instrument, MarketDailyReturn, MarketTradingCalendar, StockModelSignal, TradePlan, TradePlanItem
from backend.app.plans.rules import SOURCE_LABELS
from backend.app.plans.service import account_labels
from backend.app.quant.research_panel import A_SHARE
from backend.app.scorecard import rules

logger = get_logger("scorecard")

LOOKBACK_DAYS = 150  # 往回登记和补基准最多这么多天，更早的计划不追
_SYMBOL_BATCH = 300
_SHOWN_STATUSES = ("active", "superseded")  # 草稿没给客户看过，不算建议
_RECENT_LIMIT = 60


def _compact(day: str) -> str:
    return day.replace("-", "")


def open_days(db: Session, first: str, last: str) -> list[str]:
    return list(
        db.scalars(
            select(MarketTradingCalendar.cal_date)
            .where(MarketTradingCalendar.exchange == "SSE", MarketTradingCalendar.is_open.is_(True), MarketTradingCalendar.cal_date >= first, MarketTradingCalendar.cal_date <= last)
            .order_by(MarketTradingCalendar.cal_date)
        ).all()
    )


def ensure_market_returns(db: Session, first: str, last: str, *, min_stocks: int = rules.MIN_MARKET_STOCKS) -> int:
    """补上 first 到 last 之间还没算的交易日。数据没到齐的日子不存，下次再算。返回新补的天数。"""
    days = open_days(db, first, last)
    have = set(db.scalars(select(MarketDailyReturn.trade_date).where(MarketDailyReturn.trade_date >= first, MarketDailyReturn.trade_date <= last)).all())
    missing = [day for day in days if day not in have]
    if not missing:
        return 0
    wanted = set(missing)
    codes = sorted(code for code in db.scalars(select(Instrument.symbol)).all() if A_SHARE.fullmatch(code))
    totals: dict[str, list[float]] = {day: [0.0, 0] for day in missing}
    # 按股票分批读：daily_bars 的索引以股票代码开头，按日期扫全市场是全表扫描
    for start in range(0, len(codes), _SYMBOL_BATCH):
        rows = db.execute(
            select(DailyBar.trade_date, DailyBar.close, DailyBar.pre_close).where(
                DailyBar.symbol.in_(codes[start : start + _SYMBOL_BATCH]),
                DailyBar.trade_date >= missing[0],
                DailyBar.trade_date <= missing[-1],
                DailyBar.source == "tushare",
            )
        ).all()
        for trade_date, close, pre_close in rows:
            change = rules.daily_return(close, pre_close)
            if change is not None and trade_date in wanted:
                totals[trade_date][0] += change
                totals[trade_date][1] += 1
        if start >= 2 * _SYMBOL_BATCH and not any(count for _, count in totals.values()):
            break  # 前几批一行都没有，说明这些天的数据还没入库
    added = 0
    for day, (total, count) in totals.items():
        if count >= min_stocks:
            db.add(MarketDailyReturn(trade_date=day, mean_return=total / count, stock_count=count))
            added += 1
    db.flush()
    return added


def _model_ranks(db: Session, pairs: Iterable[tuple[str, str]]) -> dict[tuple[str, str], float]:
    """(建议前一个交易日, 股票) → 模型排名。信号只留 30 天，更早的查不到。"""
    by_day: dict[str, set[str]] = {}
    for day, symbol in pairs:
        by_day.setdefault(day, set()).add(symbol)
    found: dict[tuple[str, str], float] = {}
    for day, symbols in by_day.items():
        rows = db.execute(select(StockModelSignal.symbol, StockModelSignal.model_rank).where(StockModelSignal.trade_date == day, StockModelSignal.symbol.in_(symbols))).all()
        found.update({(day, symbol): rank for symbol, rank in rows if rank is not None})
    return found


def record_advice(db: Session, since: str, *, owner_user_id: int | None = None) -> int:
    """把 since（YYYYMMDD）以来给客户看过的计划里的建议登记进来。返回新登记的建议数（不乘期限）。"""
    since_iso = f"{since[:4]}-{since[4:6]}-{since[6:]}"
    query = (
        select(TradePlanItem, TradePlan)
        .join(TradePlan, TradePlan.plan_id == TradePlanItem.plan_id)
        .where(TradePlan.status.in_(_SHOWN_STATUSES), TradePlan.plan_date >= since_iso, TradePlanItem.text_matched.is_(True))
    )
    if owner_user_id is not None:
        query = query.where(TradePlan.owner_user_id == owner_user_id)
    candidates = []
    for item, plan in db.execute(query).all():
        side = rules.direction(item.action)
        if side is None or item.owner_user_id != plan.owner_user_id:
            continue  # 条目和计划不是同一位用户的，不登记到任何人名下
        candidates.append(
            {
                "owner": plan.owner_user_id,
                "account": item.account_id or 0,
                "advice_date": _compact(plan.plan_date),
                "symbol": item.symbol,
                "direction": side,
                "order": (plan.activated_at or plan.created_at or datetime.min, item.id),
                "item": item,
                "plan": plan,
            }
        )
    chosen = rules.latest_per_day(candidates)
    if not chosen:
        return 0
    first = min(entry["advice_date"] for entry in chosen)
    calendar = open_days(db, (datetime.strptime(first, "%Y%m%d") - timedelta(days=15)).strftime("%Y%m%d"), max(entry["advice_date"] for entry in chosen))
    before = {day: calendar[i - 1] for i, day in enumerate(calendar) if i}
    ranks = _model_ranks(db, {(before[entry["advice_date"]], entry["symbol"]) for entry in chosen if entry["advice_date"] in before})
    saved = select(AdviceOutcome).where(AdviceOutcome.advice_date >= first)
    if owner_user_id is not None:
        saved = saved.where(AdviceOutcome.owner_user_id == owner_user_id)
    existing = {(row.owner_user_id, row.account_id, row.advice_date, row.symbol, row.direction, row.horizon_days): row for row in db.scalars(saved).all()}
    created = 0
    for entry in chosen:
        item, plan = entry["item"], entry["plan"]
        rank = ranks.get((before.get(entry["advice_date"], ""), entry["symbol"]))
        is_new = False
        for horizon in rules.HORIZONS:
            row = existing.get((entry["owner"], entry["account"], entry["advice_date"], entry["symbol"], entry["direction"], horizon))
            if row is None:
                row = AdviceOutcome(owner_user_id=entry["owner"], account_id=entry["account"], advice_date=entry["advice_date"], symbol=entry["symbol"], direction=entry["direction"], horizon_days=horizon, action=item.action, model_rank=rank)
                db.add(row)
                is_new = True
            # 同一天后来的计划替换了这条建议，或者客户后来照做了：跟着更新，结果不动
            row.name = item.name
            row.action = item.action
            row.plan_id = plan.plan_id
            row.plan_item_id = item.id
            row.plan_source = plan.source
            row.digital_human_code = plan.digital_human_code
            row.trigger_price = item.trigger_price
            row.followed = (item.executed_qty or 0) > 0
            row.size_changed = bool((item.size_note or "").strip())
            if row.model_rank is None:
                row.model_rank = rank
        created += int(is_new)
    db.flush()
    return created


def settle(db: Session, last: str, *, owner_user_id: int | None = None, now: datetime | None = None) -> int:
    """给到期的建议算结果。last 是最新一个有收盘数据的交易日（YYYYMMDD）。返回这次算出来的条数。"""
    query = select(AdviceOutcome).where(AdviceOutcome.verdict.is_(None), AdviceOutcome.advice_date <= last)
    if owner_user_id is not None:
        query = query.where(AdviceOutcome.owner_user_id == owner_user_id)
    pending = list(db.scalars(query).all())
    if not pending:
        return 0
    first = min(row.advice_date for row in pending)
    calendar = open_days(db, first, last)
    position = {day: i for i, day in enumerate(calendar)}
    market = dict(db.execute(select(MarketDailyReturn.trade_date, MarketDailyReturn.mean_return).where(MarketDailyReturn.trade_date >= first, MarketDailyReturn.trade_date <= last)).all())
    due_rows = []
    for row in pending:
        start = position.get(row.advice_date)
        if start is None or start + row.horizon_days >= len(calendar):
            continue  # 建议日不是交易日，或者还没到期
        window = calendar[start + 1 : start + 1 + row.horizon_days]
        if any(day not in market for day in window):
            continue  # 基准还没补齐，下次再算
        due_rows.append((row, window))
    if not due_rows:
        return 0
    symbols = sorted({row.symbol for row, _ in due_rows})
    bars: dict[tuple[str, str], float] = {}
    for start in range(0, len(symbols), _SYMBOL_BATCH):
        for symbol, trade_date, close, pre_close in db.execute(
            select(DailyBar.symbol, DailyBar.trade_date, DailyBar.close, DailyBar.pre_close).where(
                DailyBar.symbol.in_(symbols[start : start + _SYMBOL_BATCH]), DailyBar.trade_date > first, DailyBar.trade_date <= last, DailyBar.source == "tushare"
            )
        ).all():
            change = rules.daily_return(close, pre_close)
            if change is not None:
                bars[(symbol, trade_date)] = change
    stamp = now or datetime.utcnow()
    for row, window in due_rows:
        moves = [bars[(row.symbol, day)] for day in window if (row.symbol, day) in bars]
        row.due_date = window[-1]
        row.evaluated_at = stamp
        if not moves:
            row.verdict = rules.NO_DATA  # 这段时间一直停牌，或者这只不在全市场 K 线里（比如 ETF）
            continue
        row.stock_return = rules.compound(moves)
        row.market_return = rules.compound(market[day] for day in window)
        row.excess_return = row.stock_return - row.market_return
        row.verdict = rules.verdict(row.direction, row.excess_return)
    db.flush()
    return len(due_rows)


def run_daily(now: datetime, *, owner_user_id: int | None = None, session_factory: Callable[[], Session] = SessionLocal) -> str:
    """定时任务入口，now 是北京时间。失败只记日志，不影响同一个任务里的其他步骤。

    定时任务不传 owner_user_id，处理所有用户；页面上手动更新只处理点按钮的那位用户自己的建议。
    全市场平均是公共数据，谁触发都会补。
    """
    last = now.strftime("%Y%m%d")
    since = (now - timedelta(days=LOOKBACK_DAYS)).strftime("%Y%m%d")
    try:
        with session_factory() as db:
            added = ensure_market_returns(db, since, last)
            created = record_advice(db, since, owner_user_id=owner_user_id)
            latest = db.scalar(select(MarketDailyReturn.trade_date).order_by(MarketDailyReturn.trade_date.desc()).limit(1))
            settled = settle(db, latest, owner_user_id=owner_user_id) if latest else 0
            db.commit()
        return f"成绩单：新登记 {created} 条建议，算出 {settled} 条结果，补基准 {added} 天"
    except Exception as exc:  # noqa: BLE001 - 成绩单失败不能影响影子账户结算
        logger.error("成绩单更新失败: %s", str(exc)[:300])
        return f"成绩单更新失败: {str(exc)[:200]}"


def _group(rows: list[dict[str, Any]], key: Callable[[dict[str, Any]], str], labels: dict[str, str] | None = None) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        buckets.setdefault(key(row), []).append(row)
    result = [{"key": name, "label": (labels or {}).get(name) or name or "其他", **rules.summarize(items)} for name, items in buckets.items()]
    return sorted(result, key=lambda item: -item["total"])


def summary(db: Session, owner_user_id: int, *, account_id: int | None = None, horizon: int = rules.PRIMARY_HORIZON, days: int = 180, now: datetime | None = None) -> dict[str, Any]:
    """一位用户的成绩单：总体、按账户、按建议类型、按计划、按有没有照做、按模型看法，再加最近的明细。

    只读这位用户自己的行。account_id 只在他自己的账户里筛，传了别人的账户号只会得到空结果。
    """
    since = ((now or datetime.now()) - timedelta(days=days)).strftime("%Y%m%d")
    mine = list(
        db.scalars(
            select(AdviceOutcome)
            .where(AdviceOutcome.owner_user_id == owner_user_id, AdviceOutcome.horizon_days == horizon, AdviceOutcome.advice_date >= since)
            .order_by(AdviceOutcome.advice_date.desc(), AdviceOutcome.id.desc())
        ).all()
    )
    labels = {key or 0: value for key, value in account_labels(db, {row.account_id or None for row in mine}).items()}
    labels[0] = "没有写账户"
    accounts = [{"account_id": key, "label": labels[key]} for key in sorted({row.account_id for row in mine})]
    outcomes = [row for row in mine if account_id is None or row.account_id == account_id]
    rows = [
        {
            "verdict": row.verdict,
            "direction": row.direction,
            "excess": row.excess_return or 0.0,
            "group": rules.ACTION_GROUPS.get(row.action, row.action),
            "source": row.plan_source or "",
            "followed": "照做了" if row.followed else "没有照做",
            "model": rules.model_view(row.direction, row.model_rank),
            "size": "数字人改了股数" if row.size_changed else "按代码建议的股数",
            "account": str(row.account_id),
        }
        for row in outcomes
    ]
    recent = [
        {
            "advice_date": row.advice_date,
            "account_id": row.account_id,
            "account_label": labels[row.account_id],
            "symbol": row.symbol,
            "name": row.name,
            "action": row.action,
            "action_label": rules.ACTION_GROUPS.get(row.action, row.action),
            "plan_source": SOURCE_LABELS.get(row.plan_source or "", "操作计划"),
            "trigger_price": row.trigger_price,
            "followed": row.followed,
            "model_view": rules.model_view(row.direction, row.model_rank),
            "due_date": row.due_date,
            "stock_return": row.stock_return,
            "market_return": row.market_return,
            "excess_return": row.excess_return,
            "verdict": row.verdict,
        }
        for row in outcomes[:_RECENT_LIMIT]
    ]
    return {
        "horizon": horizon,
        "horizons": list(rules.HORIZONS),
        "account_id": account_id,
        "accounts": accounts,
        "by_account": _group(rows, lambda row: row["account"], {str(key): value for key, value in labels.items()}),
        "days": days,
        "min_samples": rules.MIN_SAMPLES,
        "dead_band": rules.DEAD_BAND,
        "overall": rules.summarize(rows),
        "pending": sum(1 for row in outcomes if row.verdict is None),
        "no_data": sum(1 for row in outcomes if row.verdict == rules.NO_DATA),
        "first_advice_date": outcomes[-1].advice_date if outcomes else None,
        "by_action": _group(rows, lambda row: row["group"]),
        "by_source": _group(rows, lambda row: row["source"], SOURCE_LABELS),
        "by_followed": _group(rows, lambda row: row["followed"]),
        "by_model": _group(rows, lambda row: row["model"], rules.MODEL_VIEW_LABELS),
        "by_size": _group(rows, lambda row: row["size"]),
        "recent": recent,
    }
