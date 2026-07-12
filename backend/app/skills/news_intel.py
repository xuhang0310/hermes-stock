"""技能：新闻与市场情报 —— Tavily/Brave 搜索（个股新闻、市场情报、综合情报）。

Phase 1：从 stock_tools 引用 schema 与执行器（单一来源、无漂移），注册进
skill_registry；本阶段不接管 run_agent_events 分发。参数抽取须与
stock_tools.execute_stock_tool 同名分支逐字一致（Phase 2 切换分发后由 registry 调用）。
"""

from backend.app.market.stock_tools import (
    STOCK_TOOL_SCHEMAS,
    _int_arg,
    _optional_str,
    search_market_intel,
    search_stock_news,
)

_SCHEMA_BY_NAME = {s["function"]["name"]: s for s in STOCK_TOOL_SCHEMAS}


def _pick(*names: str) -> list[dict]:
    """从 STOCK_TOOL_SCHEMAS 按工具名取出对应 schema，保持单一来源不漂移。"""
    return [_SCHEMA_BY_NAME[n] for n in names]


SKILL = {
    "name": "news_intel",
    "display_name": "新闻与情报",
    "description": "个股新闻搜索、市场情报搜索、综合情报（Tavily/Brave 自动择优）",
    "version": "1.0.0",
    "chat_modes": ["stock", "research"],
    "roles": [],
    "tools": _pick(
        "search_stock_news",
        "search_market_intel",
        "search_comprehensive_intel",
    ),
    "executors": {
        "search_stock_news": lambda a: search_stock_news(
            str(a.get("query") or ""),
            symbol=_optional_str(a.get("symbol")),
            max_results=_int_arg(a.get("max_results"), 5, 8),
        ),
        "search_market_intel": lambda a: search_market_intel(
            str(a.get("query") or ""),
            symbol=_optional_str(a.get("symbol")),
            max_results=_int_arg(a.get("max_results"), 6, 8),
        ),
        # search_comprehensive_intel 是 search_market_intel 的语义别名，参数抽取一致
        "search_comprehensive_intel": lambda a: search_market_intel(
            str(a.get("query") or ""),
            symbol=_optional_str(a.get("symbol")),
            max_results=_int_arg(a.get("max_results"), 6, 8),
        ),
    },
    "prompt_instructions": [],
}
