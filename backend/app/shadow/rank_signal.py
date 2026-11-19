"""线上算模型信号：全市场最近的 K 线 → 特征 → 模型 → 当天的排名和买入倍数。

和回测同一套特征和算法。两处和回测有差别：

- 上市满 120 根 K 线改成看上市日期：线上全市场 K 线只存了几个月，数不出 120 根。
- 120 日低点这个特征在 K 线存够之前（约 2026 年 12 月），用的是现有长度。

每天算一次，存进 stock_model_signals。影子账户重跑和出计划都直接读这张表，不用再扫全市场。
算不出来返回空，影子 B、C 当天推迟结算，出计划时买价不校验。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

import numpy as np
import pandas as pd
from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session

from backend.app.core.applog import get_logger
from backend.app.core.models import DailyBar, Instrument, MarketTradingCalendar, StockDailyBasic, StockModelSignal
from backend.app.quant.order_engine import BUY_GRID
from backend.app.quant.research_buy_model import EDGE_MODEL_PATH, FILL_MODEL_PATH, ONLINE_VARIANT, VARIANTS, choose
from backend.app.quant.research_features import compute_all
from backend.app.quant.research_panel import A_SHARE, Panel
from backend.app.quant.tree_model import load_model, predict, predict_probability

logger = get_logger("shadow_signal")

MODEL_PATH = Path(__file__).resolve().parents[1] / "quant" / "rank_model.json"
WINDOW_DAYS = 130
_MIN_STOCKS_TODAY = 3000  # 当天全市场的 K 线或换手率少于这么多，说明数据没到齐
_MIN_LISTED_DAYS = 180  # 上市约满 120 个交易日
LIVE_MIN_BARS = 60
_SYMBOL_BATCH = 300
_KEEP_DAYS = 30
_BAR_FIELDS = ("open", "high", "low", "close", "pre_close", "vol", "amount")
_cache: dict[str, Any] = {}


@dataclass(frozen=True)
class LiveSignals:
    ranks: dict[str, float]  # 0 到 1，越大越好
    buy_k: dict[str, float | None]  # 昨收下方几倍 ATR 挂买单；None 是模型说不挂。不在字典里的股票没有信号

    @property
    def complete(self) -> bool:
        return bool(self.ranks and self.buy_k)


EMPTY = LiveSignals({}, {})


def _model() -> dict[str, Any] | None:
    if "model" not in _cache:
        _cache["model"] = load_model(MODEL_PATH)
    return _cache["model"]


def _buy_models() -> tuple[dict[str, Any], dict[str, Any]] | None:
    if "buy" not in _cache:
        fill, edge = load_model(FILL_MODEL_PATH), load_model(EDGE_MODEL_PATH)
        _cache["buy"] = (fill, edge) if fill is not None and edge is not None else None
    return _cache["buy"]


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


def _rank_matrix(panel: Panel, features: dict[str, np.ndarray], names: list[str]) -> np.ndarray:
    """和训练时一样：特征先在当天所有有值的股票里排名。"""
    last = len(panel.dates) - 1
    columns = [pd.Series(np.asarray(features[name][last], dtype=np.float64)).rank(pct=True).to_numpy(dtype=np.float32) for name in names]
    return np.column_stack(columns)


def ranks_from_panel(panel: Panel, model: dict[str, Any], features: dict[str, np.ndarray] | None = None) -> dict[str, float]:
    names = list(model["names"])
    features = features if features is not None else compute_all(panel, names=names)
    picks = np.flatnonzero(panel.universe[len(panel.dates) - 1])
    if not len(picks):
        return {}
    scores = pd.Series(predict(model, _rank_matrix(panel, features, names)[picks])).rank(pct=True).to_numpy()
    return {panel.codes[j]: float(score) for j, score in zip(picks.tolist(), scores)}


def buy_from_panel(panel: Panel, fill_model: dict[str, Any], edge_model: dict[str, Any], features: dict[str, np.ndarray] | None = None) -> dict[str, float | None]:
    """每只股票的买入倍数：每个候选价估成交概率和成交后走势，取价值最大的，都没有优势就是 None。"""
    names = list(fill_model["names"])
    features = features if features is not None else compute_all(panel, names=names)
    picks = np.flatnonzero(panel.universe[len(panel.dates) - 1])
    if not len(picks):
        return {}
    x = _rank_matrix(panel, features, names)[picks]
    grid = np.array(BUY_GRID, dtype=np.float32)
    probability, edge = np.zeros((len(picks), len(grid))), np.zeros((len(picks), len(grid)))
    for column, k in enumerate(grid):
        rows = np.column_stack([x, np.full(len(picks), k, dtype=np.float32)])
        probability[:, column] = predict_probability(fill_model, rows)
        edge[:, column] = predict(edge_model, rows)
    chosen = choose(probability, edge, grid, VARIANTS[ONLINE_VARIANT].min_edge)
    return {panel.codes[j]: None if math.isnan(k) else float(k) for j, k in zip(picks.tolist(), chosen.tolist())}


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


def turnover_input_issue(db: Session, trade_date: str) -> str | None:
    count = db.scalar(
        select(func.count(StockDailyBasic.turnover_rate_f)).where(StockDailyBasic.trade_date == trade_date)
    )
    if count < _MIN_STOCKS_TODAY:
        return f"{trade_date} 自由流通换手率仅有 {count} 只，模型排名至少需要 {_MIN_STOCKS_TODAY} 只。"
    return None


def _live_panel(db: Session, trade_date: str) -> Panel | None:
    days = sorted(
        db.scalars(
            select(MarketTradingCalendar.cal_date)
            .where(MarketTradingCalendar.exchange == "SSE", MarketTradingCalendar.is_open.is_(True), MarketTradingCalendar.cal_date <= trade_date)
            .order_by(MarketTradingCalendar.cal_date.desc())
            .limit(WINDOW_DAYS)
        ).all()
    )
    if not days or days[-1] != trade_date:
        return None
    # 先查换手率：这张表按日期查很快，当天没到就不用去扫 K 线
    turnover = dict(
        db.execute(
            select(StockDailyBasic.symbol, StockDailyBasic.turnover_rate_f).where(StockDailyBasic.trade_date == trade_date, StockDailyBasic.turnover_rate_f.is_not(None))
        ).all()
    )
    if len(turnover) < _MIN_STOCKS_TODAY:
        return None
    instruments = db.execute(select(Instrument.symbol, Instrument.name, Instrument.list_date)).all()
    codes = sorted(symbol for symbol, _, _ in instruments if A_SHARE.fullmatch(symbol))
    panel, today = panel_from_rows(days, codes, _bar_rows(db, codes, days[0], trade_date), turnover, excluded_symbols(instruments, trade_date))
    return panel if today >= _MIN_STOCKS_TODAY else None


def latest_signals(db: Session, trade_date: str) -> LiveSignals:
    """算当天的信号。算不出来返回空。买入模型出问题只影响影子 C 和买价校验，排名照常，不能拖累影子 B。"""
    rank_model, buy_models = _model(), _buy_models()
    if rank_model is None:
        return EMPTY
    panel = _live_panel(db, trade_date)
    if panel is None:
        return EMPTY
    names = list(rank_model["names"]) + (list(buy_models[0]["names"]) if buy_models else [])
    features = compute_all(panel, names=list(dict.fromkeys(names)))
    ranks = ranks_from_panel(panel, rank_model, features)
    buy_k: dict[str, float | None] = {}
    if buy_models:
        try:
            buy_k = buy_from_panel(panel, *buy_models, features)
        except Exception as exc:  # noqa: BLE001 - 见上
            logger.warning("买入模型计算失败: %s", str(exc)[:200])
    return LiveSignals(ranks, buy_k)


def load_signals(db: Session, trade_date: str) -> LiveSignals | None:
    rows = db.execute(select(StockModelSignal.symbol, StockModelSignal.model_rank, StockModelSignal.buy_k).where(StockModelSignal.trade_date == trade_date)).all()
    if not rows:
        return None
    return LiveSignals({row.symbol: row.model_rank for row in rows}, {row.symbol: row.buy_k for row in rows})


def save_signals(db: Session, trade_date: str, signals: LiveSignals) -> None:
    cutoff = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=_KEEP_DAYS)).strftime("%Y%m%d")
    db.execute(delete(StockModelSignal).where(StockModelSignal.trade_date < cutoff))
    db.execute(
        insert(StockModelSignal),
        [{"trade_date": trade_date, "symbol": symbol, "model_rank": rank, "buy_k": signals.buy_k.get(symbol)} for symbol, rank in signals.ranks.items()],
    )
