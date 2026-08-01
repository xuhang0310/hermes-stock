"""Deterministic A-share strategy backtesting tools."""

from backend.app.quant.backtest_engine import create_backtest
from backend.app.quant.backtest_runner import (
    list_signal_engines,
    run_signal_backtest,
)


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


SKILL = {
    "name": "strategy_backtest",
    "display_name": "策略回测",
    "description": "A 股多策略回测引擎：MACD、均线交叉、布林带、RSI、风险平价组合，完整绩效指标与交易模拟。",
    "version": "2.0.0",
    "chat_modes": ["research", "stock"],
    "roles": [],
    "tools": BACKTEST_TOOL_SCHEMAS,
    "executors": {
        "run_macd_backtest": _run,
        "run_portfolio_backtest": _run_portfolio,
        "run_signal_backtest": _run_signal,
        "list_backtest_strategies": _list_strategies,
    },
    "prompt_instructions": [
        "用户要求回测时，根据策略类型选择工具：MACD 用 run_macd_backtest，组合用 run_portfolio_backtest，其他策略用 run_signal_backtest。",
        "用户询问有哪些策略时，调用 list_backtest_strategies 列出所有可用策略。",
        "用户描述交易策略（如'均线金叉买入死叉卖出'、'布林带突破'、'RSI 超卖买入'）时，识别对应策略并调用 run_signal_backtest。",
        "run_signal_backtest 支持多种策略：macd（MACD 金叉死叉）、ma_cross（均线交叉）、bollinger（布林带均值回归）、rsi（RSI 超买超卖）。",
        "最终回答必须原样保留工具返回的完整结果，包括绩效指标、逐笔交易和权益曲线。",
        "样本不足或低置信度时不得使用'策略健康''稳定盈利'等确定性表述。",
        "当用户描述一个交易策略或买卖思路时，即使没有明确说'回测'，也应主动建议用历史数据验证该策略。",
        "当用户问'这只股票过去表现怎么样'、'如果当时用某个策略买入能赚多少'等假设性历史问题时，应识别为回测意图。",
    ],
}
