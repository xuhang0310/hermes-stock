from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from backend.app.topnews.collectors.base import BaseCollector, MarketData

logger = logging.getLogger(__name__)


def _fetch_north_bound_tushare() -> list[dict]:
    """通过 TuShare moneyflow_hsgt 获取北向资金（同步）。"""
    try:
        from backend.app.market.stock_tools import _tushare_request
        end = date.today().strftime("%Y%m%d")
        start = (date.today() - timedelta(days=7)).strftime("%Y%m%d")
        fields = "trade_date,ggt_ss,ggt_sz,hgt,sgt,north_money,south_money"
        resp = _tushare_request(
            "moneyflow_hsgt",
            {"start_date": start, "end_date": end},
            fields,
        )
        rows = resp.get("rows") or []
        if not rows:
            return []
        latest = rows[-1]
        north = float(latest.get("north_money") or 0)
        hgt = float(latest.get("hgt") or 0)
        sgt = float(latest.get("sgt") or 0)
        return [{
            "name": "北向资金",
            "value": round(north / 1e4, 2),  # 万元 -> 亿元
            "unit": "亿元",
            "date": latest.get("trade_date", ""),
            "direction": "up" if north > 0 else "down",
            "detail": f"沪股通{round(hgt / 1e4, 2)}亿 + 深股通{round(sgt / 1e4, 2)}亿",
            "source": "tushare.moneyflow_hsgt",
        }]
    except Exception as e:
        logger.error("TuShare 北向资金采集失败: %s", e)
        return []


def _fetch_margin_tushare() -> list[dict]:
    """通过 TuShare margin 获取融资融券余额（同步）。"""
    try:
        from backend.app.market.stock_tools import _tushare_request
        end = date.today().strftime("%Y%m%d")
        start = (date.today() - timedelta(days=7)).strftime("%Y%m%d")
        fields = "trade_date,rzye,rzmre,rzche,mrqc,mzche"
        resp = _tushare_request(
            "margin",
            {"start_date": start, "end_date": end},
            fields,
        )
        rows = resp.get("rows") or []
        if not rows:
            return []
        latest = rows[-1]
        rzye = float(latest.get("rzye") or 0)
        rzche = float(latest.get("rzche") or 0)
        return [{
            "name": "融资余额",
            "value": round(rzye / 1e8, 2),  # 元 -> 亿元
            "unit": "亿元",
            "date": latest.get("trade_date", ""),
            "direction": "up" if rzche > 0 else "down",
            "detail": f"融资买入{round(float(latest.get('rzmre') or 0) / 1e8, 2)}亿 偿还{round(float(latest.get('rzche') or 0) / 1e8, 2)}亿",
            "source": "tushare.margin",
        }]
    except Exception as e:
        logger.error("TuShare 融资融券采集失败: %s", e)
        return []


class CapitalFlowCollector(BaseCollector):
    """资金面数据采集：TuShare 优先，akshare 降级。"""
    category = "资金面"

    async def collect(self) -> MarketData:
        items: list[dict] = []

        # 北向资金
        try:
            north = await asyncio.to_thread(_fetch_north_bound_tushare)
            items.extend(north)
        except Exception as e:
            logger.error("北向资金 TuShare 采集失败: %s", e)

        # 融资融券
        try:
            margin = await asyncio.to_thread(_fetch_margin_tushare)
            items.extend(margin)
        except Exception as e:
            logger.error("融资融券 TuShare 采集失败: %s", e)

        return MarketData(category=self.category, items=items)
