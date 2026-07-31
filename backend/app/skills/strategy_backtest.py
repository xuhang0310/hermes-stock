"""Deterministic A-share strategy backtesting tools."""

from backend.app.quant.backtest_engine import create_backtest


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


SKILL = {
    "name": "strategy_backtest",
    "display_name": "策略回测",
    "description": "A 股 MACD 与风险平价组合的可复现回测、绩效指标、归因和完整可视化报告。",
    "version": "1.0.0",
    "chat_modes": ["research", "stock"],
    "roles": [],
    "tools": BACKTEST_TOOL_SCHEMAS,
    "executors": {
        "run_macd_backtest": _run,
        "run_portfolio_backtest": _run_portfolio,
    },
    "prompt_instructions": [
        "用户要求 MACD 策略回测时，必须调用 run_macd_backtest，不要自行编造收益和交易记录。",
        "用户要求风险平价、等权对比、多标的组合回测或组合归因时，必须调用 run_portfolio_backtest，不要编写临时 Python 回测脚本。",
        "最终回答必须原样保留工具返回的完整报告链接，并披露 confidence 与 warnings。",
        "样本不足或低置信度时不得使用'策略健康''稳定盈利'等确定性表述。",
        "当用户描述一个交易策略或买卖思路（如'均线金叉买入死叉卖出'、'放量突破追入'、'MACD 金叉做多'）时，即使没有明确说'回测'，也应主动建议用历史数据验证该策略，并询问是否需要跑回测。",
        "当用户问'这只股票过去表现怎么样'、'如果当时买入能赚多少'、'这个策略历史胜率如何'等假设性历史问题时，应识别为回测意图并调用相应工具。",
        "当用户持有或关注某只股票且讨论了技术策略时，可以主动提议：'要不要我帮你回测一下这个策略在这只股票上的历史表现？'",
    ],
}
