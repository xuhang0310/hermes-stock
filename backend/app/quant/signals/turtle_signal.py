"""海龟趋势跟踪信号引擎。

最经典的趋势跟踪策略之一，纪律化风险管理的范本。规则（本实现按信号引擎
契约做了适配，全部用收盘价判断，成交由回测引擎后移一根 K 线在次根开盘执行，
无未来函数）：

- 入场：收盘价突破前 ``entry_period`` 日最高价（唐奇安通道上轨）→ 建 1 单位
- 加仓：趋势延续，每上涨 ``add_atr_mult`` × N 加 1 单位，上限 ``max_units``
- 止损：收盘价跌破「均价入场 - ``stop_atr_mult`` × N」→ 全部离场
- 离场：收盘价跌破前 ``exit_period`` 日最低价（唐奇安通道下轨）→ 全部离场

N = ATR（平均真实波幅，衡量日常波动剧烈程度），本引擎自行计算，
无共享工具依赖。仓位以资金权重表达（每单位 ``unit_weight``），
海龟原始的「按账户百分比/美元波幅定单位」留给组合层实现。

适合单边趋势行情；震荡市会被反复止损。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.app.quant.signal_engine import SignalEngine


class TurtleSignalEngine(SignalEngine):
    """海龟交易法则：突破入场 + ATR 加仓 + ATR 止损 + 通道离场。"""

    def __init__(
        self,
        entry_period: int = 20,
        exit_period: int = 10,
        atr_period: int = 20,
        max_units: int = 4,
        unit_weight: float = 0.25,
        stop_atr_mult: float = 2.0,
        add_atr_mult: float = 0.5,
    ):
        """初始化海龟参数。

        Args:
            entry_period: 入场突破窗口（前 N 日高点，默认 20）。
            exit_period: 离场通道窗口（前 N 日低点，默认 10）。
            atr_period: ATR 计算窗口（默认 20）。
            max_units: 单标的最大加仓单位数（默认 4）。
            unit_weight: 每单位占用资金权重（默认 0.25，4 单位满仓）。
            stop_atr_mult: 止损距离（N 的倍数，默认 2.0）。
            add_atr_mult: 加仓间距（N 的倍数，默认 0.5）。
        """
        if entry_period < 2:
            raise ValueError(f"entry_period 至少为 2，当前: {entry_period}")
        if exit_period < 2:
            raise ValueError(f"exit_period 至少为 2，当前: {exit_period}")
        if atr_period < 2:
            raise ValueError(f"atr_period 至少为 2，当前: {atr_period}")
        if max_units < 1:
            raise ValueError(f"max_units 至少为 1，当前: {max_units}")
        if not 0 < unit_weight <= 1:
            raise ValueError(f"unit_weight 必须在 (0, 1] 内，当前: {unit_weight}")
        if max_units * unit_weight > 1.0 + 1e-9:
            raise ValueError(
                f"max_units × unit_weight 不能超过 1（当前 {max_units} × {unit_weight}）"
            )
        if stop_atr_mult <= 0:
            raise ValueError(f"stop_atr_mult 必须为正数，当前: {stop_atr_mult}")
        if add_atr_mult <= 0:
            raise ValueError(f"add_atr_mult 必须为正数，当前: {add_atr_mult}")
        self.entry_period = int(entry_period)
        self.exit_period = int(exit_period)
        self.atr_period = int(atr_period)
        self.max_units = int(max_units)
        self.unit_weight = float(unit_weight)
        self.stop_atr_mult = float(stop_atr_mult)
        self.add_atr_mult = float(add_atr_mult)

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        signals = {}
        # warmup 必须覆盖三个窗口：突破高点、ATR、离场低点。
        # 若 exit_period 大于另两者而 warmup 未纳入它，循环早期 low[t-exit_period:t]
        # 会得到空切片，np.nanmin 抛 ValueError（自定义大窗口参数必崩）。
        warmup = max(self.entry_period, self.atr_period, self.exit_period) + 1
        for code, df in data_map.items():
            if (
                not {"open", "high", "low", "close"}.issubset(df.columns)
                or len(df) < warmup
            ):
                signals[code] = pd.Series(0.0, index=df.index)
                continue

            close = df["close"].to_numpy(dtype=float)
            high = df["high"].to_numpy(dtype=float)
            low = df["low"].to_numpy(dtype=float)
            n = len(close)

            # 真实波幅 TR 与 ATR（简单移动平均，与其余示例引擎的 rolling 口径一致）
            prev_close = np.empty(n)
            prev_close[0] = close[0]
            prev_close[1:] = close[:-1]
            tr = np.maximum(
                high - low,
                np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)),
            )
            tr_series = pd.Series(tr)
            atr = tr_series.rolling(window=self.atr_period).mean().to_numpy()

            signal = np.zeros(n)
            units = 0
            avg_entry = 0.0
            last_add = 0.0

            for t in range(warmup, n):
                n_atr = atr[t]
                if not np.isfinite(n_atr) or n_atr <= 0:
                    signal[t] = 0.0
                    continue

                # 前 N 日高/低点（不含当日）——构成唐奇安通道
                breakout = float(np.nanmax(high[t - self.entry_period : t]))
                exit_low = float(np.nanmin(low[t - self.exit_period : t]))
                price = close[t]

                if units > 0:
                    if price <= avg_entry - self.stop_atr_mult * n_atr:
                        # 触及 2N 止损：纪律性全部离场
                        units = 0
                    elif price < exit_low:
                        # 跌出 10 日低点通道：趋势结束离场
                        units = 0
                    elif units < self.max_units and price >= last_add + self.add_atr_mult * n_atr:
                        # 顺势加仓：更新均价与加仓基准
                        avg_entry = (avg_entry * units + price) / (units + 1)
                        units += 1
                        last_add = price
                elif price > breakout:
                    # 突破 20 日高点：建立首仓
                    units = 1
                    avg_entry = price
                    last_add = price

                signal[t] = units * self.unit_weight

            signals[code] = pd.Series(signal, index=df.index)
        return signals
