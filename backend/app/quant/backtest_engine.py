from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable

import numpy as np
import pandas as pd
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.app.core.database import SessionLocal
from backend.app.core.models import BacktestRun
from backend.app.market.stock_tools import (
    get_index_daily,
    get_tushare_daily,
    normalize_a_share_symbol,
)


MIN_COMMISSION_CNY = 5.0
DEFAULT_BENCHMARK = "000300.SH"
MAX_BACKTEST_BARS = 1200


@dataclass(frozen=True)
class MacdBacktestConfig:
    symbol: str
    start_date: str
    end_date: str
    fast_period: int = 12
    slow_period: int = 26
    signal_period: int = 9
    initial_cash: float = 1_000_000.0
    commission_rate: float = 0.0003
    stamp_duty_rate: float = 0.0005
    transfer_fee_rate: float = 0.00001
    slippage_rate: float = 0.0005
    lot_size: int = 100
    benchmark: str = DEFAULT_BENCHMARK


def _compact_date(value: str | None, fallback: date) -> str:
    raw = str(value or fallback.strftime("%Y%m%d")).strip().replace("-", "")
    if len(raw) != 8 or not raw.isdigit():
        raise ValueError(f"日期格式不正确：{value!r}，应为 YYYYMMDD 或 YYYY-MM-DD")
    return raw


def normalize_config(payload: dict[str, Any]) -> MacdBacktestConfig:
    symbol = normalize_a_share_symbol(str(payload.get("symbol") or "").strip())
    if not symbol.endswith((".SH", ".SZ", ".BJ")):
        raise ValueError(f"无法识别 A 股代码：{payload.get('symbol')}")
    today = date.today()
    start = _compact_date(payload.get("start_date"), today - timedelta(days=730))
    end = _compact_date(payload.get("end_date"), today)
    if start >= end:
        raise ValueError("开始日期必须早于结束日期")

    fast = int(payload.get("fast_period") or 12)
    slow = int(payload.get("slow_period") or 26)
    signal = int(payload.get("signal_period") or 9)
    if not 2 <= fast < slow <= 200 or not 2 <= signal <= 100:
        raise ValueError("MACD 参数必须满足 2 <= fast < slow <= 200，2 <= signal <= 100")

    initial_cash = float(payload.get("initial_cash") or 1_000_000)
    if not 10_000 <= initial_cash <= 1_000_000_000:
        raise ValueError("初始资金必须在 1 万至 10 亿元之间")

    return MacdBacktestConfig(
        symbol=symbol,
        start_date=start,
        end_date=end,
        fast_period=fast,
        slow_period=slow,
        signal_period=signal,
        initial_cash=initial_cash,
        commission_rate=float(payload.get("commission_rate") or 0.0003),
        stamp_duty_rate=float(payload.get("stamp_duty_rate") or 0.0005),
        transfer_fee_rate=float(payload.get("transfer_fee_rate") or 0.00001),
        slippage_rate=float(payload.get("slippage_rate") or 0.0005),
        lot_size=max(100, int(payload.get("lot_size") or 100)),
        benchmark=str(payload.get("benchmark") or DEFAULT_BENCHMARK).strip().upper(),
    )


def _bars_frame(rows: Iterable[dict[str, Any]]) -> pd.DataFrame:
    frame = pd.DataFrame(list(rows))
    required = {"trade_date", "open", "high", "low", "close"}
    if frame.empty or not required.issubset(frame.columns):
        raise ValueError("行情数据缺少 OHLC 或交易日期")
    frame["trade_date"] = pd.to_datetime(frame["trade_date"].astype(str))
    for field in ("open", "high", "low", "close", "volume", "amount"):
        if field not in frame:
            frame[field] = 0.0
        frame[field] = pd.to_numeric(frame[field], errors="coerce")
    frame = frame.dropna(subset=["trade_date", "open", "high", "low", "close"])
    return frame.sort_values("trade_date").drop_duplicates("trade_date", keep="last").reset_index(drop=True)


