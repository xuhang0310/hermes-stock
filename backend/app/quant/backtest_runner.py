"""Signal-based backtest runner.

This module provides the main entry point for running custom signal engine
backtests. It loads data, instantiates the engine, runs the backtest, and
returns formatted results.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

import pandas as pd

from backend.app.core.database import SessionLocal
from backend.app.core.models import BacktestRun
from backend.app.core.settings import settings
from backend.app.data.repository import StockRepository
from backend.app.quant.engines.china_a import ChinaAEngine
from backend.app.quant.signal_engine import SignalEngine
from backend.app.quant.signals.example_signals import (
    BollingerBandSignalEngine,
    MacdSignalEngine,
    MaCrossSignalEngine,
    RsiSignalEngine,
)

logger = logging.getLogger(__name__)


# Registry of available signal engines
SIGNAL_ENGINE_REGISTRY: Dict[str, type] = {
    "macd": MacdSignalEngine,
    "ma_cross": MaCrossSignalEngine,
    "bollinger": BollingerBandSignalEngine,
    "rsi": RsiSignalEngine,
}


def list_signal_engines() -> Dict[str, str]:
    """List available signal engines with descriptions."""
    return {
        "macd": "MACD 金叉死叉策略（快线 12，慢线 26，信号线 9）",
        "ma_cross": "均线交叉策略（短期 5 日，长期 20 日）",
        "bollinger": "布林带均值回归策略（周期 20，标准差倍数 2.0）",
        "rsi": "RSI 超买超卖策略（周期 14，超卖 30，超买 70）",
    }


def run_signal_backtest(
    strategy: str,
    symbols: list[str],
    start_date: str,
    end_date: str,
    strategy_params: Optional[Dict[str, Any]] = None,
    initial_cash: float = 1_000_000,
    commission_rate: float = 0.00025,
    slippage: float = 0.001,
) -> Dict[str, Any]:
    """Run a signal-based backtest.

    Args:
        strategy: Strategy name (one of: macd, ma_cross, bollinger, rsi).
        symbols: List of stock codes (e.g., ["000001.SZ", "600519.SH"]).
        start_date: Start date in YYYYMMDD format.
        end_date: End date in YYYYMMDD format.
        strategy_params: Optional dict of strategy-specific parameters.
        initial_cash: Initial capital (default 1,000,000).
        commission_rate: Commission rate (default 0.00025).
        slippage: Slippage rate (default 0.001).

    Returns:
        Dict with backtest results including metrics, trades, and equity curve.

    Raises:
        ValueError: 输入参数无效时抛出（中文错误消息）
    """
    # 输入验证
    if strategy not in SIGNAL_ENGINE_REGISTRY:
        available = ", ".join(SIGNAL_ENGINE_REGISTRY.keys())
        raise ValueError(f"未知策略: {strategy}，可用策略: {available}")

    if not symbols:
        raise ValueError("股票代码列表不能为空")

    if initial_cash < 10000:
        raise ValueError(f"初始资金({initial_cash})不能少于10000元")

    if commission_rate < 0 or commission_rate > 0.01:
        raise ValueError(f"佣金率({commission_rate})必须在0到0.01之间")

    if slippage < 0 or slippage > 0.01:
        raise ValueError(f"滑点率({slippage})必须在0到0.01之间")

    # Instantiate signal engine
    engine_class = SIGNAL_ENGINE_REGISTRY[strategy]
    params = strategy_params or {}
    try:
        signal_engine = engine_class(**params)
    except TypeError as e:
        raise ValueError(f"策略参数错误: {str(e)}")

    # Load data
    data_map = _load_stock_data(symbols, start_date, end_date)

    # Configure market engine
    config = {
        "initial_cash": initial_cash,
        "commission_rate": commission_rate,
        "slippage": slippage,
    }
    market_engine = ChinaAEngine(config)

    # Run backtest
    metrics = market_engine.run_backtest(config, data_map, signal_engine)

    # Format results
    # 计算回撤
    equity_values = [s.equity for s in market_engine.equity_snapshots]
    peak = 0
    drawdowns = []
    for eq in equity_values:
        peak = max(peak, eq)
        dd = (eq - peak) / peak if peak > 0 else 0
        drawdowns.append(dd)

    # 提取 K 线数据
    price_series = []
    first_symbol = symbols[0]
    if first_symbol in data_map:
        df = data_map[first_symbol]
        for idx, row in df.iterrows():
            price_series.append({
                "time": str(idx.date()) if hasattr(idx, "date") else str(idx),
                "open": float(row["open"]) if "open" in row else 0,
                "high": float(row["high"]) if "high" in row else 0,
                "low": float(row["low"]) if "low" in row else 0,
                "close": float(row["close"]) if "close" in row else 0,
                "volume": int(row["volume"]) if "volume" in row else 0,
                "macd": 0,
                "signal": 0,
                "histogram": 0,
            })

    # 提取交易标记
    trade_markers = []
    for t in market_engine.trades:
        entry_date = str(t.entry_time.date()) if hasattr(t.entry_time, "date") else str(t.entry_time)
        exit_date = str(t.exit_time.date()) if hasattr(t.exit_time, "date") else str(t.exit_time)
        trade_markers.append({
            "time": entry_date,
            "shape": "arrowUp",
            "text": f"买入 {t.symbol}",
        })
        trade_markers.append({
            "time": exit_date,
            "shape": "arrowDown",
            "text": f"卖出 {t.symbol}",
        })

    result = {
        "strategy": strategy,
        "symbols": symbols,
        "period": {"start": start_date, "end": end_date},
        "price_series": price_series,
        "trade_markers": trade_markers,
        "metrics": metrics,
        "trades": [
            {
                "symbol": t.symbol,
                "entry_date": str(t.entry_time.date()) if hasattr(t.entry_time, "date") else str(t.entry_time),
                "exit_date": str(t.exit_time.date()) if hasattr(t.exit_time, "date") else str(t.exit_time),
                "entry_price": round(t.entry_price, 2),
                "exit_price": round(t.exit_price, 2),
                "size": int(t.size),
                "pnl": round(t.pnl, 2),
                "pnl_pct": round(t.pnl_pct, 2),
                "holding_bars": t.holding_bars,
            }
            for t in market_engine.trades
        ],
        "equity_curve": [
            {
                "time": str(s.timestamp.date()) if hasattr(s.timestamp, "date") else str(s.timestamp),
                "equity": round(s.equity, 2),
                "drawdown": round(drawdowns[i], 4),
                "positions": s.positions,
            }
            for i, s in enumerate(market_engine.equity_snapshots)
        ],
    }

    # 保存结果到数据库，生成报告URL
    result = _save_backtest_result(result, strategy_code=f"signal_{strategy}")

    return result


def _save_backtest_result(
    result: Dict[str, Any],
    *,
    owner_user_id: int | None = None,
    strategy_code: str = "signal",
) -> Dict[str, Any]:
    """保存回测结果到数据库，生成 report_url 和 summary_markdown

    Args:
        result: 回测结果字典
        owner_user_id: 用户ID
        strategy_code: 策略代码

    Returns:
        添加了 run_id 和 report_url 的结果
    """
    run_id = uuid.uuid4().hex
    result["run_id"] = run_id
    result["report_url"] = f"/backtests/{run_id}"

    # 生成摘要
    metrics = result.get("metrics", {})
    total_return = metrics.get("total_return", 0)
    sharpe = metrics.get("sharpe", 0)
    max_dd = metrics.get("max_drawdown", 0)
    n_trades = metrics.get("n_trades", 0)
    win_rate = metrics.get("win_rate", 0)

    summary = (
        f"## {result.get('strategy', 'Signal')} 策略回测\n\n"
        f"- 年化收益：**{total_return * 100:.2f}%**\n"
        f"- 夏普比率：{sharpe:.2f}\n"
        f"- 最大回撤：{max_dd * 100:.2f}%\n"
        f"- 交易：{n_trades} 笔 ｜ 胜率：{win_rate * 100:.1f}%\n\n"
        f"[查看完整回测报告](/backtests/{run_id})"
    )
    result["summary_markdown"] = summary

    # 保存到数据库
    session = SessionLocal()
    try:
        session.add(
            BacktestRun(
                run_id=run_id,
                owner_user_id=owner_user_id,
                status="completed",
                symbol=result.get("symbols", [""])[0] if result.get("symbols") else "",
                strategy_code=strategy_code,
                timeframe="1D",
                start_date=result["period"]["start"],
                end_date=result["period"]["end"],
                config_json=json.dumps({"strategy": result.get("strategy")}, ensure_ascii=False),
                metrics_json=json.dumps(metrics, ensure_ascii=False),
                result_json=json.dumps(result, ensure_ascii=False),
                summary_markdown=summary,
                finished_at=datetime.now(),
            )
        )
        session.commit()
        logger.info("回测结果已保存: run_id=%s", run_id)
    except Exception:
        session.rollback()
        logger.exception("保存回测结果失败")
        raise
    finally:
        session.close()

    return result


def _load_stock_data(
    symbols: list[str],
    start_date: str,
    end_date: str,
) -> Dict[str, pd.DataFrame]:
    """Load stock data from TuShare via HTTP (consistent with stock_tools.py).

    Args:
        symbols: List of stock codes.
        start_date: Start date in YYYYMMDD format.
        end_date: End date in YYYYMMDD format.

    Returns:
        Dict mapping symbol codes to OHLCV DataFrames.

    Raises:
        ValueError: 输入参数无效时抛出（中文错误消息）
    """
    from backend.app.market.stock_tools import _tushare_request

    # 验证日期格式
    import re
    date_pattern = re.compile(r"^\d{8}$")
    if not date_pattern.match(start_date):
        raise ValueError(f"开始日期格式错误：{start_date}，应为YYYYMMDD格式")
    if not date_pattern.match(end_date):
        raise ValueError(f"结束日期格式错误：{end_date}，应为YYYYMMDD格式")

    # 验证日期范围
    if start_date > end_date:
        raise ValueError(f"开始日期({start_date})必须早于结束日期({end_date})")

    # 验证股票代码格式
    for symbol in symbols:
        if not re.match(r"^\d{6}\.(SH|SZ|BJ)$", symbol):
            raise ValueError(f"股票代码格式错误：{symbol}，应为6位数字.交易所（如000001.SZ）")

    data_map = {}
    fields = "trade_date,open,high,low,close,vol,pct_chg,pre_close"

    for symbol in symbols:
        result = _tushare_request(
            "daily",
            {"ts_code": symbol, "start_date": start_date, "end_date": end_date},
            fields,
        )

        if "error" in result:
            logger.warning("加载%s数据失败: %s", symbol, result["error"])
            continue

        rows = result.get("rows", [])
        if not rows:
            logger.warning("%s在指定日期范围内无数据", symbol)
            continue

        # 转换为DataFrame
        df = pd.DataFrame(rows)

        # 重命名列
        df = df.rename(columns={"trade_date": "date", "vol": "volume"})

        # 转换日期并设置为索引
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date").sort_index()

        # 只保留需要的列
        required_cols = ["open", "high", "low", "close", "volume", "pct_chg", "pre_close"]
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            logger.warning("%s缺少必需列: %s", symbol, missing_cols)
            continue

        df = df[required_cols]
        data_map[symbol] = df

    if not data_map:
        raise ValueError("未能加载任何股票数据，请检查股票代码和日期范围")

    return data_map


def run_custom_signal_backtest(
    signal_engine: SignalEngine,
    symbols: list[str],
    start_date: str,
    end_date: str,
    initial_cash: float = 1_000_000,
    commission_rate: float = 0.00025,
    slippage: float = 0.001,
) -> Dict[str, Any]:
    """Run a backtest with a custom signal engine.

    This is the low-level API for running backtests with user-defined strategies.

    Args:
        signal_engine: Custom SignalEngine instance.
        symbols: List of stock codes.
        start_date: Start date in YYYYMMDD format.
        end_date: End date in YYYYMMDD format.
        initial_cash: Initial capital.
        commission_rate: Commission rate.
        slippage: Slippage rate.

    Returns:
        Dict with backtest results.
    """
    # Load data
    data_map = _load_stock_data(symbols, start_date, end_date)

    # Configure market engine
    config = {
        "initial_cash": initial_cash,
        "commission_rate": commission_rate,
        "slippage": slippage,
    }
    market_engine = ChinaAEngine(config)

    # Run backtest
    metrics = market_engine.run_backtest(config, data_map, signal_engine)

    return {
        "metrics": metrics,
        "trades": len(market_engine.trades),
        "final_equity": market_engine.equity_snapshots[-1].equity if market_engine.equity_snapshots else initial_cash,
    }
