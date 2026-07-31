from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from backend.app.topnews.collectors.base import BaseCollector, MarketData
from backend.app.topnews.analysis.sentiment import SentimentAnalyzer

logger = logging.getLogger(__name__)

_sentiment_analyzer = SentimentAnalyzer()


def _fetch_tavily_news(query: str, max_results: int = 15) -> list[dict]:
    """通过 Tavily 搜索 A 股市场新闻（同步）。"""
    try:
        from backend.app.core.settings import settings
        from backend.app.market.stock_tools import TAVILY_URL, _data_source_api_key

        api_key = _data_source_api_key("tavily", settings.tavily_api_key)
        if not api_key:
            logger.warning("Tavily API key 未配置，跳过新闻采集")
            return []

        from backend.app.market.stock_tools import _tavily_search
        return _tavily_search(TAVILY_URL, api_key, query, max_results)
    except Exception as e:
        logger.error("Tavily 新闻搜索失败: %s", e)
        return []


def _is_likely_news_headline(title: str, content: str) -> bool:
    """判断标题是否像真正的新闻标题（而非网页分类/网站名）。"""
    text = title + " " + content
    # 真正的新闻通常包含动词、数字、具体实体
    has_substance = (
        any(c.isdigit() for c in text)  # 含数字
        or any(kw in text for kw in [
            "增长", "下降", "上涨", "下跌", "发布", "宣布", "预计", "建议",
            "维持", "调整", "突破", "创", "将", "或", "因", "被", "称",
            "表示", "显示", "报道", "消息", "数据", "政策", "利率", "通胀",
        ])
    )
    # 纯分类/网站名通常没有实质内容
    is_label = (
        len(title) < 15
        and not has_substance
        and ("-" in title or "|" in title or "_" in title or " " not in title)
    )
    return not is_label


def _parse_tavily_to_news(items: list[dict], category_label: str) -> list[dict]:
    """将 Tavily 搜索结果转为新闻条目格式。"""
    news_items: list[dict] = []
    skip_keywords = {
        "百科", "术语", "频道", "行情中心", "价格走势", "百度百科",
        "信息网", "研究院", "经济研究中心", "可汗学院", "牛津经济",
    }
    for item in items:
        title = (item.get("title") or "").strip()
        content = (item.get("content") or "").strip()
        url = item.get("url", "")
        if not title and not content:
            continue
        # 过滤太短或纯标签
        if len(title) < 8 and len(content) < 20:
            continue
        if any(kw in title for kw in skip_keywords):
            continue
        if not _is_likely_news_headline(title, content):
            continue
        text = title + " " + content
        sentiment_result = _sentiment_analyzer.analyze(text)
        news_items.append({
            "title": title or content[:50],
            "content": content[:300],
            "url": url,
            "source": category_label,
            "time": datetime.now().strftime("%m-%d %H:%M"),
            "sentiment_score": sentiment_result.get("score", 0.0),
            "sentiment": sentiment_result.get("label", "neutral"),
        })
    return news_items


class CLSNewsCollector(BaseCollector):
    """财经新闻采集：Tavily 搜索优先，网页抓取降级。"""
    category = "财联社新闻"

    async def collect(self) -> MarketData:
        items: list[dict] = []

        # Tavily 搜索 A 股新闻
        try:
            tavily_items = await asyncio.to_thread(
                _fetch_tavily_news, "A股 今日 重要新闻 市场动态", 15
            )
            items.extend(_parse_tavily_to_news(tavily_items, "tavily"))
        except Exception as e:
            logger.error("Tavily 新闻采集失败: %s", e)

        # 降级：尝试东方财富 7x24
        if not items:
            try:
                items.extend(await self._fetch_eastmoney_live())
            except Exception as e:
                logger.error("东方财富7x24采集失败: %s", e)

        return MarketData(category=self.category, items=items)

    async def _fetch_eastmoney_live(self) -> list[dict]:
        url = "https://np-listapi.eastmoney.com/comm/web/getNewsByColumns"
        params = {
            "client": "web",
            "biz": "web_news_col",
            "column": "350",
            "order": "1",
            "needInteractData": "0",
            "page_index": "1",
            "page_size": "30",
        }
        data = await self._get_json(url, params=params)
        if not data or not isinstance(data, dict):
            return []
        items: list[dict] = []
        try:
            news_list = data.get("data", {}).get("list", [])
            for news in news_list:
                title = news.get("title", "").strip()
                content = news.get("digest", news.get("content", "")).strip()
                if not title and not content:
                    continue
                text = title + " " + content
                sentiment_result = _sentiment_analyzer.analyze(text)
                items.append({
                    "title": title or content[:50],
                    "content": content[:300],
                    "url": news.get("url", ""),
                    "source": "eastmoney",
                    "time": news.get("showtime", "")[:16],
                    "sentiment_score": sentiment_result.get("score", 0.0),
                    "sentiment": sentiment_result.get("label", "neutral"),
                })
        except Exception as e:
            logger.error("解析东方财富新闻失败: %s", e)
        return items
