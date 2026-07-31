from __future__ import annotations

import asyncio
import logging

from backend.app.topnews.collectors.base import BaseCollector, MarketData

logger = logging.getLogger(__name__)


class FuturesCollector(BaseCollector):
    """期货数据采集：目前无可靠免费 API，返回空数据。

    注：Sina/EastMoney 反爬已封堵（403），Tavily 只能搜到网页标题而非行情。
    如需期货数据，可接入 TuShare fut_daily 或付费数据源。
    """
    category = "期货市场"

    async def collect(self) -> MarketData:
        return MarketData(category=self.category, items=[])
