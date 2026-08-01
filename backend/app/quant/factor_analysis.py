"""因子分析工具 - 评估因子有效性

核心功能：
  1. IC 分析（信息系数）：因子值与未来收益的相关性
  2. 分组收益：按因子值分5组，看单调性
  3. 因子收益率：因子多空组合的收益
  4. 因子可视化：生成分析报告

使用方法：
  from backend.app.quant.factor_analysis import analyze_factor

  result = analyze_factor("alpha101_001", ["000001.SZ", "600519.SH"], "20220101", "20240101")
  print(result["ic_mean"])  # 平均IC
  print(result["group_returns"])  # 分组收益

注意：本模块复用 factor_analysis_core.py 的核心计算函数，
     避免重复实现 IC 和分组收益逻辑。
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import numpy as np
import pandas as pd

from backend.app.quant.factors.factor_analysis_core import (
    compute_group_equity,
    compute_ic_series,
)

logger = logging.getLogger(__name__)


def analyze_factor(
    factor_id: str,
    symbols: list[str],
    start_date: str,
    end_date: str,
    groups: int = 5,
    forward_days: int = 1,
) -> dict[str, Any]:
    """分析单个因子的有效性

    Args:
        factor_id: 因子ID，如 "alpha101_001"
        symbols: 股票列表
        start_date: 开始日期 YYYYMMDD
        end_date: 结束日期 YYYYMMDD
        groups: 分组数量（默认5组）
        forward_days: 预测未来几天收益（默认1天）

    Returns:
        分析结果字典：
        - ic_mean: 平均IC（信息系数）
        - ic_std: IC标准差
        - icir: IC信息比率 (IC_mean / IC_std)
        - positive_ic_ratio: IC为正的比例
        - group_returns: 每组平均收益
        - factor_return: 因子多空收益（第1组 - 最后1组）
        - turnover: 因子换手率
    """
    from backend.app.quant.backtest_runner import _load_stock_data
    from backend.app.quant.factors.registry import Registry

    # Step 1: 加载数据
    logger.info("加载数据: %d 只股票, %s - %s", len(symbols), start_date, end_date)
    data_map = _load_stock_data(symbols, start_date, end_date)

    if not data_map:
        raise ValueError("未能加载任何股票数据")

    # Step 2: 构造宽表
    panel = _data_map_to_panel(data_map)

    # Step 3: 计算因子值
    logger.info("计算因子: %s", factor_id)
    registry = Registry()
    factor_values = registry.compute(factor_id, panel)

    # Step 4: 计算未来收益
    close = panel["close"]
    forward_ret = close.shift(-forward_days) / close - 1

    # Step 5: IC 分析（使用现有核心函数）
    ic_series = compute_ic_series(factor_values, forward_ret)
    ic_mean = ic_series.mean() if len(ic_series) > 0 else 0.0
    ic_std = ic_series.std() if len(ic_series) > 0 else 0.0
    icir = ic_mean / ic_std if ic_std > 1e-10 else 0.0
    positive_ratio = (ic_series > 0).mean() if len(ic_series) > 0 else 0.0

    # Step 6: 分组收益分析（使用现有核心函数）
    equity_df = compute_group_equity(factor_values, forward_ret, groups)
    group_returns = _extract_group_returns(equity_df)

    # Step 7: 因子多空收益
    factor_return = group_returns.iloc[0] - group_returns.iloc[-1] if len(group_returns) >= 2 else 0.0

    # Step 8: 换手率分析
    turnover = _calc_factor_turnover(factor_values, groups)

    return {
        "factor_id": factor_id,
        "symbols": symbols,
        "period": {"start": start_date, "end": end_date},
        "ic_mean": float(ic_mean),
        "ic_std": float(ic_std),
        "icir": float(icir),
        "positive_ic_ratio": float(positive_ratio),
        "group_returns": group_returns.to_dict(),
        "factor_return": float(factor_return),
        "turnover": float(turnover),
        "interpretation": _interpret_results(ic_mean, icir, factor_return),
    }


def _data_map_to_panel(data_map: dict[str, pd.DataFrame]) -> dict[str, pd.DataFrame]:
    """将长表数据转换为宽表"""
    codes = list(data_map.keys())
    all_dates = sorted(set().union(*(df.index for df in data_map.values())))
    date_index = pd.DatetimeIndex(all_dates)

    panel: dict[str, pd.DataFrame] = {}
    for col in ("open", "high", "low", "close", "volume"):
        frames: dict[str, pd.Series] = {}
        for code in codes:
            df = data_map[code]
            if col in df.columns:
                frames[code] = df[col]
        if frames:
            panel[col] = pd.DataFrame(frames).reindex(date_index)

    return panel


def _extract_group_returns(equity_df: pd.DataFrame) -> pd.Series:
    """从分组权益曲线提取每组平均收益

    Args:
        equity_df: 分组权益曲线 (index=date, columns=Group_1...Group_N)

    Returns:
        每组平均收益（index=组号，0=最高组，N-1=最低组）
    """
    if equity_df.empty:
        return pd.Series(dtype=float)

    # 计算每组最终收益（最后一个值 / 1 - 1）
    final_equity = equity_df.iloc[-1] if len(equity_df) > 0 else pd.Series(dtype=float)
    total_returns = final_equity - 1

    # 转换为Series，组号从0开始
    n_groups = len(total_returns)
    return pd.Series(total_returns.values, index=range(n_groups))


def _calc_factor_turnover(
    factor_values: pd.DataFrame,
    groups: int,
) -> float:
    """计算因子换手率（最高组的换手率）

    换手率 = 今天最高组中，昨天不在最高组的股票比例
    """
    turnover_list = []

    prev_top_group = None

    for date in factor_values.index:
        factor_row = factor_values.loc[date].dropna()
        if len(factor_row) < groups:
            continue

        # 排序并取最高组
        sorted_codes = factor_row.sort_values(ascending=False).index
        group_size = len(sorted_codes) // groups
        top_group = set(sorted_codes[:group_size])

        if prev_top_group is not None:
            # 计算换手率
            new_stocks = top_group - prev_top_group
            turnover = len(new_stocks) / len(top_group)
            turnover_list.append(turnover)

        prev_top_group = top_group

    return np.mean(turnover_list) if turnover_list else 0.0


def _interpret_results(ic_mean: float, icir: float, factor_return: float) -> str:
    """解释分析结果"""
    # IC 解释
    if abs(ic_mean) < 0.02:
        ic_comment = "IC值很低（<0.02），因子预测能力弱"
    elif abs(ic_mean) < 0.05:
        ic_comment = "IC值中等（0.02-0.05），因子有一定预测能力"
    else:
        ic_comment = "IC值较高（>0.05），因子预测能力强"

    # ICIR 解释
    if abs(icir) < 0.5:
        icir_comment = "ICIR很低（<0.5），因子不稳定"
    elif abs(icir) < 1.0:
        icir_comment = "ICIR中等（0.5-1.0），因子较稳定"
    else:
        icir_comment = "ICIR较高（>1.0），因子很稳定"

    # 因子收益解释
    if abs(factor_return) < 0.001:
        ret_comment = "因子多空收益接近0，无法获利"
    elif abs(factor_return) < 0.005:
        ret_comment = "因子多空收益较低，需要结合其他因子"
    else:
        ret_comment = "因子多空收益较好，可以单独使用"

    return f"{ic_comment}；{icir_comment}；{ret_comment}"


def compare_factors(
    factor_ids: list[str],
    symbols: list[str],
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """比较多个因子的有效性

    Args:
        factor_ids: 因子ID列表
        symbols: 股票列表
        start_date: 开始日期
        end_date: 结束日期

    Returns:
        对比表格（DataFrame）
    """
    results = []

    for factor_id in factor_ids:
        try:
            result = analyze_factor(factor_id, symbols, start_date, end_date)
            results.append({
                "因子ID": factor_id,
                "IC均值": f"{result['ic_mean']:.4f}",
                "ICIR": f"{result['icir']:.2f}",
                "IC正比例": f"{result['positive_ic_ratio']:.1%}",
                "多空收益": f"{result['factor_return']:.2%}",
                "换手率": f"{result['turnover']:.1%}",
                "评价": result["interpretation"],
            })
        except Exception as e:
            logger.error("分析因子 %s 失败: %s", factor_id, e)
            results.append({
                "因子ID": factor_id,
                "IC均值": "N/A",
                "ICIR": "N/A",
                "IC正比例": "N/A",
                "多空收益": "N/A",
                "换手率": "N/A",
                "评价": f"分析失败: {str(e)}",
            })

    return pd.DataFrame(results)


def recommend_factors(
    theme: Optional[str] = None,
    top_n: int = 10,
) -> list[str]:
    """推荐因子（按主题筛选）

    Args:
        theme: 主题过滤（如 "momentum", "value", "volatility"）
        top_n: 返回前N个因子

    Returns:
        推荐的因子ID列表
    """
    from backend.app.quant.factors.registry import Registry

    registry = Registry()

    # 列出所有因子
    all_factors = registry.list(theme=theme)

    # 简单启发式：优先选择经典因子
    priority_keywords = ["alpha101", "gtja191", "qlib158"]
    prioritized = []

    for keyword in priority_keywords:
        prioritized.extend([f for f in all_factors if keyword in f])

    # 去重并保持顺序
    seen = set()
    unique = []
    for f in prioritized:
        if f not in seen:
            seen.add(f)
            unique.append(f)

    return unique[:top_n]
