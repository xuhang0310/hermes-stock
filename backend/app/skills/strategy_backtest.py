"""Deterministic A-share strategy backtesting tools."""

from backend.app.quant.backtest_engine import create_backtest
from backend.app.quant.backtest_runner import (
    list_signal_engines,
    run_signal_backtest,
)
from backend.app.quant.factor_signal_engine import FactorSignalEngine


BACKTEST_TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "run_macd_backtest",
            "description": (
                "运行专业的 A 股 MACD 日线策略回测，生成真实指标、逐笔交易、K 线买卖点、"
                "净值回撤和完整报告链接。默认只做多，并模拟 T+1、整手、费用和滑点。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "A 股代码，如 000001.SZ"},
                    "start_date": {"type": "string", "description": "YYYYMMDD，默认回看两年"},
                    "end_date": {"type": "string", "description": "YYYYMMDD，默认今天"},
                    "fast_period": {"type": "integer", "default": 12, "minimum": 2, "maximum": 200},
                    "slow_period": {"type": "integer", "default": 26, "minimum": 3, "maximum": 200},
                    "signal_period": {"type": "integer", "default": 9, "minimum": 2, "maximum": 100},
                    "initial_cash": {"type": "number", "default": 1000000, "minimum": 10000},
                    "commission_rate": {"type": "number", "default": 0.0003, "minimum": 0},
                    "slippage_rate": {"type": "number", "default": 0.0005, "minimum": 0},
                    "benchmark": {"type": "string", "default": "000300.SH"},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_portfolio_backtest",
            "description": (
                "运行专业的 A 股多标的风险平价组合回测，并与相同标的、相同调仓频率的"
                "等权组合比较。生成权重、调仓、成本、资产贡献、市场回归、稳健性检验"
                "和完整可视化报告。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 20,
                        "description": "2 至 20 个 A 股代码，如 000001.SZ、600519.SH",
                    },
                    "start_date": {"type": "string", "description": "YYYYMMDD"},
                    "end_date": {"type": "string", "description": "YYYYMMDD"},
                    "rebalance_frequency": {
                        "type": "string",
                        "enum": ["weekly", "monthly", "quarterly"],
                        "default": "monthly",
                        "description": "调仓频率，默认月度",
                    },
                    "lookback_days": {
                        "type": "integer",
                        "default": 60,
                        "minimum": 20,
                        "maximum": 252,
                        "description": "协方差估计窗口",
                    },
                    "covariance_shrinkage": {
                        "type": "number",
                        "default": 0.1,
                        "minimum": 0,
                        "maximum": 1,
                        "description": "样本协方差向对角阵收缩的比例",
                    },
                    "initial_cash": {"type": "number", "default": 1000000, "minimum": 10000},
                    "commission_rate": {"type": "number", "default": 0.0003, "minimum": 0},
                    "slippage_rate": {"type": "number", "default": 0.0005, "minimum": 0},
                },
                "required": ["symbols", "start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_signal_backtest",
            "description": (
                "运行基于信号引擎的策略回测。支持多种内置策略：MACD、均线交叉、布林带、RSI 等。"
                "返回完整绩效指标、逐笔交易和权益曲线。支持 T+1、涨跌停、整手交易等 A 股规则。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "strategy": {
                        "type": "string",
                        "enum": ["macd", "ma_cross", "bollinger", "rsi"],
                        "description": "策略名称：macd（MACD金叉死叉）、ma_cross（均线交叉）、bollinger（布林带）、rsi（RSI超买超卖）",
                    },
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 10,
                        "description": "A 股代码列表，如 ['000001.SZ'] 或 ['600519.SH', '000858.SZ']",
                    },
                    "start_date": {"type": "string", "description": "YYYYMMDD，回测起始日期"},
                    "end_date": {"type": "string", "description": "YYYYMMDD，回测结束日期"},
                    "strategy_params": {
                        "type": "object",
                        "description": "策略参数（可选），如 MACD: {fast: 12, slow: 26, signal: 9}",
                    },
                    "initial_cash": {"type": "number", "default": 1000000, "description": "初始资金"},
                    "commission_rate": {"type": "number", "default": 0.00025, "description": "佣金率"},
                    "slippage": {"type": "number", "default": 0.001, "description": "滑点率"},
                },
                "required": ["strategy", "symbols", "start_date", "end_date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_backtest_strategies",
            "description": "列出所有可用的回测策略及其描述。",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_factor_backtest",
            "description": (
                "运行基于因子组合的多因子策略回测。支持从462个预置因子中选择，"
                "横截面标准化，加权组合，TopN选股。可做多、做空或多空组合。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "factor_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "maxItems": 10,
                        "description": "因子ID列表，如 ['alpha101_001', 'qlib158_roc20']",
                    },
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 5,
                        "maxItems": 100,
                        "description": "股票池（至少5只，用于横截面比较）",
                    },
                    "weights": {
                        "type": "array",
                        "items": {"type": "number"},
                        "description": "因子权重（可选，默认等权）",
                    },
                    "top_n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "做多前N只股票",
                    },
                    "bottom_n": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 50,
                        "description": "做空后N只股票（可选）",
                    },
                    "standardize": {
                        "type": "boolean",
                        "default": True,
                        "description": "是否横截面Z-score标准化",
                    },
                    "start_date": {"type": "string", "description": "YYYYMMDD"},
                    "end_date": {"type": "string", "description": "YYYYMMDD"},
                    "initial_cash": {"type": "number", "default": 1000000},
                },
                "required": ["factor_ids", "symbols", "start_date", "end_date"],
            },
        },
    },
]


