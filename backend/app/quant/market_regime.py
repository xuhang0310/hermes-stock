"""L1 A 股本土市场环境温度计（Market Regime）。

工程铁律：
1. 摒弃美债收益率/VIX 等美股指标，改用 A 股场内真实温度计：
   - 全市场站上 20 日 / 60 日均线宽度占比（Breadth MA20 / MA60）
   - 两市总成交额及近 120 日成交分位（Volume Energy Percentile）
   - 沪深300（大盘）与中证1000（中小盘）60/200 日均线趋势状态
2. L1 绝不作为加权分掺入个股综合得分，只独立输出：
   - 建议总仓位上限：进攻 90% / 中性 60% / 防守 30%
   - 大小盘风格象限：普涨共振 / 中小盘占优 / 大盘价值占优 / 防御控仓
"""
from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.models import (
    DailyBar,
    Instrument,
    MarketRegimeDaily,
    MarketTradingCalendar,
    StockDailyBasic,
)
from backend.app.market.index_sync import load_index_closes
from backend.app.quant.tech_signal import Bar, adjusted_bars

logger = logging.getLogger(__name__)

STANCE_ATTACK = "attack"
STANCE_NEUTRAL = "neutral"
STANCE_DEFENSE = "defense"

STANCE_LABELS = {
    STANCE_ATTACK: "进攻",
    STANCE_NEUTRAL: "中性",
    STANCE_DEFENSE: "防守",
}

POSITION_CAPS = {
    STANCE_ATTACK: 0.90,
    STANCE_NEUTRAL: 0.60,
    STANCE_DEFENSE: 0.30,
}

STYLE_LABELS = {
    "broad_rally": "大小盘共振顺风",
    "small_cap_active": "中小盘弹性占优",
    "large_cap_value": "大盘蓝筹稳健占优",
    "defensive_cash": "缩量弱势防御控仓",
    "balanced": "均衡震荡轮动",
}


def _compact_date(value: Any) -> str:
    raw = str(value or "").strip().replace("-", "")
    return raw if len(raw) == 8 and raw.isdigit() else ""


def _ma(values: list[float], window: int) -> float | None:
    if len(values) < window or window <= 0:
        return None
    return sum(values[-window:]) / window


def classify_index_trend(closes: list[float]) -> dict[str, Any]:
    """根据指数收盘序列计算 MA60 / MA200 与多空趋势。"""
    if not closes:
        return {"close": None, "ma60": None, "ma200": None, "ret20": None, "trend": "neutral", "score": 50.0}
    last = closes[-1]
    ma20 = _ma(closes, 20)
    ma60 = _ma(closes, 60)
    ma200 = _ma(closes, 200)
    ret20 = (last / closes[-21] - 1.0) if len(closes) >= 21 and closes[-21] > 0 else None

    if ma60 is None:
        if ma20 is not None and last > ma20:
            trend, score = "bull", 70.0
        elif ma20 is not None and last < ma20:
            trend, score = "bear", 35.0
        else:
            trend, score = "neutral", 50.0
    else:
        above_60 = last >= ma60
        above_200 = True if ma200 is None else (last >= ma200)
        if above_60 and above_200:
            trend, score = "bull", 85.0
        elif not above_60 and (ma200 is None or last < ma200):
            trend, score = "bear", 20.0
        else:
            trend, score = "neutral", 52.0

    return {
        "close": round(last, 2),
        "ma60": round(ma60, 2) if ma60 is not None else None,
        "ma200": round(ma200, 2) if ma200 is not None else None,
        "ret20": round(ret20, 4) if ret20 is not None else None,
        "trend": trend,
        "score": score,
    }


