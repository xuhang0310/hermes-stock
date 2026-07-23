"""持仓查询域 mixin：positions（按账户/标的取最新快照）。"""
from __future__ import annotations

from typing import Any

from sqlalchemy import and_, desc, func, select

from backend.app.core.models import Instrument, Position, PositionAccount, PositionHoldingSnapshot
from backend.app.data._helpers import dt


class PositionQueryMixin:
    def positions(self, account_id: int | None = None) -> list[dict[str, Any]]:
        # 按 (账户, 标的) 取最新快照，使同一 symbol 在不同券商账户各保留一条
        ranked_snapshots = (
            select(
            PositionHoldingSnapshot.id.label("snapshot_id"),
            PositionHoldingSnapshot.account_id.label("account_id"),
            PositionHoldingSnapshot.symbol.label("symbol"),
            func.row_number().over(
                partition_by=(PositionHoldingSnapshot.account_id, PositionHoldingSnapshot.symbol),
                order_by=(
                    desc(PositionHoldingSnapshot.as_of_date),
                    desc(PositionHoldingSnapshot.captured_at),
                    desc(PositionHoldingSnapshot.id),
                ),
            ).label("rank"),
            )
            .where(self._owner_clause(PositionHoldingSnapshot))
            .subquery()
        )
        query = (
            select(Position, Instrument.name, PositionHoldingSnapshot, PositionAccount)
            .join(Instrument, Instrument.symbol == Position.symbol)
            .outerjoin(
                ranked_snapshots,
                and_(
                    ranked_snapshots.c.rank == 1,
                    ranked_snapshots.c.symbol == Position.symbol,
                    func.coalesce(ranked_snapshots.c.account_id, 0) == func.coalesce(Position.account_id, 0),
                ),
            )
            .outerjoin(PositionHoldingSnapshot, PositionHoldingSnapshot.id == ranked_snapshots.c.snapshot_id)
            .outerjoin(PositionAccount, PositionAccount.id == Position.account_id)
            .where(self._owner_clause(Position))
            .order_by(
                Position.status,
                desc(func.coalesce(PositionHoldingSnapshot.position_pct, Position.position_pct)),
                desc(func.coalesce(PositionHoldingSnapshot.captured_at, Position.updated_at)),
            )
        )
        if account_id is not None:
            query = query.where(Position.account_id == account_id)
        rows = self.db.execute(query).all()
        result = []
        for item, name, snapshot, account in rows:
            quantity = snapshot.position_quantity if snapshot else item.quantity
            position_pct = snapshot.position_pct if snapshot and snapshot.position_pct is not None else item.position_pct
            result.append(
                {
                    "id": item.id,
                    "symbol": item.symbol,
                    "name": snapshot.symbol_name if snapshot and snapshot.symbol_name else name,
                    "position_pct": position_pct,
                    "cost_price": snapshot.cost_price if snapshot else item.cost_price,
                    "quantity": quantity,
                    "position_quantity": quantity,
                    "available_quantity": snapshot.available_quantity if snapshot else None,
                    "frozen_quantity": snapshot.frozen_quantity if snapshot else None,
                    "today_buy_quantity": snapshot.today_buy_quantity if snapshot else None,
                    "today_sell_quantity": snapshot.today_sell_quantity if snapshot else None,
                    "cost_amount": snapshot.cost_amount if snapshot else None,
                    "latest_price": snapshot.latest_price if snapshot else None,
                    "market_value": snapshot.market_value if snapshot else None,
                    "floating_pnl": snapshot.floating_pnl if snapshot else None,
                    "floating_pnl_pct": snapshot.floating_pnl_pct if snapshot else None,
                    "day_pnl": snapshot.day_pnl if snapshot else None,
                    "stop_price": snapshot.stop_price if snapshot and snapshot.stop_price is not None else item.stop_price,
                    "take_profit_price": (
                        snapshot.take_profit_price if snapshot and snapshot.take_profit_price is not None else item.take_profit_price
                    ),
                    "thesis": snapshot.thesis if snapshot and snapshot.thesis else item.thesis,
                    "risk_note": snapshot.risk_note if snapshot and snapshot.risk_note else item.risk_note,
                    "status": item.status,
                    "opened_at": dt(item.opened_at),
                    "updated_at": dt(snapshot.captured_at if snapshot else item.updated_at),
                    "as_of_date": snapshot.as_of_date if snapshot else None,
                    "source_type": snapshot.source_type if snapshot else "manual",
                    "confidence": snapshot.confidence if snapshot else None,
                    "account_id": item.account_id,
                    "broker_name": account.broker_name if account else None,
                    "broker_code": account.broker_code if account else None,
                    "account_name": account.account_name if account else None,
                    "account_no_masked": account.account_no_masked if account else None,
                }
            )
        return result
