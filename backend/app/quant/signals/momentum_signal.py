"""横截面动量信号引擎。

学术上叫 cross-sectional momentum：每天把股票池内所有股票按过去
``lookback`` 天的涨幅排名，等权持有最强的 ``top_n`` 只，次日按新排名
重算（标准日频横截面动量；"月度持有"等变体后续批次再做）。

只依赖现有 OHLCV 数据（close 列），无新数据源。回测引擎会把信号整体
后移一根 K 线执行（次根开盘成交），故今日排名用今日收盘、明日开盘换仓，
无未来函数。
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from backend.app.quant.signal_engine import SignalEngine


class MomentumSignalEngine(SignalEngine):
    """横截面动量：持有过去一段时间涨得最好的 top_n 只股票。"""

    def __init__(self, lookback: int = 20, top_n: int = 5):
        """初始化动量参数。

        Args:
            lookback: 涨幅回看天数（排名窗口，默认 20）。
            top_n: 持有最强的前 N 只（默认 5，等权）。
        """
        if lookback < 2:
            raise ValueError(f"lookback 至少为 2，当前: {lookback}")
        if top_n < 1:
            raise ValueError(f"top_n 至少为 1，当前: {top_n}")
        self.lookback = int(lookback)
        self.top_n = int(top_n)

    def generate(self, data_map: dict[str, pd.DataFrame]) -> dict[str, pd.Series]:
        if not data_map:
            return {}

        codes = list(data_map.keys())
        close_series = {
            code: data_map[code]["close"]
            for code in codes
            if "close" in data_map[code].columns
        }
        if not close_series:
            return {code: pd.Series(0.0, index=data_map[code].index) for code in codes}

        # 长表 → 宽表（行=日期，列=股票）
        all_dates = sorted(set().union(*(series.index for series in close_series.values())))
        close_wide = pd.DataFrame(close_series).reindex(all_dates)

        # 过去 lookback 天涨幅；前 lookback 天为 NaN（数据不足 → 当日不持仓）
        returns = close_wide.pct_change(self.lookback)

        # 横截面排名：每天降序（涨幅最高者 rank=1），NaN 排到最后
        ranks = returns.rank(axis=1, method="first", ascending=False, na_option="bottom")
        valid = returns.notna()
        # 每天实际可选数量 = min(top_n, 当日有效股票数)，等权 1/选中数
        select_count = np.minimum(self.top_n, valid.sum(axis=1)).replace(0, np.nan)

        selected = ranks.le(select_count, axis=0) & valid
        weights = selected.astype(float).div(select_count, axis=0).fillna(0.0)

        # 宽表 → 每只股票自己的日期索引
        out: dict[str, pd.Series] = {}
        for code in codes:
            if code in weights.columns:
                out[code] = weights[code].reindex(data_map[code].index).fillna(0.0)
            else:
                out[code] = pd.Series(0.0, index=data_map[code].index)
        return out
