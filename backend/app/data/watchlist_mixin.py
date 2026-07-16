"""自选股域 mixin：watchlist 查询 + add_watchlist_item upsert。"""
from __future__ import annotations

import json
from typing import Any

from sqlalchemy import desc, select

from backend.app.core.models import Instrument, SystemLog, WatchlistGroup, WatchlistItem
from backend.app.data._helpers import dt


class WatchlistMixin:
    def watchlist(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            select(WatchlistItem, Instrument.name, WatchlistGroup.name.label("group_name"))
            .join(Instrument, Instrument.symbol == WatchlistItem.symbol)
            .join(WatchlistGroup, WatchlistGroup.id == WatchlistItem.group_id)
            .order_by(WatchlistItem.status, desc(WatchlistItem.updated_at))
        ).all()
        return [
            {
                "id": item.id,
                "symbol": item.symbol,
                "name": instrument_name,
                "group_name": group_name,
                "reason": item.reason,
                "status": item.status,
                "added_at": dt(item.added_at),
                "updated_at": dt(item.updated_at),
            }
            for item, instrument_name, group_name in rows
        ]

    def add_watchlist_item(self, symbol: str, group_name: str, reason: str | None) -> dict[str, Any]:
        normalized_symbol = symbol.strip().upper()
        normalized_group = group_name.strip() or "默认"
        if self.db.get(Instrument, normalized_symbol) is None:
            raise ValueError(f"unknown instrument: {normalized_symbol}")

        group = self.db.scalar(select(WatchlistGroup).where(WatchlistGroup.name == normalized_group))
        if group is None:
            group = WatchlistGroup(name=normalized_group, description="用户创建的自选池分组")
            self.db.add(group)
            self.db.flush()

        item = self.db.scalar(
            select(WatchlistItem).where(WatchlistItem.symbol == normalized_symbol, WatchlistItem.group_id == group.id)
        )
        if item is None:
            self.db.add(WatchlistItem(symbol=normalized_symbol, group_id=group.id, reason=reason, status="active"))
        else:
            item.reason = reason
            item.status = "active"
            item.removed_at = None

        self.db.add(
            SystemLog(
                level="INFO",
                module="watchlist",
                message="watchlist item upserted",
                context_json=json.dumps({"symbol": normalized_symbol, "group": normalized_group}, ensure_ascii=False),
            )
        )
        self.db.commit()
        return {"symbol": normalized_symbol, "group_name": normalized_group, "reason": reason}
