"""技术信号：用固定规则算每只股票的方向。规则写死在代码里，数字人只解释，不能改结论。

现状：这条 MACD 规则没通过全市场验证（docs/technical-signal-plan.md 第 4 节），没有接进任何流程。

规则（RULE_VERSION）：MACD(12, 26, 9) 的柱子大于 0 是红，代表持有；否则是绿，代表不持有。
收盘后算信号，第二天开盘执行。回测口径和结果见 docs/technical-signal-plan.md。

这里只有纯计算，不碰数据库，同样的 K 线永远得到同样的结果；存取在 tech_signal_service。

复权用 close/pre_close 串起来，不用 stock_adj_factors：因子表只有 2026-07-20 以后的数据，
而 MACD 要足够长的历史才稳定。pre_close 是 TuShare 给的除权价，两种算法在除权日只差
0.004%（澜起科技 2026-07-29、2026-10-23 核对过）。
"""
from __future__ import annotations

from dataclasses import dataclass

RULE_VERSION = "macd-12-26-9-v1"
FAST, SLOW, SIGNAL = 12, 26, 9
MIN_BARS = 60  # 短于这个长度 MACD 还没稳定，不给方向

LONG = "long"  # 红：持有
FLAT = "flat"  # 绿：不持有
UNKNOWN = "unknown"  # K 线不够，不下结论

_ALPHA_FAST = 2 / (FAST + 1)
_ALPHA_SLOW = 2 / (SLOW + 1)
_ALPHA_SIGNAL = 2 / (SIGNAL + 1)
_WILDER = 1 / 14
_STEP_RANGE = (0.1, 10.0)  # 单日除权比例超出这个范围多半是脏数据，不当除权处理


@dataclass(frozen=True)
class Bar:
    trade_date: str
    high: float
    low: float
    close: float
    pre_close: float | None = None


@dataclass(frozen=True)
class TechSignal:
    trade_date: str
    direction: str
    close: float
    bars_used: int
    flipped: bool = False  # 今天刚翻转
    flip_date: str | None = None  # 当前方向从哪天开始；K 线范围内没翻转过时为空
    days_in_state: int = 0
    flip_price: float | None = None  # 下一个交易日收盘价高于它是红，否则是绿
    dif: float | None = None
    dea: float | None = None
    macd_hist: float | None = None
    ma5: float | None = None
    ma10: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    rsi14: float | None = None
    atr14: float | None = None


def adjusted_bars(bars: list[Bar]) -> list[Bar]:
    """前复权：最后一根保持真实价格，之前的按除权比例缩放，分红送转不会造成假信号。"""
    factor = 1.0
    adjusted: list[Bar] = []
    for index in range(len(bars) - 1, -1, -1):
        bar = bars[index]
        adjusted.append(
            Bar(
                trade_date=bar.trade_date,
                high=bar.high * factor,
                low=bar.low * factor,
                close=bar.close * factor,
            )
        )
        previous_close = bars[index - 1].close if index else None
        if previous_close and bar.pre_close:
            step = bar.pre_close / previous_close
            if _STEP_RANGE[0] <= step <= _STEP_RANGE[1]:
                factor *= step
    adjusted.reverse()
    return adjusted


def _ema_series(values: list[float], alpha: float) -> list[float]:
    series: list[float] = []
    for value in values:
        series.append(value if not series else alpha * value + (1 - alpha) * series[-1])
    return series


def _wilder(values: list[float]) -> float:
    average = values[0]
    for value in values[1:]:
        average = _WILDER * value + (1 - _WILDER) * average
    return average


def _rsi(closes: list[float]) -> float | None:
    if len(closes) < 2:
        return None
    deltas = [after - before for before, after in zip(closes, closes[1:])]
    gain = _wilder([max(delta, 0.0) for delta in deltas])
    loss = _wilder([max(-delta, 0.0) for delta in deltas])
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - 100 / (1 + gain / loss)


def _atr(bars: list[Bar]) -> float:
    ranges = [bars[0].high - bars[0].low]
    for before, bar in zip(bars, bars[1:]):
        ranges.append(max(bar.high - bar.low, abs(bar.high - before.close), abs(bar.low - before.close)))
    return _wilder(ranges)


def _ma(closes: list[float], window: int) -> float | None:
    return sum(closes[-window:]) / window if len(closes) >= window else None


def _flip_price(ema_fast: float, ema_slow: float, dea: float) -> float | None:
    """柱子明天等于 0 时的收盘价。

    明天的 DIF 是收盘价的一次函数，柱子为 0 等价于明天的 DIF 等于今天的 DEA，可以直接解出来。
    收盘价越高柱子越大，所以高于这个价是红，否则是绿。
    """
    price = (dea - (1 - _ALPHA_FAST) * ema_fast + (1 - _ALPHA_SLOW) * ema_slow) / (_ALPHA_FAST - _ALPHA_SLOW)
    return price if price > 0 else None


def macd_hist_series(closes: list[float]) -> list[float]:
    """逐日 MACD 柱子（2 × (DIF − DEA)），和常见行情软件同一算法：首日收盘价作初值。"""
    fast = _ema_series(closes, _ALPHA_FAST)
    slow = _ema_series(closes, _ALPHA_SLOW)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = _ema_series(dif, _ALPHA_SIGNAL)
    return [2 * (a - b) for a, b in zip(dif, dea)]


def _direction(hist: float) -> str:
    return LONG if hist > 0 else FLAT


def compute_signal(bars: list[Bar]) -> TechSignal:
    """按日期升序的 K 线算最后一根的信号。只用传进来的 K 线，没有未来数据。"""
    if not bars:
        raise ValueError("没有 K 线，算不了信号")
    last = bars[-1]
    if len(bars) < MIN_BARS:
        return TechSignal(trade_date=last.trade_date, direction=UNKNOWN, close=last.close, bars_used=len(bars))

    adjusted = adjusted_bars(bars)
    closes = [bar.close for bar in adjusted]
    fast = _ema_series(closes, _ALPHA_FAST)
    slow = _ema_series(closes, _ALPHA_SLOW)
    dif = [a - b for a, b in zip(fast, slow)]
    dea = _ema_series(dif, _ALPHA_SIGNAL)
    hist = [2 * (a - b) for a, b in zip(dif, dea)]

    direction = _direction(hist[-1])
    days = 1
    while days < len(hist) and _direction(hist[-days - 1]) == direction:
        days += 1
    return TechSignal(
        trade_date=last.trade_date,
        direction=direction,
        close=last.close,
        bars_used=len(bars),
        flipped=days == 1,
        flip_date=bars[-days].trade_date if days < len(hist) else None,
        days_in_state=days,
        flip_price=_flip_price(fast[-1], slow[-1], dea[-1]),
        dif=dif[-1],
        dea=dea[-1],
        macd_hist=hist[-1],
        ma5=_ma(closes, 5),
        ma10=_ma(closes, 10),
        ma20=_ma(closes, 20),
        ma60=_ma(closes, 60),
        rsi14=_rsi(closes),
        atr14=_atr(adjusted),
    )
