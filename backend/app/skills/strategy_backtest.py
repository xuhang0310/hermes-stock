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
    }
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


def _run(args: dict) -> dict:
    return run_backtest_tool(args)


SKILL = {
    "name": "strategy_backtest",
    "display_name": "策略回测",
    "description": "A 股 MACD 策略的可复现回测、绩效指标、交易归因和完整可视化报告。",
    "version": "1.0.0",
    "chat_modes": ["research", "stock"],
    "roles": [],
    "tools": BACKTEST_TOOL_SCHEMAS,
    "executors": {"run_macd_backtest": _run},
    "prompt_instructions": [
        "用户要求 MACD、策略回测、回归测试、收益测试或用历史数据验证策略时，必须调用 run_macd_backtest，不要自行编造收益和交易记录。",
        "最终回答必须原样保留工具返回的完整报告链接，并披露 confidence 与 warnings。",
        "样本不足或低置信度时不得使用“策略健康”“稳定盈利”等确定性表述。",
    ],
}
