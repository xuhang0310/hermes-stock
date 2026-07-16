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
    def __init__(self, db: Session) -> None:
        self.db = db