def load_market_data(config: MacdBacktestConfig) -> tuple[pd.DataFrame, pd.Series | None, list[str]]:
    warnings: list[str] = []
    stock = get_tushare_daily(
        config.symbol,
        start_date=config.start_date,
        end_date=config.end_date,
        limit=MAX_BACKTEST_BARS,
    )
    if stock.get("error"):
        raise ValueError(f"获取 {config.symbol} 日线失败：{stock['error']}")
    frame = _bars_frame(stock.get("bars") or [])

    benchmark_returns: pd.Series | None = None
    benchmark = get_index_daily(
        config.benchmark,
        start_date=config.start_date,
        end_date=config.end_date,
    )
    if benchmark.get("error"):
        warnings.append(f"基准 {config.benchmark} 获取失败，Beta 与超额收益不可用")
    else:
        rows = benchmark.get("rows") or []
        if rows:
            benchmark_frame = _bars_frame(rows)
            benchmark_returns = benchmark_frame.set_index("trade_date")["close"].pct_change()
        else:
            warnings.append(f"基准 {config.benchmark} 没有返回行情")
    return frame, benchmark_returns, warnings


def _commission(value: float, config: MacdBacktestConfig) -> float:
    return max(MIN_COMMISSION_CNY, value * config.commission_rate)


def _buy_cost(price: float, quantity: int, config: MacdBacktestConfig) -> float:
    value = price * quantity
    return value + _commission(value, config) + value * config.transfer_fee_rate


def _sell_proceeds(price: float, quantity: int, config: MacdBacktestConfig) -> tuple[float, float]:
    value = price * quantity
    fees = _commission(value, config) + value * (config.transfer_fee_rate + config.stamp_duty_rate)
    return value - fees, fees


def _metric_value(value: float) -> float:
    return round(float(value), 8) if np.isfinite(value) else 0.0


def _compute_metrics(
    equity: pd.Series,
    trades: list[dict[str, Any]],
    config: MacdBacktestConfig,
    benchmark_returns: pd.Series | None,
) -> dict[str, Any]:
    daily_returns = equity.pct_change().fillna(0.0)
    total_return = equity.iloc[-1] / config.initial_cash - 1
    periods = max(len(equity) - 1, 1)
    annual_return = (max(equity.iloc[-1], 0.01) / config.initial_cash) ** (252 / periods) - 1
    volatility = daily_returns.std(ddof=1)
    sharpe = daily_returns.mean() / volatility * math.sqrt(252) if volatility > 0 else 0.0
    downside = daily_returns[daily_returns < 0]
    downside_std = downside.std(ddof=1) if len(downside) > 1 else 0.0
    sortino = daily_returns.mean() / downside_std * math.sqrt(252) if downside_std > 0 else 0.0
    drawdown = equity / equity.cummax() - 1
    max_drawdown = float(drawdown.min())
    calmar = annual_return / abs(max_drawdown) if max_drawdown < 0 else 0.0

    wins = [item for item in trades if item["pnl"] > 0]
    losses = [item for item in trades if item["pnl"] < 0]
    gross_win = sum(item["pnl"] for item in wins)
    gross_loss = abs(sum(item["pnl"] for item in losses))
    profit_loss_ratio = (
        (gross_win / len(wins)) / (gross_loss / len(losses))
        if wins and losses and gross_loss > 0
        else 0.0
    )

    benchmark_return = None
    excess_return = None
    alpha_daily = None
    beta = None
    r_squared = None
    alpha_t_stat = None
    information_ratio = None
    if benchmark_returns is not None:
        aligned = pd.concat(
            [daily_returns.rename("strategy"), benchmark_returns.rename("benchmark")],
            axis=1,
            join="inner",
        ).dropna()
        if len(aligned) >= 3:
            benchmark_return = float((1 + aligned["benchmark"]).prod() - 1)
            excess_return = total_return - benchmark_return
            x = aligned["benchmark"].to_numpy(dtype=float)
            y = aligned["strategy"].to_numpy(dtype=float)
            design = np.column_stack([np.ones(len(x)), x])
            coefficients, _, _, _ = np.linalg.lstsq(design, y, rcond=None)
            alpha_daily, beta = float(coefficients[0]), float(coefficients[1])
            fitted = design @ coefficients
            residual = y - fitted
            ss_total = float(np.square(y - y.mean()).sum())
            r_squared = 1 - float(np.square(residual).sum()) / ss_total if ss_total > 0 else 0.0
            if len(x) > 2:
                sigma2 = float(np.square(residual).sum()) / (len(x) - 2)
                covariance = sigma2 * np.linalg.pinv(design.T @ design)
                alpha_se = math.sqrt(max(float(covariance[0, 0]), 0.0))
                alpha_t_stat = alpha_daily / alpha_se if alpha_se > 0 else 0.0
            active = aligned["strategy"] - aligned["benchmark"]
            active_std = active.std(ddof=1)
            information_ratio = active.mean() / active_std * math.sqrt(252) if active_std > 0 else 0.0

    avg_holding = float(np.mean([item["holding_days"] for item in trades])) if trades else 0.0
    return {
        "initial_cash": _metric_value(config.initial_cash),
        "final_equity": _metric_value(equity.iloc[-1]),
        "total_return": _metric_value(total_return),
        "annual_return": _metric_value(annual_return),
        "max_drawdown": _metric_value(max_drawdown),
        "sharpe": _metric_value(sharpe),
        "sortino": _metric_value(sortino),
        "calmar": _metric_value(calmar),
        "win_rate": _metric_value(len(wins) / len(trades) if trades else 0.0),
        "profit_loss_ratio": _metric_value(profit_loss_ratio),
        "trade_count": len(trades),
        "winning_trades": len(wins),
        "losing_trades": len(losses),
        "avg_holding_days": _metric_value(avg_holding),
        "benchmark_return": _metric_value(benchmark_return) if benchmark_return is not None else None,
        "excess_return": _metric_value(excess_return) if excess_return is not None else None,
        "information_ratio": _metric_value(information_ratio) if information_ratio is not None else None,
        "alpha_daily": _metric_value(alpha_daily) if alpha_daily is not None else None,
        "alpha_annual": _metric_value((1 + alpha_daily) ** 252 - 1) if alpha_daily is not None else None,
        "alpha_t_stat": _metric_value(alpha_t_stat) if alpha_t_stat is not None else None,
        "beta": _metric_value(beta) if beta is not None else None,
        "r_squared": _metric_value(r_squared) if r_squared is not None else None,
    }


