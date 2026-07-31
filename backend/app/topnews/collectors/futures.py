from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from backend.app.topnews.collectors.base import BaseCollector, MarketData
from backend.app.topnews.collectors.cls_news import _fetch_tavily_news

logger = logging.getLogger(__name__)


class FuturesCollector(BaseCollector):
    """期货数据采集：Tavily 搜索期货行情作为主要来源。"""
    category = "期货市场"

    async def collect(self) -> MarketData:
        items: list[dict] = []

        queries = [
            "富时A50期指 实时行情 今日",
            "国际期货 原油 黄金 铜 最新价格",
            "国内期货 铁矿石 螺纹钢 行情",
        ]

        for query in queries:
            try:
                results = await asyncio.to_thread(_fetch_tavily_news, query, 5)
                for r in results:
                    title = r.get("title", "")
                    content = r.get("content", "")
                    if not title and not content:
                        continue
                    items.append({
                        "name": title[:30] if title else content[:30],
                        "info": content[:150],
                        "source": "tavily",
                        "url": r.get("url", ""),
                        "time": datetime.now().strftime("%m-%d %H:%M"),
                    })
            except Exception as e:
                logger.error("期货 Tavily 搜索失败 [%s]: %s", query, e)

        # 去重
        seen: set[str] = set()
        unique: list[dict] = []
        for item in items:
            key = item.get("name", "")[:25]
            if key and key not in seen:
                seen.add(key)
                unique.append(item)

        return MarketData(category=self.category, items=unique[:12])