def run_backtest_tool(
    args: dict,
    *,
    owner_user_id: int | None = None,
    agent_run_id: str | None = None,
) -> dict:
    result = create_backtest(
        args,
        owner_user_id=owner_user_id,
        agent_run_id=agent_run_id,
    )
    return {
        "run_id": result["run_id"],
        "report_url": result["report_url"],
        "summary_markdown": result["summary_markdown"],
        "strategy": result["strategy"],
        "symbol": result["symbol"],
        "period": result["period"],
        "metrics": result["metrics"],
        "confidence": result["confidence"],
        "rating": result["rating"],
        "warnings": result["warnings"],
    }


def run_portfolio_backtest_tool(
    args: dict,
    *,
    owner_user_id: int | None = None,
    agent_run_id: str | None = None,
) -> dict:
    result = create_backtest(
        {**args, "strategy": "risk_parity"},
        owner_user_id=owner_user_id,
        agent_run_id=agent_run_id,
    )
    return {
        "run_id": result["run_id"],
        "report_url": result["report_url"],
        "summary_markdown": result["summary_markdown"],
        "strategy": result["strategy"],
        "symbol": result["symbol"],
        "symbols": result["symbols"],
        "period": result["period"],
        "metrics": result["metrics"],
        "benchmark_metrics": result["benchmark_metrics"],
        "confidence": result["confidence"],
        "rating": result["rating"],
        "warnings": result["warnings"],
    }


def _run(args: dict) -> dict:
    return run_backtest_tool(args)


def _run_portfolio(args: dict) -> dict:
    return run_portfolio_backtest_tool(args)


def _run_signal(args: dict) -> dict:
    return run_signal_backtest(
        strategy=args["strategy"],
        symbols=args["symbols"],
        start_date=args["start_date"],
        end_date=args["end_date"],
        strategy_params=args.get("strategy_params"),
        initial_cash=args.get("initial_cash", 1_000_000),
        commission_rate=args.get("commission_rate", 0.00025),
        slippage=args.get("slippage", 0.001),
    )


def _list_strategies(args: dict) -> dict:
    return {"strategies": list_signal_engines()}


def _run_factor_backtest(args: dict) -> dict:
    """运行多因子策略回测"""
    from backend.app.quant.backtest_runner import _load_stock_data
    from backend.app.quant.engines.china_a import ChinaAEngine

    # 创建因子信号引擎
    engine = FactorSignalEngine.from_factors(
        factor_ids=args["factor_ids"],
        weights=args.get("weights"),
        standardize=args.get("standardize", True),
        top_n=args.get("top_n"),
        bottom_n=args.get("bottom_n"),
    )

    # 加载数据
    data_map = _load_stock_data(args["symbols"], args["start_date"], args["end_date"])

    # 配置市场引擎
    config = {
        "initial_cash": args.get("initial_cash", 1_000_000),
        "commission_rate": 0.00025,
        "slippage": 0.001,
    }
    market_engine = ChinaAEngine(config)

    # 运行回测
    metrics = market_engine.run_backtest(config, data_map, engine)

    return {
        "factor_ids": args["factor_ids"],
        "symbols": args["symbols"],
        "period": {"start": args["start_date"], "end": args["end_date"]},
        "metrics": metrics,
        "trades": len(market_engine.trades),
        "final_equity": market_engine.equity_snapshots[-1].equity if market_engine.equity_snapshots else config["initial_cash"],
    }


SKILL = {
    "name": "strategy_backtest",
    "display_name": "策略回测",
    "description": "A 股完整量化回测平台：462个因子库、多因子组合策略、技术指标策略、完整绩效分析。",
    "version": "3.0.0",
    "chat_modes": ["research", "stock"],
    "roles": [],
    "tools": BACKTEST_TOOL_SCHEMAS,
    "executors": {
        "run_macd_backtest": _run,
        "run_portfolio_backtest": _run_portfolio,
        "run_signal_backtest": _run_signal,
        "list_backtest_strategies": _list_strategies,
        "run_factor_backtest": _run_factor_backtest,
    },
    "prompt_instructions": [
        "用户要求回测时，根据需求选择工具：",
        "  - 技术指标策略（MACD/均线/布林带/RSI）→ run_signal_backtest",
        "  - 因子组合策略（用462个因子）→ run_factor_backtest",
        "  - 风险平价组合 → run_portfolio_backtest",
        "用户想分析因子有效性时 → 使用 factor_research 技能的 benchmark_factor_alphas",
        "用户想对比多个因子 → 使用 factor_research 技能的 benchmark_factor_alphas",
        "用户想知道有哪些因子 → 使用 factor_research 技能的 list_factor_alphas",
        "run_factor_backtest 支持：选择因子、横截面标准化、TopN选股、多空策略",
        "最终回答必须原样保留工具返回的完整结果。",
        "样本不足时不得使用确定性表述。",
    ],
}
