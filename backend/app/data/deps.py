from __future__ import annotations

from fastapi import Depends
from sqlalchemy.orm import Session

from backend.app.core.database import get_db
from backend.app.data.repository import StockRepository


def get_repo(db: Session = Depends(get_db)) -> StockRepository:
    """数据域共享依赖：按当前请求会话构造 StockRepository。"""
    return StockRepository(db)