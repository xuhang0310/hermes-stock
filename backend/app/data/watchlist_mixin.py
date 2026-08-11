"""自选股域 mixin：查询、标的检索和自选项增删。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, desc, func, or_, select

from backend.app.core.models import (
    Instrument,
    StockDailyBasic,
    StockLatestPrice,
    SystemLog,
    WatchlistGroup,
    WatchlistItem,
)
from backend.app.data._helpers import dt, normalize_a_share_symbol


class WatchlistMixin:
    def watchlist(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(WatchlistItem, Instrument.name, WatchlistGroup.name.label("group_name"))
            .join(Instrument, Instrument.symbol == WatchlistItem.symbol)
            .join(WatchlistGroup, WatchlistGroup.id == WatchlistItem.group_id)
            .where(WatchlistItem.status == "active", self._owner_clause(WatchlistItem))
            .order_by(desc(WatchlistItem.updated_at))
        ).all()
        symbols = {item.symbol for item, _, _ in rows}
        latest_prices = {
            row.symbol: row
            for row in self.db.scalars(
                select(StockLatestPrice).where(StockLatestPrice.symbol.in_(symbols))
            ).all()
        } if symbols else {}
        latest_basic_dates = (
            select(
                StockDailyBasic.symbol.label("symbol"),
                func.max(StockDailyBasic.trade_date).label("trade_date"),
            )
            .where(StockDailyBasic.symbol.in_(symbols))
            .group_by(StockDailyBasic.symbol)
            .subquery()
        )
        latest_basics = {
            row.symbol: row
            for row in self.db.scalars(
                select(StockDailyBasic).join(
                    latest_basic_dates,
                    and_(
                        StockDailyBasic.symbol == latest_basic_dates.c.symbol,
                        StockDailyBasic.trade_date == latest_basic_dates.c.trade_date,
                    ),
                )
            ).all()
        } if symbols else {}

        payload: list[dict[str, Any]] = []
        for item, instrument_name, group_name in rows:
            price = latest_prices.get(item.symbol)
            basic = latest_basics.get(item.symbol)
            total_mv = basic.total_mv if basic else None
            current_change: float | None = None
            if price and price.price is not None:
                if price.pre_close is not None and float(price.pre_close) > 0:
                    current_change = round(float(price.price) - float(price.pre_close), 4)
                elif price.pct_chg is not None and float(price.pct_chg) > -100:
                    previous_close = float(price.price) / (1 + float(price.pct_chg) / 100)
                    current_change = round(float(price.price) - previous_close, 4)
            payload.append({
                "id": item.id,
                "symbol": item.symbol,
                "name": instrument_name,
                "group_name": group_name,
                "reason": item.reason,
                "status": item.status,
                "added_at": dt(item.added_at),
                "updated_at": dt(item.updated_at),
                "current_price": price.price if price else None,
                "current_pct_chg": price.pct_chg if price else None,
                "current_change": current_change,
                # TuShare daily_basic.total_mv uses ten-thousand CNY.
                "total_mv": total_mv,
                "total_mv_yi": round(float(total_mv) / 10_000, 4) if total_mv is not None else None,
                "market_trade_date": (price.trade_date if price else None) or (basic.trade_date if basic else None),
                "market_fetched_at": dt(price.fetched_at) if price else None,
                "market_source": "+".join(
                    source
                    for source in (
                        price.source if price else None,
                        "tushare.daily_basic" if basic else None,
                    )
                    if source
                ) or None,
            })
        return payload

    def add_watchlist_item(
        self,
        symbol: str,
        group_name: str,
        reason: str | None,
        *,
        added_by: str = "user",
        commit: bool = True,
    ) -> dict[str, Any]:
        normalized_symbol = normalize_a_share_symbol(symbol)
        normalized_group = group_name.strip() or "默认"
        instrument = self.db.get(Instrument, normalized_symbol)
        if instrument is None:
            raise ValueError(f"unknown instrument: {normalized_symbol}")

        group = self.db.scalar(
            select(WatchlistGroup).where(
                WatchlistGroup.name == normalized_group,
                self._owner_clause(WatchlistGroup),
            )
        )
        if group is None:
            group = WatchlistGroup(
                owner_user_id=self.owner_user_id,
                name=normalized_group,
                description="用户创建的自选池分组",
            )
            self.db.add(group)
            self.db.flush()

        item = self.db.scalar(
            select(WatchlistItem).where(
                WatchlistItem.symbol == normalized_symbol,
                WatchlistItem.group_id == group.id,
                self._owner_clause(WatchlistItem),
            )
        )
        if item is None:
            item = WatchlistItem(
                symbol=normalized_symbol,
                group_id=group.id,
                owner_user_id=self.owner_user_id,
                reason=reason,
                added_by=added_by[:80] or "user",
                status="active",
            )
            self.db.add(item)
        else:
            item.reason = reason
            item.added_by = added_by[:80] or item.added_by
            item.status = "active"
            item.removed_at = None
        self.db.flush()

        self.db.add(
            SystemLog(
                level="INFO",
                module="watchlist",
                message="watchlist item upserted",
                context_json=json.dumps({"symbol": normalized_symbol, "group": normalized_group}, ensure_ascii=False),
            )
        )
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return {
            "id": item.id,
            "symbol": normalized_symbol,
            "name": instrument.name,
            "group_name": normalized_group,
            "reason": reason,
            "status": item.status,
            "added_at": dt(item.added_at),
            "updated_at": dt(item.updated_at),
        }

    def remove_watchlist_item(
        self,
        item_id: int | None = None,
        *,
        symbol: str | None = None,
        group_name: str | None = None,
        removed_by: str = "user",
        commit: bool = True,
    ) -> dict[str, Any]:
        query = (
            select(WatchlistItem, WatchlistGroup.name.label("group_name"))
            .join(WatchlistGroup, WatchlistGroup.id == WatchlistItem.group_id)
            .where(WatchlistItem.status == "active", self._owner_clause(WatchlistItem))
        )
        if item_id is not None:
            query = query.where(WatchlistItem.id == item_id)
        elif symbol:
            query = query.where(WatchlistItem.symbol == normalize_a_share_symbol(symbol))
            if group_name:
                query = query.where(WatchlistGroup.name == group_name.strip())
        else:
            raise ValueError("缺少要删除的自选标的")

        rows = self.db.execute(query).all()
        if not rows:
            raise ValueError("自选标的不存在或已经删除")
        if item_id is None and len(rows) > 1:
            groups = "、".join(group for _, group in rows)
            raise ValueError(f"该标的存在于多个分组，请指定分组：{groups}")

        item, resolved_group = rows[0]
        item.status = "removed"
        item.removed_at = datetime.now(timezone.utc).replace(tzinfo=None)
        self.db.add(
            SystemLog(
                level="INFO",
                module="watchlist",
                message="watchlist item removed",
                context_json=json.dumps(
                    {
                        "id": item.id,
                        "symbol": item.symbol,
                        "group": resolved_group,
                        "removed_by": removed_by[:80],
                    },
                    ensure_ascii=False,
                ),
            )
        )
        if commit:
            self.db.commit()
        else:
            self.db.flush()
        return {"deleted": True, "id": item.id, "symbol": item.symbol, "group_name": resolved_group}

    def find_instruments(self, query: str, limit: int = 10) -> list[dict[str, str]]:
        value = query.strip()
        if not value:
            return []
        normalized = normalize_a_share_symbol(value)
        exact = self.db.get(Instrument, normalized)
        if exact is not None:
            return [{"symbol": exact.symbol, "name": exact.name, "market": exact.market}]

        escaped = value.replace("%", "\\%").replace("_", "\\_")
        rows = self.db.scalars(
            select(Instrument)
            .where(
                Instrument.status == "active",
                or_(
                    func.lower(Instrument.name) == value.lower(),
                    Instrument.name.like(f"%{escaped}%", escape="\\"),
                    Instrument.symbol.like(f"%{escaped.upper()}%", escape="\\"),
                ),
            )
            .order_by(
                (func.lower(Instrument.name) == value.lower()).desc(),
                Instrument.symbol,
            )
            .limit(max(1, min(limit, 20)))
        ).all()
        return [{"symbol": row.symbol, "name": row.name, "market": row.market} for row in rows]
