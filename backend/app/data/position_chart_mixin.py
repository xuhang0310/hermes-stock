"""K 线图域 mixin：position_chart 及相关解析/本地缓存/远程 TuShare 数据。"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from sqlalchemy import desc, select

from backend.app.core.models import DailyBar, Instrument
from backend.app.market.stock_tools import get_tushare_daily, get_tushare_minutes
from backend.app.data._helpers import CHINA_TZ, MINUTE_RESOLUTIONS, dt, normalize_a_share_symbol, number_or_none


class PositionChartMixin:
    def position_chart(self, symbol: str, limit: int = 240, refresh: bool = False, resolution: str = "1D") -> dict[str, Any]:
        normalized = normalize_a_share_symbol(symbol)
        instrument = self.db.get(Instrument, normalized)
        position = next((item for item in self.positions() if item["symbol"] == normalized), None)
        normalized_resolution = self._normalize_chart_resolution(resolution)
        is_daily = normalized_resolution == "1D"
        bars = [] if refresh or not is_daily else self._local_daily_bars(normalized, limit)
        source = "local.daily_bars" if bars else "empty"
        remote_error = None

        if not is_daily:
            freq = MINUTE_RESOLUTIONS[normalized_resolution]
            remote = get_tushare_minutes(normalized, freq=freq, limit=limit)
            if "error" in remote:
                remote_error = remote["error"]
            else:
                remote_bars = self._chart_bars_from_tushare_minutes(remote.get("bars") or [])
                if remote_bars:
                    bars = remote_bars
                    source = remote.get("source") or f"tushare.stk_mins.{freq}"
        elif refresh or len(bars) < min(limit, 60):
            remote = get_tushare_daily(normalized, limit=limit)
            if "error" in remote:
                remote_error = remote["error"]
            else:
                remote_bars = self._chart_bars_from_tushare(remote.get("bars") or [])
                if remote_bars:
                    bars = remote_bars
                    source = remote.get("source") or "tushare.daily"

        if not bars:
            empty_message = (
                "暂无可用分钟 K 线数据，请检查 TuShare 历史分钟权限或切回日线。"
                if not is_daily
                else "暂无可用 K 线数据，请检查 TuShare 数据源或先同步日线。"
            )
            return {
                "symbol": normalized,
                "name": position.get("name") if position else (instrument.name if instrument else normalized),
                "source": source,
                "resolution": normalized_resolution,
                "error": remote_error or empty_message,
                "bars": [],
                "overlays": {},
                "position": position,
            }

        overlays = self._chart_overlays(bars)
        return {
            "symbol": normalized,
            "name": position.get("name") if position else (instrument.name if instrument else normalized),
            "source": source,
            "resolution": normalized_resolution,
            "error": remote_error,
            "bars": bars[-limit:],
            "overlays": overlays,
            "position": position,
        }

    @staticmethod
    def _normalize_chart_resolution(value: str | None) -> str:
        text = str(value or "1D").strip().upper()
        if text in {"D", "DAY", "1DAY", "1D"}:
            return "1D"
        text = text.replace("MIN", "").replace("M", "")
        if text in MINUTE_RESOLUTIONS:
            return text
        raise ValueError("不支持的图表周期，仅支持日线、1分、5分、15分、30分、60分")

    def _local_daily_bars(self, symbol: str, limit: int) -> list[dict[str, Any]]:
        rows = self.db.scalars(
            select(DailyBar)
            .where(DailyBar.symbol == symbol)
            .order_by(desc(DailyBar.trade_date), desc(DailyBar.id))
            .limit(limit)
        ).all()
        bars = [
            {
                "time": self._chart_time(row.trade_date),
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "amount": row.amount,
                "change_pct": row.change_pct,
            }
            for row in reversed(rows)
        ]
        return [row for row in bars if row["time"] and row["open"] is not None and row["high"] is not None and row["low"] is not None and row["close"] is not None]

    def _chart_bars_from_tushare(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bars = []
        for row in rows:
            trade_date = self._chart_time(str(row.get("trade_date") or ""))
            if not trade_date:
                continue
            open_price = number_or_none(row.get("open"))
            high = number_or_none(row.get("high"))
            low = number_or_none(row.get("low"))
            close = number_or_none(row.get("close"))
            if open_price is None or high is None or low is None or close is None:
                continue
            bars.append(
                {
                    "time": trade_date,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": number_or_none(row.get("vol") or row.get("volume")),
                    "amount": number_or_none(row.get("amount")),
                    "change_pct": number_or_none(row.get("pct_chg") or row.get("change_pct")),
                }
            )
        return bars

    def _chart_bars_from_tushare_minutes(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        bars = []
        previous_close = None
        for row in rows:
            trade_time = self._chart_time(str(row.get("trade_time") or ""))
            if not trade_time:
                continue
            open_price = number_or_none(row.get("open"))
            high = number_or_none(row.get("high"))
            low = number_or_none(row.get("low"))
            close = number_or_none(row.get("close"))
            if open_price is None or high is None or low is None or close is None:
                continue
            change_pct = (close / previous_close - 1) * 100 if previous_close else None
            bars.append(
                {
                    "time": trade_time,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": number_or_none(row.get("vol") or row.get("volume")),
                    "amount": number_or_none(row.get("amount")),
                    "change_pct": round(change_pct, 4) if change_pct is not None else None,
                }
            )
            previous_close = close
        return bars

    @staticmethod
    def _chart_time(value: str | None) -> str | int | None:
        text = str(value or "").strip()
        if re.fullmatch(r"\d{8}", text):
            return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text
        if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", text):
            dt_value = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA_TZ)
            return int(dt_value.timestamp())
        return None

    @staticmethod
    def _chart_overlays(bars: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        closes = [number_or_none(row.get("close")) for row in bars]

        def ma(window: int) -> list[dict[str, Any]]:
            points = []
            for index, row in enumerate(bars):
                if index + 1 < window:
                    continue
                segment = [value for value in closes[index + 1 - window : index + 1] if value is not None]
                if len(segment) != window:
                    continue
                points.append({"time": row["time"], "value": round(sum(segment) / window, 4)})
            return points

        return {
            "ma5": ma(5),
            "ma10": ma(10),
            "ma20": ma(20),
            "ma60": ma(60),
        }