def evaluate_regime_metrics(
    *,
    trade_date: str,
    breadth_ma20: float | None,
    breadth_ma60: float | None,
    up_down_ratio: float | None,
    total_amount_yi: float | None,
    amount_pct_120: float | None,
    hs300_info: dict[str, Any],
    zz1000_info: dict[str, Any],
) -> dict[str, Any]:
    """纯函数：由市场客观温度计合成 L1 状态、总仓位上限与风格象限。"""
    b20 = breadth_ma20 if breadth_ma20 is not None else 0.50
    b60 = breadth_ma60 if breadth_ma60 is not None else b20
    breadth_score = max(0.0, min(100.0, (0.60 * b20 + 0.40 * b60) * 100.0))

    vol_pct = amount_pct_120 if amount_pct_120 is not None else 0.50
    vol_score = max(0.0, min(100.0, vol_pct * 100.0))

    idx_score = 0.5 * float(hs300_info.get("score", 50.0)) + 0.5 * float(zz1000_info.get("score", 50.0))
    regime_score = round(0.45 * breadth_score + 0.25 * vol_score + 0.30 * idx_score, 1)

    if regime_score >= 65.0 and b20 >= 0.50:
        stance = STANCE_ATTACK
    elif regime_score < 40.0 or b20 < 0.25:
        stance = STANCE_DEFENSE
    else:
        stance = STANCE_NEUTRAL

    max_pos = POSITION_CAPS[stance]

    hs_ret = hs300_info.get("ret20")
    zz_ret = zz1000_info.get("ret20")
    hs_trend = hs300_info.get("trend", "neutral")
    zz_trend = zz1000_info.get("trend", "neutral")

    if stance == STANCE_DEFENSE:
        style_quadrant = "defensive_cash"
    elif hs_trend == "bull" and zz_trend == "bull":
        if zz_ret is not None and hs_ret is not None and zz_ret - hs_ret > 0.03:
            style_quadrant = "small_cap_active"
        elif hs_ret is not None and zz_ret is not None and hs_ret - zz_ret > 0.03:
            style_quadrant = "large_cap_value"
        else:
            style_quadrant = "broad_rally"
    elif zz_trend == "bull" or (zz_ret is not None and hs_ret is not None and zz_ret - hs_ret > 0.025):
        style_quadrant = "small_cap_active"
    elif hs_trend == "bull" or (hs_ret is not None and zz_ret is not None and hs_ret - zz_ret > 0.025):
        style_quadrant = "large_cap_value"
    else:
        style_quadrant = "balanced"

    amt_text = f"{total_amount_yi:,.0f} 亿元" if total_amount_yi is not None else "暂无"
    b20_text = f"{b20 * 100:.1f}%" if breadth_ma20 is not None else "暂无"
    b60_text = f"{b60 * 100:.1f}%" if breadth_ma60 is not None else "暂无"
    summary = (
        f"L1 市场环境【{STANCE_LABELS[stance]}】（温度 {regime_score:.0f}/100，建议总仓位上限 {int(max_pos * 100)}%）："
        f"全 A 站上 20 日线占比 {b20_text}、60 日线占比 {b60_text}，两市成交 {amt_text}，"
        f"风格象限为「{STYLE_LABELS.get(style_quadrant, style_quadrant)}」。"
    )

    return {
        "trade_date": trade_date,
        "breadth_ma20": round(breadth_ma20, 4) if breadth_ma20 is not None else None,
        "breadth_ma60": round(breadth_ma60, 4) if breadth_ma60 is not None else None,
        "up_down_ratio": round(up_down_ratio, 3) if up_down_ratio is not None else None,
        "total_amount_yi": round(total_amount_yi, 2) if total_amount_yi is not None else None,
        "amount_pct_120": round(amount_pct_120, 4) if amount_pct_120 is not None else None,
        "hs300_close": hs300_info.get("close"),
        "hs300_ma60": hs300_info.get("ma60"),
        "hs300_ma200": hs300_info.get("ma200"),
        "hs300_trend": hs_trend,
        "zz1000_close": zz1000_info.get("close"),
        "zz1000_ma60": zz1000_info.get("ma60"),
        "zz1000_ma200": zz1000_info.get("ma200"),
        "zz1000_trend": zz_trend,
        "regime_score": regime_score,
        "stance": stance,
        "stance_label": STANCE_LABELS[stance],
        "max_position_ratio": max_pos,
        "style_quadrant": style_quadrant,
        "style_label": STYLE_LABELS.get(style_quadrant, style_quadrant),
        "summary": summary,
    }


