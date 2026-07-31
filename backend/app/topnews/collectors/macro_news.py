from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from backend.app.topnews.collectors.base import BaseCollector, MarketData
from backend.app.topnews.collectors.cls_news import _fetch_tavily_news, _parse_tavily_to_news

logger = logging.getLogger(__name__)


class MacroNewsCollector(BaseCollector):
    """宏观与国际新闻：Tavily 搜索优先。"""
    category = "宏观与国际"

    async def collect(self) -> MarketData:
        items: list[dict] = []

        queries = [
            "宏观经济 央行 货币政策 降准 降息 今日",
            "美联储 鲍威尔 利率 通胀 最新",
            "国际市场 地缘政治 原油 黄金 最新动态",
        ]

        for query in queries:
            try:
                results = await asyncio.to_thread(_fetch_tavily_news, query, 8)
                items.extend(_parse_tavily_to_news(results, "macro"))
            except Exception as e:
                logger.error("宏观新闻 Tavily 搜索失败 [%s]: %s", query, e)

        # 去重（按标题前 30 字符）
        seen_titles: set[str] = set()
        unique_items: list[dict] = []
        for item in items:
            key = item.get("title", "")[:30]
            if key and key not in seen_titles:
                seen_titles.add(key)
                unique_items.append(item)

        return MarketData(category=self.category, items=unique_items[:20])
