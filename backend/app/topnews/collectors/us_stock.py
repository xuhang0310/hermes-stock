from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from backend.app.topnews.collectors.base import BaseCollector, MarketData

logger = logging.getLogger(__name__)


def _fetch_tushare_global_indices() -> list[dict]:
    """通过 TuShare index_global 获取国际主要指数（同步，在 executor 中运行）。"""
    try:
        from backend.app.market.stock_tools import _tushare_request
        end = date.today().strftime("%Y%m%d")
        start = (date.today() - timedelta(days=7)).strftime("%Y%m%d")
        fields = "ts_code,trade_date,open,close,high,low,change,pct_chg"
        resp = _tushare_request(
            "index_global",
            {"start_date": start, "end_date": end},
            fields,
        )
        rows = resp.get("rows") or []
        if not rows:
            return []

        # 每个指数取最新一行
        latest_by_code: dict[str, dict] = {}
        for row in sorted(rows, key=lambda r: str(r.get("trade_date") or "")):
            ts_code = str(row.get("ts_code") or "")
            latest_by_code[ts_code] = row

        name_map = {
            "DJI": "道琼斯",
            "IXIC": "纳斯达克",
            "SPX": "标普500",
            "VIX": "VIX恐慌指数",
            "HSI": "恒生指数",
            "HSTECH": "恒生科技",
        }
        items: list[dict] = []
        for ts_code, row in latest_by_code.items():
            short = ts_code.split(".")[0] if "." in ts_code else ts_code
            name = name_map.get(short, ts_code)
            close = float(row.get("close") or 0)
            pct = float(row.get("pct_chg") or 0)
            change = float(row.get("change") or 0)
            if close <= 0:
                continue
            items.append({
                "name": name,
                "code": short,
                "price": round(close, 2),
                "change": round(change, 2),
                "change_pct": round(pct, 2),
                "direction": "up" if pct > 0 else "down" if pct < 0 else "flat",
                "trade_date": row.get("trade_date"),
                "source": "tushare.index_global",
            })
        return items
    except Exception as e:
        logger.error("TuShare 国际指数采集失败: %s", e)
        return []


class USStockCollector(BaseCollector):
    """美股/国际指数行情采集：TuShare 优先，网页抓取降级。"""
    category = "美股行情"

    async def collect(self) -> MarketData:
        # TuShare 是同步 API，放到线程池执行避免阻塞 event loop
        try:
            items = await asyncio.to_thread(_fetch_tushare_global_indices)
            if items:
                return MarketData(category=self.category, items=items)
        except Exception as e:
            logger.error("TuShare 美股采集失败: %s", e)
        return MarketData(category=self.category, items=[])