def _confidence(metrics: dict[str, Any], bars: int) -> tuple[str, str]:
    trades = int(metrics["trade_count"])
    if bars < 120 or trades < 5:
        return "insufficient", "样本不足"
    if bars < 252 or trades < 10:
        return "low", "低置信度"
    if metrics["sharpe"] > 1 and metrics["max_drawdown"] > -0.2:
        return "medium", "初步健康"
    return "medium", "需要优化"


def _round_trip_trade(
    entry: dict[str, Any],
    exit_date: str,
    exit_price: float,
    exit_reason: str,
    sell_fees: float,
) -> dict[str, Any]:
    gross = (exit_price - entry["entry_price"]) * entry["quantity"]
    pnl = gross - entry["buy_fees"] - sell_fees
    return {
        "symbol": entry["symbol"],
        "direction": "long",
        "entry_date": entry["entry_date"],
        "exit_date": exit_date,
        "entry_price": round(entry["entry_price"], 4),
        "exit_price": round(exit_price, 4),
        "quantity": entry["quantity"],
        "holding_days": entry["holding_days"],
        "gross_pnl": round(gross, 2),
        "fees": round(entry["buy_fees"] + sell_fees, 2),
        "pnl": round(pnl, 2),
        "return_pct": round(pnl / entry["entry_value"], 6) if entry["entry_value"] else 0.0,
        "exit_reason": exit_reason,
    }


