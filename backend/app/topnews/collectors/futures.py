from __future__ import annotations

import asyncio
import logging
from datetime import date, timedelta

from backend.app.topnews.collectors.base import BaseCollector, MarketData

logger = logging.getLogger(__name__)

_CORE_FUTURES: tuple[tuple[str, str], ...] = (
    ("IF.CFX", "沪深300股指连续"),
    ("IM.CFX", "中证1000股指连续"),
    ("AU.SHF", "沪金连续"),
    ("CU.SHF", "沪铜连续"),
    ("RB.SHF", "螺纹钢连续"),
    ("SC.INE", "原油连续"),
)


def _fetch_tushare_futures() -> list[dict]:
    """通过 TuShare fut_daily / sge_daily 获取核心股指与商品期货及上海现货黄金行情。"""
    items: list[dict] = []
    try:
        from backend.app.market.stock_tools import _tushare_request

        end = date.today().strftime("%Y%m%d")
        start = (date.today() - timedelta(days=10)).strftime("%Y%m%d")
        for ts_code, label in _CORE_FUTURES:
            resp = _tushare_request(
                "fut_daily",
                {"ts_code": ts_code, "start_date": start, "end_date": end},
                "ts_code,trade_date,close,pre_close,pre_settle,change1,change2",
            )
            rows = sorted(resp.get("rows") or [], key=lambda r: str(r.get("trade_date") or ""))
            if not rows:
                continue
            latest = rows[-1]
            close = float(latest.get("close") or 0)
            if close <= 0:
                continue
            base = float(latest.get("pre_settle") or latest.get("pre_close") or 0)
            chg = (
                float(latest["change1"])
                if latest.get("change1") is not None
                else ((close - base) if base > 0 else 0.0)
            )
            pct = (chg / base * 100.0) if base > 0 else 0.0
            items.append({
                "name": label,
                "code": ts_code,
                "price": round(close, 2),
                "change": round(chg, 2),
                "change_pct": round(pct, 2),
                "direction": "up" if pct > 0 else "down" if pct < 0 else "flat",
                "trade_date": latest.get("trade_date"),
                "source": "tushare.fut_daily",
            })

        sge_resp = _tushare_request(
            "sge_daily",
            {"ts_code": "Au99.99", "start_date": start, "end_date": end},
            "ts_code,trade_date,close,change,pct_change",
        )
        sge_rows = sorted(sge_resp.get("rows") or [], key=lambda r: str(r.get("trade_date") or ""))
        if sge_rows:
            s_last = sge_rows[-1]
            s_close = float(s_last.get("close") or 0)
            if s_close > 0:
                s_chg = float(s_last.get("change") or 0)
                s_prev = s_close - s_chg
                s_pct = (s_chg / s_prev * 100.0) if s_prev > 0 else 0.0
                items.append({
                    "name": "上海现货黄金 Au99.99",
                    "code": "Au99.99",
                    "price": round(s_close, 2),
                    "change": round(s_chg, 2),
                    "change_pct": round(s_pct, 2),
                    "direction": "up" if s_pct > 0 else "down" if s_pct < 0 else "flat",
                    "trade_date": s_last.get("trade_date"),
                    "source": "tushare.sge_daily",
                })
    except Exception as e:
        logger.error("TuShare 期货/黄金采集失败: %s", e)
    return items


class FuturesCollector(BaseCollector):
    """期货与黄金现货数据采集：通过 TuShare fut_daily 与 sge_daily 提取。"""
    category = "期货市场"

    async def collect(self) -> MarketData:
        try:
            items = await asyncio.to_thread(_fetch_tushare_futures)
            return MarketData(category=self.category, items=items)
        except Exception as e:
            logger.error("期货市场采集异常: %s", e)
            return MarketData(category=self.category, items=[])
