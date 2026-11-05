"""线上算模型 1 的排名：全市场最近的 K 线 → 特征 → 模型 → 当天的横截面排名。

和回测同一套特征和算法（research_features、research_models.predict_ranks 的做法）。两处和回测有差别：

- 上市满 120 根 K 线改成看上市日期：线上全市场 K 线只存了几个月，数不出 120 根。
- 120 日低点这个特征在 K 线存够之前（约 2026 年 12 月），用的是现有长度。

算不出来返回空，影子 B 当天就按不带排名的目标股数算。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

import numpy as np
import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.core.models import DailyBar, Instrument, MarketTradingCalendar, StockDailyBasic
from backend.app.quant.research_features import compute_all
from backend.app.quant.research_panel import A_SHARE, Panel
from backend.app.quant.tree_model import load_model, predict

MODEL_PATH = Path(__file__).resolve().parents[1] / "quant" / "rank_model.json"
WINDOW_DAYS = 130
_MIN_STOCKS_TODAY = 3000  # 当天全市场的 K 线或换手率少于这么多，说明数据没到齐
_MIN_LISTED_DAYS = 180  # 上市约满 120 个交易日
LIVE_MIN_BARS = 60
_SYMBOL_BATCH = 300
_BAR_FIELDS = ("open", "high", "low", "close", "pre_close", "vol", "amount")
_cache: dict[str, Any] = {}


def _model() -> dict[str, Any] | None:
    if "model" not in _cache:
        _cache["model"] = load_model(MODEL_PATH)
    return _cache["model"]


def excluded_symbols(instruments: Iterable[tuple[str, str | None, str | None]], trade_date: str) -> set[str]:
    """ST 和上市不满 120 个交易日的股票，不进股票池。"""
    limit = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=_MIN_LISTED_DAYS)).strftime("%Y%m%d")
    out = set()
    for symbol, name, list_date in instruments:
        listed = (list_date or "").replace("-", "")
        if "ST" in (name or "").upper() or (listed and listed > limit):
            out.add(symbol)
    return out


def panel_from_rows(days: list[str], codes: list[str], rows: Iterable[tuple], turnover: dict[str, float], excluded: set[str]) -> tuple[Panel, int]:
    """rows 每行是 (代码, 日期, 开, 高, 低, 收, 昨收, 成交量, 成交额)。返回面板和最后一天有 K 线的股票数。"""
    column = {code: j for j, code in enumerate(codes)}
    row_of = {day: i for i, day in enumerate(days)}
    shape = (len(days), len(codes))
    arrays = {name: np.full(shape, np.nan, dtype=np.float32) for name in _BAR_FIELDS}
    last = len(days) - 1
    today = 0
    for symbol, day, *values in rows:
        j, i = column.get(symbol), row_of.get(day)
        if j is None or i is None:
            continue
        today += i == last
        for name, value in zip(_BAR_FIELDS, values):
            if value is not None:
                arrays[name][i, j] = value
    data: dict[tuple[str, str], np.ndarray] = {("daily", name): array for name, array in arrays.items()}
    # 模型只看当天的换手率，前面的日子不用存
    turnover_array = np.full(shape, np.nan, dtype=np.float32)
    for symbol, value in turnover.items():
        j = column.get(symbol)
        if j is not None:
            turnover_array[last, j] = value
    data[("daily_basic", "turnover_rate_f")] = turnover_array
    st = np.zeros(shape, dtype=bool)
    for symbol in excluded:
        j = column.get(symbol)
        if j is not None:
            st[:, j] = True
    return Panel(days, codes, data, st=st, min_listed=LIVE_MIN_BARS), today


def ranks_from_panel(panel: Panel, model: dict[str, Any]) -> dict[str, float]:
    names = list(model["names"])
    features = compute_all(panel, names=names)
    last = len(panel.dates) - 1
    # 和训练时一样：特征先在当天所有有值的股票里排名，预测只算股票池
    columns = [pd.Series(np.asarray(features[name][last], dtype=np.float64)).rank(pct=True).to_numpy(dtype=np.float32) for name in names]
    x = np.column_stack(columns)
    picks = np.flatnonzero(panel.universe[last])
    if not len(picks):
        return {}
    scores = pd.Series(predict(model, x[picks])).rank(pct=True).to_numpy()
    return {panel.codes[j]: float(score) for j, score in zip(picks.tolist(), scores)}


def _bar_rows(db: Session, codes: list[str], first_day: str, last_day: str) -> Iterator[tuple]:
    """按股票分批读。这张表的索引以股票代码开头，按日期扫全市场是全表扫描，大结果集在这条链路上容易断。"""
    for start in range(0, len(codes), _SYMBOL_BATCH):
        yield from db.execute(
            select(DailyBar.symbol, DailyBar.trade_date, DailyBar.open, DailyBar.high, DailyBar.low, DailyBar.close, DailyBar.pre_close, DailyBar.volume, DailyBar.amount).where(
                DailyBar.symbol.in_(codes[start : start + _SYMBOL_BATCH]),
                DailyBar.trade_date >= first_day,
                DailyBar.trade_date <= last_day,
                DailyBar.source == "tushare",
                DailyBar.close.is_not(None),
            )
        ).all()


def latest_ranks(db: Session, trade_date: str) -> dict[str, float]:
    """当天每只股票的排名（0 到 1，越大越好）。算不出来返回空。"""
    model = _model()
    if model is None:
        return {}
    days = sorted(
        db.scalars(
            select(MarketTradingCalendar.cal_date)
            .where(MarketTradingCalendar.exchange == "SSE", MarketTradingCalendar.is_open.is_(True), MarketTradingCalendar.cal_date <= trade_date)
            .order_by(MarketTradingCalendar.cal_date.desc())
            .limit(WINDOW_DAYS)
        ).all()
    )
    if not days or days[-1] != trade_date:
        return {}
    # 先查换手率：这张表按日期查很快，当天没到就不用去扫 K 线
    turnover = dict(
        db.execute(
            select(StockDailyBasic.symbol, StockDailyBasic.turnover_rate_f).where(StockDailyBasic.trade_date == trade_date, StockDailyBasic.turnover_rate_f.is_not(None))
        ).all()
    )
    if len(turnover) < _MIN_STOCKS_TODAY:
        return {}
    instruments = db.execute(select(Instrument.symbol, Instrument.name, Instrument.list_date)).all()
    codes = sorted(symbol for symbol, _, _ in instruments if A_SHARE.fullmatch(symbol))
    panel, today = panel_from_rows(days, codes, _bar_rows(db, codes, days[0], trade_date), turnover, excluded_symbols(instruments, trade_date))
    if today < _MIN_STOCKS_TODAY:
        return {}
    return ranks_from_panel(panel, model)