def run_macd_backtest(
    config: MacdBacktestConfig,
    frame: pd.DataFrame,
    benchmark_returns: pd.Series | None = None,
    initial_warnings: list[str] | None = None,
) -> dict[str, Any]:
    minimum = config.slow_period + config.signal_period + 5
    if len(frame) < minimum:
        raise ValueError(f"有效日线只有 {len(frame)} 条，MACD 至少需要 {minimum} 条")

    data = frame.copy()
    data["ema_fast"] = data["close"].ewm(span=config.fast_period, adjust=False).mean()
    data["ema_slow"] = data["close"].ewm(span=config.slow_period, adjust=False).mean()
    data["macd"] = data["ema_fast"] - data["ema_slow"]
    data["signal"] = data["macd"].ewm(span=config.signal_period, adjust=False).mean()
    data["histogram"] = (data["macd"] - data["signal"]) * 2
    data["cross_up"] = (data["macd"] > data["signal"]) & (data["macd"].shift(1) <= data["signal"].shift(1))
    data["cross_down"] = (data["macd"] < data["signal"]) & (data["macd"].shift(1) >= data["signal"].shift(1))
    data.loc[: minimum - 1, ["cross_up", "cross_down"]] = False

    cash = config.initial_cash
    quantity = 0
    entry: dict[str, Any] | None = None
    trades: list[dict[str, Any]] = []
    markers: list[dict[str, Any]] = []
    equity_rows: list[dict[str, Any]] = []

    for index, row in data.iterrows():
        trade_date = row["trade_date"].strftime("%Y-%m-%d")
        if entry is not None:
            entry["holding_days"] += 1

        previous = data.iloc[index - 1] if index > 0 else None
        if previous is not None and bool(previous["cross_down"]) and quantity > 0:
            execution_price = float(row["open"]) * (1 - config.slippage_rate)
            proceeds, sell_fees = _sell_proceeds(execution_price, quantity, config)
            cash += proceeds
            trades.append(_round_trip_trade(entry or {}, trade_date, execution_price, "signal", sell_fees))
            markers.append({"time": trade_date, "position": "aboveBar", "shape": "arrowDown", "text": "卖出"})
            quantity = 0
            entry = None

        if previous is not None and bool(previous["cross_up"]) and quantity == 0:
            execution_price = float(row["open"]) * (1 + config.slippage_rate)
            affordable = int(cash / (execution_price * config.lot_size)) * config.lot_size
            while affordable >= config.lot_size and _buy_cost(execution_price, affordable, config) > cash:
                affordable -= config.lot_size
            if affordable >= config.lot_size:
                entry_value = execution_price * affordable
                total_cost = _buy_cost(execution_price, affordable, config)
                buy_fees = total_cost - entry_value
                cash -= total_cost
                quantity = affordable
                entry = {
                    "symbol": config.symbol,
                    "entry_date": trade_date,
                    "entry_price": execution_price,
                    "entry_value": entry_value,
                    "quantity": affordable,
                    "buy_fees": buy_fees,
                    "holding_days": 0,
                }
                markers.append({"time": trade_date, "position": "belowBar", "shape": "arrowUp", "text": "买入"})

        equity_rows.append(
            {
                "time": trade_date,
                "equity": round(cash + quantity * float(row["close"]), 2),
            }
        )

    if quantity > 0 and entry is not None:
        last = data.iloc[-1]
        trade_date = last["trade_date"].strftime("%Y-%m-%d")
        execution_price = float(last["close"]) * (1 - config.slippage_rate)
        proceeds, sell_fees = _sell_proceeds(execution_price, quantity, config)
        cash += proceeds
        trades.append(_round_trip_trade(entry, trade_date, execution_price, "end_of_backtest", sell_fees))
        markers.append({"time": trade_date, "position": "aboveBar", "shape": "arrowDown", "text": "到期平仓"})
        equity_rows[-1]["equity"] = round(cash, 2)

    equity = pd.Series(
        [item["equity"] for item in equity_rows],
        index=pd.DatetimeIndex([item["time"] for item in equity_rows]),
        dtype=float,
    )
    drawdown = equity / equity.cummax() - 1
    for index, value in enumerate(drawdown):
        equity_rows[index]["drawdown"] = round(float(value), 8)

    metrics = _compute_metrics(equity, trades, config, benchmark_returns)
    confidence, rating = _confidence(metrics, len(data))
    warnings = list(initial_warnings or [])
    warnings.extend(
        [
            "当前引擎仅允许 A 股现货做多，不模拟无券源做空。",
            "信号在收盘后确认，并在下一交易日开盘执行，避免未来函数。",
            "已模拟 T+1、100 股整数手、佣金最低 5 元、印花税、过户费和滑点。",
        ]
    )
    if confidence in {"insufficient", "low"}:
        warnings.append("样本量不足，年化收益和风险比率不具备稳定统计意义。")

    price_series = [
        {
            "time": row["trade_date"].strftime("%Y-%m-%d"),
            "open": round(float(row["open"]), 4),
            "high": round(float(row["high"]), 4),
            "low": round(float(row["low"]), 4),
            "close": round(float(row["close"]), 4),
            "volume": round(float(row.get("volume") or 0), 2),
            "macd": round(float(row["macd"]), 6),
            "signal": round(float(row["signal"]), 6),
            "histogram": round(float(row["histogram"]), 6),
        }
        for _, row in data.iterrows()
    ]
    top_winner = max(trades, key=lambda item: item["pnl"], default=None)
    top_loser = min(trades, key=lambda item: item["pnl"], default=None)
    fingerprint_payload = json.dumps(price_series, ensure_ascii=False, separators=(",", ":"))
    return {
        "status": "completed",
        "strategy": {
            "code": "macd_cross",
            "name": f"MACD({config.fast_period},{config.slow_period},{config.signal_period})",
            "timeframe": "1D",
            "source": "Hermes deterministic backtest engine",
        },
        "symbol": config.symbol,
        "period": {
            "start_date": price_series[0]["time"],
            "end_date": price_series[-1]["time"],
            "trading_days": len(price_series),
        },
        "config": asdict(config),
        "metrics": metrics,
        "confidence": confidence,
        "rating": rating,
        "warnings": warnings,
        "price_series": price_series,
        "trade_markers": markers,
        "equity_curve": equity_rows,
        "trades": trades,
        "attribution": {
            "top_winner": top_winner,
            "top_loser": top_loser,
            "remove_top_winner_pnl": (
                round(sum(item["pnl"] for item in trades) - top_winner["pnl"], 2)
                if top_winner
                else None
            ),
            "exit_reasons": _exit_reason_stats(trades),
        },
        "data_fingerprint": hashlib.sha256(fingerprint_payload.encode("utf-8")).hexdigest(),
    }


