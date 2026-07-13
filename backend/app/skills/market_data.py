"""技能：行情数据 —— 股票解析、TuShare 日线、实时行情、基本信息、分析上下文。

Phase 1：从 stock_tools 引用 schema 与执行器（单一来源、无漂移），注册进
skill_registry；本阶段不接管 run_agent_events 分发。参数抽取须与
stock_tools.execute_stock_tool 同名分支逐字一致（Phase 2 切换分发后由 registry 调用）。
"""

from backend.app.market.stock_tools import (
    MAX_DAILY_ROWS,
    STOCK_TOOL_SCHEMAS,
    _int_arg,
    _optional_str,
    get_analysis_context,
    get_latest_price,
    get_realtime_quote,
    get_stock_info,
    get_tushare_daily,
    resolve_stock,
)

_SCHEMA_BY_NAME = {s["function"]["name"]: s for s in STOCK_TOOL_SCHEMAS}


def _pick(*names: str) -> list[dict]:
    """从 STOCK_TOOL_SCHEMAS 按工具名取出对应 schema，保持单一来源不漂移。"""
    return [_SCHEMA_BY_NAME[n] for n in names]


SKILL = {
    "name": "market_data",
    "display_name": "行情数据",
    "description": "股票代码解析、TuShare 日线、实时行情、基本信息与分析上下文",
    "version": "1.0.0",
    "chat_modes": ["stock", "research"],
    # Phase 3 再按 ROLE_TOOL_NAMES 精细化各数字人角色可见集合；空=所有角色可见
    "roles": [],
    "tools": _pick(
        "resolve_stock",
        "get_latest_price",
        "get_tushare_daily",
        "get_daily_history",
        "get_realtime_quote",
        "get_stock_info",
        "get_analysis_context",
    ),
    # 参数抽取与 stock_tools.execute_stock_tool 同名分支逐字一致
    "executors": {
        "resolve_stock": lambda a: resolve_stock(str(a.get("query") or "")),
        "get_latest_price": lambda a: get_latest_price(str(a.get("symbol") or "")),
        "get_tushare_daily": lambda a: get_tushare_daily(
            str(a.get("symbol") or ""),
            start_date=_optional_str(a.get("start_date")),
            end_date=_optional_str(a.get("end_date")),
            limit=_int_arg(a.get("limit"), 90, MAX_DAILY_ROWS),
        ),
        "get_daily_history": lambda a: get_tushare_daily(
            str(a.get("symbol") or ""),
            limit=_int_arg(a.get("days"), 90, MAX_DAILY_ROWS),
        ),
        "get_realtime_quote": lambda a: get_realtime_quote(str(a.get("symbol") or "")),
        "get_stock_info": lambda a: get_stock_info(str(a.get("symbol") or "")),
        "get_analysis_context": lambda a: get_analysis_context(
            str(a.get("symbol") or ""),
            limit=_int_arg(a.get("limit"), 20, 60),
        ),
    },
    "prompt_instructions": [],
}