def compute_and_store_market_regime(
    db: Session,
    trade_date: str,
    *,
    preloaded_bars: dict[str, list[Bar]] | None = None,
    preloaded_up_down: tuple[int, int] | None = None,
    preloaded_daily_amounts_yi: dict[str, float] | None = None,
) -> dict[str, Any]:
    """基于本地不晚于 trade_date 的日线与宽基指数计算并幂等写入 MarketRegimeDaily。"""
    compact = _compact_date(trade_date)
    if not compact:
        raise ValueError(f"无效交易日：{trade_date}")

    # 取最近 120 个开市日（走交易日历索引或 stock_daily_basic 日期索引，绝不在无 symbol 条件下全表扫 daily_bars）
    cal_days = sorted(
        db.scalars(
            select(MarketTradingCalendar.cal_date)
            .where(
                MarketTradingCalendar.exchange == "SSE",
                MarketTradingCalendar.is_open.is_(True),
                MarketTradingCalendar.cal_date <= compact,
            )
            .order_by(MarketTradingCalendar.cal_date.desc())
            .limit(120)
        ).all()
    )
    if not cal_days:
        cal_days = sorted(
            db.scalars(
                select(StockDailyBasic.trade_date)
                .where(StockDailyBasic.trade_date <= compact)
                .group_by(StockDailyBasic.trade_date)
                .order_by(StockDailyBasic.trade_date.desc())
                .limit(120)
            ).all()
        )
    if not cal_days and preloaded_bars:
        all_d = {b.trade_date for blist in preloaded_bars.values() for b in blist if b.trade_date <= compact}
        cal_days = sorted(all_d)[-120:]
    if not cal_days:
        cal_days = [compact]

    breadth_days = cal_days[-65:]
    start_breadth = breadth_days[0]

    # 1. 市场宽度、涨跌比与每日总成交额：单次按 symbol 索引分批扫描同时产出（或直接复用上游预加载数据）
    daily_amounts_yi: dict[str, float] = dict(preloaded_daily_amounts_yi or {})
    if preloaded_bars is not None and preloaded_up_down is not None:
        bars_by_sym = preloaded_bars
        up_count, down_count = preloaded_up_down
    else:
        active_symbols = sorted(
            db.scalars(
                select(Instrument.symbol).where(
                    Instrument.type == "stock",
                    (Instrument.list_status.is_(None)) | (Instrument.list_status == "L"),
                )
            ).all()
        )
        bars_by_sym = defaultdict(list)
        up_count = 0
        down_count = 0
        amt_sum_qian: dict[str, float] = defaultdict(float)

        for idx in range(0, len(active_symbols), 300):
            batch = active_symbols[idx : idx + 300]
            rows = db.execute(
                select(
                    DailyBar.symbol,
                    DailyBar.trade_date,
                    DailyBar.high,
                    DailyBar.low,
                    DailyBar.close,
                    DailyBar.pre_close,
                    DailyBar.change_pct,
                    DailyBar.volume,
                    DailyBar.amount,
                ).where(
                    DailyBar.symbol.in_(batch),
                    DailyBar.trade_date >= start_breadth,
                    DailyBar.trade_date <= compact,
                    DailyBar.source == "tushare",
                    DailyBar.close.is_not(None),
                    DailyBar.close > 0,
                )
            ).all()
            dedup: dict[tuple[str, str], Any] = {}
            for r in rows:
                dedup[(str(r.symbol), str(r.trade_date))] = r
            for (sym, day), r in sorted(dedup.items()):
                bars_by_sym[sym].append(
                    Bar(
                        trade_date=day,
                        high=float(r.high if r.high is not None else r.close),
                        low=float(r.low if r.low is not None else r.close),
                        close=float(r.close),
                        pre_close=float(r.pre_close) if r.pre_close is not None else None,
                    )
                )
                if r.amount is not None and math.isfinite(float(r.amount)) and float(r.amount) > 0:
                    amt_sum_qian[day] += float(r.amount)
                if day == compact and (r.volume is None or float(r.volume) > 0):
                    pct = float(r.change_pct) if r.change_pct is not None else (
                        (float(r.close) / float(r.pre_close) - 1.0) * 100.0
                        if r.pre_close and float(r.pre_close) > 0
                        else 0.0
                    )
                    if pct > 0:
                        up_count += 1
                    elif pct < 0:
                        down_count += 1
        if not daily_amounts_yi:
            daily_amounts_yi = {d: val / 100_000.0 for d, val in amt_sum_qian.items() if val > 0}

    total_amount_yi = daily_amounts_yi.get(compact)
    amount_pct_120: float | None = None
    if total_amount_yi is not None and len(daily_amounts_yi) >= 5:
        series_vals = list(daily_amounts_yi.values())
        amount_pct_120 = sum(1 for v in series_vals if v <= total_amount_yi) / len(series_vals)

    ma20_above, ma20_total = 0, 0
    ma60_above, ma60_total = 0, 0
    adj_by_sym: dict[str, dict[str, float]] = {}
    for sym, sym_bars in bars_by_sym.items():
        if not sym_bars or sym_bars[-1].trade_date != compact:
            continue
        adj = adjusted_bars(sym_bars)
        adj_by_sym[sym] = {b.trade_date: b.close for b in adj}
        closes = [b.close for b in adj]
        last_close = closes[-1]
        if len(closes) >= 20:
            ma20_total += 1
            if last_close >= sum(closes[-20:]) / 20.0:
                ma20_above += 1
        if len(closes) >= 60:
            ma60_total += 1
            if last_close >= sum(closes[-60:]) / 60.0:
                ma60_above += 1

    breadth_ma20 = (ma20_above / ma20_total) if ma20_total >= 10 else None
    breadth_ma60 = (ma60_above / ma60_total) if ma60_total >= 10 else None
    up_down_ratio = (up_count / max(down_count, 1)) if (up_count + down_count) > 0 else None

    # 3. 宽基指数趋势（优先 index_daily_bars；当网关未开通指数日线时，自动用市值前300与800-1800构建大盘/中小盘真实等权净值曲线）
    hs300_closes = [c for _, c in load_index_closes(db, "000300.SH", as_of_date=compact, limit=250)]
    zz1000_closes = [c for _, c in load_index_closes(db, "000852.SH", as_of_date=compact, limit=250)]
    if (not hs300_closes or not zz1000_closes) and len(adj_by_sym) >= 50:
        mv_rows = db.execute(
            select(StockDailyBasic.symbol, StockDailyBasic.total_mv)
            .where(StockDailyBasic.trade_date == compact, StockDailyBasic.total_mv.is_not(None))
            .order_by(StockDailyBasic.total_mv.desc())
        ).all()
        ranked_syms = [str(s) for s, _ in mv_rows if str(s) in adj_by_sym]
        if len(ranked_syms) >= 30:
            large_basket = ranked_syms[: min(300, max(10, len(ranked_syms) // 5))]
            mid_start = min(800, len(ranked_syms) // 3)
            small_basket = ranked_syms[mid_start : min(mid_start + 1000, len(ranked_syms))]

            def _synthetic_curve(basket: list[str]) -> list[float]:
                if not basket:
                    return []
                nav = 1000.0
                curve: list[float] = []
                prev_d: str | None = None
                for d in breadth_days:
                    if prev_d is None:
                        curve.append(round(nav, 2))
                        prev_d = d
                        continue
                    rets = []
                    for s in basket:
                        p0 = adj_by_sym[s].get(prev_d)
                        p1 = adj_by_sym[s].get(d)
                        if p0 and p1 and p0 > 0:
                            rets.append(p1 / p0 - 1.0)
                    if rets:
                        nav *= 1.0 + sum(rets) / len(rets)
                        curve.append(round(nav, 2))
                        prev_d = d
                return curve

            if not hs300_closes:
                hs300_closes = _synthetic_curve(large_basket)
            if not zz1000_closes:
                zz1000_closes = _synthetic_curve(small_basket)

    hs300_info = classify_index_trend(hs300_closes)
    zz1000_info = classify_index_trend(zz1000_closes)

    payload = evaluate_regime_metrics(
        trade_date=compact,
        breadth_ma20=breadth_ma20,
        breadth_ma60=breadth_ma60,
        up_down_ratio=up_down_ratio,
        total_amount_yi=total_amount_yi,
        amount_pct_120=amount_pct_120,
        hs300_info=hs300_info,
        zz1000_info=zz1000_info,
    )

    metrics_extra = {
        "ma20_sample": ma20_total,
        "ma60_sample": ma60_total,
        "up_count": up_count,
        "down_count": down_count,
        "hs300_ret20": hs300_info.get("ret20"),
        "zz1000_ret20": zz1000_info.get("ret20"),
    }
    record = db.get(MarketRegimeDaily, compact)
    if record is None:
        record = MarketRegimeDaily(trade_date=compact)
        db.add(record)

    for col in (
        "breadth_ma20",
        "breadth_ma60",
        "up_down_ratio",
        "total_amount_yi",
        "amount_pct_120",
        "hs300_close",
        "hs300_ma60",
        "hs300_ma200",
        "hs300_trend",
        "zz1000_close",
        "zz1000_ma60",
        "zz1000_ma200",
        "zz1000_trend",
        "regime_score",
        "stance",
        "max_position_ratio",
        "style_quadrant",
        "summary",
    ):
        setattr(record, col, payload[col])
    record.metrics_json = json.dumps(metrics_extra, ensure_ascii=False)
    payload["metrics"] = metrics_extra
    return payload


def regime_row_to_dict(row: MarketRegimeDaily) -> dict[str, Any]:
    try:
        metrics = json.loads(row.metrics_json) if row.metrics_json else {}
    except ValueError:
        metrics = {}
    return {
        "trade_date": row.trade_date,
        "breadth_ma20": row.breadth_ma20,
        "breadth_ma60": row.breadth_ma60,
        "up_down_ratio": row.up_down_ratio,
        "total_amount_yi": row.total_amount_yi,
        "amount_pct_120": row.amount_pct_120,
        "hs300_close": row.hs300_close,
        "hs300_ma60": row.hs300_ma60,
        "hs300_ma200": row.hs300_ma200,
        "hs300_trend": row.hs300_trend,
        "zz1000_close": row.zz1000_close,
        "zz1000_ma60": row.zz1000_ma60,
        "zz1000_ma200": row.zz1000_ma200,
        "zz1000_trend": row.zz1000_trend,
        "regime_score": row.regime_score,
        "stance": row.stance,
        "stance_label": STANCE_LABELS.get(row.stance, row.stance),
        "max_position_ratio": row.max_position_ratio,
        "style_quadrant": row.style_quadrant,
        "style_label": STYLE_LABELS.get(row.style_quadrant, row.style_quadrant),
        "summary": row.summary,
        "metrics": metrics,
    }


def get_latest_market_regime(*, trade_date: str | None = None, compute_if_missing: bool = True) -> dict[str, Any]:
    """获取最新或指定交易日的 L1 市场环境温度计；若未预计算且本地有日线，可按需即时补算。"""
    target = _compact_date(trade_date) if trade_date else ""
    with SessionLocal() as db:
        if target:
            row = db.get(MarketRegimeDaily, target)
            if row is not None:
                return regime_row_to_dict(row)
        else:
            row = db.scalar(select(MarketRegimeDaily).order_by(MarketRegimeDaily.trade_date.desc()).limit(1))
            latest_bar_day = db.scalar(
                select(func.max(StockDailyBasic.trade_date)).where(
                    StockDailyBasic.trade_date <= date.today().strftime("%Y%m%d"),
                )
            )
            if not latest_bar_day:
                sample_sym = db.scalar(select(Instrument.symbol).where(Instrument.type == "stock").limit(1))
                if sample_sym:
                    latest_bar_day = db.scalar(
                        select(func.max(DailyBar.trade_date)).where(
                            DailyBar.symbol == sample_sym,
                            DailyBar.trade_date <= date.today().strftime("%Y%m%d"),
                        )
                    )
            if row is not None and (not latest_bar_day or row.trade_date >= str(latest_bar_day)):
                return regime_row_to_dict(row)
            target = str(latest_bar_day or "")

        if compute_if_missing and target:
            computed = compute_and_store_market_regime(db, target)
            db.commit()
            return computed
        if row is not None:
            return regime_row_to_dict(row)

    return evaluate_regime_metrics(
        trade_date=target or date.today().strftime("%Y%m%d"),
        breadth_ma20=None,
        breadth_ma60=None,
        up_down_ratio=None,
        total_amount_yi=None,
        amount_pct_120=None,
        hs300_info=classify_index_trend([]),
        zz1000_info=classify_index_trend([]),
    )
