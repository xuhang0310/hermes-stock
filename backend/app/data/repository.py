from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.data.agent_mixin import AgentMixin
from backend.app.data.watchlist_mixin import WatchlistMixin
from backend.app.data.position_query_mixin import PositionQueryMixin
from backend.app.data.position_chart_mixin import PositionChartMixin
from backend.app.data.system_mixin import SystemMixin
from backend.app.data.position_account_mixin import PositionAccountMixin
from backend.app.data.position_write_mixin import PositionWriteMixin


class StockRepository(PositionWriteMixin, PositionAccountMixin, PositionQueryMixin, PositionChartMixin, WatchlistMixin, AgentMixin, SystemMixin):
    def __init__(self, db: Session, owner_user_id: int | None = None) -> None:
        self.db = db
        self.owner_user_id = owner_user_id

    def for_user(self, user_id: int) -> "StockRepository":
        return StockRepository(self.db, owner_user_id=int(user_id))

    def _owner_clause(self, model):
        if self.owner_user_id is None:
            return model.owner_user_id.is_(None) | model.owner_user_id.isnot(None)
        return model.owner_user_id == self.owner_user_id

    def _owns(self, row) -> bool:
        return self.owner_user_id is None or row.owner_user_id == self.owner_user_id
