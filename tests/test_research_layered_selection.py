"""离线分层回测校准引擎单元测试：验证熊市回撤期 L1 仓位刹车与破位飞刀否决有效压降组合回撤且无未来函数。"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta

import numpy as np

from backend.app.quant.research_layered_selection import (
    compare_baseline_vs_layered,
    panel_downtrend_knife_veto,
    panel_regime_caps,
)
from backend.app.quant.research_panel import Panel


def _dates(n: int, start: str = "20200102") -> list[str]:
    d = datetime.strptime(start, "%Y%m%d").date()
    out: list[str] = []
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return out


class ResearchLayeredSelectionTests(unittest.TestCase):
    def test_l1_regime_brake_and_knife_veto_reduce_bear_market_drawdown(self) -> None:
        days = _dates(260, "20200102")
        codes = [f"6000{i:02d}.SH" for i in range(20)]
        shape = (len(days), len(codes))

        close = np.zeros(shape, dtype=np.float32)
        for j in range(len(codes)):
            px = 20.0 + j * 0.5
            for i in range(len(days)):
                if i < 100:
                    # 牛市阶段：稳步上涨
                    px *= 1.003
                elif i < 190:
                    # 熊市大跌阶段：全市场跌破均线，其中第 0, 1 号股持续深跌飞刀
                    drop = 0.985 if j in (0, 1) else 0.994
                    px *= drop
                else:
                    # 企稳阶段
                    px *= 1.002
                close[i, j] = px

        pre_close = np.vstack([close[0:1], close[:-1]])
        open_px = close * 0.999
        high_px = close * 1.01
        low_px = close * 0.99
        vol = np.full(shape, 50_000.0, dtype=np.float32)
        amt = np.full(shape, 60_000.0, dtype=np.float32)  # 6000万 > 2000万门槛

        panel = Panel(
            days,
            codes,
            {
                ("daily", "open"): open_px,
                ("daily", "high"): high_px,
                ("daily", "low"): low_px,
                ("daily", "close"): close,
                ("daily", "pre_close"): pre_close,
                ("daily", "vol"): vol,
                ("daily", "amount"): amt,
            },
            min_listed=20,
        )

        caps = panel_regime_caps(panel)
        # 牛市后段（第 85 天）应为 0.90 进攻，熊市中段（第 150 天）应自动降到 0.30 防守！
        self.assertAlmostEqual(float(caps[85]), 0.90)
        self.assertAlmostEqual(float(caps[150]), 0.30)

        veto = panel_downtrend_knife_veto(panel)
        self.assertTrue(bool(veto[150, 0]))

        # 假设原始纯量价反转分把深跌股 0, 1 排在前面（典型纯反转模型在熊市单边暴跌时的回撤来源）
        scores = np.zeros(shape, dtype=np.float32)
        for j in range(len(codes)):
            scores[:, j] = float(20 - j)

        report = compare_baseline_vs_layered(
            panel,
            scores,
            top_n=5,
            start=days[65],
            end=days[-1],
        )
        base_dd = report["baseline_model_rank"]["drawdown"]
        layered_dd = report["layered_l1_veto"]["drawdown"]
        # L1 30% 防守控仓 + 破位飞刀否决显著缩小最大回撤（drawdown 是负数，越接近 0 越小）
        self.assertGreater(layered_dd, base_dd + 0.15)
        self.assertLess(report["layered_l1_veto"]["avg_position_ratio"], 0.85)


if __name__ == "__main__":
    unittest.main()
