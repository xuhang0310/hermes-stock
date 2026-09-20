"""Read-only industry snapshots from the existing TuShare daily cache.

TuShare daily: change_pct is percent, amount is thousand CNY. Only this known
source is accepted; other sources cannot silently contribute different units.
Classification is the CURRENT stock_basic classification, not a historical index.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from statistics import mean, median

from sqlalchemy import func, or_, select

from backend.app.core.database import SessionLocal
from backend.app.core.models import DailyBar, Instrument, MarketTradingCalendar, StockDailyBasic

MIN_COVERAGE = 0.90
# Stock catalogs can also contain B shares (900xxx.SH / 200xxx.SZ).
_SYMBOL = re.compile(r"^(?:6\d{5}\.SH|[03]\d{5}\.SZ|[489]\d{5}\.BJ)$")


def _number(value) -> float | None:
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def _date(value) -> str:
    compact = str(value or "").replace("-", "")
    try:
        if len(compact) != 8 or not compact.isdigit():
            return ""
        return date.fromisoformat(compact).strftime("%Y%m%d")
    except ValueError:
        return ""


def _iso(compact: str | None) -> str | None:
    return f"{compact[:4]}-{compact[4:6]}-{compact[6:]}" if compact else None


def _as_of_inventory(inventory, day):
    return [item for item in inventory if not _date(item.list_date) or _date(item.list_date) <= day]


def _member(instrument, bar, basic, as_of: str) -> dict:
    price = _number(bar.close) if bar else None
    volume = _number(bar.volume) if bar else None
    pct = _number(bar.change_pct) if bar else None
    if pct is None and bar and (previous := _number(bar.pre_close)) and previous > 0 and price:
        pct = _number((price / previous - 1) * 100)
    status = "missing"
    if bar:
        if volume == 0:
            status = "no_trade"
        elif price is not None and price > 0 and volume is not None and volume > 0 and pct is not None and pct >= -100:
            status = "valid"
        else:
            status = "invalid"
    # Missing/zero-volume bars must not be counted as flat or as trading candidates.
    if status != "valid":
        pct = None
    amount = _number(bar.amount) if bar and status == "valid" else None
    amount_yi = amount / 100_000 if amount is not None and amount >= 0 else None
    total_mv = _number(basic.total_mv) if basic else None
    turnover = _number(basic.turnover_rate) if basic and status == "valid" else None
    ratio = _number(basic.volume_ratio) if basic and status == "valid" else None
    return {
        "symbol": instrument.symbol, "name": instrument.name, "market": instrument.market,
        "industry": (instrument.industry or "").strip() or None,
        "price": price if price is not None and price > 0 else None,
        "pct_chg": pct, "amount_yi": amount_yi,
        "turnover_rate": turnover if turnover is not None and turnover >= 0 else None,
        "volume_ratio": ratio if ratio is not None and ratio >= 0 else None,
        "total_mv_yi": total_mv / 10_000 if total_mv is not None and total_mv > 0 else None,
        "pe_ttm": _number(basic.pe_ttm) if basic else None,
        "trade_date": _iso(as_of), "ma_value": None, "status": status,
    }


def _aggregate(industry: str, members: list[dict]) -> dict:
    valid = [item for item in members if item["status"] == "valid"]
    pcts = [item["pct_chg"] for item in valid]
    amounts = [item["amount_yi"] for item in valid if item["amount_yi"] is not None]
    up = sum(value > 0 for value in pcts)
    down = sum(value < 0 for value in pcts)
    leaders = sorted(valid, key=lambda item: (-item["pct_chg"], item["symbol"]))[:3]
    return {
        "industry": industry, "stock_count": len(members), "valid_count": len(valid),
        "coverage": len(valid) / len(members),
        "avg_pct": mean(pcts) if pcts else None,
        "median_pct": median(pcts) if pcts else None,
        "up_count": up, "down_count": down, "flat_count": len(pcts) - up - down,
        "up_ratio": up / len(pcts) if pcts else None,
        "amount_yi": sum(amounts) if amounts else None,
        "amount_count": len(amounts),
        "missing_count": sum(item["status"] == "missing" for item in members),
        "no_trade_count": sum(item["status"] == "no_trade" for item in members),
        "invalid_count": sum(item["status"] == "invalid" for item in members),
        "leader": leaders[0] if leaders else None,
        "leaders": leaders,
    }


def load_snapshot(*, now: datetime | None = None) -> dict:
    """No external calls, writes or persistent cache; each request sees one DB transaction.

    Prefer the newest daily cross section covering >=90% of the eligible current
    catalog in the last 120 calendar days. This is a coverage threshold, not proof
    of a complete universe. A newer partial day is disclosed, never merged in.
    """
    now = now or datetime.now(timezone(timedelta(hours=8)))
    today = now.date()
    cutoff = today if now.hour >= 16 else today - timedelta(days=1)
    end = cutoff.strftime("%Y%m%d")
    start = (today - timedelta(days=120)).strftime("%Y%m%d")
    normalized_day = func.replace(DailyBar.trade_date, "-", "")
    with SessionLocal() as db:
        inventory = [item for item in db.scalars(select(Instrument).where(
            Instrument.type == "stock",
            or_(Instrument.list_status.is_(None), Instrument.list_status.in_(("L", "P"))),
        ).order_by(Instrument.symbol)).all() if _SYMBOL.fullmatch(item.symbol)]
        symbols = [item.symbol for item in inventory]
        counts = db.execute(select(
            normalized_day.label("day"), func.count(func.distinct(DailyBar.symbol)).label("count"),
        ).where(
            DailyBar.source == "tushare", DailyBar.symbol.in_(symbols),
            or_(DailyBar.trade_date.between(start, end), DailyBar.trade_date.between(_iso(start), _iso(end))),
        ).group_by(normalized_day).order_by(normalized_day.desc())).all() if symbols else []
        counts = [row for row in counts if _date(row.day)]
        as_of = counts[0].day if counts else None
        latest_available = as_of
        for row in counts:
            expected = len(_as_of_inventory(inventory, row.day))
            if expected and row.count / expected >= MIN_COVERAGE:
                as_of = row.day
                break
        inventory = _as_of_inventory(inventory, as_of or end)
        bars, basics = {}, {}
        if as_of:
            variants = [as_of, _iso(as_of)]
            # Ascending id means a newer normalized-date duplicate wins exactly once.
            for bar in db.scalars(select(DailyBar).where(
                DailyBar.source == "tushare", DailyBar.symbol.in_(symbols),
                DailyBar.trade_date.in_(variants),
            ).order_by(DailyBar.id)):
                bars[bar.symbol] = bar
            for basic in db.scalars(select(StockDailyBasic).where(
                StockDailyBasic.symbol.in_(symbols), StockDailyBasic.trade_date.in_(variants),
            ).order_by(StockDailyBasic.id)):
                basics[basic.symbol] = basic
        members = [_member(item, bars.get(item.symbol), basics.get(item.symbol), as_of or "") for item in inventory]
        basic_count = sum(item.symbol in basics for item in inventory)
        # Only claim calendar freshness when the local calendar reaches the cutoff.
        calendar_end = db.scalar(select(func.max(MarketTradingCalendar.cal_date)).where(
            MarketTradingCalendar.exchange == "SSE", MarketTradingCalendar.source == "tushare",
            MarketTradingCalendar.cal_date <= end,
        ))
        expected_date = db.scalar(select(func.max(MarketTradingCalendar.cal_date)).where(
            MarketTradingCalendar.exchange == "SSE", MarketTradingCalendar.source == "tushare",
            MarketTradingCalendar.is_open.is_(True), MarketTradingCalendar.cal_date <= end,
        )) if calendar_end == end else None
    groups = defaultdict(list)
    for item in members:
        if item["industry"]:
            groups[item["industry"]].append(item)
    industries = [_aggregate(key, rows) for key, rows in sorted(groups.items())]
    industries.sort(key=lambda item: (item["avg_pct"] is None, -(item["avg_pct"] or 0), item["industry"]))
    total = len(members)
    valid = sum(item["status"] == "valid" for item in members)
    missing = sum(item["status"] == "missing" for item in members)
    coverage = (total - missing) / total if total else 0
    warnings = []
    if not as_of:
        warnings.append("最近 120 天没有可用的 TuShare 日线，请在 AI 选股页同步市场数据。")
    elif coverage < MIN_COVERAGE:
        warnings.append("当前日线覆盖不足 90%，榜单仅代表已覆盖样本。")
    if latest_available and as_of != latest_available:
        warnings.append(f"{_iso(latest_available)} 的覆盖不足，已使用 {_iso(as_of)} 的较完整截面。")
    if expected_date and (not as_of or as_of < expected_date):
        warnings.append(f"日线尚未覆盖最近已收盘交易日 {_iso(expected_date)}，请同步市场数据。")
    if not expected_date:
        warnings.append("本地交易日历不足，无法确认是否已覆盖最近已收盘交易日；请核对统计日期。")
    unknown = sum(not item["industry"] for item in members)
    if unknown:
        warnings.append(f"{unknown} 只股票缺少行业分类，未纳入行业榜单。")
    if as_of and basic_count < total - missing:
        warnings.append(f"同日每日指标仅覆盖 {basic_count} 只，缺失的换手率、量比和市值显示为 —；未补用其他日期数据。")
    output = {
        "as_of": _iso(as_of), "latest_available_date": _iso(latest_available),
        "expected_date": _iso(expected_date),
        "classification": "TuShare stock_basic 当前行业分类",
        "source": "tushare.daily / tushare.daily_basic",
        "coverage_status": "empty" if not as_of else "sufficient" if coverage >= MIN_COVERAGE else "partial",
        "coverage_threshold": MIN_COVERAGE,
        "coverage": {"stock_count": total, "bar_count": total - missing, "valid_count": valid,
                     "daily_basic_count": basic_count,
                     "classified_count": total - unknown, "unclassified_count": unknown,
                     "missing_count": missing, "bar_ratio": coverage,
                     "no_trade_count": sum(item["status"] == "no_trade" for item in members),
                     "invalid_count": sum(item["status"] == "invalid" for item in members)},
        "industries": industries, "warnings": warnings,
    }
    fingerprint = json.dumps([output, members], sort_keys=True, ensure_ascii=False, allow_nan=False)
    output["snapshot_id"] = hashlib.sha256(fingerprint.encode()).hexdigest()[:24]
    return {"overview": output, "members": members}


def industry_members(snapshot: dict, industry: str, *, sort_by="pct_chg", order="desc", offset=0, limit=50) -> dict:
    rows = [item for item in snapshot["members"] if item["industry"] == industry]
    direction = -1 if order == "desc" else 1
    rows.sort(key=lambda item: (item[sort_by] is None, direction * (item[sort_by] or 0), item["symbol"]))
    overview = snapshot["overview"]
    return {"snapshot_id": overview["snapshot_id"], "as_of": overview["as_of"],
            "industry": industry, "total": len(rows), "items": rows[offset:offset + limit]}
