"""技能：新闻与市场情报 —— Tavily/Brave 搜索（个股新闻、市场情报、综合情报）。

Phase 2：registry 接管分发；roles + prompt_instructions 补齐。
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
    "roles": ["intelligence-officer", "stock-pm"],
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
    "prompt_instructions": [
        "消息过滤：先区分内部事件、公告/新闻、政策/行业叙事和未经验证的外部文本。",
        "可信度分层：对每条情报标注来源、时效、影响方向和是否需要进一步核验。",
        "面向持仓和自选池输出真正需要行动或转交其他 Agent 的信息。",
        "遇到系统外的标的、行业、政策、新闻时，用 search_market_intel / search_stock_news 联网搜索补充，绝不因为'不在持仓/自选池'就拒绝回答。",
    ],
}