def _exit_reason_stats(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for trade in trades:
        groups.setdefault(trade["exit_reason"], []).append(trade)
    return [
        {
            "reason": reason,
            "count": len(rows),
            "total_pnl": round(sum(item["pnl"] for item in rows), 2),
            "avg_pnl": round(sum(item["pnl"] for item in rows) / len(rows), 2),
            "win_rate": round(sum(item["pnl"] > 0 for item in rows) / len(rows), 4),
        }
        for reason, rows in groups.items()
    ]


def source_code_for(config: MacdBacktestConfig) -> str:
    return (
        f"# MACD({config.fast_period}, {config.slow_period}, {config.signal_period})\n"
        "ema_fast = close.ewm(span=fast, adjust=False).mean()\n"
        "ema_slow = close.ewm(span=slow, adjust=False).mean()\n"
        "macd = ema_fast - ema_slow\n"
        "signal = macd.ewm(span=signal_period, adjust=False).mean()\n"
        "cross_up = (macd > signal) & (macd.shift(1) <= signal.shift(1))\n"
        "cross_down = (macd < signal) & (macd.shift(1) >= signal.shift(1))\n"
        "# Signal is confirmed at close and executed at next open.\n"
    )


def summary_markdown(run_id: str, result: dict[str, Any]) -> str:
    metrics = result["metrics"]
    period = result["period"]
    confidence = result["confidence"]
    confidence_text = {"insufficient": "样本不足", "low": "低置信度", "medium": "中等置信度"}.get(
        confidence,
        confidence,
    )
    return (
        f"### {result['strategy']['name']} 回测完成 · {result['symbol']}\n\n"
        f"- 区间：{period['start_date']} 至 {period['end_date']}（{period['trading_days']} 个交易日）\n"
        f"- 总收益：{metrics['total_return'] * 100:+.2f}% ｜ 年化：{metrics['annual_return'] * 100:+.2f}%\n"
        f"- 最大回撤：{metrics['max_drawdown'] * 100:.2f}% ｜ Sharpe：{metrics['sharpe']:.2f}\n"
        f"- 交易：{metrics['trade_count']} 笔 ｜ 胜率：{metrics['win_rate'] * 100:.1f}%\n"
        f"- 可信度：**{confidence_text}**\n\n"
        f"[查看完整回测报告](/backtests/{run_id})"
    )


def create_backtest(
    payload: dict[str, Any],
    *,
    owner_user_id: int | None = None,
    agent_run_id: str | None = None,
    db: Session | None = None,
) -> dict[str, Any]:
    if isinstance(payload.get("symbols"), (list, tuple)) or str(payload.get("strategy") or "").lower() == "risk_parity":
        from backend.app.quant.portfolio_backtest import create_portfolio_backtest

        return create_portfolio_backtest(
            payload,
            owner_user_id=owner_user_id,
            agent_run_id=agent_run_id,
            db=db,
        )
    config = normalize_config(payload)
    frame, benchmark_returns, warnings = load_market_data(config)
    result = run_macd_backtest(config, frame, benchmark_returns, warnings)
    run_id = uuid.uuid4().hex
    result["run_id"] = run_id
    result["report_url"] = f"/backtests/{run_id}"
    summary = summary_markdown(run_id, result)

    owns_session = db is None
    session = db or SessionLocal()
    try:
        session.add(
            BacktestRun(
                run_id=run_id,
                owner_user_id=owner_user_id,
                agent_run_id=agent_run_id,
                status="completed",
                symbol=config.symbol,
                strategy_code="macd_cross",
                timeframe="1D",
                start_date=result["period"]["start_date"],
                end_date=result["period"]["end_date"],
                confidence=result["confidence"],
                config_json=json.dumps(result["config"], ensure_ascii=False),
                metrics_json=json.dumps(result["metrics"], ensure_ascii=False),
                result_json=json.dumps(result, ensure_ascii=False),
                summary_markdown=summary,
                source_code=source_code_for(config),
                data_fingerprint=result["data_fingerprint"],
                finished_at=datetime.now(),
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        if owns_session:
            session.close()
    return {**result, "summary_markdown": summary, "source_code": source_code_for(config)}


def get_backtest(run_id: str, *, owner_user_id: int | None = None, db: Session | None = None) -> dict[str, Any]:
    owns_session = db is None
    session = db or SessionLocal()
    try:
        query = select(BacktestRun).where(BacktestRun.run_id == run_id)
        if owner_user_id is not None:
            query = query.where(
                or_(BacktestRun.owner_user_id == owner_user_id, BacktestRun.owner_user_id.is_(None))
            )
        row = session.scalar(query)
        if row is None:
            raise KeyError(f"回测记录不存在：{run_id}")
        result = json.loads(row.result_json)
        result["summary_markdown"] = row.summary_markdown
        result["source_code"] = row.source_code
        return result
    finally:
        if owns_session:
            session.close()


def list_backtests(owner_user_id: int, *, limit: int = 30, db: Session | None = None) -> list[dict[str, Any]]:
    owns_session = db is None
    session = db or SessionLocal()
    try:
        rows = session.scalars(
            select(BacktestRun)
            .where(or_(BacktestRun.owner_user_id == owner_user_id, BacktestRun.owner_user_id.is_(None)))
            .order_by(BacktestRun.created_at.desc())
            .limit(max(1, min(limit, 100)))
        ).all()
        return [
            {
                "run_id": row.run_id,
                "symbol": row.symbol,
                "strategy_code": row.strategy_code,
                "timeframe": row.timeframe,
                "start_date": row.start_date,
                "end_date": row.end_date,
                "confidence": row.confidence,
                "metrics": json.loads(row.metrics_json),
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "report_url": f"/backtests/{row.run_id}",
            }
            for row in rows
        ]
    finally:
        if owns_session:
            session.close()
